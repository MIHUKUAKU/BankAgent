"""Synthetic control tests, not business evidence or independent reviews."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from bankagent.core import Store, Conflict
from bankagent.enterprises import digest, publish_bundle
from bankagent.retrieval import context


class EnterpriseRagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'test.sqlite')
        (self.root / 'raw.txt').write_text('TEST FIXTURE. Revenue: 100, operating cash flow: 20.')
        self.source = dict(source_id='fixture', url='https://example.com/fixture',
                           title='TEST FIXTURE', publisher='fixture', accessed_at='2026-10-27',
                           snapshot_path='raw.txt', sha256=digest(self.root / 'raw.txt'))
        citation = dict(self.source, locator='fixture only', excerpt='Revenue: 100')
        self.bundle = dict(schema_version=1, domain='enterprises', company_id='alpha',
                           knowledge_version='alpha-v1', researcher='fixture-researcher',
                           as_of='2026-10-27', base_revision=0, sources=[self.source],
                           facts=[dict(fact_id='revenue', title='营业收入', content='测试营业收入100元。',
                                       topic='financial', aliases=['营收'], period='2025',
                                       entity_scope='测试集团合并', fact_type='reported',
                                       metrics=[dict(name='营业收入', value=100, unit='元', period='2025')],
                                       sources=[citation], limitations=['synthetic'])],
                           hypotheses=[dict(hypothesis_id='h1', statement='火星专用融资需求',
                                            basis_fact_ids=['revenue'], status='unverified',
                                            verification_questions=['待核实'])], limitations=['fixture'])
        self.path = self.root / 'bundle.json'
        self.review_path = self.root / 'review.json'
        self.write_bundle()

    def write_bundle(self):
        self.path.write_text(json.dumps(self.bundle))
        self.review = dict(company_id=self.bundle['company_id'], decision='PASS',
                           researcher=self.bundle['researcher'], reviewer='synthetic-reviewer',
                           bundle_sha256=digest(self.path),
                           approved_fact_ids=[f['fact_id'] for f in self.bundle['facts']],
                           evidence=['synthetic control check, not independent business review'])
        self.write_review()

    def write_review(self):
        self.review_path.write_text(json.dumps(self.review))

    def publish(self):
        return publish_bundle(self.store, self.path, self.review_path, self.root)

    def test_scoped_fact_retrieval_and_unverified_hypothesis_exclusion(self):
        self.publish()
        self.bundle['company_id'] = 'beta'
        self.bundle['knowledge_version'] = 'beta-v1'
        self.bundle['facts'][0]['content'] = '另一公司营业收入200元。'
        self.bundle['facts'][0]['metrics'][0]['value'] = 200
        self.write_bundle(); self.publish()
        worker = self.store.session('enterprise', company_id='alpha')
        hits = worker.search('enterprises', '营收')
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['payload']['metrics'][0]['value'], 100)
        self.assertEqual(hits[0]['object_id'], 'alpha')
        self.assertEqual(hits[0]['fact_id'], 'revenue')
        with self.assertRaises(PermissionError): worker.search('enterprises', '营收', company_id='beta')
        with self.assertRaises(PermissionError): worker.history('enterprises', 'beta')
        result = context(worker, 'enterprises', '营业收入')
        self.assertEqual(result['passages'][0]['citation_id'], 'alpha/revenue@1')
        self.assertEqual(result['passages'][0]['period'], '2025')
        self.assertEqual(context(worker, 'enterprises', '火星专用融资需求')['status'], 'insufficient_evidence')
        self.assertEqual(worker.search('enterprises', '营业收入', topic='other'), [])
        with self.assertRaises(PermissionError): self.store.session('product').search('enterprises', '营收')

    def test_exact_independent_review_required(self):
        for change in ({'decision': 'REVISE'}, {'reviewer': self.bundle['researcher']},
                       {'researcher': 'wrong'}, {'company_id': 'beta'}, {'approved_fact_ids': []}):
            original = copy.deepcopy(self.review)
            self.review.update(change); self.write_review()
            with self.assertRaises(PermissionError): self.publish()
            self.review = original
        self.write_review()
        self.path.write_text(self.path.read_text() + ' ')
        with self.assertRaises(PermissionError): self.publish()
        self.assertEqual(self.store.session('controller').history('enterprises', 'alpha'), [])

    def test_candidate_changed_between_read_and_review_is_rejected(self):
        from unittest.mock import patch
        original_read = Path.read_bytes
        changed = False

        def read_and_replace(path):
            nonlocal changed
            data = original_read(path)
            if path == self.path and not changed:
                changed = True
                self.bundle['facts'][0]['content'] = 'Concurrent replacement'
                self.path.write_text(json.dumps(self.bundle))
                import hashlib
                self.review['bundle_sha256'] = hashlib.sha256(original_read(self.path)).hexdigest()
                self.write_review()
            return data

        with patch.object(Path, 'read_bytes', read_and_replace):
            with self.assertRaises(PermissionError): self.publish()
        self.assertEqual(self.store.session('controller').history('enterprises', 'alpha'), [])

    def test_source_integrity_and_fact_metadata(self):
        (self.root / 'raw.txt').write_text('tampered')
        with self.assertRaises(ValueError): self.publish()
        self.bundle['sources'][0]['sha256'] = digest(self.root / 'raw.txt')
        self.bundle['facts'][0]['metrics'][0]['unit'] = ''
        self.write_bundle()
        with self.assertRaises(ValueError): self.publish()
        self.bundle['facts'][0]['metrics'][0]['unit'] = '元'
        self.bundle['facts'][0]['sources'][0]['url'] = 'https://example.com/not-captured'
        self.write_bundle()
        with self.assertRaises(ValueError): self.publish()
        self.assertEqual(self.store.session('controller').history('enterprises', 'alpha'), [])

    def test_atomic_company_release_and_rebuildable_historical_index(self):
        self.assertEqual(self.publish()['version'], 1)
        self.assertEqual(self.publish()['status'], 'already_published')
        original = self.store.session('controller').history('enterprises', 'alpha')[0]
        self.bundle['knowledge_version'] = 'alpha-v2'; self.bundle['base_revision'] = 1
        self.bundle['facts'][0]['content'] = '修订营业收入101元。'
        self.bundle['facts'][0]['metrics'][0]['value'] = 101
        self.write_bundle()
        self.assertEqual(self.publish()['version'], 2)
        with self.store.db() as db:
            db.execute('DELETE FROM enterprise_facts_fts')
        reopened = Store(self.store.path)
        worker = reopened.session('enterprise', company_id='alpha')
        self.assertEqual(worker.search('enterprises', '营业收入')[0]['payload']['metrics'][0]['value'], 101)
        self.assertEqual(worker.search('enterprises', '营业收入', version=1)[0]['payload']['metrics'][0]['value'], 100)
        self.assertEqual(worker.search('enterprises', '营业收入', knowledge_version='alpha-v1')[0]['version'], 1)
        self.assertEqual(worker.history('enterprises', 'alpha')[0], original)
        # Rebuilding the other index must not duplicate fact results.
        with reopened.db() as db:
            db.execute('DELETE FROM knowledge_fts')
        self.assertEqual(len(Store(self.store.path).session('controller').search('enterprises', '营收')), 1)
        self.bundle['base_revision'] = 1
        self.bundle['knowledge_version'] = 'alpha-v3'
        self.write_bundle()
        with self.assertRaises(Conflict): self.publish()

    def test_missing_later_fact_evidence_prevents_entire_publication(self):
        self.bundle['facts'].append(dict(self.bundle['facts'][0], fact_id='broken', sources=[]))
        self.write_bundle()
        with self.assertRaises(ValueError): self.publish()
        self.assertEqual(self.store.session('controller').history('enterprises', 'alpha'), [])

    def parser_fixture(self):
        folder = self.root / 'data/staging/enterprises/alpha/documents/fixture'
        folder.mkdir(parents=True, exist_ok=True)
        chunks = {'company_id': 'alpha', 'source': {'sha256': self.source['sha256']},
                  'chunks': [{'chunk_id': 'fixture:0', 'company_id': 'alpha',
                              'source_sha256': self.source['sha256'], 'text': 'Synthetic revenue',
                              'locations': [{'page_no': 1}]}]}
        (folder / 'chunks.json').write_text(json.dumps(chunks))
        manifest = {'request': {'company_id': 'alpha', 'source': {'sha256': self.source['sha256']}},
                    'selected_pages': [1], 'files': {'chunks.json': digest(folder / 'chunks.json')}}
        (folder / 'manifest.json').write_text(json.dumps(manifest))
        return {'manifest_path': str((folder / 'manifest.json').relative_to(self.root)),
                'manifest_sha256': digest(folder / 'manifest.json'),
                'chunks_path': str((folder / 'chunks.json').relative_to(self.root)),
                'chunks_sha256': digest(folder / 'chunks.json'),
                'chunk_ids': ['fixture:0'], 'pdf_pages': [1]}

    def test_parser_evidence_is_bound_and_returned_in_context(self):
        ref = self.parser_fixture()
        self.bundle['facts'][0]['parser_evidence'] = [ref]
        self.write_bundle(); self.publish()
        passage = context(self.store.session('enterprise', company_id='alpha'),
                          'enterprises', '营业收入')['passages'][0]
        self.assertEqual(passage['parser_evidence'][0]['chunk_ids'], ['fixture:0'])
        self.assertEqual(passage['parser_evidence'][0]['manifest_sha256'], ref['manifest_sha256'])

    def test_wrong_parser_company_page_chunk_and_hash_are_rejected(self):
        ref = self.parser_fixture()
        for changes in ({'chunk_ids': ['missing']}, {'pdf_pages': [2]},
                        {'manifest_sha256': '0' * 64}, {'source_sha256': '0' * 64},
                        {'auxiliary_path': 'data/staging/enterprises/beta/documents/fake/pdfplumber.json', 'auxiliary_sha256': '0' * 64},
                        {'chunks_path': ref['chunks_path'].replace('/alpha/', '/beta/')}):
            self.bundle['facts'][0]['parser_evidence'] = [dict(ref, **changes)]
            self.write_bundle()
            with self.assertRaises(ValueError): self.publish()
        self.assertEqual(self.store.session('controller').history('enterprises', 'alpha'), [])

    def test_parser_source_and_company_metadata_must_match_original(self):
        ref = self.parser_fixture()
        path = self.root / ref['manifest_path']
        manifest = json.loads(path.read_text())
        for changes in ({'company_id': 'beta'}, {'source': {'sha256': '0' * 64}}):
            altered = copy.deepcopy(manifest); altered['request'].update(changes)
            path.write_text(json.dumps(altered))
            self.bundle['facts'][0]['parser_evidence'] = [dict(ref, manifest_sha256=digest(path))]
            self.write_bundle()
            with self.assertRaises(ValueError): self.publish()
        self.assertEqual(self.store.session('controller').history('enterprises', 'alpha'), [])

    def test_company_publish_and_fact_index_rollback_together(self):
        with self.store.db() as db:
            db.execute("CREATE TRIGGER fail_publish BEFORE INSERT ON events WHEN NEW.kind='version.published' "
                       "BEGIN SELECT RAISE(ABORT,'injected publication failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError): self.publish()
        self.assertEqual(self.store.session('controller').history('enterprises', 'alpha'), [])
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM enterprise_facts_fts').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
