"""Synthetic ingestion-boundary tests. Real Docling run is logged separately."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from bankagent import documents as docs


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf = self.root / 'fixture.pdf'
        self.pdf.write_bytes(b'%PDF-1.7\nSYNTHETIC FIXTURE, NOT A REAL PDF')
        self.state = {'status': 'ready', 'versions': {'docling': 'test-fixture'},
                      'model_lock_sha256': 'fixture', 'model_directory': str(self.root / 'models')}
        self.page_reader = types.SimpleNamespace(open=lambda _: self)
        self.pages = [None] * 5
        for p in [patch.object(docs, 'parser_status', return_value=self.state),
                  patch.dict(sys.modules, {'pdfplumber': self.page_reader}),
                  patch.object(docs, '_convert', side_effect=self.convert),
                  patch.object(docs, '_auxiliary', return_value=[])]:
            p.start()
            self.addCleanup(p.stop)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def convert(self, pdf, pages, models, output):
        self.assertEqual(docs.digest(pdf), docs.digest(self.pdf))
        docs._json(output / 'pipeline-options.json', {'mode': 'fixture'})
        (output / 'document.md').write_text('SYNTHETIC FIXTURE')
        for lo, hi in docs._ranges(pages):
            docs._json(output / f'docling-{lo}-{hi}.json', {'fixture': True})
        return [{'text': 'FIXTURE: 财务表格', 'kind': 'table',
                 'locations': [{'page_no': pages[0], 'bbox_top_left': [1, 2, 3, 4]}]}]

    def parse(self, **kwargs):
        args = dict(pdf=self.pdf, company_id='alpha', source_url='https://example.com/fixture.pdf',
                    title='SYNTHETIC', publisher='fixture', period='2025', pages='2,4-5', root=self.root)
        args.update(kwargs)
        return docs.parse_document(**args)

    def test_scoped_unreviewed_evidence_idempotence_and_tamper(self):
        result = self.parse()
        target = Path(result['manifest']).parent
        data = json.loads((target / 'chunks.json').read_text())
        self.assertEqual(data['company_id'], 'alpha')
        self.assertEqual(data['chunks'][0]['review_status'], 'unreviewed')
        self.assertEqual(data['chunks'][0]['locations'][0]['page_no'], 2)
        original = (target / 'manifest.json').read_bytes()
        with patch.object(docs, '_convert', side_effect=AssertionError('must not reparse')):
            self.assertEqual(self.parse()['status'], 'already_parsed')
        self.assertEqual((target / 'manifest.json').read_bytes(), original)
        beta = self.parse(company_id='beta')
        self.assertNotEqual(result['artifact_id'], beta['artifact_id'])
        self.assertIn('/beta/', beta['manifest'])
        self.assertFalse((self.root / 'knowledge').exists())
        (target / 'chunks.json').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.parse()

    def test_primary_failure_does_not_silently_fall_back_or_complete(self):
        with patch.object(docs, '_convert', side_effect=RuntimeError('fixture failure')), \
             patch.object(docs, '_auxiliary', side_effect=AssertionError('no fallback')):
            with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                self.parse()
        parent = self.root / 'data/staging/enterprises/alpha/documents'
        self.assertEqual(list(parent.iterdir()), [])
        self.assertEqual(len(list((self.root / 'data/raw/enterprises/alpha/documents').glob('*.pdf'))), 1)

    def test_auxiliary_failure_is_explicit_and_metadata_change_appends(self):
        with patch.object(docs, '_auxiliary', side_effect=ValueError('fixture aux failure')):
            result = self.parse()
        manifest = json.loads(Path(result['manifest']).read_text())
        self.assertEqual(manifest['auxiliary_status'], 'failed')
        self.assertIn('fixture aux failure', manifest['warnings'][-1])
        other = self.parse(title='Changed source metadata')
        self.assertNotEqual(result['artifact_id'], other['artifact_id'])
        self.assertTrue(Path(result['manifest']).exists())

    def test_traversal_invalid_sources_and_corrupt_raw_snapshot_rejected(self):
        for company in ['../beta', '/tmp/beta', 'a/b', '.', '']:
            with self.assertRaises(ValueError):
                self.parse(company_id=company)
        for url in ['http://example.com', 'https://user:pass@example.com/a', 'file:///tmp/a']:
            with self.assertRaises(ValueError):
                self.parse(source_url=url)
        self.parse()
        raw = next((self.root / 'data/raw/enterprises/alpha/documents').glob('*.pdf'))
        raw.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'raw snapshot checksum mismatch'):
            self.parse()

    def test_page_selection_rejects_invalid_and_excessive_ranges(self):
        self.assertEqual(docs.select_pages('4-5,2,2', 5), [2, 4, 5])
        for pages in ['0', '-1', '6', '4-2', '1-99999999999', '1,', 'abc', '1-2-3']:
            with self.assertRaises(ValueError):
                docs.select_pages(pages, 5)

    def test_company_symlink_aliases_are_rejected_inside_project(self):
        for kind in ['raw', 'staging']:
            with self.subTest(kind=kind):
                base = self.root / 'data' / kind / 'enterprises'
                (base / 'beta').mkdir(parents=True, exist_ok=True)
                alias = base / 'alpha'
                alias.symlink_to(base / 'beta', target_is_directory=True)
                try:
                    with self.assertRaisesRegex(ValueError, 'company partition.*symlinks'):
                        self.parse()
                    self.assertEqual(list((base / 'beta').iterdir()), [])
                finally:
                    alias.unlink()


class ModelSetupTests(unittest.TestCase):
    def test_locked_cache_corruption_and_model_path_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'cache/models--fixture--model/snapshots/revision'
            source.mkdir(parents=True)
            (source / 'weights.bin').write_bytes(b'fixture model')
            lock = root / 'lock.json'
            lock.write_text(json.dumps({'models': [{'repo_id': 'fixture/model', 'folder': 'fixture--model',
                            'revision': 'revision', 'files': [{'path': 'weights.bin',
                            'sha256': docs.digest(source / 'weights.bin')}]}]}))
            with patch.object(docs, 'MODEL_LOCK', lock):
                result = docs.setup_models(root, root / 'cache')
                self.assertEqual(result['model_issues'], [])
                target = Path(result['model_directory']) / 'fixture--model/weights.bin'
                target.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'local model checksum mismatch'):
                    docs.setup_models(root, root / 'cache')
                target.unlink()
                target.symlink_to(source / 'weights.bin')
                # A symlink within the project is readable; an external one is denied.
                self.assertEqual(docs.parser_status(root)['model_issues'], [])
                target.unlink()
                target.symlink_to('/etc/hosts')
                with self.assertRaisesRegex(ValueError, 'escapes'):
                    docs.parser_status(root)


if __name__ == '__main__':
    unittest.main()
