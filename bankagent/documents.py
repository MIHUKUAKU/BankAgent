"""Trusted local PDF ingestion. Parsed evidence stays unreviewed and company-scoped.

Heavy dependencies are lazy imports, so ordinary knowledge operations do not need
Docling. This module never publishes facts or writes the knowledge database.
"""
import hashlib
import importlib.metadata as metadata
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
MODEL_LOCK = Path(__file__).with_name('parser_models.json')
EXPECTED = {'docling': '2.143.0', 'pdfplumber': '0.11.10'}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _inside(root, path):
    path = Path(path).resolve()
    if root.resolve() not in path.parents:
        raise ValueError('document path escapes project root')
    return path


def _company_path(root, kind, company, *parts):
    # Resolving only against the project root would allow alpha -> beta links.
    # Reject aliases on the entire company path, including its shared parents.
    path = root / 'data' / kind / 'enterprises' / company
    path = path.joinpath(*parts)
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError('company partition path cannot contain symlinks')
    return _inside(root, path)


def _model_spec(root):
    lock = json.loads(MODEL_LOCK.read_text(encoding='utf-8'))
    lock_hash = hashlib.sha256(_canonical(lock)).hexdigest()
    base = _inside(root, root / 'data/models/docling' / lock_hash[:16])
    return lock, lock_hash, base


def parser_status(root=ROOT):
    root = Path(root).resolve()
    versions, missing = {}, []
    for name in list(EXPECTED) + ['docling-slim', 'docling-core', 'docling-parse',
                                  'docling-ibm-models', 'torch', 'transformers',
                                  'pdfminer.six', 'pypdfium2']:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
    lock, lock_hash, base = _model_spec(root)
    issues = []
    for model in lock['models']:
        for f in model['files']:
            path = _inside(root, base / model['folder'] / f['path'])
            if not path.is_file() or digest(path) != f['sha256']:
                issues.append(model['folder'] + '/' + f['path'])
    mismatch = {k: v for k, v in EXPECTED.items() if versions.get(k) != v}
    return {'status': 'ready' if not (missing or mismatch or issues) else 'setup_required',
            'versions': versions, 'missing_packages': missing, 'required_versions': mismatch,
            'model_lock_sha256': lock_hash, 'model_directory': str(base),
            'model_issues': issues, 'primary': 'docling', 'auxiliary': 'pdfplumber',
            'ocr': False, 'device': 'cpu', 'threads': 4}


def setup_models(root=ROOT, cache=None, download=False):
    """Copy hash-locked official model files from a HF cache; network is opt-in."""
    root = Path(root).resolve()
    lock, _, base = _model_spec(root)
    cache = Path(cache) if cache else root / 'staging/parser_benchmark/cache/huggingface/hub'
    for model in lock['models']:
        source = cache / ('models--' + model['folder']) / 'snapshots' / model['revision']
        if download:
            try:
                from huggingface_hub import snapshot_download
            except ImportError as exc:
                raise ValueError('install parser dependencies first: pip install -e ".[parsers]"') from exc
            source = Path(snapshot_download(repo_id=model['repo_id'], revision=model['revision'],
                          cache_dir=cache, allow_patterns=[f['path'] for f in model['files']]))
        for f in model['files']:
            target = _inside(root, base / model['folder'] / f['path'])
            if target.exists():
                if digest(target) != f['sha256']:
                    raise ValueError('local model checksum mismatch: ' + str(target))
                continue
            original = source / f['path']
            if not original.is_file() or digest(original) != f['sha256']:
                raise ValueError('locked model missing from cache; use parser-setup --download: ' + str(original))
            target.parent.mkdir(parents=True, exist_ok=True)
            # Copy to a temporary file first so an interrupted setup is retryable.
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
                temporary = Path(tmp.name)
            try:
                shutil.copyfile(original, temporary)
                if digest(temporary) != f['sha256']:
                    raise ValueError('model changed while copying')
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
    return parser_status(root)


def select_pages(spec, total):
    if not spec:
        return list(range(1, total + 1))
    pages = set()
    try:
        for part in spec.split(','):
            bounds = part.strip().split('-')
            if len(bounds) == 1:
                pages.add(int(bounds[0]))
            elif len(bounds) == 2:
                lo, hi = map(int, bounds)
                if lo > hi or lo < 1 or hi > total:
                    raise ValueError()
                pages.update(range(lo, hi + 1))
            else:
                raise ValueError()
    except ValueError as exc:
        raise ValueError('pages must be 1-based numbers/ranges, e.g. 9-11,13,132-133') from exc
    if not pages or min(pages) < 1 or max(pages) > total:
        raise ValueError('page selection outside PDF')
    return sorted(pages)


def _ranges(pages):
    groups = []
    for page in pages:
        if groups and page == groups[-1][1] + 1:
            groups[-1][1] = page
        else:
            groups.append([page, page])
    return groups


def _snapshot(source, target, expected):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if digest(target) != expected:
            raise ValueError('existing raw snapshot checksum mismatch')
        return
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        shutil.copyfile(source, temporary)
        if digest(temporary) != expected:
            raise ValueError('input PDF changed while copying')
        # Exclusive link prevents a concurrent import from replacing a snapshot.
        try:
            os.link(temporary, target)
        except FileExistsError:
            if digest(target) != expected:
                raise ValueError('concurrent raw snapshot checksum mismatch')
    finally:
        temporary.unlink(missing_ok=True)


def _convert(pdf, pages, models, output):
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat, ConversionStatus
    from docling.datamodel.pipeline_options import PdfPipelineOptions, AcceleratorOptions, AcceleratorDevice
    options = PdfPipelineOptions(artifacts_path=models, do_ocr=False, enable_remote_services=False)
    options.accelerator_options = AcceleratorOptions(num_threads=4, device=AcceleratorDevice.CPU)
    _json(output / 'pipeline-options.json', options.model_dump(mode='json'))
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    chunks, markdown = [], []
    observed_pages = set()
    for lo, hi in _ranges(pages):
        result = converter.convert(pdf, page_range=(lo, hi), raises_on_error=True)
        if result.status != ConversionStatus.SUCCESS:
            raise ValueError('Docling did not complete requested pages: ' + str(result.status))
        doc = result.document
        observed_pages.update(doc.pages)
        _json(output / f'docling-{lo}-{hi}.json', doc.export_to_dict())
        markdown.append(f'<!-- PDF physical pages {lo}-{hi} -->\n' + doc.export_to_markdown())
        nearby = []
        for item, _ in doc.iterate_items():
            prov = getattr(item, 'prov', [])
            if not prov:
                continue
            kind = str(item.label.value)
            text = item.export_to_markdown(doc=doc) if kind == 'table' else getattr(item, 'text', '')
            if not text.strip():
                continue
            locations = []
            for p in prov:
                if p.page_no not in pages:
                    raise ValueError('unexpected page in Docling provenance')
                height = doc.pages[p.page_no].size.height
                box = p.bbox.model_dump(mode='json')
                top, bottom = box['t'], box['b']
                if box['coord_origin'] == 'BOTTOMLEFT':
                    top, bottom = height - top, height - bottom
                locations.append({'page_no': p.page_no, 'bbox': box,
                                  'bbox_top_left': [box['l'], top, box['r'], bottom],
                                  'page_height': height, 'unit': 'pt'})
            chunk = {'node_ref': f'{lo}-{hi}:{item.self_ref}', 'kind': kind,
                     'text': text, 'locations': locations}
            if kind == 'table':
                chunk['context_before'] = nearby[-4:]
            else:
                nearby.append({'kind': kind, 'text': text, 'locations': locations})
            chunks.append(chunk)
    if observed_pages != set(pages):
        raise ValueError('Docling page coverage does not match request')
    (output / 'document.md').write_text('\n\n'.join(markdown), encoding='utf-8')
    return chunks


def _auxiliary(pdf, pages):
    import pdfplumber
    result = []
    with pdfplumber.open(pdf) as source:
        for page_no in pages:
            page = source.pages[page_no - 1]
            result.append({'page_no': page_no, 'width': page.width, 'height': page.height,
                           'text': page.extract_text() or '', 'words': page.extract_words(),
                           'coordinate_origin': 'TOPLEFT', 'unit': 'pt'})
            page.close()
    return result


def _verify_existing(directory, request):
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('request') != request or manifest.get('status') != 'parsed_unreviewed':
        raise ValueError('existing parse manifest does not match request')
    required = {'pipeline-options.json', 'document.md', 'chunks.json'}
    required.update(f'docling-{lo}-{hi}.json' for lo, hi in _ranges(request['pages']))
    if manifest.get('auxiliary_status') == 'complete':
        required.add('pdfplumber.json')
    if not required.issubset(manifest.get('files', {})):
        raise ValueError('incomplete parse manifest')
    for name, expected in manifest['files'].items():
        path = _inside(directory, directory / name)
        if not path.is_file() or digest(path) != expected:
            raise ValueError('existing parse artifact checksum mismatch: ' + name)
    return manifest


def parse_document(pdf, company_id, source_url, title, publisher, period,
                   pages=None, auxiliary=True, root=ROOT):
    """Parse a local public PDF into immutable, unreviewed evidence artifacts."""
    root, pdf = Path(root).resolve(), Path(pdf).resolve()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', company_id):
        raise ValueError('invalid company_id')
    url = urlsplit(source_url)
    if url.scheme != 'https' or not url.hostname or url.username or url.password:
        raise ValueError('public HTTPS source URL required, without credentials')
    if not all(isinstance(s, str) and s.strip() for s in (title, publisher, period)):
        raise ValueError('source title, publisher and document period required')
    if not pdf.is_file():
        raise ValueError('input PDF not found')
    with pdf.open('rb') as stream:
        if b'%PDF-' not in stream.read(1024):
            raise ValueError('input is not a PDF')
    state = parser_status(root)
    if state['status'] != 'ready':
        raise ValueError('parser setup required; run parser-status and parser-setup in .venv-parser')
    import pdfplumber
    with pdfplumber.open(pdf) as source:
        total = len(source.pages)
    selected = select_pages(pages, total)
    source_hash = digest(pdf)
    raw = _company_path(root, 'raw', company_id, 'documents', source_hash + '.pdf')
    _snapshot(pdf, raw, source_hash)
    source = {'source_id': company_id + '_pdf_' + source_hash[:16], 'url': source_url,
              'title': title, 'publisher': publisher, 'document_period': period,
              'metadata_status': 'operator_supplied_not_verified_by_parser',
              'snapshot_path': str(raw.relative_to(root)), 'sha256': source_hash}
    request = {'schema': 'bankagent.document-parse.v1', 'company_id': company_id,
               'adapter_sha256': digest(Path(__file__)),
               'source': source, 'pages': selected, 'auxiliary': auxiliary,
               'versions': state['versions'], 'model_lock_sha256': state['model_lock_sha256'],
               'configuration': {'ocr': False, 'threads': 4, 'device': 'cpu',
                                 'table_mode': 'accurate', 'remote_services': False}}
    artifact_id = hashlib.sha256(_canonical(request)).hexdigest()
    target = _company_path(root, 'staging', company_id, 'documents', artifact_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        manifest = _verify_existing(target, request)
        return {'status': 'already_parsed', 'company_id': company_id, 'artifact_id': artifact_id,
                'manifest': str(target / 'manifest.json'), 'chunks': manifest['chunks']}
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='.parsing-', dir=target.parent) as temp:
        output = Path(temp)
        chunks = _convert(raw, selected, Path(state['model_directory']), output)
        if not chunks:
            raise ValueError('no document evidence extracted; scan/OCR may require separate handling')
        warnings = ['Parsed evidence is unreviewed; document period is metadata, not every fact period.',
                    'Confirm units, column scope and cross-page headers before creating enterprise facts.',
                    'OCR is disabled; scanned documents are not covered by this pipeline.']
        for index, chunk in enumerate(chunks):
            chunk.update(chunk_id=artifact_id + ':' + str(index), company_id=company_id,
                         source_id=source['source_id'], source_sha256=source_hash,
                         review_status='unreviewed')
        _json(output / 'chunks.json', {'schema': 'bankagent.parsed-evidence.v1',
                                      'company_id': company_id, 'source': source, 'chunks': chunks})
        aux_status = 'disabled'
        if auxiliary:
            try:
                _json(output / 'pdfplumber.json', {'engine': 'pdfplumber', 'pages': _auxiliary(raw, selected)})
                aux_status = 'complete'
            except Exception as exc:
                aux_status = 'failed'
                warnings.append('pdfplumber auxiliary failed: ' + str(exc))
        files = {p.name: digest(p) for p in output.iterdir() if p.is_file()}
        manifest = {'schema': 'bankagent.parsed-document.v1', 'artifact_id': artifact_id,
                    'status': 'parsed_unreviewed', 'request': request,
                    'created_at': datetime.now(timezone.utc).isoformat(), 'total_pdf_pages': total,
                    'selected_pages': selected, 'chunks': len(chunks), 'auxiliary_status': aux_status,
                    'seconds': time.perf_counter() - started, 'warnings': warnings, 'files': files}
        _json(output / 'manifest.json', manifest)
        # A completed directory appears atomically; do not replace earlier runs.
        try:
            os.rename(output, target)
        except OSError:
            if not target.exists():
                raise
            _verify_existing(target, request)
    return {'status': 'parsed_unreviewed', 'company_id': company_id, 'artifact_id': artifact_id,
            'manifest': str(target / 'manifest.json'), 'chunks': manifest['chunks'],
            'selected_pages': selected, 'seconds': manifest['seconds'],
            'auxiliary_status': aux_status}
