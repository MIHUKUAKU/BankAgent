"""BankAgent V0.1: local trusted orchestrator with scoped agent capabilities."""
import hashlib
import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DOMAINS = ('finance', 'products', 'enterprises', 'engineering')
WRITES = {'controller': {'engineering'}, 'enterprise': {'enterprises'},
          'product': {'finance', 'products'}, 'reviewer': set(), 'bootstrap': set(DOMAINS)}
READS = {'controller': set(DOMAINS), 'reviewer': set(DOMAINS),
         'bootstrap': set(DOMAINS), 'enterprise': {'finance','products','enterprises'},
         'product': {'finance','products'}}

def now():
    return datetime.now(timezone.utc).isoformat()

def encode(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)

class Conflict(ValueError):
    pass

class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._sessions = {}
        self._identities = {}
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO settings VALUES ('bootstrap','active');
            CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, role TEXT NOT NULL, object_id TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, task TEXT NOT NULL, author TEXT NOT NULL, domain TEXT NOT NULL, object_id TEXT NOT NULL, base INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reviews (proposal TEXT PRIMARY KEY, decision TEXT NOT NULL, notes TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS versions (domain TEXT NOT NULL, object_id TEXT NOT NULL, version INTEGER NOT NULL, proposal TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(domain,object_id,version));
            CREATE TABLE IF NOT EXISTS enterprise_publication_ledger (proposal TEXT PRIMARY KEY, company_id TEXT NOT NULL, version INTEGER NOT NULL, reviewer_id TEXT NOT NULL, digest TEXT NOT NULL, review_notes TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, detail TEXT NOT NULL, created TEXT NOT NULL);
            ''')
            for table in ('proposals','reviews','versions','events','enterprise_publication_ledger'):
                for action in ('UPDATE','DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable record'); END")
            for domain in DOMAINS:
                db.execute(f'CREATE TABLE IF NOT EXISTS rag_{domain} (object_id TEXT PRIMARY KEY, version INTEGER NOT NULL, content TEXT NOT NULL)')
            from .cross_review import initialize_governance
            initialize_governance(db)
            from .retrieval import initialize_index
            initialize_index(db)

    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        return db

    def session(self, role, company_id=None, identity=None):
        # Trusted host only. Do not expose session creation or SQLite to an LLM.
        if role not in WRITES:
            raise ValueError('unknown role')
        if role == 'enterprise' and not company_id:
            raise ValueError('enterprise session requires company_id')
        token = secrets.token_hex(24)
        self._sessions[token] = role
        if not hasattr(self, '_identities'):
            self._identities = {}
        self._identities[token] = identity
        return Agent(self, token, company_id)

    def role(self, token):
        if token not in self._sessions:
            raise PermissionError('invalid capability')
        role = self._sessions[token]
        with self.db() as db:
            if role == 'bootstrap' and db.execute("SELECT value FROM settings WHERE key='bootstrap'").fetchone()[0] != 'active':
                raise PermissionError('bootstrap retired')
        return role

    @staticmethod
    def event(db, kind, detail):
        db.execute('INSERT INTO events(kind,detail,created) VALUES (?,?,?)', (kind,encode(detail),now()))

class Agent:
    def __init__(self, store, token, company_id=None):
        self.store, self.token = store, token
        self.company_id = company_id

    def check_company(self, domain, object_id):
        if self.store.role(self.token) == "enterprise" and domain == "enterprises" and object_id != self.company_id:
            raise PermissionError("cross-company access denied")

    def require(self, role):
        if self.store.role(self.token) != role:
            raise PermissionError(role + ' capability required')

    def assign(self, role, object_id):
        self.require('controller')
        if role not in WRITES or role in ('controller','reviewer'):
            raise ValueError('invalid executor')
        task = secrets.token_hex(8)
        with self.store.db() as db:
            if role == 'bootstrap' and db.execute("SELECT value FROM settings WHERE key='bootstrap'").fetchone()[0] != 'active':
                raise PermissionError('bootstrap retired')
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?)',(task,role,object_id,'active',now()))
            self.store.event(db,'task.assigned',{'task':task,'role':role,'object_id':object_id})
        return task

    def propose(self, task, domain, object_id, payload, base=0):
        role = self.store.role(self.token)
        self.check_company(domain, object_id)
        if domain not in WRITES[role]:
            raise PermissionError('domain write denied')
        if not re.fullmatch(r'[a-z0-9_-]+',object_id):
            raise ValueError('invalid object id')
        if not payload.get('content') or not payload.get('title') or not payload.get('sources'):
            raise ValueError('content, title and evidence required')
        for source in payload['sources']:
            if not all(source.get(k) for k in ('url','title','publisher','accessed_at','locator')) or not source['url'].startswith('https://'):
                raise ValueError('incomplete evidence')
        body = encode(payload)
        pid = secrets.token_hex(8)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
            if not row or row['role'] != role or row['status'] != 'active' or row['object_id'] not in (object_id,'*'):
                raise PermissionError('task scope denied')
            from .cross_review import check_author
            identity = getattr(self.store, '_identities', {}).get(self.token)
            check_author(db, domain, object_id, identity)
            db.execute('INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?)',(pid,task,role,domain,object_id,base,body,hashlib.sha256(body.encode()).hexdigest()))
            db.execute('INSERT INTO cross_authors VALUES (?,?)', (pid, identity))
            self.store.event(db,'proposal.created',{'proposal':pid})
        return pid

    def review(self, proposal, approve, notes, report_sha256=None, cross_fact_ids=None):
        self.require('reviewer')
        if not notes.strip():
            raise ValueError('review rationale required')
        with self.store.db() as db:
            row = db.execute('SELECT * FROM proposals WHERE id=?',(proposal,)).fetchone()
            if not row:
                raise ValueError('unknown proposal')
            from .cross_review import bind_review
            bind_review(db, row, getattr(self.store, '_identities', {}).get(self.token), report_sha256, approve, cross_fact_ids)
            db.execute('INSERT INTO reviews VALUES (?,?,?,?)',(proposal,'approved' if approve else 'rejected',notes,now()))
            self.store.event(db,'review.recorded',{'proposal':proposal,'approved':approve})

    def publish(self, proposal):
        role = self.store.role(self.token)
        if role not in ('controller', 'reviewer'):
            raise PermissionError('controller or fixed enterprise reviewer required')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            p = db.execute('SELECT * FROM proposals WHERE id=?',(proposal,)).fetchone()
            r = db.execute('SELECT * FROM reviews WHERE proposal=?',(proposal,)).fetchone()
            if not p or not r or r['decision'] != 'approved':
                raise PermissionError('approved review required')
            identity = self.store._identities.get(self.token)
            if role == 'reviewer':
                reg = db.execute('SELECT * FROM cross_registry WHERE company_id=?', (p['object_id'],)).fetchone()
                if (p['domain'] != 'enterprises' or not reg or not identity
                        or identity != reg['reviewer_id'] or self.company_id != p['object_id']):
                    raise PermissionError('publication limited to bound own company reviewer')
            from .cross_review import check_publish
            check_publish(db, p)
            task = db.execute('SELECT status FROM tasks WHERE id=?',(p['task'],)).fetchone()
            if task[0] != 'active':
                raise PermissionError('task closed')
            if p['author'] == 'bootstrap' and db.execute("SELECT value FROM settings WHERE key='bootstrap'").fetchone()[0] != 'active':
                raise PermissionError('bootstrap retired')
            current = db.execute('SELECT COALESCE(MAX(version),0) FROM versions WHERE domain=? AND object_id=?',(p['domain'],p['object_id'])).fetchone()[0]
            if current != p['base']:
                raise Conflict('base version changed; rebase and re-review required')
            version = current + 1
            db.execute('INSERT INTO versions VALUES (?,?,?,?,?,?,?)',(p['domain'],p['object_id'],version,proposal,p['payload'],p['digest'],now()))
            db.execute(f"INSERT OR REPLACE INTO rag_{p['domain']} VALUES (?,?,?)",(p['object_id'],version,p['payload']))
            from .retrieval import index_payload
            index_payload(db, p['domain'], p['object_id'], version, json.loads(p['payload']))
            if role == 'reviewer':
                db.execute('INSERT INTO enterprise_publication_ledger VALUES (?,?,?,?,?,?,?)',
                           (proposal, p['object_id'], version, identity, p['digest'], r['notes'], now()))
            self.store.event(db,'version.published',{'proposal':proposal,'version':version})
            return version

    def search(self, domain, query, limit=5, **filters):
        from .retrieval import search
        return search(self, domain, query, limit, **filters)

    def history(self, domain, object_id):
        self.check_company(domain, object_id)
        if domain not in READS[self.store.role(self.token)]:
            raise PermissionError('domain read denied')
        with self.store.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM versions WHERE domain=? AND object_id=? ORDER BY version',(domain,object_id))]

    def close_task(self, task):
        self.require('controller')
        with self.store.db() as db:
            if not db.execute('SELECT 1 FROM tasks WHERE id=?',(task,)).fetchone():
                raise ValueError('unknown task')
            db.execute("UPDATE tasks SET status='closed' WHERE id=?",(task,))
            self.store.event(db,'task.closed',{'task':task})

    def retire_bootstrap(self):
        self.require('controller')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            domains = {r[0] for r in db.execute('SELECT DISTINCT domain FROM versions')}
            if domains != set(DOMAINS):
                raise ValueError('all four domains must be initialized')
            pending = db.execute("SELECT COUNT(*) FROM proposals p LEFT JOIN reviews r ON p.id=r.proposal LEFT JOIN versions v ON v.proposal=p.id WHERE p.author='bootstrap' AND v.proposal IS NULL AND (r.decision IS NULL OR r.decision='approved')").fetchone()[0]
            if pending:
                raise ValueError('unresolved bootstrap proposals')
            db.execute("UPDATE settings SET value='retired' WHERE key='bootstrap'")
            db.execute("UPDATE tasks SET status='closed' WHERE role='bootstrap'")
            self.store.event(db,'bootstrap.retired',{})
