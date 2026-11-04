"""Synthetic product fixtures exercise publication controls, not business approvals."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from bankagent.core import Store
from bankagent.finance import digest, publish_bundle
from bankagent.retrieval import context


class ProductRagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'kb.sqlite')
        self.controller = self.store.session('controller')
        source_path = self.root / 'source.txt'
        source_path.write_text('SYNTHETIC TEST FIXTURE, not a fetched official document')
        source = {'source_id': 'fixture', 'url': 'https://www.bankofchina.com/test-fixture',
                  'title': 'SYNTHETIC FIXTURE', 'publisher': 'fixture', 'accessed_at': '2026-10-27',
                  'locator': 'synthetic paragraph', 'excerpt': 'SYNTHETIC TEST FIXTURE',
                  'snapshot_path': 'source.txt', 'sha256': digest(source_path)}
        payload = {'title': 'FIXTURE融资', 'content': 'FIXTURE融资测试，不是业务事实',
                   'aliases': ['融资'], 'topic': 'fixture', 'verification': 'source_supported',
                   'sources': [source], 'requires_current_verification': True,
                   'product_id': 'boc_fixture', 'institution': '中国银行', 'category': 'fixture',
                   'captured_at': '2026-10-27', 'published_at': None,
                   'core_functions': ['fixture'], 'scenarios': ['fixture'],
                   'target_customers': ['fixture'], 'disclosed_conditions': ['fixture'],
                   'unknown_conditions': ['利率、额度待核实']}
        self.bundle = {'schema_version': 1, 'domain': 'products', 'knowledge_version': 'fixture-v1',
                       'sources': [source], 'entries': [{'object_id': 'boc_fixture', 'payload': payload}]}
        self.path, self.review_path = self.root / 'bundle.json', self.root / 'review.json'
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.bundle))
        self.review_path.write_text(json.dumps({'decision': 'PASS', 'reviewer': 'synthetic-reviewer',
            'researcher': 'synthetic-author', 'bundle_sha256': digest(self.path),
            'approved_entry_ids': [e['object_id'] for e in self.bundle['entries']],
            'evidence': ['synthetic control test only']}))

    def publish(self):
        return publish_bundle(self.store, self.path, self.review_path, self.root, domain='products')

    def test_domain_separation_and_structured_context(self):
        with self.assertRaises(ValueError):
            publish_bundle(self.store, self.path, self.review_path, self.root)
        self.assertEqual(self.publish()['published'], ['boc_fixture'])
        self.assertEqual(self.controller.search('finance', '融资'), [])
        passage = context(self.controller, 'products', '融资')['passages'][0]
        self.assertTrue(passage['requires_current_verification'])
        self.assertEqual(passage['product']['unknown_conditions'], ['利率、额度待核实'])
        self.assertEqual(passage['product']['product_id'], 'boc_fixture')

    def test_current_conditions_cannot_be_silently_confirmed(self):
        self.bundle['entries'][0]['payload']['requires_current_verification'] = False
        self.write()
        with self.assertRaises(ValueError): self.publish()
        self.assertEqual(self.controller.history('products', 'boc_fixture'), [])

    def test_missing_later_product_fields_prevents_publication(self):
        later = copy.deepcopy(self.bundle['entries'][0])
        later['object_id'] = later['payload']['product_id'] = 'boc_fixture_later'
        del later['payload']['unknown_conditions']
        self.bundle['entries'].append(later)
        self.write()
        with self.assertRaises(ValueError): self.publish()
        self.assertEqual(self.controller.history('products', 'boc_fixture'), [])

    def test_lookalike_official_domain_rejected(self):
        url = 'https://www.bankofchina.com.example.org/fixture'
        self.bundle['sources'][0]['url'] = url
        self.bundle['entries'][0]['payload']['sources'][0]['url'] = url
        self.write()
        with self.assertRaises(ValueError): self.publish()

    def test_idempotence_and_published_product_cannot_be_overwritten(self):
        self.publish()
        self.assertEqual(self.publish()['skipped'], ['boc_fixture'])
        old = self.controller.history('products', 'boc_fixture')[0]['digest']
        self.bundle['entries'][0]['payload']['content'] += ' CHANGED'
        self.write()
        with self.assertRaises(ValueError): self.publish()
        history = self.controller.history('products', 'boc_fixture')
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['digest'], old)
