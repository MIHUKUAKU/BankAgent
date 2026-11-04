"""Narrow current market-source policy. No network fetch, no automatic API discovery.

Only previously inspected quote landing paths are active. New API endpoints and
company watchlist drafts grant no capability. Historical reads are unaffected.
"""
from datetime import datetime
from decimal import Decimal
import re
from urllib.parse import urlsplit, unquote
from zoneinfo import ZoneInfo

INDICES = {
    'csi300': ('沪深300', 'CSI 300', 'Asia/Shanghai'),
    'hsi': ('恒生指数', 'Hang Seng', 'Asia/Hong_Kong'),
    'nikkei225': ('日经225', 'Nikkei 225', 'Asia/Tokyo'),
    'nasdaq': ('纳斯达克综合指数', 'NASDAQ Composite', 'America/New_York'),
    'djia': ('道琼斯工业平均指数', 'Dow Jones Industrial Average', 'America/New_York'),
    'sp500': ('标普500', 'S&P 500', 'America/New_York'),
    'eurostoxx50': ('欧洲斯托克50', 'EURO STOXX 50', 'Europe/Berlin'),
}
FX = {'USD/CNY', 'EUR/USD', 'EUR/CNY', 'JPY/CNY', 'GBP/CNY'}
QUOTE_PATHS = {'/center/', '/zs000300.html', '/cnyrate/USDCNYC.html'}
# No confirmed fixed-company routes yet; do not read the 50+2 draft as approval.
APPROVED_COMPANY_ROUTES = ()


def _time(value):
    if not isinstance(value, str):
        raise ValueError('explicit quote timestamp required')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('quote timestamp requires timezone')
    return result


def check_url(url):
    u = urlsplit(url)
    path = u.path
    for _ in range(3):
        path = unquote(path)
    if u.scheme != 'https' or u.username or u.password or u.port not in (None,443) or '..' in path.split('/') or '\\' in path:
        raise ValueError('safe HTTPS source route required')
    if u.hostname == 'www.pbc.gov.cn':
        return 'central_bank'
    if u.hostname == 'quote.eastmoney.com' and path in QUOTE_PATHS and not u.query:
        return 'eastmoney_quote'
    raise PermissionError('source outside active PBC/inspected Eastmoney quote scope; companies/APIs unconfirmed')


def check_bundle(bundle):
    records = bundle.get('records', [])
    if not isinstance(records,list):
        raise ValueError('records list required')
    for record in records:
        if not isinstance(record,dict) or not isinstance(record.get('sources'),list):
            raise ValueError('record/source format required')
        if any(not isinstance(s,dict) or not isinstance(s.get('url'),str) for s in record['sources']):
            raise ValueError('source URL required')
        categories = [check_url(s['url']) for s in record['sources']]
        instrument = record.get('instrument')
        if 'series' in record:
            raise PermissionError('legacy three-index schema is historical-only; new scope uses explicit instrument')
        if 'eastmoney_quote' not in categories:
            if instrument or record.get('quote_type'):
                raise ValueError('time-series quotes must use inspected quote-provider sources')
            continue
        if record.get('kind') == 'analysis':
            if record.get('derived') and (not record.get('fact_ids') or not record.get('derivation')):
                raise ValueError('derived analysis requires original fact IDs and explicit method')
            continue
        if record.get('kind') != 'fact' or record.get('derived'):
            raise ValueError('quote facts must be direct, unconverted source observations')
        if record.get('quote_type') not in ('index_close','fx'):
            raise ValueError('Eastmoney scope is index/FX quotes, not news/forum/report prose')
        observed = _time(record.get('observed_at'))
        if observed > _time(bundle['as_of']) or observed > _time(record['event_at']):
            raise ValueError('future quote observation')
        if record['quote_type'] == 'index_close':
            if instrument not in INDICES or record.get('unit') != '点' or record.get('closed') is not True:
                raise ValueError('one of seven specified indices and closing-point unit required')
            chinese, english, tz = INDICES[instrument]
            closed = _time(record.get('closed_at'))
            if (record.get('market_timezone') != tz or closed > observed
                    or closed.astimezone(ZoneInfo(tz)).date().isoformat() != record.get('trading_day')):
                raise ValueError('market-local closing date/time mismatch; no shared China closing time')
            required = (record['trading_day'],)
        else:
            if instrument not in FX or record.get('base_amount') != '1':
                raise ValueError('five specified FX pairs, exactly one unit base currency required')
            base, quoted = instrument.split('/')
            if record.get('unit') != quoted + ' per 1 ' + base:
                raise ValueError('FX direction/unit mismatch (100 JPY is not 1 JPY)')
            if record.get('rate_type') not in ('central_parity','onshore_spot','spot','bank_bid','bank_ask'):
                raise ValueError('explicit central-parity/spot/bid/ask convention required')
            if record['rate_type'] == 'central_parity' and instrument != 'USD/CNY':
                raise ValueError('only inspected USD/CNY central-parity landing route is active')
            chinese, english = instrument, instrument
            required = (record['observed_at'], record['rate_type'], '1 ' + base)
        value = str(record.get('value', ''))
        if not re.fullmatch(r'\d+(?:\.\d+)?', value) or Decimal(value) <= 0:
            raise ValueError('positive direct quote decimal required')
        qualified = False
        for source in record.get('sources', []):
            if check_url(source['url']) != 'eastmoney_quote':
                continue
            if _time(source['accessed_at']) < observed:
                continue
            if record['quote_type'] == 'index_close' and _time(source['published_at']) < closed:
                continue
            path = urlsplit(source['url']).path
            if path == '/zs000300.html' and instrument != 'csi300':
                continue
            if path == '/cnyrate/USDCNYC.html' and (instrument != 'USD/CNY' or record.get('rate_type') != 'central_parity'):
                continue
            evidence = source.get('observation_excerpt', '')
            if (not isinstance(evidence,str) or not evidence or len(evidence)>700 or evidence not in source['excerpt']
                    or not all(v in evidence for v in required) or not (chinese in evidence or english in evidence)):
                continue
            if not re.search(r'(?<![\d.,])' + re.escape(value) + r'(?![\d.,])', evidence):
                continue
            if record['quote_type'] == 'index_close' and not re.search(r'(?:收盘|close)\s*[:：]?\s*' + re.escape(value) + r'\s*(?:点|points)', evidence,re.I):
                continue
            qualified = True
        if not qualified:
            raise ValueError('same source observation must bind instrument, date/time, value and FX convention')
    return bundle


def chart_data(store, month, *, status='published', bundle_sha256=None):
    """Current seven-index/five-FX panels, retained JSON only; never guesses quotes."""
    from .market import _month, lookup, candidate_lookup
    _month(month)
    if status not in ('published','candidate'):
        raise ValueError('published or candidate status required')
    if status == 'published':
        latest = {}
        for row in lookup(store,domain='finance'):
            if row['object_id'].startswith('market_'):
                latest[row['object_id']] = row
        rows = [{'record':r['payload'],'version':r['version'],'digest':r['digest']} for r in latest.values()]
    else:
        rows = candidate_lookup(store,bundle_sha256=bundle_sha256)
    panels = {name:{} for name in INDICES}
    panels.update({pair:{} for pair in sorted(FX)})
    groups,conflicts,cutoffs = {},[],[]
    for item in rows:
        r=item['record'];instrument=r.get('instrument')
        if r.get('kind')!='fact' or r.get('retracted') or instrument not in set(INDICES)|FX:
            continue
        stamp=r.get('trading_day') if r.get('quote_type')=='index_close' else r.get('observed_at')
        if not isinstance(stamp,str) or not stamp.startswith(month):
            continue
        panel=instrument if instrument in INDICES else instrument+' ['+r['rate_type']+']'
        if instrument in FX:
            panels.pop(instrument,None)
        panels.setdefault(panel,{})
        groups.setdefault((panel,stamp),[]).append(item)
        cutoffs.append(r.get('research_as_of') or item.get('as_of') or r.get('event_at'))
    for (panel,stamp),items in groups.items():
        if len({__import__('json').dumps(i['record'],sort_keys=True) for i in items})>1:
            conflicts.append({'panel':panel,'stamp':stamp,'status':'conflicting_observations_omitted'})
        else:
            r=dict(items[-1]['record']);r['provenance']={k:v for k,v in items[-1].items() if k!='record'}
            panels[panel][stamp]=r
    return {'month':month,'status':'published' if status=='published' else 'candidate_unreviewed',
            'series':panels,'as_of':max((c for c in cutoffs if c),key=_time,default=None),'conflicts':conflicts,
            'data_mode':('FIXTURE_SYNTHETIC' if all(i['record']['content'].startswith('FIXTURE') for items in groups.values() for i in items) else 'SOURCE_OBSERVATIONS') if groups else 'NO_DATA',
            'source_scope':'PBC + inspected Eastmoney quotes + user-confirmed companies only (none activated)',
            'availability':'No active quote API. Empty panels mean missing evidence, not zero prices.',
            'date_axis':'index exchange-local trading date; FX offset-aware observation timestamp; no shared China close'}


if __name__=='__main__':
    import argparse,json,sqlite3
    from pathlib import Path
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True);parser.add_argument('--month',required=True)
    parser.add_argument('--status',choices=['published','candidate'],default='published');parser.add_argument('--output',required=True)
    args=parser.parse_args()
    class ReadOnly:
        def db(self):
            connection=sqlite3.connect(Path(args.db).resolve(strict=True).as_uri()+'?mode=ro',uri=True)
            connection.row_factory=sqlite3.Row;connection.execute('PRAGMA query_only=ON');return connection
    data=chart_data(ReadOnly(),args.month,status=args.status)
    with Path(args.output).open('x') as f:json.dump(data,f,ensure_ascii=False,indent=2)
    print(json.dumps({'output':args.output,'status':data['status'],'panels':len(data['series'])}))
