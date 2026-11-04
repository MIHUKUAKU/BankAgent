"""Synthetic fixtures test controls; these approvals are not business reviews."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from bankagent.core import Store
from bankagent.finance import digest, publish_bundle
from bankagent.retrieval import context


class FinanceRagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'kb.sqlite')
        self.controller = self.store.session('controller')
        raw = self.root / 'source.txt'
        raw.write_text('SYNTHETIC TEST FIXTURE: working capital, not verified finance evidence')
        self.source = {'source_id': 'test', 'url': 'https://example.com/fixture',
                       'title': 'TEST FIXTURE', 'publisher': 'fixture',
                       'accessed_at': '2026-10-27', 'locator': 'fixture paragraph',
                       'excerpt': 'SYNTHETIC TEST FIXTURE',
                       'snapshot_path': 'source.txt', 'sha256': digest(raw)}
        payload = {'title': '现金转换周期', 'content': '现金转换周期与营运资本分析。',
                   'aliases': ['CCC', '现金周期'], 'topic': 'working_capital',
                   'verification': 'source_supported', 'sources': [self.source]}
        self.bundle = {'schema_version': 1, 'domain': 'finance', 'knowledge_version': 'test-v1',
                       'sources': [self.source],
                       'entries': [{'object_id': 'test_ccc', 'payload': payload}]}
        self.path, self.review_path = self.root / 'bundle.json', self.root / 'review.json'
        self.write_bundle()

    def write_bundle(self):
        self.path.write_text(json.dumps(self.bundle))
        self.review = {'decision': 'PASS', 'reviewer': 'synthetic-reviewer',
                       'researcher': 'synthetic-author', 'bundle_sha256': digest(self.path),
                       'approved_entry_ids': ['test_ccc'], 'evidence': ['fixture control check only']}
        self.write_review()

    def write_review(self):
        self.review_path.write_text(json.dumps(self.review))

    def publish(self):
        return publish_bundle(self.store, self.path, self.review_path, self.root)

    def test_exact_bundle_review_and_source_integrity(self):
        self.bundle['entries'][0]['payload']['content'] += ' tampered'
        self.path.write_text(json.dumps(self.bundle))
        with self.assertRaises(PermissionError): self.publish()
        self.write_bundle()
        (self.root / 'source.txt').write_text('tampered')
        with self.assertRaises(ValueError): self.publish()
        self.assertEqual(self.controller.search('finance', 'CCC'), [])

    def test_rejected_self_or_partial_reviews_cannot_publish(self):
        for change in ({'decision': 'REVISE'}, {'reviewer': 'synthetic-author'},
                       {'approved_entry_ids': []}):
            old = copy.deepcopy(self.review)
            self.review.update(change)
            self.write_review()
            with self.assertRaises(PermissionError): self.publish()
            self.review = old
        self.assertEqual(self.controller.search('finance', 'CCC'), [])

    def test_publish_idempotence_citations_filters_and_abstention(self):
        self.assertEqual(self.publish()['published'], ['test_ccc'])
        self.assertEqual(self.publish()['skipped'], ['test_ccc'])
        result = context(self.controller, 'finance', 'CCC怎么理解', knowledge_version='test-v1')
        self.assertEqual(result['status'], 'retrieved')
        self.assertEqual(result['passages'][0]['sources'][0]['source_id'], 'test')
        natural = context(self.controller, 'finance', '现金是什么')
        self.assertEqual(natural['status'], 'retrieved')
        self.assertEqual(natural['question'], '现金是什么')
        self.assertEqual(self.controller.search('finance', 'CCC', topic='other'), [])
        self.assertEqual(self.controller.search('finance', 'CCC', knowledge_version='other'), [])
        self.assertEqual(context(self.controller, 'finance', '火星种植气候')['status'], 'insufficient_evidence')
        self.assertEqual(self.controller.search('products', 'CCC'), [])

    def test_historical_index_and_reopen(self):
        self.publish()
        worker = self.store.session('product')
        task = self.controller.assign('product', 'test_ccc')
        payload = dict(self.bundle['entries'][0]['payload'], content='修订版本', knowledge_version='test-v2')
        p = worker.propose(task, 'finance', 'test_ccc', payload, 1)
        self.store.session('reviewer').review(p, True, 'synthetic fixture review')
        self.controller.publish(p)
        self.assertEqual(self.controller.search('finance', 'CCC')[0]['version'], 2)
        self.assertEqual(self.controller.search('finance', 'CCC', version=1)[0]['payload']['knowledge_version'], 'test-v1')
        self.assertEqual(self.controller.search('finance', 'CCC', knowledge_version='test-v1')[0]['version'], 1)
        reopened = Store(self.store.path).session('controller')
        self.assertEqual(reopened.search('finance', 'CCC')[0]['version'], 2)
        with self.assertRaises(ValueError): self.publish()

    def test_index_backfill_for_existing_database(self):
        self.publish()
        with self.store.db() as db:
            db.execute('DELETE FROM knowledge_fts')
        reopened = Store(self.store.path).session('controller')
        self.assertEqual(reopened.search('finance', '现金周期')[0]['object_id'], 'test_ccc')

    def test_invalid_later_card_does_not_start_publication(self):
        other = copy.deepcopy(self.bundle['entries'][0])
        other['object_id'] = 'bad_card'
        other['payload']['sources'][0]['publisher'] = ''
        self.bundle['entries'].append(other)
        self.write_bundle()
        self.review['approved_entry_ids'].append('bad_card')
        self.write_review()
        with self.assertRaises(ValueError): self.publish()
        self.assertEqual(self.controller.search('finance', 'CCC'), [])

    def test_tool_usage_local_evidence_is_pinned_and_returned_in_context(self):
        local = self.root / 'local_validation.json'
        local.write_text('SYNTHETIC local command validation')
        payload = self.bundle['entries'][0]['payload']
        payload['content_kind'] = 'engineering_usage_in_finance'
        payload['local_evidence'] = [{'snapshot_path': 'local_validation.json',
                                      'sha256': digest(local), 'title': 'fixture local evidence',
                                      'locator': 'fixture only'}]
        self.write_bundle()
        original = local.read_bytes()
        local.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'local evidence'):
            self.publish()
        self.assertEqual(self.controller.search('finance', 'CCC'), [])
        local.write_bytes(original)
        self.publish()
        p = context(self.controller, 'finance', 'CCC')['passages'][0]
        self.assertEqual(p['content_kind'], 'engineering_usage_in_finance')
        self.assertEqual(p['local_evidence'][0]['sha256'], digest(local))

    def test_bundle_read_is_bound_to_review_even_if_file_replaced(self):
        from unittest.mock import patch
        original_read = Path.read_bytes
        changed = False

        def replace_after_read(path):
            nonlocal changed
            data = original_read(path)
            if path == self.path and not changed:
                changed = True
                self.bundle['entries'][0]['payload']['content'] = 'Concurrent replacement'
                self.path.write_text(json.dumps(self.bundle))
                self.review['bundle_sha256'] = digest(self.path)
                self.write_review()
            return data

        with patch.object(Path, 'read_bytes', replace_after_read):
            with self.assertRaises(PermissionError):
                self.publish()
        self.assertEqual(self.controller.search('finance', 'CCC'), [])


if __name__ == '__main__':
    unittest.main()
