import concurrent.futures
import sqlite3
import tempfile
import unittest
from pathlib import Path
from bankagent import Store, Conflict, DOMAINS

P = {'title':'测试证据','content':'企业经营与流动资金', 'sources':[{'url':'https://example.com/evidence','title':'test fixture','publisher':'fixture','accessed_at':'2026-10-27','locator':'section 1'}]}

class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = Store(Path(self.temp.name)/'test.db')
        self.c = self.s.session('controller')
        self.e = self.s.session('enterprise', company_id='acme')
        self.r = self.s.session('reviewer')
        self.t = self.c.assign('enterprise','acme')

    def proposal(self, base=0):
        return self.e.propose(self.t,'enterprises','acme',P,base)

    def test_no_unreviewed_or_self_publication(self):
        p = self.proposal()
        with self.assertRaises(PermissionError): self.c.publish(p)
        with self.assertRaises(PermissionError): self.e.review(p,True,'self')
        with self.assertRaises(PermissionError): self.e.publish(p)
        self.r.review(p,False,'insufficient factual evidence')
        with self.assertRaises(PermissionError): self.c.publish(p)
        self.assertEqual(self.e.search('enterprises','企业'),[])

    def test_scope_and_domain(self):
        with self.assertRaises(PermissionError): self.e.propose(self.t,'products','acme',P)
        with self.assertRaises(PermissionError): self.e.propose(self.t,'enterprises','other',P)
        with self.assertRaises(PermissionError): self.e.search('engineering','企业')
        with self.assertRaises(ValueError): self.e.propose(self.t,'enterprises','acme',{'content':'x'})

    def test_history_inherits_after_task_ends(self):
        p = self.proposal(); self.r.review(p,True,'fixture verified'); self.c.publish(p)
        self.c.close_task(self.t)
        with self.assertRaises(PermissionError): self.proposal(1)
        self.t = self.c.assign('enterprise','acme')
        p = self.proposal(1); self.r.review(p,True,'increment reviewed'); self.c.publish(p)
        self.assertEqual([r['version'] for r in self.e.history('enterprises','acme')],[1,2])
        self.assertEqual(self.e.search('enterprises','企业')[0]['version'],2)
        self.assertEqual(self.c.search('products','企业'),[])
        with self.s.db() as db:
            with self.assertRaises(sqlite3.IntegrityError): db.execute("UPDATE versions SET payload='overwritten'")
            with self.assertRaises(sqlite3.IntegrityError): db.execute('DELETE FROM reviews')

    def test_concurrent_publish(self):
        ps = [self.proposal(),self.proposal()]
        for p in ps: self.r.review(p,True,'fixture verified')
        def publish(p):
            try: return self.c.publish(p)
            except Conflict: return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(publish,ps))
        self.assertCountEqual(results,[1,'conflict'])

    def test_bootstrap_retirement(self):
        b = self.s.session('bootstrap'); t = self.c.assign('bootstrap','*')
        with self.assertRaises(ValueError): self.c.retire_bootstrap()
        for d in DOMAINS:
            p = b.propose(t,d,'seed',P); self.r.review(p,True,'fixture reviewed'); self.c.publish(p)
        self.c.retire_bootstrap()
        with self.assertRaises(PermissionError): b.search('finance','资金')
        with self.assertRaises(PermissionError): self.c.assign('bootstrap','*')
        other = Store(self.s.path).session('bootstrap')
        with self.assertRaises(PermissionError): other.propose(t,'finance','seed',P,1)

    def test_closed_task_cannot_publish(self):
        p = self.proposal(); self.r.review(p,True,'verified'); self.c.close_task(self.t)
        with self.assertRaises(PermissionError): self.c.publish(p)

    def test_cross_company_reads_and_writes(self):
        p = self.proposal(); self.r.review(p,True,'fixture'); self.c.publish(p)
        other = self.s.session('enterprise', company_id='other')
        self.assertEqual(other.search('enterprises','企业'), [])
        with self.assertRaises(PermissionError): other.history('enterprises','acme')
        with self.assertRaises(PermissionError): other.propose(self.t,'enterprises','acme',P)
        with self.assertRaises(ValueError): self.s.session('enterprise')

    def test_demo_preserves_existing_database(self):
        from bankagent.__main__ import demo
        path = Path(self.temp.name)/'demo.sqlite'
        result = demo(path)
        self.assertEqual(result['enterprise_versions'], [1, 2])
        self.assertTrue(result['stale_write_blocked'])
        before = Store(path).session('controller').history('enterprises','fixture_a')
        with self.assertRaises(ValueError): demo(path)
        self.assertEqual(before, Store(path).session('controller').history('enterprises','fixture_a'))

if __name__ == '__main__': unittest.main()
