"""Market evidence and append-only archives. Trusted operator API; no LLM calls.

Facts are verbatim source quotations, not generated conclusions. Default market
release is program-validated and unreviewed, using a trusted controller capability.
Explicit legacy reviewed imports remain available; shared domain gates are unchanged.
"""
import argparse
import calendar
from contextlib import contextmanager
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import textwrap
from urllib.parse import urlsplit, unquote

from .core import Store, encode

TZ = timezone(timedelta(hours=8))
HOSTS = {'www.pbc.gov.cn', 'quote.eastmoney.com', 'www.csrc.gov.cn', 'www.sse.com.cn', 'star.sse.com.cn',
         'www.szse.cn', 'www.csindex.com.cn', 'www.cnindex.com.cn', 'static.cninfo.com.cn',
         'www.stats.gov.cn', 'www.safe.gov.cn', 'www.gov.cn', 'www.federalreserve.gov',
         'www.ecb.europa.eu', 'www.boj.or.jp', 'www.hkexnews.hk'}
SERIES = {'sse': 'Shanghai Composite', 'shenzhen': 'Shenzhen Component', 'chinext': 'ChiNext'}
SERIES_LABELS = {'sse': ('上证综指', '上证指数', 'Shanghai Composite', 'SSE Composite'), 'shenzhen': ('深证成指', '深证成份指数', 'Shenzhen Component'), 'chinext': ('创业板指', '创业板指数', 'ChiNext')}

INJECTION = re.compile(r'ignore\s+(?:all\s+)?(?:previous|prior|system).*?(?:instructions?|rules?)|'
                       r'execute\s+(?:shell|command)|system\s*prompt|忽略.{0,16}(?:指令|规则)|'
                       r'执行.{0,8}(?:命令|shell)|<\s*(?:script|system)\b', re.I | re.S)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def instant(value):
    if not isinstance(value, str):
        raise ValueError('ISO timestamp string required')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('invalid timestamp') from exc
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result


def _month(month):
    if not isinstance(month, str) or not re.fullmatch(r'\d{4}-\d{2}', month):
        raise ValueError('YYYY-MM month required')
    start = date.fromisoformat(month + '-01')
    return start, date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _number(value):
    if not isinstance(value, str) or not re.fullmatch(r'-?\d+(?:\.\d+)?', value):
        raise ValueError('unambiguous decimal string required')
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('invalid decimal') from exc
    if not number.is_finite():
        raise ValueError('finite number required')
    return number


def _has_value(value, text):
    return re.search(r'(?<![\d.,])' + re.escape(value) + r'(?![\d.,])', text) is not None


def _bounded_quote(text, quote):
    """Conservative sentence/line boundaries reduce misleading mid-clause clipping."""
    offset = 0
    while True:
        found = text.find(quote, offset)
        if found < 0:
            return False
        before = text[:found].rstrip(' \t')
        after = text[found + len(quote):].lstrip(' \t')
        punctuation = '\n\r。.!?！？；;：:|'
        left = not before or before[-1] in punctuation
        right = not after or after[0] in punctuation or quote[-1] in punctuation
        if left and right:
            return True
        offset = found + 1


def validate(bundle, root):
    """Pure validation: never creates a DB or fetches/executes source material."""
    if not isinstance(bundle, dict):
        raise ValueError('bundle object required')
    from .market_sources import check_bundle
    check_bundle(bundle)
    root = Path(root).resolve()
    cutoff = instant(bundle.get('as_of'))
    if cutoff > datetime.now(timezone.utc) or bundle.get('schema_version') != 1 or not _text(bundle.get('researcher')):
        raise ValueError('invalid schema/researcher or future research cutoff')
    records = bundle.get('records')
    if not isinstance(records, list) or not records:
        raise ValueError('nonempty records required')
    ids, observations, source_ids = set(), {}, {}
    for r in records:
        if not isinstance(r, dict) or not re.fullmatch(r'market_[a-z0-9_-]+', str(r.get('record_id', ''))):
            raise ValueError('market namespace required')
        if r['record_id'] in ids:
            raise ValueError('duplicate record id')
        ids.add(r['record_id'])
        if 'retracted' in r and (type(r['retracted']) is not bool or r.get('kind') != 'fact'):
            raise ValueError('retracted must be boolean on a fact correction')
        if r.get('kind') not in ('fact', 'analysis') or not all(_text(r.get(k)) for k in ('title', 'content')):
            raise ValueError('record type/title/content required')
        event = instant(r.get('event_at'))
        if event > cutoff:
            raise ValueError('future event cannot be represented as occurred')
        if any(k in r for k in ('verification', 'verified', 'review_status', 'knowledge_version')):
            raise ValueError('status and verification are generated, not caller-controlled')
        if INJECTION.search(encode(r)):
            raise ValueError('quarantined source instructions')
        sources = r.get('sources')
        if not isinstance(sources, list) or not sources:
            raise ValueError('sources required')
        for s in sources:
            if not isinstance(s, dict) or not all(_text(s.get(k)) for k in ('source_id', 'url', 'title', 'publisher', 'locator', 'excerpt', 'snapshot_path')):
                raise ValueError('source metadata required')
            u = urlsplit(s['url'])
            urlpath = u.path
            for _ in range(3):
                urlpath = unquote(urlpath)
            if (u.scheme != 'https' or u.hostname not in HOSTS or u.username or u.password
                    or u.port not in (None, 443) or '..' in urlpath.split('/') or '\\' in urlpath):
                raise ValueError('exact official HTTPS host and safe path required')
            relative = Path(s['snapshot_path'])
            resolved = (root / relative).resolve()
            if relative.is_absolute() or '..' in relative.parts or root not in resolved.parents:
                raise ValueError('snapshot outside root')
            cursor = root / relative
            while cursor != root:
                if cursor.is_symlink():
                    raise ValueError('symlink snapshot denied')
                cursor = cursor.parent
            raw = resolved.read_bytes()
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise ValueError('use explicitly marked UTF8 extract for binary documents') from exc
            if (s.get('snapshot_kind') not in ('raw', 'extract') or digest(raw) != s.get('sha256')
                    or not _bounded_quote(text, s['excerpt'])):
                raise ValueError('source hash/excerpt mismatch')
            if INJECTION.search(text):
                raise ValueError('quarantined instructions in source snapshot')
            published, accessed = instant(s.get('published_at')), instant(s.get('accessed_at'))
            if published > accessed or accessed > cutoff:
                raise ValueError('publication/capture after cutoff')
            signature = (s['url'], s['sha256'], s['snapshot_path'], s['published_at'], s['snapshot_kind'])
            if s['source_id'] in source_ids and source_ids[s['source_id']] != signature:
                raise ValueError('ambiguous source id')
            source_ids[s['source_id']] = signature
        if r['kind'] == 'analysis':
            for key in ('fact_ids', 'assumptions', 'uncertainties'):
                if not isinstance(r.get(key), list) or not r[key] or not all(_text(v) for v in r[key]):
                    raise ValueError('analysis needs fact references, assumptions, uncertainties')
            if any(not re.fullmatch(r'market_[a-z0-9_-]+(?:@[1-9]\d*)?', v) for v in r['fact_ids']):
                raise ValueError('market fact references required')
            if 'series' in r or 'closed' in r or 'value' in r:
                raise ValueError('analysis cannot be daily market observation')
        else:
            if any(k in r for k in ('fact_ids', 'fact_refs', 'assumptions', 'uncertainties', 'analysis', 'hypotheses', 'banking_implications')):
                raise ValueError('analysis fields cannot masquerade as fact')
            # Conservative machine-enforceable boundary: facts quote source language.
            if not any(_bounded_quote(s['excerpt'], r['content']) for s in sources):
                raise ValueError('fact content must be a verbatim source quotation; put interpretation in analysis')
            if 'value' in r:
                _number(r['value'])
                if not _text(r.get('unit')) or not _text(r.get('period', r.get('trading_day'))):
                    raise ValueError('numeric facts require unit and period')
                if not _has_value(r['value'], r['content']) or r['unit'] not in r['content'] or not any(_has_value(r['value'], s['excerpt']) for s in sources):
                    raise ValueError('numeric value absent from citation')
            if 'series' in r:
                if r['series'] not in SERIES or r.get('closed') is not True or 'value' not in r or _number(r['value']) <= 0:
                    raise ValueError('daily closing metadata required')
                day = date.fromisoformat(r['trading_day'])
                close = instant(r['trading_day'] + 'T15:00:00+08:00')
                if day.weekday() >= 5 or close > cutoff or close > event:
                    raise ValueError('not a completed trading day')
                if not any(s['published_at'] and instant(s['published_at']) >= close for s in sources):
                    raise ValueError('closing source must be published after that close')
                # Explicitly supplied source date, not inferred from URL or weekday.
                if r['unit'] != '点':
                    raise ValueError('daily index unit must be 点')
                qualified = []
                for source in sources:
                    evidence = source.get('observation_excerpt', '')
                    if (not isinstance(evidence, str) or not evidence or len(evidence) > 500
                            or evidence not in source['excerpt'] or r['trading_day'] not in evidence):
                        continue
                    if not any(label.casefold() in evidence.casefold() for label in SERIES_LABELS[r['series']]):
                        continue
                    if any(label.casefold() in evidence.casefold() for other, labels in SERIES_LABELS.items()
                           if other != r['series'] for label in labels):
                        continue
                    pattern = r'(?:收盘|closing|close)\s*[:：]?\s*' + re.escape(r['value']) + r'\s*(?:点|points)(?![\d.])'
                    if re.search(pattern, evidence, re.I):
                        qualified.append(source)
                if not qualified:
                    raise ValueError('same bounded source observation must bind index/date/close/value/point unit')
                key = (r['series'], r['trading_day'])
                if key in observations:
                    raise ValueError('duplicate series/day in bundle')
                observations[key] = r['record_id']
    return bundle


def initialize(store):
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS market_identities(slot INTEGER PRIMARY KEY CHECK(slot=1),researcher TEXT,reviewer TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS market_candidates(digest TEXT PRIMARY KEY,body TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS market_archives(month TEXT,revision INTEGER,body TEXT,PRIMARY KEY(month,revision))')
        db.execute('CREATE TABLE IF NOT EXISTS market_charts(month TEXT,revision INTEGER,body TEXT,PRIMARY KEY(month,revision))')
        db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS market_archive_fts USING fts5(month UNINDEXED,revision UNINDEXED,text)')
        for table in ('market_identities', 'market_candidates', 'market_archives', 'market_charts'):
            for op in ('UPDATE', 'DELETE'):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{op} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable'); END")


def configure(store, researcher, reviewer=None):
    """Trusted operator binds the already assigned humans/chats, never a model tool."""
    if not _text(researcher) or (reviewer is not None and (not _text(reviewer) or researcher == reviewer)):
        raise ValueError('researcher required; optional legacy reviewer must be distinct')
    initialize(store)
    with store.db() as db:
        old = db.execute('SELECT researcher,reviewer FROM market_identities WHERE slot=1').fetchone()
        if old and (old['researcher'] != researcher or (reviewer is not None and old['reviewer'] != reviewer)):
            raise PermissionError('market identity binding is immutable')
        db.execute('INSERT OR IGNORE INTO market_identities VALUES (1,?,?)', (researcher, reviewer))
    return {'researcher': researcher, 'reviewer': old['reviewer'] if old else reviewer, 'mode': 'trusted_operator_binding_not_cryptographic_auth'}


def stage(store, bundle, root):
    frozen = json.loads(encode(bundle))
    validate(frozen, root)
    initialize(store)
    body = encode(frozen)
    h = digest(body.encode())
    with store.db() as db:
        db.execute('INSERT OR IGNORE INTO market_candidates VALUES (?,?)', (h, body))
    return {'status': 'candidate', 'bundle_sha256': h}


class _BorrowedConnection:
    """Suppress nested context commits/BEGIN only, under one outer transaction.

    Not a security wrapper. Only used internally by existing normal Agent methods.
    """
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, parameters=()):
        if sql.strip().upper() == 'BEGIN IMMEDIATE':
            if not self.connection.in_transaction:
                raise RuntimeError('outer publication transaction missing')
            return self.connection.execute('SELECT 1')
        return self.connection.execute(sql, parameters)


class _TransactionStore(Store):
    def __init__(self, store, connection):
        self.path = store.path
        self._sessions, self._identities = {}, {}
        self.connection = _BorrowedConnection(connection)

    def db(self):
        return self.connection


@contextmanager
def _transaction(store):
    connection = store.db()
    try:
        connection.execute('BEGIN IMMEDIATE')
        yield _TransactionStore(store, connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()



def _publish_market_unreviewed(tx, controller, proposal, expected_payload):
    """Private market-only commit under _transaction; never generalizes core gates."""
    from .core import Conflict, now
    from .retrieval import index_payload
    controller.require('controller')
    with tx.db() as db:
        if not db.connection.in_transaction:
            raise RuntimeError('market publication transaction required')
        p = db.execute('SELECT * FROM proposals WHERE id=?', (proposal,)).fetchone()
        body = encode(expected_payload)
        if (not p or p['domain'] != 'finance' or p['author'] != 'product'
                or not re.fullmatch(r'market_[a-z0-9_-]+', p['object_id'])
                or expected_payload.get('record_id') != p['object_id']
                or expected_payload.get('kind') not in ('fact', 'analysis')
                or expected_payload.get('verification') != 'program_validated_unreviewed'
                or expected_payload.get('publication_status') != 'program_validated_unreviewed'
                or p['payload'] != body or p['digest'] != digest(body.encode())):
            raise PermissionError('validated market proposal only')
        task = db.execute('SELECT * FROM tasks WHERE id=?', (p['task'],)).fetchone()
        if not task or task['status'] != 'active' or task['role'] != 'product' or task['object_id'] != p['object_id']:
            raise PermissionError('active exact market task required')
        current = db.execute('SELECT COALESCE(MAX(version),0) FROM versions WHERE domain=? AND object_id=?',
                             ('finance', p['object_id'])).fetchone()[0]
        if current != p['base']:
            raise Conflict('market base version changed')
        version = current + 1
        db.execute('INSERT INTO versions VALUES (?,?,?,?,?,?,?)',
                   ('finance', p['object_id'], version, proposal, body, p['digest'], now()))
        db.execute('INSERT OR REPLACE INTO rag_finance VALUES (?,?,?)', (p['object_id'], version, body))
        index_payload(db, 'finance', p['object_id'], version, expected_payload)
        tx.event(db, 'market.program_validated_published',
                 {'proposal': proposal, 'version': version, 'publication_status': 'program_validated_unreviewed'})
        return version


def publish(store, bundle, review=None, root='.', *, operator=None):
    """Atomic market release. Default requires a trusted controller capability,
    program validation and bound researcher, but no reviewer or PASS document.
    Explicit review retains the legacy reviewed-import contract for compatibility.
    Neither interface is a model tool or cryptographic operator authentication.
    """
    unreviewed = review is None
    if unreviewed:
        # An already-issued controller capability, not a caller-supplied role name.
        if operator is None or operator.store is not store:
            raise PermissionError('same-store trusted controller capability required')
        operator.require('controller')
    bundle, review = json.loads(encode(bundle)), json.loads(encode(review))
    result = stage(store, bundle, root)
    ids = {r['record_id'] for r in bundle['records']}
    approved = review.get('approved_record_ids') if not unreviewed else None
    if not unreviewed and (review.get('decision') != 'PASS' or review.get('bundle_sha256') != result['bundle_sha256']
            or review.get('researcher') != bundle['researcher'] or not _text(review.get('reviewer'))
            or review['reviewer'] == bundle['researcher'] or not isinstance(approved, list)
            or len(approved) != len(ids) or set(approved) != ids or not review.get('evidence')):
        raise PermissionError('exact independently reviewed candidate required')
    outputs = []
    with _transaction(store) as tx:
        with tx.db() as db:
            binding = db.execute('SELECT researcher,reviewer FROM market_identities WHERE slot=1').fetchone()
        if not binding or binding['researcher'] != bundle['researcher'] or (not unreviewed and binding['reviewer'] != review['reviewer']):
            raise PermissionError('configured market researcher required; legacy reviewed mode also requires own reviewer')
        if unreviewed:
            validate(bundle, root)
        controller = tx.session('controller')
        worker = tx.session('product', identity=bundle['researcher'])
        reviewer = tx.session('reviewer', identity=review['reviewer']) if not unreviewed else None
        # Facts first permits analysis to cite a fact in this same atomic release.
        for r in sorted(bundle['records'], key=lambda x: x['kind'] == 'analysis'):
            refs = []
            for reference in r.get('fact_ids', []):
                fid, _, ver = reference.partition('@')
                history = controller.history('finance', fid)
                match = next((v for v in history if v['version'] == int(ver)), None) if ver else (history[-1] if history else None)
                if not match or json.loads(match['payload']).get('kind') != 'fact' or json.loads(history[-1]['payload']).get('retracted'):
                    raise ValueError('analysis requires an actual published market fact/version')
                refs.append(fid + '@' + str(match['version']))
            history = controller.history('finance', r['record_id'])
            payload = dict(r, knowledge_version=bundle['as_of'], researcher=bundle['researcher'],
                           research_as_of=bundle['as_of'], content_kind='official_disclosure_quote' if r['kind'] == 'fact' else 'research_analysis', verification='source_supported' if r['kind'] == 'fact' else 'analysis_unverified')
            if unreviewed:
                payload.update(verification='program_validated_unreviewed',
                               publication_status='program_validated_unreviewed',
                               evidence_status='source_quote_program_validated' if r['kind'] == 'fact' else 'analysis_unverified')
            if refs:
                payload['fact_refs'] = refs
            if history and json.loads(history[-1]['payload']) == payload:
                outputs.append({'id': r['record_id'], 'status': 'already_published', 'version': history[-1]['version']})
                continue
            expected = r['record_id'] + '@' + str(history[-1]['version']) if history else None
            if (history and r.get('supersedes') != expected) or (not history and r.get('supersedes')):
                raise ValueError('exact existing supersedes id@version required for corrections')
            if history:
                previous = json.loads(history[-1]['payload'])
                if r['kind'] != previous['kind'] or r.get('series') != previous.get('series') or r.get('trading_day') != previous.get('trading_day'):
                    raise ValueError('correction cannot change record type or series/day identity')
            if r.get('series'):
                with tx.db() as db:
                    existing = db.execute('SELECT object_id,content FROM rag_finance').fetchall()
                if any(x['object_id'] != r['record_id'] and json.loads(x['content']).get('series') == r['series']
                       and json.loads(x['content']).get('trading_day') == r['trading_day'] for x in existing):
                    raise ValueError('duplicate series/day; explicitly correct original record')
            task = controller.assign('product', r['record_id'])
            pid = worker.propose(task, 'finance', r['record_id'], payload, len(history))
            if unreviewed:
                version = _publish_market_unreviewed(tx, controller, pid, payload)
            else:
                reviewer.review(pid, True, encode(dict(review, market_record_id=r['record_id'])))
                version = controller.publish(pid)
            controller.close_task(task)
            outputs.append({'id': r['record_id'], 'version': version, 'status': 'published'})
    if unreviewed:
        for output in outputs:
            output['publication_status'] = 'program_validated_unreviewed'
    return outputs


def _published_rows(store):
    with store.db() as db:
        return [dict(row) for row in db.execute('SELECT * FROM versions ORDER BY domain,object_id,version')]


def lookup(store, *, month=None, day=None, object_id=None, version=None, domain=None, kind=None):
    """Return retained exact published versions; event date and publication date separate."""
    if month is not None:
        _month(month)
    if day is not None:
        date.fromisoformat(day)
    if version is not None and (type(version) is not int or version < 1 or not object_id):
        raise ValueError('positive version requires object_id')
    if domain is not None and domain not in ('finance', 'products', 'enterprises', 'engineering'):
        raise ValueError('invalid domain')
    if kind is not None and kind not in ('fact', 'analysis', 'legacy_unclassified'):
        raise ValueError('invalid kind')
    result = []
    for row in _published_rows(store):
        p = json.loads(row['payload'])
        event_day = p.get('trading_day') or p.get('event_at', '')[:10]
        published_day = instant(row['created']).astimezone(TZ).date().isoformat()
        if object_id and row['object_id'] != object_id or version and row['version'] != version or domain and row['domain'] != domain:
            continue
        if kind and p.get('kind', 'legacy_unclassified') != kind:
            continue
        if month and not (event_day.startswith(month) or published_day.startswith(month)):
            continue
        if day and day not in (event_day, published_day):
            continue
        result.append(dict(row, payload=p, event_day=event_day, published_day=published_day))
    return result


def archive(store, month):
    start, end = _month(month)
    if end >= datetime.now(TZ).date():
        raise ValueError('archive completed months only')
    initialize(store)
    with _transaction(store) as tx:
        rows = lookup(tx, month=month)
        index, groups = [], {}
        for row in rows:
            p = row['payload']
            kind = p.get('kind', 'legacy_unclassified')
            if kind not in ('fact', 'analysis'):
                kind = 'legacy_unclassified'
            source_list = p.get('source_manifest', p.get('sources', []))
            source_refs = [{k: s[k] for k in ('source_id', 'url', 'sha256', 'locator', 'snapshot_path') if k in s} for s in source_list]
            snippet = p.get('content', '')[:180]
            entry = dict(domain=row['domain'], object_id=row['object_id'], version=row['version'], digest=row['digest'],
                         kind=kind, title=p.get('title', ''), snippet=snippet, source_refs=source_refs,
                         event_day=row['event_day'], published_day=row['published_day'], supersedes=p.get('supersedes'),
                         fact_refs=p.get('fact_refs', []), assumptions=p.get('assumptions', []),
                         uncertainties=p.get('uncertainties', []), counterexamples=p.get('counterexamples', []),
                         verification=p.get('verification', 'legacy_unclassified'), original_kind=p.get('kind'), retracted=p.get('retracted', False))
            index.append(entry)
            groups.setdefault((row['domain'], kind), []).append(entry)
        summaries = []
        for (domain, kind), entries in sorted(groups.items()):
            summaries.append(dict(domain=domain, kind=kind, count=len(entries),
                                  text=' | '.join(e['title'] + ': ' + e['snippet'][:80] + ('; uncertainty: ' + '; '.join(e['uncertainties']) if e['uncertainties'] else '') for e in entries)[:900],
                                  citations=[e['object_id'] + '@' + str(e['version']) for e in entries]))
        body = encode({'month': month, 'summary_method': 'deterministic_extract_not_semantic_verification',
                       'summaries': summaries, 'index': index, 'original_versions_retained': True,
                       'scope': 'event-month OR publication-month; all versions including corrections'})
        with tx.db() as db:
            old = db.execute('SELECT revision,body FROM market_archives WHERE month=? ORDER BY revision DESC LIMIT 1', (month,)).fetchone()
            if old and old['body'] == body:
                return {'revision': old['revision'], 'status': 'already_archived', 'records': len(index)}
            revision = old['revision'] + 1 if old else 1
            db.execute('INSERT INTO market_archives VALUES (?,?,?)', (month, revision, body))
            # Full index text is indexed, even if compact human summary is truncated.
            db.execute('INSERT INTO market_archive_fts VALUES (?,?,?)', (month, revision, body))
    return {'month': month, 'revision': revision, 'records': len(index), 'status': 'archived'}


def archive_query(store, month, revision=None, query=None, domain=None, kind=None):
    _month(month)
    if revision is not None and (type(revision) is not int or revision < 1):
        raise ValueError('positive archive revision required')
    with store.db() as db:
        args = [month]
        sql = 'SELECT revision,body FROM market_archives WHERE month=?'
        if revision is not None:
            sql += ' AND revision=?'
            args.append(revision)
        row = db.execute(sql + ' ORDER BY revision DESC LIMIT 1', args).fetchone()
    if not row:
        return {'status': 'not_found', 'month': month, 'index': [], 'summaries': []}
    body = json.loads(row['body'])
    def keep(item):
        return ((not domain or item['domain'] == domain) and (not kind or item['kind'] == kind)
                and (not query or query.casefold() in encode(item).casefold()))
    body['index'] = [x for x in body['index'] if keep(x)]
    body['summaries'] = [x for x in body['summaries'] if keep(x)]
    return dict(body, revision=row['revision'], status='archived')


def candidate_lookup(store, month=None, day=None, bundle_sha256=None):
    if month:
        _month(month)
    if day:
        date.fromisoformat(day)
    with store.db() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='market_candidates'").fetchone()
        rows = db.execute('SELECT rowid,digest,body FROM market_candidates ORDER BY rowid').fetchall() if exists else []
    result = []
    for row in rows:
        if bundle_sha256 and row['digest'] != bundle_sha256:
            continue
        bundle = json.loads(row['body'])
        for r in bundle['records']:
            event_day = r.get('trading_day') or r['event_at'][:10]
            if month and not event_day.startswith(month) or day and day != event_day:
                continue
            result.append(dict(record=r, status='candidate_unreviewed', bundle_sha256=row['digest'],
                               candidate_sequence=row['rowid'], as_of=bundle['as_of']))
    return result


def chart(store, month, output, *, status='published', bundle_sha256=None):
    """Versioned SVG+JSON, actual dates only. Conflicting candidates are omitted."""
    start, end = _month(month)
    if status not in ('published', 'candidate'):
        raise ValueError('chart status must be published/candidate')
    initialize(store)
    series = {name: {} for name in SERIES}
    conflicts, revisions = [], []
    if status == 'published':
        latest = {}
        for row in lookup(store, domain='finance'):
            latest[row['object_id']] = row
        candidates = [dict(record=x['payload'], version=x['version'], digest=x['digest'],
                           status='published', as_of=x['payload'].get('research_as_of', ''), record_id=x['object_id']) for x in latest.values() if x['payload'].get('kind') == 'fact']
        revisions = [{'record_id': r['object_id'], 'version': r['version'], 'digest': r['digest']}
                     for r in lookup(store, month=month, domain='finance', kind='fact') if r['payload'].get('series')]
    else:
        candidates = candidate_lookup(store, month=month, bundle_sha256=bundle_sha256)
    grouped = {}
    for item in candidates:
        r = item['record']
        if r.get('kind') == 'fact' and not r.get('retracted') and r.get('series') in SERIES and r.get('trading_day', '').startswith(month):
            grouped.setdefault((r['series'], r['trading_day']), []).append(item)
    for (name, day), items in sorted(grouped.items()):
        # Candidate retries never silently replace differing evidence or values.
        signatures = {encode(i['record']) for i in items}
        if len(signatures) > 1:
            conflicts.append({'series': name, 'day': day, 'status': 'conflicting_' + status + '_omitted',
                              'references': [{k: i[k] for k in ('bundle_sha256', 'candidate_sequence') if k in i} for i in items]})
            continue
        item = items[-1]
        series[name][day] = dict(item['record'], provenance={k: v for k, v in item.items() if k != 'record'})
    dates = sorted({d for values in series.values() for d in values})
    cutoffs = sorted({i.get('as_of', '') for i in candidates if i.get('as_of')})
    data = {'month': month, 'status': 'published' if status == 'published' else 'candidate_unreviewed',
            'series': series, 'conflicts': conflicts, 'dates': dates, 'as_of': cutoffs[-1] if cutoffs else None,
            'historical_revisions': revisions, 'missing_policy': 'no interpolation, no zero fill; absent dates are not asserted trading days',
            'calendar': 'official holiday calendar not integrated; require source-quoted closing date, never synthesize a session'}
    data['data_mode'] = ('FIXTURE_SYNTHETIC' if candidates and all(i['record']['content'].startswith('FIXTURE') for i in candidates) else 'SOURCE_OBSERVATIONS' if candidates else 'NO_DATA')
    # Deterministic chart versions and provenance live in append-only DB as well as files.
    body = encode(data)
    with _transaction(store) as tx:
        with tx.db() as db:
            old = db.execute('SELECT revision,body FROM market_charts WHERE month=? ORDER BY revision DESC LIMIT 1', (month,)).fetchone()
            if old and old['body'] == body:
                revision = old['revision']
            else:
                revision = old['revision'] + 1 if old else 1
                db.execute('INSERT INTO market_charts VALUES (?,?,?)', (month, revision, body))
    data['chart_revision'] = revision
    color = '#b45309' if status == 'candidate' else '#12616e'
    components = ['<rect width="100%" height="100%" fill="#fafbfc"/>',
                  f'<text x="50" y="35" font-size="22">Market closes | {escape(month)} | {escape(data["status"])} | v{revision} | {data["data_mode"]}</text>',
                  f'<text x="50" y="60">Research cutoff: {escape(data["as_of"] or "none; no observations")}</text>',
                  '<text x="50" y="82">Legend: circles = source-reported close; separate y scales; missing dates are never filled.</text>']
    source_lines = []
    for index, (name, label) in enumerate(SERIES.items()):
        top, bottom = 130 + index * 280, 310 + index * 280
        points = series[name]
        units = {r['unit'] for r in points.values()}
        if len(units) > 1:
            raise ValueError('mixed units within index panel')
        unit = next(iter(units)) if units else 'points (no data)'
        components.append(f'<text x="65" y="{top-20}" font-size="18">{label} | unit: {escape(unit)}</text>')
        components.append(f'<path d="M110 {top} V{bottom} H1020" fill="none" stroke="#64748b"/>')
        if not points:
            components.append(f'<text x="160" y="{top+75}">no data: no reported closes for this month/status</text>')
            continue
        values = [float(r['value']) for r in points.values()]
        lower, upper = min(values), max(values)
        padding = (upper - lower) * .12 or max(abs(lower) * .005, 1)
        lower, upper = lower - padding, upper + padding
        for tick in range(5):
            value = lower + (upper - lower) * tick / 4
            y = bottom - 180 * tick / 4
            components.append(f'<text x="15" y="{y+4:.1f}" font-size="12">{value:.2f}</text><path d="M110 {y:.1f} H1020" stroke="#e2e8f0"/>')
        coords = []
        for day, r in sorted(points.items()):
            x = 130 + (date.fromisoformat(day) - start).days * 860 / max(1, (end - start).days)
            y = bottom - (float(r['value']) - lower) * 180 / (upper - lower)
            # A visible source-backed point, never a synthetic point on missing days.
            coords.append(f'{x:.2f},{y:.2f}')
            tooltip = day + ' | ' + r['value'] + ' ' + r['unit'] + ' | ' + data['status'] + ' | ' + '; '.join(s['url'] + ' ' + s['locator'] for s in r['sources'])
            components.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="{color}"><title>{escape(tooltip)}</title></circle>')
            components.append(f'<text x="{x:.1f}" y="{bottom+18}" text-anchor="middle" font-size="10">{day[5:]}</text>')
            for s in r['sources']:
                source_lines.append(s['source_id'] + ' | ' + s['url'] + ' | ' + s['locator'])
        # Connecting observed dates is a visual guide, not estimates on other dates.
        components.append(f'<polyline points="{" ".join(coords)}" fill="none" stroke="{color}" stroke-dasharray="4 3"/>')
        components.append(f'<text x="900" y="{bottom+38}" font-size="12">Date (Asia/Shanghai)</text>')
    components.append(f'<text x="50" y="970">Conflicting sessions omitted: {len(conflicts)}. Full source/date/version metadata: accompanying JSON.</text>')
    wrapped_sources = [part for line in sorted(set(source_lines)) for part in textwrap.wrap(line, width=76)]
    for i, line in enumerate(wrapped_sources):
        # Expand canvas for all source references; do not truncate the evidence list.
        components.append(f'<text x="50" y="{1005+i*20}" font-size="11">{escape(line)}</text>')
    height = max(1080, 1040 + len(wrapped_sources) * 20)
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="' + str(height) + '" font-family="Arial,sans-serif">' + ''.join(components) + '</svg>'
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    json_path = out.with_suffix('.json')
    if out == json_path or out.exists() or json_path.exists():
        raise FileExistsError('retain old SVG and JSON; choose new output path')
    # Exclusive creates never overwrite earlier artifacts, even for concurrent writers.
    with out.open('x') as f:
        f.write(svg)
    try:
        with json_path.open('x') as f:
            f.write(encode(data))
    except BaseException:
        out.unlink()
        raise
    return dict(data, output=str(out), data_output=str(json_path))


def chart_query(store, month, revision=None):
    _month(month)
    if revision is not None and (type(revision) is not int or revision < 1):
        raise ValueError('positive chart revision required')
    with store.db() as db:
        args = [month]
        sql = 'SELECT revision,body FROM market_charts WHERE month=?'
        if revision is not None:
            sql += ' AND revision=?'
            args.append(revision)
        row = db.execute(sql + ' ORDER BY revision DESC LIMIT 1', args).fetchone()
    return dict(json.loads(row['body']), chart_revision=row['revision']) if row else {'status': 'not_found', 'month': month}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', required=True)
    p.add_argument('--root', default='.')
    p.add_argument('action', choices=['configure', 'validate', 'stage', 'publish', 'archive', 'archive-query', 'lookup', 'candidate-lookup', 'chart', 'chart-query'])
    p.add_argument('--bundle')
    p.add_argument('--review')
    p.add_argument('--researcher')
    p.add_argument('--reviewer-id')
    p.add_argument('--month')
    p.add_argument('--day')
    p.add_argument('--object-id')
    p.add_argument('--version', type=int)
    p.add_argument('--revision', type=int)
    p.add_argument('--domain')
    p.add_argument('--kind')
    p.add_argument('--query')
    p.add_argument('--status', default='published', choices=['candidate', 'published'])
    p.add_argument('--bundle-sha256')
    p.add_argument('--output')
    a = p.parse_args()
    bundle = json.loads(Path(a.bundle).read_text()) if a.bundle else None
    if a.action == 'validate':
        result = validate(bundle, a.root)
    else:
        store = Store(a.db)
        if a.action == 'configure':
            result = configure(store, a.researcher, a.reviewer_id)
        elif a.action == 'stage':
            result = stage(store, bundle, a.root)
        elif a.action == 'publish':
            result = publish(store, bundle, json.loads(Path(a.review).read_text()) if a.review else None, a.root,
                             operator=store.session('controller') if not a.review else None)
        elif a.action == 'archive':
            result = archive(store, a.month)
        elif a.action == 'archive-query':
            result = archive_query(store, a.month, a.revision, a.query, a.domain, a.kind)
        elif a.action == 'lookup':
            result = lookup(store, month=a.month, day=a.day, object_id=a.object_id, version=a.version, domain=a.domain, kind=a.kind)
        elif a.action == 'chart-query':
            result = chart_query(store, a.month, a.revision)
        elif a.action == 'candidate-lookup':
            result = candidate_lookup(store, a.month, a.day, a.bundle_sha256)
        else:
            result = chart(store, a.month, a.output, status=a.status, bundle_sha256=a.bundle_sha256)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
