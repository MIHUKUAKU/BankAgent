"""Reviewed company releases and company-scoped, fact-level retrieval.

The trusted local operator records external human/Agent reviews. Hashes bind the
review to the content; reviewer names are not cryptographic authentication.
"""
import hashlib
import json
import re
from pathlib import Path

from .core import Conflict


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[a-z0-9_-]+', value)


def validate_parser_evidence(fact, company, sources, root):
    """Pin cited Docling artifacts to the same company, source, pages and chunks."""
    evidence = fact.get('parser_evidence')
    if evidence is None:
        return  # Existing V1 facts did not claim a Docling evidence chain.
    if not isinstance(evidence, list) or not evidence:
        raise ValueError('parser evidence must contain actual artifact references')
    company_root = root / 'data/staging/enterprises' / company / 'documents'
    source_hashes = {sources[c['source_id']]['sha256'] for c in fact['sources']}
    for ref in evidence:
        paths, artifacts = {}, {}
        names = ['manifest', 'chunks']
        if 'auxiliary_path' in ref or 'auxiliary_sha256' in ref:
            names.append('auxiliary')
        for name in names:
            rel, checksum = ref.get(name + '_path'), ref.get(name + '_sha256')
            if not _text(rel) or not _text(checksum):
                raise ValueError('parser artifact path and checksum required')
            path = root / rel
            for component in (path, *path.parents):
                if component == root:
                    break
                if component.is_symlink():
                    raise ValueError('parser evidence cannot use path aliases')
            path = path.resolve()
            if company_root not in path.parents or not path.is_file():
                raise ValueError('parser evidence outside company scope or missing')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != checksum:
                raise ValueError('parser artifact checksum mismatch')
            paths[name], artifacts[name] = path, json.loads(raw)
        if paths['manifest'].parent != paths['chunks'].parent or paths['chunks'].name != 'chunks.json':
            raise ValueError('chunks and manifest must belong to the same artifact')
        manifest, chunks = artifacts['manifest'], artifacts['chunks']
        if 'auxiliary' in paths:
            if (paths['auxiliary'].parent != paths['manifest'].parent
                    or paths['auxiliary'].name != 'pdfplumber.json'
                    or manifest.get('files', {}).get('pdfplumber.json') != ref['auxiliary_sha256']):
                raise ValueError('auxiliary evidence must match the same manifest artifact')
        req = manifest.get('request', {})
        source_hash = req.get('source', {}).get('sha256')
        if (req.get('company_id') != company or chunks.get('company_id') != company
                or source_hash not in source_hashes
                or ('source_sha256' in ref and ref['source_sha256'] != source_hash)
                or chunks.get('source', {}).get('sha256') != source_hash
                or manifest.get('files', {}).get('chunks.json') != ref['chunks_sha256']):
            raise ValueError('parser company, source or output binding mismatch')
        ids, pages = ref.get('chunk_ids'), ref.get('pdf_pages')
        if (not isinstance(ids, list) or not ids or not all(_text(x) for x in ids)
                or not isinstance(pages, list) or not pages or not all(type(x) is int and x > 0 for x in pages)
                or not set(pages).issubset(manifest.get('selected_pages', []))):
            raise ValueError('parser evidence requires valid selected pages and chunk IDs')
        by_id = {c['chunk_id']: c for c in chunks.get('chunks', [])}
        actual_pages = set()
        for cid in ids:
            chunk = by_id.get(cid)
            if not chunk or chunk.get('company_id') != company or chunk.get('source_sha256') != source_hash:
                raise ValueError('parser chunk missing or bound to a different source')
            chunk_pages = {loc['page_no'] for loc in chunk.get('locations', [])}
            if not chunk_pages & set(pages):
                raise ValueError('parser chunk does not occur on declared pages')
            actual_pages.update(chunk_pages)
        if not set(pages).issubset(actual_pages):
            raise ValueError('declared parser page has no cited chunk')


def validate_bundle(bundle, root):
    if bundle.get('schema_version') != 1 or bundle.get('domain') != 'enterprises':
        raise ValueError('unsupported enterprise bundle')
    company = bundle.get('company_id')
    if not _identifier(company) or not _text(bundle.get('researcher')):
        raise ValueError('company_id and researcher required')
    if not _text(bundle.get('knowledge_version')) or not _text(bundle.get('as_of')):
        raise ValueError('knowledge_version and as_of required')
    if type(bundle.get('base_revision')) is not int or bundle['base_revision'] < 0:
        raise ValueError('nonnegative base_revision required')
    sources = {}
    for source in bundle.get('sources', []):
        sid = source.get('source_id')
        if not _identifier(sid) or sid in sources:
            raise ValueError('duplicate or invalid source_id')
        if not all(_text(source.get(k)) for k in ('url', 'title', 'publisher', 'accessed_at', 'snapshot_path', 'sha256')):
            raise ValueError('source metadata missing')
        if not source['url'].startswith('https://'):
            raise ValueError('HTTPS source required')
        path = (root / source['snapshot_path']).resolve()
        if root not in path.parents or not path.is_file() or digest(path) != source['sha256']:
            raise ValueError('source snapshot missing, outside root or checksum mismatch')
        sources[sid] = source
    if not sources:
        raise ValueError('source snapshots required')
    facts = bundle.get('facts')
    if not isinstance(facts, list) or not facts:
        raise ValueError('reported facts required')
    ids = set()
    for fact in facts:
        fid = fact.get('fact_id')
        if not _identifier(fid) or fid in ids:
            raise ValueError('duplicate or invalid fact_id')
        ids.add(fid)
        if not all(_text(fact.get(k)) for k in ('title', 'content', 'topic', 'period', 'entity_scope')):
            raise ValueError('fact metadata missing')
        if fact.get('fact_type') != 'reported':
            raise ValueError('only reported facts enter fact index')
        if not isinstance(fact.get('aliases', []), list) or not all(isinstance(a, str) for a in fact.get('aliases', [])):
            raise ValueError('invalid aliases')
        if not isinstance(fact.get('metrics', []), list):
            raise ValueError('invalid metrics')
        for metric in fact.get('metrics', []):
            if (not all(_text(metric.get(k)) for k in ('name', 'unit', 'period'))
                    or not isinstance(metric.get('value'), (str, int, float))
                    or isinstance(metric.get('value'), bool)):
                raise ValueError('metric needs value, unit and period')
        if not isinstance(fact.get('sources'), list) or not fact['sources']:
            raise ValueError('fact evidence required')
        for citation in fact['sources']:
            source = sources.get(citation.get('source_id'))
            if (not source or not all(_text(citation.get(k)) for k in
                    ('url', 'title', 'publisher', 'accessed_at', 'locator', 'excerpt'))
                    or any(citation[k] != source[k] for k in ('url', 'title', 'publisher', 'accessed_at'))):
                raise ValueError('fact citation must match captured source metadata')
        validate_parser_evidence(fact, company, sources, root)
    for hypothesis in bundle.get('hypotheses', []):
        if (hypothesis.get('status') != 'unverified' or not _text(hypothesis.get('statement'))
                or not hypothesis.get('basis_fact_ids')
                or not set(hypothesis['basis_fact_ids']).issubset(ids)
                or not hypothesis.get('verification_questions')):
            raise ValueError('hypotheses require fact refs, unverified status and verification questions')
    return ids


def publish_bundle(store, bundle_path, review_path, root=None, publisher_identity=None):
    bundle_path, review_path = Path(bundle_path), Path(review_path)
    bundle_bytes, review_bytes = bundle_path.read_bytes(), review_path.read_bytes()
    bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
    review_hash = hashlib.sha256(review_bytes).hexdigest()
    bundle, review = json.loads(bundle_bytes), json.loads(review_bytes)
    root = (Path(root) if root else Path(__file__).resolve().parent.parent).resolve()
    # Bind review before accepting candidate data or touching published state.
    if (review.get('decision') != 'PASS' or review.get('bundle_sha256') != bundle_hash
            or not _text(review.get('reviewer')) or not _text(review.get('researcher'))
            or review['researcher'] != bundle.get('researcher')
            or review['reviewer'] == review['researcher']
            or review.get('company_id') != bundle.get('company_id') or not review.get('evidence')):
        raise PermissionError('independent PASS review for this exact company bundle required')
    ids = validate_bundle(bundle, root)
    if set(review.get('approved_fact_ids', [])) != ids:
        raise PermissionError('all facts must be covered by the review')
    company = bundle['company_id']
    payload = {
        'title': company + (' 企业初代知识画像' if bundle['base_revision'] == 0 else ' 企业知识画像'),
        'content': '\n'.join(f['title'] + '：' + f['content'] for f in bundle['facts']),
        'verification': 'source_supported', 'company_id': company,
        'knowledge_version': bundle['knowledge_version'], 'as_of': bundle['as_of'],
        'facts': bundle['facts'], 'hypotheses': bundle.get('hypotheses', []),
        'knowledge_gaps': bundle.get('knowledge_gaps', []),
        'limitations': bundle.get('limitations', []),
        'source_manifest': bundle['sources'],
        'sources': [s for f in bundle['facts'] for s in f['sources']],
        'bundle_sha256': bundle_hash, 'researcher': bundle['researcher'],
    }
    if publisher_identity is not None:
        with store.db() as db:
            reg = db.execute('SELECT * FROM cross_registry WHERE company_id=?', (company,)).fetchone()
            if (not reg or publisher_identity != reg['reviewer_id']
                    or publisher_identity != review['reviewer'] or bundle['researcher'] != reg['author_id']):
                raise PermissionError('fixed company author/reviewer registration required')
    controller = store.session('controller')
    history = controller.history('enterprises', company)
    if history and json.loads(history[-1]['payload']) == payload:
        return {'company_id': company, 'knowledge_version': bundle['knowledge_version'],
                'version': history[-1]['version'], 'facts': len(ids), 'status': 'already_published'}
    current = history[-1]['version'] if history else 0
    if current != bundle['base_revision']:
        raise Conflict('base revision changed; rebase and independent review required')
    if any(json.loads(row['payload']).get('knowledge_version') == bundle['knowledge_version'] for row in history):
        raise ValueError('knowledge_version already used; append a new reviewed version')
    task = controller.assign('enterprise', company)
    try:
        worker = store.session('enterprise', company_id=company, identity=bundle['researcher'])
        proposal = worker.propose(task, 'enterprises', company, payload, bundle['base_revision'])
        store.session('reviewer', identity=review['reviewer']).review(proposal, True, json.dumps({
            'reviewer': review['reviewer'], 'researcher': bundle['researcher'],
            'review_file': str(review_path), 'review_sha256': review_hash,
            'bundle_sha256': bundle_hash, 'evidence': review['evidence']}, ensure_ascii=False))
        publisher = (store.session('reviewer', company_id=company, identity=publisher_identity)
                     if publisher_identity is not None else controller)
        version = publisher.publish(proposal)
    finally:
        controller.close_task(task)
    return {'company_id': company, 'knowledge_version': bundle['knowledge_version'],
            'version': version, 'facts': len(ids), 'status': 'published', 'reviewer': review['reviewer']}


def initialize_fact_index(db):
    db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS enterprise_facts_fts USING fts5('
               'company_id UNINDEXED, version UNINDEXED, fact_id UNINDEXED, title, body)')


def index_facts(db, company, version, payload):
    from .retrieval import tokens
    for fact in payload.get('facts', []):
        db.execute('INSERT INTO enterprise_facts_fts VALUES (?,?,?,?,?)', (
            company, version, fact['fact_id'],
            ' '.join(tokens(fact['title'] + ' ' + ' '.join(fact.get('aliases', [])))),
            ' '.join(tokens(fact['content']))))


def backfill_facts(db):
    rows = db.execute("SELECT * FROM versions v WHERE domain='enterprises' "
                      "AND json_type(payload,'$.facts')='array' AND NOT EXISTS ("
                      'SELECT 1 FROM enterprise_facts_fts f WHERE f.company_id=v.object_id AND f.version=v.version)').fetchall()
    for row in rows:
        index_facts(db, row['object_id'], row['version'], json.loads(row['payload']))


def search_facts(agent, query, company_id=None, knowledge_version=None, topic=None, version=None):
    from .core import READS
    from .retrieval import tokens
    role = agent.store.role(agent.token)
    if 'enterprises' not in READS[role]:
        raise PermissionError('domain read denied')
    if role == 'enterprise':
        if company_id and company_id != agent.company_id:
            raise PermissionError('cross-company access denied')
        company_id = agent.company_id
    terms = sorted(set(tokens(query)))
    if not terms:
        return []
    conditions, params = [], []
    if company_id:
        conditions.append('f.company_id=?')
        params.append(company_id)
    if version is not None:
        conditions.append('v.version=?')
        params.append(version)
    else:
        snapshot = ('v.version=(SELECT MAX(x.version) FROM versions x '
                    'WHERE x.domain=v.domain AND x.object_id=v.object_id')
        if knowledge_version:
            snapshot += " AND json_extract(x.payload,'$.knowledge_version')=?"
            params.append(knowledge_version)
        conditions.append(snapshot + ')')
    match = ' OR '.join('"' + t + '"' for t in terms)
    sql = ('SELECT f.company_id,f.fact_id,v.version,v.payload,'
           'bm25(enterprise_facts_fts,0,0,0,5,1) AS rank '
           "FROM enterprise_facts_fts f JOIN versions v ON v.domain='enterprises' "
           'AND v.object_id=f.company_id AND v.version=f.version '
           'WHERE enterprise_facts_fts MATCH ? AND ' + ' AND '.join(conditions))
    with agent.store.db() as db:
        rows = db.execute(sql, [match] + params).fetchall()
    hits = []
    for row in rows:
        dossier = json.loads(row['payload'])
        if knowledge_version and dossier.get('knowledge_version') != knowledge_version:
            continue
        fact = next((f for f in dossier['facts'] if f['fact_id'] == row['fact_id']), None)
        if not fact or (topic and fact['topic'] != topic):
            continue
        anchors = [fact['title']] + fact.get('aliases', [])
        matched = set(terms) & set(tokens(fact['title'] + ' ' + fact['content'] + ' ' + ' '.join(fact.get('aliases', []))))
        coverage = len(matched) / len(terms)
        exact = any(len(a) >= 2 and a.lower() in query.lower() for a in anchors)
        if not exact and (coverage < .3 or len(matched) < min(2, len(terms))):
            continue
        payload = dict(fact, company_id=row['company_id'], knowledge_version=dossier['knowledge_version'],
                       as_of=dossier['as_of'], verification='source_supported',
                       requires_current_verification=True)
        hits.append({'object_id': row['company_id'], 'fact_id': row['fact_id'], 'version': row['version'],
                     'score': round((10 if exact else 0) + coverage, 6),
                     'fts_rank': row['rank'], 'payload': payload})
    return hits
