"""Trusted-host cross review coordinator. No automatic model invocation.

Workers are disposable JSON protocol processes; they never receive a Store or
formal DB path. Local operator identity is not cryptographic authentication.
"""
import hashlib
import json
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager, closing
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from .core import encode, now


def sha(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def canonical(url):
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password:
        raise ValueError('canonical HTTPS source required')
    query = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
    return urlunsplit(('https', p.netloc.lower(), p.path.rstrip('/') or '/', urlencode(sorted(query)), ''))


def suspicious(value):
    # Conservative quarantine heuristic, not a claim of universal injection detection.
    return bool(re.search(r'ignore\s+(all\s+)?(previous|prior|system)|system\s*prompt|execute\s+(shell|command)|<\s*(system|script)|\u5ffd\u7565.{0,12}(\u6307\u4ee4|\u89c4\u5219)|\u6267\u884c.{0,6}(shell|\u547d\u4ee4)', encode(value), re.I))


def source_records(payload):
    # Enterprise importer keeps full source snapshot metadata in source_manifest;
    # payload.sources contains per-fact citations and may omit the original hash.
    return {s['source_id']:s for s in payload.get('source_manifest',payload.get('sources',[]))}


def initialize_governance(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS cross_registry(company_id TEXT PRIMARY KEY, author_id TEXT NOT NULL, reviewer_id TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS cross_policies(company_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled=1));
    CREATE TABLE IF NOT EXISTS cross_authors(proposal TEXT PRIMARY KEY, author_id TEXT);
    CREATE TABLE IF NOT EXISTS cross_own_reviews(proposal TEXT PRIMARY KEY, digest TEXT NOT NULL, report_sha256 TEXT NOT NULL, author_id TEXT NOT NULL, reviewer_id TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS cross_clearances(proposal TEXT PRIMARY KEY, digest TEXT NOT NULL, report_sha256 TEXT NOT NULL, result_sha256 TEXT NOT NULL, status TEXT NOT NULL, run_id TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS cross_attempts(proposal TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE);
    ''')
    if 'cross_fact_ids' not in {r[1] for r in db.execute('PRAGMA table_info(cross_own_reviews)')}:
        db.execute("ALTER TABLE cross_own_reviews ADD COLUMN cross_fact_ids TEXT NOT NULL DEFAULT '[]'")
    for table in ('cross_registry', 'cross_policies', 'cross_authors', 'cross_own_reviews', 'cross_clearances', 'cross_attempts'):
        for action in ('UPDATE', 'DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable cross governance'); END")


def policy(db, company):
    return db.execute('SELECT 1 FROM cross_policies WHERE company_id=? AND enabled=1', (company,)).fetchone() is not None


def check_author(db, domain, company, identity):
    if domain == 'enterprises':
        row = db.execute('SELECT * FROM cross_registry WHERE company_id=?', (company,)).fetchone()
        if row and (not identity or identity != row['author_id']):
            raise PermissionError('registered enterprise author identity required')


def bind_review(db, proposal, identity, report_hash, approved, cross_fact_ids=None):
    if proposal['domain'] != 'enterprises':
        return
    reg = db.execute('SELECT * FROM cross_registry WHERE company_id=?', (proposal['object_id'],)).fetchone()
    if not reg:
        return
    author = db.execute('SELECT author_id FROM cross_authors WHERE proposal=?', (proposal['id'],)).fetchone()
    if not reg or identity != reg['reviewer_id'] or not author or author[0] != reg['author_id'] or identity == author[0]:
        raise PermissionError('fixed independent own reviewer identity required')
    enabled = policy(db, proposal['object_id'])
    if (enabled or report_hash is not None) and not valid_hash(report_hash):
        raise ValueError('exact report SHA256 required')
    if approved:
        facts = {f['fact_id'] for f in json.loads(proposal['payload']).get('facts', [])}
        ids = (sorted(facts) if cross_fact_ids is None else cross_fact_ids) if report_hash is not None or enabled else []
        if ((report_hash is not None or enabled) and (not isinstance(ids,list) or not ids or any(not isinstance(i,str) for i in ids)
                or len(set(ids)) != len(ids) or not set(ids) <= facts)):
            raise ValueError('own review must select actual nonempty cross fact IDs')
        db.execute('INSERT INTO cross_own_reviews VALUES (?,?,?,?,?,?)', (proposal['id'], proposal['digest'], report_hash or '', author[0], identity, encode(sorted(ids))))


def check_publish(db, proposal):
    if proposal['domain'] != 'enterprises':
        return
    reg=db.execute('SELECT * FROM cross_registry WHERE company_id=?',(proposal['object_id'],)).fetchone()
    own = db.execute('SELECT * FROM cross_own_reviews WHERE proposal=?', (proposal['id'],)).fetchone()
    if reg and (not own or own['digest']!=proposal['digest'] or own['author_id']!=reg['author_id'] or own['reviewer_id']!=reg['reviewer_id']):
        raise PermissionError('fixed own author/reviewer binding required for registered company')
    if not policy(db,proposal['object_id']):
        return
    clear = db.execute('SELECT * FROM cross_clearances WHERE proposal=?', (proposal['id'],)).fetchone()
    if (not own or not clear or clear['status'] != 'independent_supported'
            or clear['digest'] != proposal['digest'] or own['digest'] != proposal['digest']
            or clear['report_sha256'] != own['report_sha256']):
        raise PermissionError('cross review clearance bound to exact proposal and report required')


class CrossReview:
    """Trusted coordinator only. Never expose this object as a model tool."""
    def __init__(self, store, audit_path):
        self.store = store
        self.audit_path = str(audit_path)
        if Path(audit_path).resolve() == Path(store.path).resolve():
            raise ValueError('audit must be a separate database')
        Path(audit_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.audit_path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, operation TEXT, detail TEXT, created TEXT)')
            for action in ('UPDATE','DELETE'):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_audit_{action} BEFORE {action} ON audit BEGIN SELECT RAISE(ABORT,'immutable audit'); END")
            db.commit()
        self.runs = {}
        self.origins = {}

    @contextmanager
    def _read(self):
        db = sqlite3.connect(Path(self.store.path).resolve().as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        try:
            yield db
        finally:
            db.close()

    def _log(self, run, session, operation, detail):
        # Only explicit metadata constructed here; never store arbitrary worker input.
        with closing(sqlite3.connect(self.audit_path)) as db:
            db.execute('INSERT INTO audit(run_id,session_id,operation,detail,created) VALUES (?,?,?,?,?)', (run,session,operation,encode(detail),now()))
            db.commit()

    def register_company(self, company_id, author_id, reviewer_id):
        if not all(isinstance(x,str) and x.strip() for x in (company_id,author_id,reviewer_id)) or author_id == reviewer_id:
            raise ValueError('company and distinct author/reviewer identities required')
        with self.store.db() as db:
            for existing in db.execute('SELECT * FROM cross_registry'):
                if existing['company_id'] != company_id and ({author_id, reviewer_id} & {existing['author_id'], existing['reviewer_id']}):
                    raise PermissionError('cross-company author/reviewer identities must be distinct')
            old = db.execute('SELECT * FROM cross_registry WHERE company_id=?',(company_id,)).fetchone()
            if old:
                if (old['author_id'],old['reviewer_id']) != (author_id,reviewer_id):
                    raise PermissionError('fixed registry binding is immutable')
                return
            db.execute('INSERT INTO cross_registry VALUES (?,?,?)',(company_id,author_id,reviewer_id))

    def enable_policy(self, company_id):
        with self.store.db() as db:
            if not db.execute('SELECT 1 FROM cross_registry WHERE company_id=?',(company_id,)).fetchone():
                raise ValueError('register fixed identities first')
            db.execute('INSERT OR IGNORE INTO cross_policies VALUES (?,1)',(company_id,))

    def register_origin(self, source_id, origin_id, canonical_url, content_sha256):
        if any(not r['closed'] for r in self.runs.values()):
            raise PermissionError('origin registry frozen while any run is active')
        if not all(isinstance(x,str) and x.strip() for x in (source_id,origin_id)) or not valid_hash(content_sha256):
            raise ValueError('verified origin ID and content SHA256 required')
        value = (origin_id, canonical(canonical_url), content_sha256)
        if source_id in self.origins and self.origins[source_id] != value:
            raise ValueError('ambiguous source ID lineage')
        self.origins[source_id] = value

    def _origin(self, source):
        record = self.origins.get(source.get('source_id'))
        if not record:
            return None
        if canonical(source.get('url','')) != record[1] or source.get('sha256') != record[2]:
            return None
        return set(('id:'+record[0], 'url:'+record[1], 'sha:'+record[2]))

    def _same(self, left, right):
        # Transitive mirror/reprint lineage through the entire verified registry.
        expanded = set(left)
        changed = True
        while changed:
            changed = False
            for origin, url, digest in self.origins.values():
                group = {'id:'+origin,'url:'+url,'sha:'+digest}
                if group & expanded and not group <= expanded:
                    expanded.update(group)
                    changed = True
        return bool(expanded & right)

    def _evidence_origin(self, citation, sources):
        source=sources.get(citation.get('source_id'),{})
        try:
            if canonical(citation.get('url','')) != canonical(source.get('url','')):
                return None
        except ValueError:
            return None
        if citation.get('sha256',source.get('sha256')) != source.get('sha256'):
            return None
        return self._origin(source)

    def _published(self, company=None, version=None):
        with self._read() as db:
            if company is None:
                return [r[0] for r in db.execute("SELECT DISTINCT object_id FROM versions WHERE domain='enterprises'")]
            sql = "SELECT * FROM versions WHERE domain='enterprises' AND object_id=?"
            args = [company]
            if version is not None:
                if type(version) is not int or version < 1:
                    raise ValueError('positive historical version required')
                sql += ' AND version=?'; args.append(version)
            row = db.execute(sql+' ORDER BY version DESC LIMIT 1',args).fetchone()
            if not row:
                raise ValueError('published company/version not found')
            return {'company_id':company,'version':row['version'],'digest':row['digest'],'payload':json.loads(row['payload'])}

    def coverage(self, company_id):
        companies = sorted(c for c in self._published() if c != company_id)
        return {'company_id':company_id,'external_companies':companies,'required':2,'status':'coverage_insufficient' if len(companies)<2 else 'selection_required'}

    def start(self, proposal_id, report_sha256, claims):
        if not valid_hash(report_sha256) or not isinstance(claims,list) or not claims:
            raise ValueError('report hash and nonempty fixed claims required')
        with self._read() as db:
            p = db.execute('SELECT * FROM proposals WHERE id=?',(proposal_id,)).fetchone()
            own = db.execute('SELECT * FROM cross_own_reviews WHERE proposal=?',(proposal_id,)).fetchone()
            review = db.execute('SELECT * FROM reviews WHERE proposal=?',(proposal_id,)).fetchone()
            if not p or p['domain'] != 'enterprises' or not own or not review or review['decision'] != 'approved' or own['digest'] != p['digest'] or own['report_sha256'] != report_sha256:
                raise PermissionError('actual bound own approved review required')
            reg = db.execute('SELECT * FROM cross_registry WHERE company_id=?',(p['object_id'],)).fetchone()
            if not reg or own['author_id'] != reg['author_id'] or own['reviewer_id'] != reg['reviewer_id']:
                raise PermissionError('own reviewer/author binding mismatch')
            payload = json.loads(p['payload'])
        claims = json.loads(encode(claims))
        facts = {f['fact_id']:f for f in payload.get('facts',[])}
        required = set(json.loads(own['cross_fact_ids']))
        sources = source_records(payload)
        if not facts:
            raise ValueError('structured published-style facts required')
        snippets, a_origins, a_known, seen, covered = {}, {}, {}, set(), set()
        fields = {'claim_id','company_id','subject','relation','period','source_ids','evidence_ids'}
        for c in claims:
            if not isinstance(c,dict) or set(c) not in (fields,fields|{'assertion'}):
                raise ValueError('fixed claim fields only')
            if any(not isinstance(c[k],str) or not c[k].strip() or len(c[k])>256 for k in fields-{'source_ids','evidence_ids'}):
                raise ValueError('bounded nonempty claim strings required')
            if c['company_id'] != p['object_id'] or c['claim_id'] in seen:
                raise ValueError('claim company/id mismatch')
            seen.add(c['claim_id'])
            for k in ('source_ids','evidence_ids'):
                if not isinstance(c[k],list) or not 1<=len(c[k])<=4 or any(not isinstance(x,str) for x in c[k]) or len(set(c[k]))!=len(c[k]):
                    raise ValueError('bounded distinct evidence/source IDs required')
            if not set(c['source_ids']) <= sources.keys() or not set(c['evidence_ids']) <= facts.keys():
                raise ValueError('claim references missing actual evidence')
            if len(c['evidence_ids']) != 1:
                raise ValueError('one concrete fact per claim required')
            assertion = facts[c['evidence_ids'][0]].get('content')
            if not isinstance(assertion,str) or not assertion or len(assertion)>2000 or c.get('assertion',assertion)!=assertion:
                raise ValueError('assertion must equal own-reviewed fact content')
            c['assertion'] = assertion
            texts, origins, lineage_known = [], [], True
            for eid in c['evidence_ids']:
                f = facts[eid]
                if c['period'] != f.get('period') or c['subject'] != f.get('entity_scope') or c['relation'] not in [f.get('title'),*f.get('cross_review_keys',[])]:
                    raise ValueError('claim subject/relation/period must match reviewed fact')
                cited = {s.get('source_id'):s for s in f.get('sources',[])}
                if not set(c['source_ids']) <= cited.keys():
                    raise ValueError('claim source must be cited by each referenced fact')
                for sid in c['source_ids']:
                    excerpt = cited[sid].get('excerpt','')
                    if not excerpt or len(excerpt)>2000:
                        raise ValueError('minimal bounded excerpt required')
                    texts.append({'evidence_id':eid,'source_id':sid,'excerpt':excerpt})
                for citation in cited.values():
                    o=self._evidence_origin(citation,sources)
                    if o:origins.append(o)
                    else:lineage_known=False
                covered.add(eid)
            snippets[c['claim_id']] = texts
            a_origins[c['claim_id']] = origins
            a_known[c['claim_id']] = lineage_known and bool(origins)
        if covered != required:
            raise ValueError('claims must cover exactly own-review selected fact subset')
        run_id = secrets.token_hex(16)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM cross_attempts WHERE proposal=?',(proposal_id,)).fetchone() or db.execute('SELECT 1 FROM cross_clearances WHERE proposal=?',(proposal_id,)).fetchone():
                raise PermissionError('proposal cross review is one-shot; submit a newly reviewed proposal to retry')
            db.execute('INSERT INTO cross_attempts VALUES (?,?)',(proposal_id,run_id))
        self.runs[run_id] = {'proposal':proposal_id,'digest':p['digest'],'report_sha256':report_sha256,'company_id':p['object_id'],'claims':claims,'cross_fact_ids':sorted(required),'not_cross_reviewed_fact_ids':sorted(facts.keys()-required),'snippets':snippets,'a_origins':a_origins,'a_known':a_known,'workers':{},'selection':None,'closed':False,'quarantined':suspicious(claims) or suspicious(snippets)}
        try:
            self._log(run_id,None,'start',{'proposal':proposal_id,'digest':p['digest'],'report_sha256':report_sha256,'claims_sha256':sha(claims)})
        except Exception:
            self.abort(run_id)
            raise
        return run_id

    def _run(self, run_id):
        r = self.runs.get(run_id)
        if not r or r['closed']:
            raise PermissionError('unknown or closed run')
        return r

    def select(self, run_id, candidates=None):
        r = self._run(run_id)
        if r['workers']:
            raise PermissionError('selection locked after worker launch')
        companies = self._published() if candidates is None else list(candidates)
        if len(set(companies)) != len(companies):
            raise ValueError('duplicate candidates')
        selection = {}
        for c in r['claims']:
            ranked = []
            lineage = {}
            for company in companies:
                if company == r['company_id']:
                    continue
                snap = self._published(company)
                payload = snap['payload']
                sources = source_records(payload)
                eligible, known, cost = [], [], 0
                # Relevance only: never infer support/refutation here.
                from .retrieval import tokens
                wanted = set(tokens(c['subject']+' '+c['relation']))
                for f in payload.get('facts',[]):
                    labels = ' '.join(f.get('cross_review_keys',[]))+' '+f.get('title','')+' '+f.get('content','')
                    overlap = len(wanted & set(tokens(labels)))
                    if f.get('period') != c['period'] or not overlap:
                        continue
                    eligible.append(f['fact_id'])
                    cost += len(f.get('content',''))
                    for source in f.get('sources',[]):
                        o = self._evidence_origin(source,sources)
                        if o and all(not self._same(o,a) for a in r['a_origins'][c['claim_id']]): known.append(o)
                if eligible:
                    independent_groups = []
                    for o in known:
                        if all(not self._same(o,other) for other in independent_groups): independent_groups.append(o)
                    ranked.append({'company_id':company,'version':snap['version'],'evidence_ids':eligible,'relevance':len(eligible),'independent_origins':len(independent_groups),'cost':cost})
                    lineage[company] = independent_groups
            ranked.sort(key=lambda x:(-bool(x['independent_origins']),-x['relevance'],x['cost'],x['company_id']))
            # Choose complementary origins ahead of same-origin mirrors.
            ordered, used = [], []
            while ranked:
                for item in ranked:
                    item['complementarity'] = sum(all(not self._same(o,u) for u in used) for o in lineage[item['company_id']])
                ranked.sort(key=lambda x:(-bool(x['independent_origins']),-x['complementarity'],-x['relevance'],x['cost'],x['company_id']))
                best = ranked.pop(0); ordered.append(best); used.extend(lineage[best['company_id']])
            ranked = ordered
            selection[c['claim_id']] = {'status':'selection_required' if len(ranked)>=2 else 'coverage_insufficient','candidates':ranked}
        r['selection'] = selection
        return json.loads(encode(selection))

    def open_worker(self, run_id, company_id, version=None):
        r = self._run(run_id)
        if r['quarantined']:
            raise PermissionError('quarantined run')
        if r['selection'] is None:
            self.select(run_id)
        if company_id in r['workers'] or company_id == r['company_id']:
            raise PermissionError('one disposable session per external company')
        with self._read() as db:
            reg = db.execute('SELECT * FROM cross_registry WHERE company_id=?',(company_id,)).fetchone()
            a_reg = db.execute('SELECT * FROM cross_registry WHERE company_id=?',(r['company_id'],)).fetchone()
        if not reg or reg['reviewer_id'] in (a_reg['author_id'],a_reg['reviewer_id']):
            raise PermissionError('distinct registered external own reviewer required')
        claims, allowed = [], set()
        for c in r['claims']:
            match = next((x for x in r['selection'][c['claim_id']]['candidates'] if x['company_id']==company_id),None)
            if match:
                if version is not None and version != match['version']:
                    raise PermissionError('selection pins exact published version')
                version = match['version']; allowed.update(match['evidence_ids']); claims.append(c)
        if not claims:
            raise PermissionError('company not selected for any claim')
        snapshot = self._published(company_id,version)
        if suspicious(snapshot['payload']):
            r['quarantined'] = True
            self._log(run_id,None,'quarantine',{'company_id':company_id,'snapshot_sha256':snapshot['digest']})
            raise PermissionError('suspicious source data quarantined')
        try:
            worker = Worker(self,run_id,company_id,snapshot,claims,allowed,reg['reviewer_id'])
        except Exception:
            self.abort(run_id)
            raise
        r['workers'][company_id] = worker
        return worker

    def finish(self, run_id):
        try:
            return self._finish(run_id)
        except Exception:
            self.abort(run_id)
            raise

    def _finish(self, run_id):
        r = self._run(run_id)
        if r['selection'] is None:
            self.select(run_id)
        for w in r['workers'].values(): w.close()
        rows = []
        for c in r['claims']:
            results = []
            opinion_origins = {}
            for company,w in r['workers'].items():
                item = w.results.get(c['claim_id'])
                if item:
                    annotated = {'company_id':company,'reviewer_id':w.reviewer_id,
                                 'snapshot_version':w.snapshot['version'],
                                 'snapshot_sha256':w.snapshot['digest'],**item}
                    sources = source_records(w.snapshot['payload'])
                    facts = {f['fact_id']:f for f in w.snapshot['payload'].get('facts',[])}
                    origins, unknown = [], not bool(item['evidence_ids'])
                    for eid in item['evidence_ids']:
                        if eid.startswith('a:'):continue
                        for source in facts[eid].get('sources',[]):
                            o=self._evidence_origin(source,sources)
                            if o:origins.append(o)
                            else:unknown=True
                    a_known = r['a_known'][c['claim_id']]
                    classification = 'unknown' if unknown or not a_known or not origins else 'shared' if any(self._same(o,a) for o in origins for a in r['a_origins'][c['claim_id']]) else 'independent'
                    if item['phase']=='phase2':classification='source_only'
                    annotated['independence']=classification
                    annotated['independent_of_a']=classification=='independent'
                    annotated['independent']=classification=='independent'
                    opinion_origins[company]=origins
                    results.append(annotated)
            for item in results:
                if item['independence']=='independent' and any(
                        self._same(o,other) for o in opinion_origins[item['company_id']]
                        for company, origins in opinion_origins.items() if company!=item['company_id']
                        for other in origins):
                    item['independence']='shared';item['independent']=False
            status = 'unverifiable'
            if r['quarantined'] or any(w.quarantined for w in r['workers'].values()): status='quarantined'
            elif any(x['decision']=='refute' for x in results): status='conflict'
            else:
                supports = []
                for item in results:
                    if item['decision'] != 'support': continue
                    w = r['workers'][item['company_id']]
                    sources = source_records(w.snapshot['payload'])
                    facts = {f['fact_id']:f for f in w.snapshot['payload'].get('facts',[])}
                    origins=[]
                    all_known=True
                    for eid in item['evidence_ids']:
                        for source in facts[eid].get('sources',[]):
                            o=self._evidence_origin(source,sources)
                            if o: origins.append(o)
                            else: all_known=False
                    # Unknown A or B lineage is conservatively not independent.
                    a_known = r['a_known'][c['claim_id']]
                    if a_known and all_known and origins and all(not self._same(o,a) for o in origins for a in r['a_origins'][c['claim_id']]):
                        supports.append((item['company_id'],origins))
                independent = any(all(not self._same(a,b) for a in oa for b in ob) for i,(_,oa) in enumerate(supports) for _,ob in supports[i+1:])
                if independent: status='independent_supported'
                elif any(x['decision'] in ('support','non_independent_support') for x in results):status='non_independent_support'
                if len(r['selection'][c['claim_id']]['candidates'])<2 or len(results)<2:
                    status='coverage_insufficient'
            rows.append({'claim_id':c['claim_id'],'status':status,'results':results})
        priorities=['quarantined','conflict','coverage_insufficient','unverifiable','non_independent_support','independent_supported']
        status=next(s for s in priorities if any(row['status']==s for row in rows))
        result={'run_id':run_id,'proposal':r['proposal'],'proposal_sha256':r['digest'],'report_sha256':r['report_sha256'],'status':status,'claims':rows,'cross_fact_ids':r['cross_fact_ids'],'not_cross_reviewed_fact_ids':r['not_cross_reviewed_fact_ids'],'scope':'selected own-review approved claims only; not full report validation','mode':'structured_operator_input_no_model','remote_context_deletion':'not_integrated'}
        if status=='independent_supported':
            with self.store.db() as db:
                check = db.execute('SELECT digest FROM proposals WHERE id=?',(r['proposal'],)).fetchone()
                if not check or check[0]!=r['digest']:raise PermissionError('proposal changed')
                # Persist final evidence BEFORE granting a publishable clearance.
                # Audit failure raises inside this transaction: no grant commits.
                # A crash after audit commit but before this DB commits is safe:
                # evidence may exist without a grant, never the reverse.
                self._log(run_id,None,'finish',{'result_sha256':sha(result),'status':status,
                                               'final_result':result})
                db.execute('INSERT INTO cross_clearances VALUES (?,?,?,?,?,?)',(r['proposal'],r['digest'],r['report_sha256'],sha(result),status,run_id))
        else:
            self._log(run_id,None,'finish',{'result_sha256':sha(result),'status':status,
                                           'final_result':result})
        r['closed']=True
        r['snippets'].clear()
        r['claims'].clear()
        for w in r['workers'].values():w.snapshot.clear()
        return result

    def abort(self, run_id):
        r=self.runs.get(run_id)
        if not r or r['closed']:return {'status':'closed'}
        for w in r['workers'].values():
            w.close();w.snapshot.clear()
        r['snippets'].clear();r['claims'].clear();r['closed']=True
        self._log(run_id,None,'abort',{'revoked':True})
        return {'status':'aborted'}

    def close(self):
        for rid in list(self.runs):self.abort(rid)

    def __enter__(self):return self
    def __exit__(self,*args):self.close()


class Worker:
    """Trusted parent proxy. Expose only call(operation, **args) to model adapters.

    A model receives protocol schemas, never a Python object or this process.
    """
    def __init__(self, coordinator, run_id, company, snapshot, claims, allowed, reviewer_id):
        self.coordinator=coordinator;self.run_id=run_id;self.company_id=company
        self.reviewer_id=reviewer_id
        self.snapshot=snapshot;self.allowed=allowed;self._results={};self.phases={c['claim_id']:'phase1' for c in claims}
        self.session_id=secrets.token_hex(24);self.closed=False;self.quarantined=False
        self._temp=tempfile.TemporaryDirectory(prefix='bankagent-cross-')
        self.snapshot_path=str(Path(self._temp.name)/'snapshot.sqlite')
        with sqlite3.connect(self.snapshot_path) as db:
            db.execute('CREATE TABLE snapshot(payload TEXT)');db.execute('INSERT INTO snapshot VALUES (?)',(encode(snapshot),))
        db.close()
        Path(self.snapshot_path).chmod(0o400)
        script=Path(__file__).with_name('cross_worker.py')
        try:
            self._process=subprocess.Popen([sys.executable,'-I',str(script)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,bufsize=1,cwd=self._temp.name)
        except Exception:
            self.closed=True;self.session_id=None;self._temp.cleanup()
            raise
        self.pid=self._process.pid
        try:
            self._rpc({'operation':'initialize','snapshot_path':self.snapshot_path,'claims':claims,'session_id':self.session_id,'reviewer_id':reviewer_id})
            coordinator._log(run_id,self.session_id,'open',{'company_id':company,'version':snapshot['version'],'snapshot_sha256':snapshot['digest'],'pid':self.pid})
        except Exception:
            self.close()
            raise

    def _rpc(self, value):
        import select
        self._process.stdin.write(json.dumps(value)+'\n');self._process.stdin.flush()
        ready,_,_=select.select([self._process.stdout],[],[],10)
        if not ready:raise RuntimeError('worker protocol timeout')
        line=self._process.stdout.readline()
        if not line:raise RuntimeError('worker exited')
        result=json.loads(line)
        if 'error' in result:raise PermissionError(result['error'])
        return result

    def call(self, operation, **kwargs):
        if self.closed:raise PermissionError('session revoked')
        try:
            permitted={'task':set(),'snapshot':set(),'submit':{'claim_id','decision','evidence_ids'},'unlock':{'claim_id'},'close':set()}
            if operation not in permitted or not set(kwargs)<=permitted[operation] or suspicious(kwargs):
                raise PermissionError('invalid or suspicious tool request')
            if operation=='close':self.close();return {'closed':True}
            if operation in ('submit','unlock'):
                cid=kwargs.get('claim_id')
                if cid not in self.phases:raise PermissionError('claim scope denied')
                phase=self.phases[cid]
                if operation=='unlock':
                    if phase!='unable':raise PermissionError('only unable may unlock')
                    snippets=self.coordinator._run(self.run_id)['snippets'][cid]
                    response=self._rpc({'operation':'unlock','claim_id':cid,'snippets':snippets,'session_id':self.session_id})
                    self.phases[cid]='phase2'
                    self.coordinator._log(self.run_id,self.session_id,'unlock',{'claim_id':cid,'evidence_sha256':sha(snippets)})
                    return response
                decision=kwargs.get('decision');ids=kwargs.get('evidence_ids',[])
                valid=('support','refute','unable') if phase=='phase1' else ('non_independent_support','refute','unverifiable') if phase=='phase2' else ()
                a_ids = {'a:'+eid for c in self.coordinator._run(self.run_id)['claims'] if c['claim_id']==cid for eid in c['evidence_ids']} if phase=='phase2' else set()
                if decision not in valid or not isinstance(ids,list) or any(not isinstance(x,str) for x in ids) or len(set(ids))!=len(ids) or any(x not in self.allowed|a_ids for x in ids):
                    raise PermissionError('decision, phase or actual evidence scope denied')
                if decision in ('support','refute','non_independent_support') and not ids:raise PermissionError('actual evidence IDs required')
                # Must be eligible for THIS claim, not merely another claim.
                candidates=self.coordinator._run(self.run_id)['selection'][cid]['candidates']
                eligible=next(x['evidence_ids'] for x in candidates if x['company_id']==self.company_id)
                if not set(ids)<=set(eligible)|a_ids:raise PermissionError('evidence does not qualify for claim')
                response=self._rpc({'operation':'submit',**kwargs,'session_id':self.session_id})
                self._results[cid]={'decision':decision,'evidence_ids':ids,'independent':False,'independence':'source_only' if phase=='phase2' else 'pending','phase':phase}
                self.phases[cid]='unable' if decision=='unable' else 'final'
                self.coordinator._log(self.run_id,self.session_id,'submit',{'claim_id':cid,'decision':decision,'evidence_ids':ids})
                return response
            return self._rpc({'operation':operation,'session_id':self.session_id})
        except Exception:
            self.quarantined=True
            self.coordinator._run(self.run_id)['quarantined']=True
            try:
                self.coordinator._log(self.run_id,self.session_id,'quarantine',{'operation_sha256':sha(str(operation))})
            finally:
                self.close()
            raise

    def close(self):
        if self.closed:return
        self.closed=True
        try:
            if self._process.poll() is None:
                self._process.terminate()
                try:self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:self._process.kill();self._process.wait(timeout=2)
        finally:
            for stream in (self._process.stdin,self._process.stdout):
                if stream:
                    try:stream.close()
                    except OSError:pass
            self._temp.cleanup()
            session_id=self.session_id
            self.session_id=None
            try:
                self.coordinator._log(self.run_id,session_id,'close',{'token_revoked':True,'process_reaped':self._process.poll() is not None})
            except sqlite3.Error:
                # Audit failure cannot skip cleanup of other processes or permit publication.
                self.quarantined=True
                if self.run_id in self.coordinator.runs:
                    self.coordinator.runs[self.run_id]['quarantined']=True

    def __enter__(self):return self
    def __exit__(self,*args):self.close()

    @property
    def results(self):
        return json.loads(encode(self._results))
