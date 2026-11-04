"""New-source policy and task-local evidence refusal, using synthetic temporary data."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from bankagent.core import Store
from bankagent.market import configure, validate, stage, publish, lookup
from bankagent.market_sources import check_url, chart_data, INDICES, FX
from bankagent.market_refusal import EvidenceInbox


def sha(raw):return hashlib.sha256(raw).hexdigest()


class SourceScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.store=Store(self.root/'fixture.sqlite');configure(self.store,'fixture_author','fixture_reviewer')
        self.bundle=self.make_bundle('FIXTURE central-bank source statement')

    def make_bundle(self,text):
        (self.root/'source.txt').write_text(text)
        source=dict(source_id='fixture',url='https://www.pbc.gov.cn/fixture',title='FIXTURE',publisher='FIXTURE',
                    published_at='2026-10-30T16:00:00+08:00',accessed_at='2026-10-30T17:00:00+08:00',snapshot_path='source.txt',
                    snapshot_kind='extract',sha256=sha(text.encode()),locator='FIXTURE line 1',excerpt=text,observation_excerpt=text)
        return dict(schema_version=1,researcher='fixture_author',as_of='2026-10-30T18:00:00+08:00',records=[dict(record_id='market_fixture',kind='fact',title='FIXTURE',content=text,event_at='2026-10-30T17:00:00+08:00',sources=[source])])

    def review(self,bundle):
        return dict(decision='PASS',researcher='fixture_author',reviewer='fixture_reviewer',bundle_sha256=stage(self.store,bundle,self.root)['bundle_sha256'],approved_record_ids=[r['record_id'] for r in bundle['records']],evidence=['FIXTURE simulated approval'])

    def index(self):
        b=self.make_bundle('FIXTURE 2026-10-30 沪深300 收盘 3000.12 点')
        r=b['records'][0];r.update(instrument='csi300',quote_type='index_close',value='3000.12',unit='点',period='2026-10-30',trading_day='2026-10-30',closed=True,closed_at='2026-10-30T15:00:00+08:00',observed_at='2026-10-30T17:00:00+08:00',market_timezone='Asia/Shanghai')
        r['sources'][0]['url']='https://quote.eastmoney.com/zs000300.html';return b

    def test_pbc_candidate_and_normal_review_publish_still_work(self):
        self.assertEqual(publish(self.store,self.bundle,self.review(self.bundle),self.root)[0]['version'],1)

    def test_old_other_sources_cannot_enter_new_publication(self):
        for url in ['https://www.federalreserve.gov/example','https://www.sse.com.cn/example','https://static.cninfo.com.cn/example','https://www.csrc.gov.cn/example','https://www.example-company.com/ir']:
            b=copy.deepcopy(self.bundle);b['records'][0]['sources'][0]['url']=url
            with self.subTest(url=url),self.assertRaises(PermissionError):stage(self.store,b,self.root)
        self.assertEqual(lookup(self.store),[])

    def test_unapproved_api_subdomains_news_and_forums_are_closed(self):
        for url in ['https://push2.eastmoney.com/api/qt/stock/get','https://quote.eastmoney.com/news','https://quote.eastmoney.com/center/?api=1','https://guba.eastmoney.com','https://finance.eastmoney.com','https://quote.eastmoney.com.evil.example/center/']:
            with self.subTest(url=url),self.assertRaises((PermissionError,ValueError)):check_url(url)

    def test_seven_indices_and_five_fx_pairs_not_draft_company_activation(self):
        self.assertEqual(len(INDICES),7);self.assertEqual(len(FX),5)
        from bankagent.market_sources import APPROVED_COMPANY_ROUTES
        self.assertEqual(APPROVED_COMPANY_ROUTES,())
        with self.assertRaises(PermissionError):check_url('https://www.alibaba.com/ir')

    def test_new_index_uses_explicit_instrument_and_old_schema_refused(self):
        b=self.index();validate(b,self.root);publish(self.store,b,self.review(b),self.root)
        data=chart_data(self.store,'2026-10');self.assertEqual(len(data['series']),12);self.assertEqual(len(data['series']['csi300']),1)
        for timestamp_field,timestamp in [('published_at','2026-10-30T14:00:00+08:00'),('accessed_at','2026-10-30T16:30:00+08:00')]:
            bad=copy.deepcopy(b);bad['records'][0]['sources'][0][timestamp_field]=timestamp
            with self.subTest(timestamp_field=timestamp_field),self.assertRaises(ValueError):validate(bad,self.root)
        b['records'][0]['series']='sse'
        with self.assertRaises(PermissionError):validate(b,self.root)

    def test_us_close_not_forced_to_china_1500(self):
        b=self.make_bundle('FIXTURE 2026-10-30 NASDAQ Composite 收盘 20000.00 点');b['as_of']='2026-10-31T08:00:00+08:00';r=b['records'][0]
        r.update(instrument='nasdaq',quote_type='index_close',value='20000.00',unit='点',period='2026-10-30',trading_day='2026-10-30',closed=True,closed_at='2026-10-30T16:00:00-04:00',observed_at='2026-10-30T16:05:00-04:00',market_timezone='America/New_York',event_at='2026-10-30T16:05:00-04:00')
        r['sources'][0].update(url='https://quote.eastmoney.com/center/',published_at='2026-10-30T16:01:00-04:00',accessed_at='2026-10-30T16:06:00-04:00')
        validate(b,self.root)
        r['market_timezone']='Asia/Shanghai'
        with self.assertRaises(ValueError):validate(b,self.root)

    def test_fx_one_unit_direction_and_convention_are_not_mixed(self):
        b=self.make_bundle('FIXTURE 2026-10-30T17:00:00+08:00 JPY/CNY spot 1 JPY 0.048 CNY per 1 JPY');r=b['records'][0]
        r.update(instrument='JPY/CNY',quote_type='fx',value='0.048',unit='CNY per 1 JPY',period='2026-10-30',base_amount='1',observed_at='2026-10-30T17:00:00+08:00',rate_type='spot')
        r['sources'][0]['url']='https://quote.eastmoney.com/center/'
        validate(b,self.root)
        for change in [{'base_amount':'100'},{'instrument':'CNY/JPY'},{'rate_type':'offshore_spot'},{'unit':'JPY per 1 CNY'},{'derived':True}]:
            bad=copy.deepcopy(b);bad['records'][0].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):validate(bad,self.root)

    def test_central_parity_route_cannot_be_called_spot(self):
        b=self.index();r=b['records'][0];r['sources'][0]['url']='https://quote.eastmoney.com/cnyrate/USDCNYC.html'
        with self.assertRaises(ValueError):validate(b,self.root)

    def test_empty_current_scope_has_twelve_panels_and_no_fabricated_prices(self):
        data=chart_data(self.store,'2026-11')
        self.assertEqual(len(data['series']),12);self.assertTrue(all(not x for x in data['series'].values()));self.assertEqual(data['data_mode'],'NO_DATA')


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.formal=self.root/'formal.sqlite';self.store=Store(self.formal);self.journal=self.root/'refusals.sqlite'
        self.payload=dict(kind='fact',title='FIXTURE old market record',content='FIXTURE quoted value 1.0',value='1.0',unit='点',sources=[dict(source_id='fixture',url='https://www.pbc.gov.cn/fixture',title='FIXTURE',publisher='FIXTURE',accessed_at='2026-10-30',locator='fixture line',snapshot_path='original.txt',sha256=sha(b'FIXTURE original'))])
        self.release('market_fixture',self.payload)
        (self.root/'evidence.txt').write_text('FIXTURE counterevidence value 2.0')
        self.evidence=dict(url='https://www.pbc.gov.cn/fixture',locator='FIXTURE counterevidence line',snapshot_path='evidence.txt',sha256=sha((self.root/'evidence.txt').read_bytes()))
        self.target=dict(record_id='market_fixture',version=1)
        self.actor=EvidenceInbox(self.formal,self.journal,self.root,identity='执行2号')
        self.reviewer=EvidenceInbox(self.formal,self.journal,self.root,identity='审核2号')

    def release(self,oid,payload):
        c=self.store.session('controller');t=c.assign('product',oid);pid=self.store.session('product').propose(t,'finance',oid,payload)
        self.store.session('reviewer').review(pid,True,'FIXTURE simulated structural approval');c.publish(pid)

    def submit(self,**kwargs):
        args=dict(target=self.target,task_id='fixture-own-task',problem='FIXTURE value mismatch',comparison=dict(field='value',recorded='1.0',expected='2.0'),evidence=self.evidence);args.update(kwargs)
        return self.actor.submit(**args)

    def formal_hash(self):
        with sqlite3.connect(self.formal) as db:return sha('\n'.join(db.iterdump()).encode())

    def test_refusal_is_scoped_to_own_task_and_does_not_write_formal_rag(self):
        before=self.formal_hash();issue=self.submit()
        self.assertFalse(self.actor.can_reference(self.target,task_id='fixture-own-task'))
        self.assertTrue(self.actor.can_reference(self.target,task_id='another-task'))
        other=EvidenceInbox(self.formal,self.journal,self.root,identity='执行1号')
        self.assertTrue(other.can_reference(self.target,task_id='fixture-own-task'))
        self.assertEqual(self.formal_hash(),before)
        self.assertEqual(self.actor.history(issue['issue_id'])[0]['body']['review_task']['owner'],'审核2号')
        self.assertFalse(hasattr(self.actor,'publish'))

    def test_missing_or_forged_evidence_rejected(self):
        for ev in [None,{},dict(self.evidence,sha256='0'*64),dict(self.evidence,snapshot_path='../other')]:
            with self.subTest(evidence=ev),self.assertRaises((ValueError,FileNotFoundError)):self.submit(evidence=ev)
        with self.assertRaises(ValueError):self.submit(comparison=dict(field='value',recorded='wrong',expected='2.0'))

    def test_nonexistent_version_and_non_market_target_rejected(self):
        for target in [dict(record_id='market_fixture',version=99),dict(record_id='sf_holding',version=1),dict(record_id='market_fixture',version=0)]:
            with self.subTest(target=target),self.assertRaises(ValueError):self.submit(target=target)

    def test_missing_source_metadata_uses_exact_record_export_hash(self):
        p=copy.deepcopy(self.payload);p['sources'][0].pop('sha256');self.release('market_missing',p)
        issue=self.submit(target=dict(record_id='market_missing',version=1),missing_sources=True,evidence=None,comparison=None)
        body=self.actor.history(issue['issue_id'])[0]['body']
        self.assertEqual(body['record_export_sha256'],sha(json.dumps(body['record_export'],ensure_ascii=False,sort_keys=True).encode()))
        with self.assertRaises(ValueError):self.submit(missing_sources=True)

    def test_only_existing_reviewer_two_appends_reviews_and_history_remains(self):
        issue=self.submit();body=self.actor.history(issue['issue_id'])[0]['body'];before=self.formal_hash()
        args=dict(submitted_sha256=issue['event_sha256'],decision='confirmed',reason='FIXTURE independent simulation only',evidence_hashes=body['evidence_hashes'])
        with self.assertRaises(PermissionError):self.actor.review(issue['issue_id'],**args)
        self.reviewer.review(issue['issue_id'],**args);self.reviewer.review(issue['issue_id'],**dict(args,decision='insufficient_evidence'))
        history=self.actor.history(issue['issue_id']);self.assertEqual(len(history),3);self.assertEqual(history[1]['body']['decision'],'confirmed')
        self.assertEqual(self.formal_hash(),before)
        with sqlite3.connect(self.journal) as db:
            with self.assertRaises(sqlite3.IntegrityError):db.execute('DELETE FROM refusal_events')

    def test_changed_snapshot_or_wrong_issue_hash_cannot_be_reviewed(self):
        issue=self.submit();body=self.actor.history(issue['issue_id'])[0]['body']
        args=dict(submitted_sha256='0'*64,decision='confirmed',reason='FIXTURE',evidence_hashes=body['evidence_hashes'])
        with self.assertRaises(ValueError):self.reviewer.review(issue['issue_id'],**args)
        (self.root/'evidence.txt').write_text('changed')
        with self.assertRaises(ValueError):self.reviewer.review(issue['issue_id'],**dict(args,submitted_sha256=issue['event_sha256']))

    def test_formal_db_cannot_be_used_as_journal_and_unknown_identity_denied(self):
        before=self.formal_hash()
        with self.assertRaises(PermissionError):EvidenceInbox(self.formal,self.formal,self.root,identity='执行2号')
        with self.assertRaises(PermissionError):EvidenceInbox(self.formal,self.journal,self.root,identity='new_reviewer')
        self.assertEqual(before,self.formal_hash())
