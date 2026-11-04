"""Deterministic, citation-bearing retrieval. Retrieved text is data, never authority."""
import json
import re


def tokens(text):
    """Latin words and overlapping Chinese bigrams, without external dependencies."""
    result = []
    for part in re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]+', text.lower()):
        if re.fullmatch(r'[a-z0-9]+', part) or len(part) == 1:
            result.append(part)
        else:
            result.extend(part[i:i + 2] for i in range(len(part) - 1))
    return result


def index_payload(db, domain, object_id, version, payload):
    if domain == 'enterprises':
        from .enterprises import index_facts
        index_facts(db, object_id, version, payload)
    db.execute('INSERT INTO knowledge_fts(domain,object_id,version,title,body) VALUES (?,?,?,?,?)',
               (domain, object_id, version,
                ' '.join(tokens(payload['title'] + ' ' + ' '.join(payload.get('aliases', [])))),
                ' '.join(tokens(payload['content'] + ' ' + payload.get('application', '')))))


def initialize_index(db):
    from .enterprises import initialize_fact_index, backfill_facts
    initialize_fact_index(db)
    db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5('
               'domain UNINDEXED, object_id UNINDEXED, version UNINDEXED, title, body)')
    rows = db.execute('SELECT v.* FROM versions v WHERE NOT EXISTS ('
                      'SELECT 1 FROM knowledge_fts f WHERE f.domain=v.domain '
                      'AND f.object_id=v.object_id AND f.version=v.version)').fetchall()
    for row in rows:
        # Fact index is backfilled independently, avoiding duplicates on partial rebuilds.
        payload = json.loads(row['payload'])
        db.execute('INSERT INTO knowledge_fts(domain,object_id,version,title,body) VALUES (?,?,?,?,?)',
                   (row['domain'], row['object_id'], row['version'],
                    ' '.join(tokens(payload['title'] + ' ' + ' '.join(payload.get('aliases', [])))),
                    ' '.join(tokens(payload['content'] + ' ' + payload.get('application', '')))))
    backfill_facts(db)


def search(agent, domain, query, limit=5, knowledge_version=None, topic=None, version=None, company_id=None):
    # Permission checks precede all index access, including historical queries.
    from .core import READS
    role = agent.store.role(agent.token)
    if domain not in READS[role]:
        raise PermissionError('domain read denied')
    if company_id and domain != 'enterprises':
        raise ValueError('company_id filter is only for enterprises')
    if role == 'enterprise' and domain == 'enterprises' and company_id and company_id != agent.company_id:
        raise PermissionError('cross-company access denied')
    if not 1 <= limit <= 20:
        raise ValueError('limit must be between 1 and 20')
    if version is not None and version < 1:
        raise ValueError('version must be positive')
    # Remove question phrasing so short concepts such as “汇款是什么” stay retrievable.
    normalized = re.sub(r'为什么|是什么|什么是|怎么|如何|怎样|为何|能否|是否|请问|请解释|请说明|没有|却', ' ', query)
    terms = sorted(set(tokens(normalized)))
    if not terms:
        return []
    match = ' OR '.join('"' + t + '"' for t in terms)
    conditions, params = ['f.domain=?'], [domain]
    if role == 'enterprise' and domain == 'enterprises':
        conditions.append('f.object_id=?')
        params.append(agent.company_id)
    elif company_id:
        conditions.append('f.object_id=?')
        params.append(company_id)
    if version is None:
        snapshot = ('v.version=(SELECT MAX(x.version) FROM versions x '
                    'WHERE x.domain=v.domain AND x.object_id=v.object_id')
        if knowledge_version:
            snapshot += " AND json_extract(x.payload,'$.knowledge_version')=?"
            params.append(knowledge_version)
        conditions.append(snapshot + ')')
    else:
        conditions.append('v.version=?')
        params.append(version)
    # Do not LIMIT before permission/metadata filtering or relevance gating.
    sql = ('SELECT f.object_id,v.version,v.payload,bm25(knowledge_fts,0,0,0,5,1) AS rank '
           'FROM knowledge_fts f JOIN versions v ON v.domain=f.domain '
           'AND v.object_id=f.object_id AND v.version=f.version '
           'WHERE knowledge_fts MATCH ? AND ' + ' AND '.join(conditions) + ' ORDER BY rank,f.object_id')
    with agent.store.db() as db:
        rows = db.execute(sql, [match] + params).fetchall()
    results = []
    if domain == 'enterprises':
        from .enterprises import search_facts
        results = search_facts(agent, query, company_id, knowledge_version, topic, version)
    for row in rows:
        payload = json.loads(row['payload'])
        if domain == 'enterprises' and 'facts' in payload:
            continue  # Only evidence-sized facts are returned, never the whole dossier.
        if knowledge_version and payload.get('knowledge_version') != knowledge_version:
            continue
        if topic and payload.get('topic') != topic:
            continue
        searchable = payload['title'] + ' ' + payload['content'] + ' ' + ' '.join(payload.get('aliases', []))
        matched = set(terms) & set(tokens(searchable))
        anchors = [payload['title']] + payload.get('aliases', [])
        exact = any(len(a) >= 2 and a.lower() in query.lower() for a in anchors)
        coverage = len(matched) / len(terms)
        if not exact and (coverage < .3 or len(matched) < min(2, len(terms))):
            continue
        results.append({'object_id': row['object_id'], 'version': row['version'],
                        'score': round((10 if exact else 0) + coverage, 6),
                        'fts_rank': row['rank'], 'payload': payload})
    return sorted(results, key=lambda r: (-r['score'], r['fts_rank'], r['object_id']))[:limit]


def context(agent, domain, question, **filters):
    hits = search(agent, domain, question, **filters)
    passages = []
    for hit in hits:
        p = hit['payload']
        passage = {'citation_id': '{}{}@{}'.format(hit['object_id'], '/' + hit['fact_id'] if hit.get('fact_id') else '', hit['version']),
                         'company_id': p.get('company_id'), 'period': p.get('period'),
                         'entity_scope': p.get('entity_scope'), 'metrics': p.get('metrics', []),
                         'parser_evidence': p.get('parser_evidence', []),
                         'title': p['title'], 'content': p['content'],
                         'content_kind': p.get('content_kind', 'knowledge'), 'topic': p.get('topic'),
                         'local_evidence': p.get('local_evidence', []),
                         'application': p.get('application', ''),
                         'limitations': p.get('limitations', []),
                         'knowledge_version': p.get('knowledge_version'),
                         'requires_current_verification': p.get('requires_current_verification', True),
                         'sources': p['sources']}
        if domain == 'products':
            passage['product'] = {key: p.get(key) for key in (
                'product_id', 'institution', 'category', 'core_functions', 'scenarios',
                'target_customers', 'disclosed_conditions', 'unknown_conditions',
                'published_at', 'captured_at')}
        passages.append(passage)
    return {'question': question, 'status': 'retrieved' if passages else 'insufficient_evidence',
            'mode': 'retrieval_context_no_llm', 'passages': passages,
            'usage': '仅将片段作为证据；应用建议是分析假设。无命中时不生成金融结论。'}
