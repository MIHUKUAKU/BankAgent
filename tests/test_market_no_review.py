"""Temporary synthetic data only; no independent reviewer actions in new mode."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from bankagent.core import Store
from bankagent.market import configure, publish, lookup, stage


class MarketNoReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'fixture.sqlite')
        configure(self.store, 'fixture_author')
        self.operator = self.store.session('controller')
        text = 'FIXTURE central bank historical statement 123 units'
        (self.root / 'source.txt').write_text(text)
        source = dict(source_id='fixture', url='https://www.pbc.gov.cn/fixture', title='FIXTURE', publisher='FIXTURE',
                      published_at='2026-10-30T16:00:00+08:00', accessed_at='2026-10-30T17:00:00+08:00',
                      snapshot_path='source.txt', snapshot_kind='extract', sha256=hashlib.sha256(text.encode()).hexdigest(),
                      locator='FIXTURE line 1', excerpt=text)
        self.bundle = dict(schema_version=1, researcher='fixture_author', as_of='2026-10-30T18:00:00+08:00',
                           records=[dict(record_id='market_fixture', kind='fact', title='FIXTURE', content=text,
                                         event_at='2026-10-30T17:00:00+08:00', value='123', unit='units', period='2026-10-30', sources=[source])])

    def release(self, bundle=None):
        return publish(self.store, bundle or self.bundle, root=self.root, operator=self.operator)

    def counts(self):
        with self.store.db() as db:
            return {t: db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]
                    for t in ('tasks','proposals','reviews','versions','events','rag_finance','knowledge_fts','cross_authors')}

    def analysis(self, ref='market_fixture'):
        return dict(record_id='market_analysis', kind='analysis', title='FIXTURE hypothesis', content='FIXTURE interpretation',
                    event_at='2026-10-30T17:00:00+08:00', fact_ids=[ref], assumptions=['FIXTURE assumption'],
                    uncertainties=['FIXTURE uncertainty'], sources=copy.deepcopy(self.bundle['records'][0]['sources']))

    def test_publish_without_reviewer_or_pass_and_no_review_events(self):
        result = self.release()
        self.assertEqual(result[0]['publication_status'], 'program_validated_unreviewed')
        fact = lookup(self.store, object_id='market_fixture')[0]['payload']
        self.assertEqual(fact['verification'], 'program_validated_unreviewed')
        self.assertEqual(fact['content_kind'], 'official_disclosure_quote')
        with self.store.db() as db:
            self.assertIsNone(db.execute('SELECT reviewer FROM market_identities').fetchone()[0])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='review.recorded'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='market.program_validated_published'").fetchone()[0], 1)
        self.assertEqual(self.operator.search('finance', 'FIXTURE')[0]['object_id'], 'market_fixture')

    def test_missing_wrong_role_foreign_store_and_unbound_author_denied(self):
        for operator in (None, self.store.session('product'), Store(self.root/'other.sqlite').session('controller')):
            before = self.counts()
            with self.subTest(operator=operator), self.assertRaises(PermissionError):
                publish(self.store, self.bundle, root=self.root, operator=operator)
            self.assertEqual(self.counts(), before)
        changed = copy.deepcopy(self.bundle); changed['researcher'] = 'unbound'
        with self.assertRaises(PermissionError): self.release(changed)
        with self.assertRaises(PermissionError): configure(self.store, 'unbound')
        self.assertEqual(lookup(self.store), [])

    def test_default_validation_rejects_source_time_number_hash_namespace_and_status(self):
        variants=[]
        for where,key,value in [('source','url','https://evil.example/'), ('source','published_at','2026-10-31T00:00:00+08:00'),
                                ('source','sha256','0'*64), ('record','value','124'), ('record','record_id','enterprise_bypass'),
                                ('record','verification','independently_verified'), ('record','kind','product')]:
            b=copy.deepcopy(self.bundle)
            target=b['records'][0]['sources'][0] if where=='source' else b['records'][0]
            target[key]=value;variants.append((key,b))
        for key,b in variants:
            before=self.counts()
            with self.subTest(key=key),self.assertRaises((ValueError,PermissionError)):self.release(b)
            self.assertEqual(self.counts(),before)

    def test_fact_analysis_separation_pinning_and_missing_dependency_atomicity(self):
        b=copy.deepcopy(self.bundle);b['records'].insert(0,self.analysis('market_missing@1'))
        before=self.counts()
        with self.assertRaises(ValueError):self.release(b)
        self.assertEqual(self.counts(),before)
        b['records'][0]=self.analysis();self.release(b)
        analysis=lookup(self.store,object_id='market_analysis')[0]['payload']
        self.assertEqual(analysis['fact_refs'],['market_fixture@1'])
        self.assertEqual(analysis['verification'],'program_validated_unreviewed')
        self.assertEqual(analysis['evidence_status'],'analysis_unverified')
        self.assertEqual(analysis['content_kind'],'research_analysis')

    def test_idempotence_correction_append_and_immutable_history(self):
        self.release()
        old=lookup(self.store,object_id='market_fixture',version=1)[0]
        self.assertEqual(self.release()[0]['status'],'already_published')
        b=copy.deepcopy(self.bundle);b['records'][0]['title']='FIXTURE correction'
        with self.assertRaises(ValueError):self.release(b)
        b['records'][0]['supersedes']='market_fixture@1';self.assertEqual(self.release(b)[0]['version'],2)
        self.assertEqual(lookup(self.store,object_id='market_fixture',version=1)[0],old)
        with self.store.db() as db:
            with self.assertRaises(sqlite3.IntegrityError):db.execute("UPDATE versions SET payload='{}'")

    def test_real_indexing_failure_rolls_back_full_new_publication(self):
        from bankagent.retrieval import index_payload
        def fail_after_index(*args):
            index_payload(*args)
            raise RuntimeError('FIXTURE late failure')
        before=self.counts()
        with patch('bankagent.retrieval.index_payload',side_effect=fail_after_index),self.assertRaisesRegex(RuntimeError,'late failure'):
            self.release()
        self.assertEqual(self.counts(),before)
        self.assertEqual(self.operator.search('finance','FIXTURE'),[])

    def test_enterprise_products_and_generic_finance_still_require_real_review(self):
        for domain,role,oid in [('enterprises','enterprise','fixture_company'),('products','product','fixture_product'),('finance','product','market_generic')]:
            worker=self.store.session(role, company_id=oid if role=='enterprise' else None)
            task=self.operator.assign(role,oid)
            payload=dict(title='FIXTURE',content='FIXTURE',sources=copy.deepcopy(self.bundle['records'][0]['sources']),
                         verification='program_validated_unreviewed')
            pid=worker.propose(task,domain,oid,payload)
            with self.subTest(domain=domain),self.assertRaisesRegex(PermissionError,'approved review required'):
                self.operator.publish(pid)
        self.assertEqual(lookup(self.store),[])

    def test_reviewed_history_unchanged_when_appending_unreviewed_version(self):
        legacy=Store(self.root/'legacy.sqlite');configure(legacy,'fixture_author','fixture_reviewer')
        review=dict(decision='PASS',researcher='fixture_author',reviewer='fixture_reviewer',
                    bundle_sha256=stage(legacy,self.bundle,self.root)['bundle_sha256'],approved_record_ids=['market_fixture'],evidence=['FIXTURE historical simulated review'])
        publish(legacy,self.bundle,review,self.root)
        old=lookup(legacy,object_id='market_fixture',version=1)[0]
        # Existing reviewer binding stays immutable; new mode only needs author.
        configure(legacy,'fixture_author')
        b=copy.deepcopy(self.bundle);b['records'][0].update(supersedes='market_fixture@1',title='FIXTURE next revision')
        publish(legacy,b,root=self.root,operator=legacy.session('controller'))
        self.assertEqual(lookup(legacy,object_id='market_fixture',version=1)[0],old)
        with legacy.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0],1)

    def test_cli_publish_has_no_review_file_dependency(self):
        path=self.root/'bundle.json';path.write_text(json.dumps(self.bundle))
        command=[sys.executable,'-m','bankagent.market','--db',str(self.store.path),'--root',str(self.root),'publish','--bundle',str(path)]
        p=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(json.loads(p.stdout)[0]['publication_status'],'program_validated_unreviewed')
