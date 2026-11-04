"""Synthetic permissions/transaction tests; not independent business review."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bankagent.core import Store
from bankagent.cross_review import CrossReview

class ReviewerPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'test.sqlite')
        CrossReview(self.store,Path(self.tmp.name)/'audit.sqlite').register_company('a','author','reviewer')
        c=self.store.session('controller');t=c.assign('enterprise','a')
        payload={'title':'FIXTURE','content':'synthetic','sources':[{'url':'https://example.com','title':'FIXTURE','publisher':'fixture','accessed_at':'2026-11-03','locator':'1'}]}
        self.pid=self.store.session('enterprise','a',identity='author').propose(t,'enterprises','a',payload)
        self.reviewer=self.store.session('reviewer','a',identity='reviewer')
    def test_approval_and_fixed_scope_required(self):
        with self.assertRaises(PermissionError):self.reviewer.publish(self.pid)
        self.reviewer.review(self.pid,True,'FIXTURE')
        for company,identity in [('b','reviewer'),('a','other'),(None,'reviewer')]:
            with self.assertRaises(PermissionError):self.store.session('reviewer',company,identity).publish(self.pid)
        self.assertEqual(self.reviewer.publish(self.pid),1)
        with self.store.db() as db:
            row=db.execute('SELECT * FROM enterprise_publication_ledger').fetchone()
            self.assertEqual(row['company_id'],'a');self.assertEqual(row['reviewer_id'],'reviewer')
            with self.assertRaises(Exception):db.execute('DELETE FROM enterprise_publication_ledger')
    def test_index_failure_rolls_back_ledger_and_version(self):
        self.reviewer.review(self.pid,True,'FIXTURE')
        with patch('bankagent.retrieval.index_payload',side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):self.reviewer.publish(self.pid)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM versions').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM enterprise_publication_ledger').fetchone()[0],0)
    def test_cross_gate_still_enforced(self):
        self.reviewer.review(self.pid,True,'FIXTURE')
        CrossReview(self.store,Path(self.tmp.name)/'audit.sqlite').enable_policy('a')
        with self.assertRaises(PermissionError):self.reviewer.publish(self.pid)
