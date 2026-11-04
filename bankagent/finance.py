"""Import independently reviewed, checksum-pinned finance or product releases."""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_product(object_id, payload):
    if (payload.get('product_id') != object_id or payload.get('institution') != '中国银行'
            or not isinstance(payload.get('category'), str) or not payload['category'].strip()
            or payload.get('requires_current_verification') is not True
            or not payload.get('captured_at')):
        raise ValueError('BOC product identity, category and current-verification flag required')
    for key in ('core_functions', 'scenarios', 'target_customers',
                'disclosed_conditions', 'unknown_conditions'):
        if (not isinstance(payload.get(key), list) or not payload[key]
                or not all(isinstance(v, str) and v.strip() for v in payload[key])):
            raise ValueError('product list required: ' + key)
    for source in payload['sources']:
        host = urlparse(source['url']).hostname or ''
        if not any(host == suffix or host.endswith('.' + suffix)
                   for suffix in ('bankofchina.com', 'boc.cn')):
            raise ValueError('BOC products require official Bank of China sources')


def publish_bundle(store, bundle_path, review_path, root=None, domain='finance'):
    bundle_path, review_path = Path(bundle_path), Path(review_path)
    bundle_bytes, review_bytes = bundle_path.read_bytes(), review_path.read_bytes()
    bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
    review_hash = hashlib.sha256(review_bytes).hexdigest()
    bundle = json.loads(bundle_bytes)
    review = json.loads(review_bytes)
    root = (Path(root) if root else Path(__file__).resolve().parent.parent).resolve()
    if domain not in ('finance', 'products') or bundle.get('domain') != domain or bundle.get('schema_version') != 1:
        raise ValueError('unsupported knowledge bundle or domain mismatch')
    if not isinstance(bundle.get('knowledge_version'), str) or not bundle['knowledge_version'].strip():
        raise ValueError('knowledge version required')
    entries = bundle.get('entries', [])
    ids = [e['object_id'] for e in entries]
    if not entries or len(ids) != len(set(ids)):
        raise ValueError('empty or duplicate knowledge entries')
    if (review.get('decision') != 'PASS' or review.get('bundle_sha256') != bundle_hash
            or review.get('reviewer') == review.get('researcher')
            or not review.get('reviewer') or not review.get('researcher')
            or set(review.get('approved_entry_ids', [])) != set(ids)
            or not review.get('evidence')):
        raise PermissionError('independent PASS review for this exact bundle required')
    sources = {s['source_id']: s for s in bundle['sources']}
    if not sources or len(sources) != len(bundle['sources']):
        raise ValueError('empty or duplicate sources')
    for source in sources.values():
        path = (root / source['snapshot_path']).resolve()
        if root not in path.parents or digest(path) != source['sha256']:
            raise ValueError('source snapshot missing, outside root or checksum mismatch')
    controller = store.session('controller')
    pending, skipped = [], []
    for entry in entries:
        payload = dict(entry['payload'], knowledge_version=bundle['knowledge_version'])
        if (not payload.get('title') or not isinstance(payload.get('title'), str)
                or not payload.get('content') or not isinstance(payload.get('content'), str)
                or not isinstance(payload.get('application', ''), str)
                or not isinstance(payload.get('aliases', []), list)
                or not all(isinstance(a, str) for a in payload.get('aliases', []))
                or not isinstance(payload.get('sources'), list) or not payload['sources']):
            raise ValueError('invalid knowledge payload')
        if not re.fullmatch(r'[a-z0-9_-]+', entry['object_id']):
            raise ValueError('invalid object id')
        if payload.get('verification') != 'source_supported':
            raise ValueError('only source-supported knowledge may be imported')
        # Tool usage may cite actual local validation separately from upstream docs.
        # These are passive source snapshots, never executable instructions to us.
        local_evidence = payload.get('local_evidence', [])
        if not isinstance(local_evidence, list):
            raise ValueError('local evidence must be a list')
        for evidence in local_evidence:
            if not isinstance(evidence, dict) or not all(
                    isinstance(evidence.get(k), str) and evidence[k].strip()
                    for k in ('snapshot_path', 'sha256', 'title', 'locator')):
                raise ValueError('local evidence metadata required')
            path = (root / evidence['snapshot_path']).resolve()
            if root not in path.parents or not path.is_file() or digest(path) != evidence['sha256']:
                raise ValueError('local evidence snapshot missing, outside root or checksum mismatch')
        for source in payload['sources']:
            if (source.get('source_id') not in sources or not source.get('excerpt')
                    or not source.get('locator')
                    or not all(source.get(k) for k in ('url', 'title', 'publisher', 'accessed_at'))
                    or not source['url'].startswith('https://')
                    or source['url'] != sources[source['source_id']]['url']):
                raise ValueError('entry evidence must reference a captured source')
        if domain == 'products':
            validate_product(entry['object_id'], payload)
        history = controller.history(domain, entry['object_id'])
        if history:
            if json.loads(history[-1]['payload']) != payload:
                raise ValueError('existing knowledge differs; append a separately reviewed revision')
            skipped.append(entry['object_id'])
        else:
            pending.append((entry['object_id'], payload))
    if not pending:
        return {'knowledge_version': bundle['knowledge_version'], 'published': [], 'skipped': skipped}
    task = controller.assign('product', '*')
    researcher = store.session('product')
    reviewer = store.session('reviewer')
    published = []
    # The local operator records an actual external Agent review; this is not a model review call.
    for object_id, payload in pending:
        proposal = researcher.propose(task, domain, object_id, payload)
        reviewer.review(proposal, True, json.dumps({
            'reviewer': review['reviewer'], 'review_file': str(review_path),
            'review_sha256': review_hash, 'bundle_sha256': bundle_hash,
            'evidence': review['evidence']}, ensure_ascii=False))
        controller.publish(proposal)
        published.append(object_id)
    controller.close_task(task)
    return {'knowledge_version': bundle['knowledge_version'], 'published': published,
            'skipped': skipped, 'reviewer': review['reviewer']}
