"""Task-local market citation refusal + existing reviewer-2 evidence journal.

No Store or publication methods. The formal database is opened read-only.
Identity strings are trusted local operator bindings, not remote authentication.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit

REPORTERS = {'执行1号', '执行2号', '执行3号', '金融市场研究员'}
REVIEWER = '审核2号'
DECISIONS = {'confirmed', 'not_confirmed', 'insufficient_evidence'}


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


class EvidenceInbox:
    def __init__(self, formal_db, journal_db, evidence_root, *, identity):
        if identity not in REPORTERS | {REVIEWER}:
            raise PermissionError('assigned executor or existing reviewer-2 required')
        self.identity = identity
        self.formal = Path(formal_db).resolve(strict=True)
        self.journal = Path(journal_db)
        if self.journal.is_symlink():
            raise ValueError('journal symlink denied')
        self.journal = self.journal.resolve()
        if self.journal == self.formal or (self.journal.exists() and os.path.samefile(self.formal, self.journal)):
            raise PermissionError('journal must never be the formal DB')
        self.root = Path(evidence_root).resolve(strict=True)
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.journal) as db:
            db.execute('CREATE TABLE IF NOT EXISTS refusal_events(seq INTEGER PRIMARY KEY,issue_id TEXT,event_type TEXT,actor TEXT,body TEXT,digest TEXT,previous_digest TEXT,created TEXT)')
            for op in ('UPDATE', 'DELETE'):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS refusal_immutable_{op} BEFORE {op} ON refusal_events BEGIN SELECT RAISE(ABORT,'immutable evidence journal'); END")

    def target(self, reference):
        if (not isinstance(reference, dict) or set(reference) != {'record_id', 'version'}
                or not isinstance(reference['record_id'], str) or not reference['record_id'].startswith('market_')
                or type(reference['version']) is not int or reference['version'] < 1):
            raise ValueError('exact published market record and positive version required')
        db = sqlite3.connect(self.formal.as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            db.set_authorizer(lambda action,a,b,c,d: sqlite3.SQLITE_OK if action == sqlite3.SQLITE_SELECT or action == sqlite3.SQLITE_READ and a == 'versions' else sqlite3.SQLITE_DENY)
            row = db.execute("SELECT * FROM versions WHERE domain='finance' AND object_id=? AND version=?", (reference['record_id'], reference['version'])).fetchone()
            if not row:
                raise ValueError('published market record version does not exist')
            value = dict(row)
            if _sha(value['payload'].encode()) != value['digest']:
                raise ValueError('formal payload hash mismatch')
            value['payload'] = json.loads(value['payload'])
            return value
        finally:
            db.close()

    def _snapshot(self, evidence):
        fields = ('url', 'locator', 'snapshot_path', 'sha256')
        if not isinstance(evidence, dict) or not all(isinstance(evidence.get(k),str) and evidence[k].strip() for k in fields):
            raise ValueError('URL, locator, snapshot and hash required')
        u = urlsplit(evidence['url'])
        if u.scheme != 'https' or not u.hostname or u.username or u.password:
            raise ValueError('HTTPS evidence URL required')
        relative = Path(evidence['snapshot_path'])
        path = (self.root / relative).resolve(strict=True)
        if relative.is_absolute() or '..' in relative.parts or self.root not in path.parents:
            raise ValueError('evidence outside root')
        cursor = self.root / relative
        while cursor != self.root:
            if cursor.is_symlink():
                raise ValueError('evidence symlink denied')
            cursor = cursor.parent
        raw = path.read_bytes()
        if _sha(raw) != evidence['sha256']:
            raise ValueError('evidence snapshot hash mismatch')
        return raw.decode('utf-8')

    def _append(self, issue_id, event_type, body):
        encoded = _encode(body)
        with sqlite3.connect(self.journal) as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT digest FROM refusal_events ORDER BY seq DESC LIMIT 1').fetchone()
            previous = previous[0] if previous else ''
            created = datetime.now(timezone.utc).isoformat()
            event_hash = _sha(_encode([issue_id,event_type,self.identity,encoded,previous,created]).encode())
            db.execute('INSERT INTO refusal_events(issue_id,event_type,actor,body,digest,previous_digest,created) VALUES (?,?,?,?,?,?,?)', (issue_id,event_type,self.identity,encoded,event_hash,previous,created))
        return {'issue_id': issue_id, 'event_sha256': event_hash, 'event_type': event_type}

    def submit(self, *, target, task_id, problem, comparison=None, evidence=None, missing_sources=False):
        if self.identity not in REPORTERS:
            raise PermissionError('reviewer cannot submit an executor refusal')
        if not isinstance(task_id,str) or not task_id.strip() or not isinstance(problem,str) or not problem.strip():
            raise ValueError('affected own task and concrete problem required')
        record = self.target(target)
        export = _encode(record)
        evidence_hashes = [_sha(export.encode())]
        if missing_sources:
            sources = record['payload'].get('sources', [])
            required = ('source_id','url','locator','snapshot_path','sha256')
            if sources and all(isinstance(s,dict) and all(s.get(k) for k in required) for s in sources):
                raise ValueError('source metadata is present; provide concrete discrepancy evidence')
            evidence = {'kind':'record_export_missing_source_metadata','record_export':record,'sha256':evidence_hashes[0]}
        else:
            text = self._snapshot(evidence)
            if evidence['url'] not in {s.get('url') for s in record['payload'].get('sources',[]) if isinstance(s,dict)}:
                from .market_sources import check_url
                check_url(evidence['url'])
            if (not isinstance(comparison,dict) or set(comparison) != {'field','recorded','expected'}
                    or comparison['field'] not in ('content','value','unit','trading_day','event_at','kind','sources')
                    or comparison['recorded'] != record['payload'].get(comparison['field'])
                    or not isinstance(comparison['expected'],str) or not comparison['expected']
                    or comparison['expected'] not in text or comparison['recorded'] == comparison['expected']):
                raise ValueError('precise recorded field versus actual evidence excerpt required')
            evidence_hashes.append(evidence['sha256'])
        issue_id = secrets.token_hex(16)
        body = {'target':target,'target_digest':record['digest'],'record_export':record,'record_export_sha256':_sha(export.encode()),
                'task_id':task_id,'problem':problem,'comparison':comparison,'evidence':evidence,'evidence_hashes':evidence_hashes,
                'scope':'only this executor task citation of this exact version','status':'reference_refused_pending_review',
                'not_a_truth_determination':True,'review_task':{'owner':REVIEWER,'task_id':'market-evidence-review-'+issue_id,
                'input_refs':{'issue_id':issue_id,'target':target,'target_digest':record['digest'],'record_export_sha256':_sha(export.encode()),'evidence_hashes':evidence_hashes},
                'scope':'review this exact task-local citation discrepancy; no formal RAG write or global freeze',
                'status':'queued','retry_budget':2,
                'expected_output':'append confirmed/not_confirmed/insufficient_evidence with reasons and exact evidence hashes',
                'acceptance':'independent evidence review only; corrections return to normal candidate/review/publish'}}
        event = self._append(issue_id,'submitted',body)
        return dict(event,review_task=body['review_task'],journal_path=str(self.journal))

    def history(self, issue_id):
        with sqlite3.connect(self.journal) as db:
            rows = db.execute('SELECT event_type,actor,body,digest,created FROM refusal_events WHERE issue_id=? ORDER BY seq',(issue_id,)).fetchall()
        return [{'event_type':r[0],'actor':r[1],'body':json.loads(r[2]),'event_sha256':r[3],'created':r[4]} for r in rows]

    def review(self, issue_id, *, submitted_sha256, decision, reason, evidence_hashes):
        if self.identity != REVIEWER:
            raise PermissionError('only assigned existing reviewer-2 may append a review')
        history = self.history(issue_id)
        if not history or history[0]['event_type'] != 'submitted' or history[0]['event_sha256'] != submitted_sha256:
            raise ValueError('exact submitted issue hash required')
        first = history[0]
        if first['actor'] == self.identity:
            raise PermissionError('no self review')
        if decision not in DECISIONS or not isinstance(reason,str) or not reason.strip() or not isinstance(evidence_hashes,list) or not evidence_hashes or not set(evidence_hashes) <= set(first['body']['evidence_hashes']):
            raise ValueError('decision, rationale and actual submitted evidence hashes required')
        record = self.target(first['body']['target'])
        if record['digest'] != first['body']['target_digest']:
            raise ValueError('target changed after submission')
        if first['body']['evidence'].get('kind') != 'record_export_missing_source_metadata':
            self._snapshot(first['body']['evidence'])
        elif _sha(_encode(first['body']['record_export']).encode()) != first['body']['record_export_sha256']:
            raise ValueError('record export hash mismatch')
        return self._append(issue_id,'reviewed',{'submitted_sha256':submitted_sha256,'decision':decision,'reason':reason,
                            'evidence_hashes':evidence_hashes,'next_action':'normal correction workflow if confirmed; no formal RAG mutation'})

    def can_reference(self, target, *, task_id):
        self.target(target)
        with sqlite3.connect(self.journal) as db:
            rows = db.execute("SELECT body FROM refusal_events WHERE event_type='submitted' AND actor=?",(self.identity,)).fetchall()
        return not any((body := json.loads(r[0]))['target'] == target and body['task_id'] == task_id for r in rows)
