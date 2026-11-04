"""Isolated one-shot JSON worker. No imports from BankAgent or formal DB path.

No LLM or arbitrary Python execution API; protocol only. This is NOT an OS
sandbox for a malicious local user, nor remote model context deletion.
"""
import json
import sqlite3
import sys
from pathlib import Path


def main():
    db=None
    snapshot_path=None
    token=None
    phases={}
    claims=[]
    try:
        for line in sys.stdin:
            try:
                if len(line)>2000000:raise ValueError('request too large')
                req=json.loads(line)
                op=req.get('operation')
                if token is None:
                    if op!='initialize' or set(req)!={'operation','snapshot_path','claims','session_id','reviewer_id'}:
                        raise ValueError('initialize once')
                    token=req['session_id'];claims=req['claims'];reviewer_id=req['reviewer_id']
                    phases={c['claim_id']:'phase1' for c in claims}
                    snapshot_path=Path(req['snapshot_path']).resolve()
                    db=sqlite3.connect(snapshot_path.as_uri()+'?mode=ro',uri=True)
                    db.execute('PRAGMA query_only=ON')
                    # Restrict reads to the own-company snapshot table and prevent PRAGMAs/ATTACH/write.
                    def authorizer(action,a,b,c,d):
                        if action==sqlite3.SQLITE_SELECT:return sqlite3.SQLITE_OK
                        if action==sqlite3.SQLITE_READ and a=='snapshot':return sqlite3.SQLITE_OK
                        return sqlite3.SQLITE_DENY
                    db.set_authorizer(authorizer)
                    result={'initialized':True}
                else:
                    if req.get('session_id')!=token:raise ValueError('revoked or invalid token')
                    if op=='snapshot' and set(req)=={'operation','session_id'}:
                        result=json.loads(db.execute('SELECT payload FROM snapshot').fetchone()[0])
                    elif op=='task' and set(req)=={'operation','session_id'}:
                        snapshot=json.loads(db.execute('SELECT payload FROM snapshot').fetchone()[0])
                        result={'role':'reviewer','claims':claims,'phases':phases,'company_id':snapshot['company_id'],'version':snapshot['version'],'reviewer_id':reviewer_id}
                    elif op=='submit' and set(req)<={'operation','session_id','claim_id','decision','evidence_ids'}:
                        cid=req.get('claim_id');phase=phases.get(cid)
                        valid=('support','refute','unable') if phase=='phase1' else ('non_independent_support','refute','unverifiable') if phase=='phase2' else ()
                        if req.get('decision') not in valid:raise ValueError('phase locked')
                        phases[cid]='unable' if req['decision']=='unable' else 'final'
                        result={'accepted':True,'decision':req['decision']}
                    elif op=='unlock' and set(req)=={'operation','session_id','claim_id','snippets'}:
                        cid=req['claim_id']
                        if phases.get(cid)!='unable':raise ValueError('unlock denied')
                        phases[cid]='phase2'
                        result={'claim_id':cid,'snippets':req['snippets'],'independent':False}
                    else:raise ValueError('operation or fields denied')
                print(json.dumps(result),flush=True)
            except Exception as exc:
                print(json.dumps({'error':str(exc)}),flush=True)
                break
    finally:
        token=None
        claims.clear();phases.clear()
        if db:db.close()
        if snapshot_path:
            try:
                snapshot_path.unlink(missing_ok=True)
                snapshot_path.parent.rmdir()
            except OSError:
                pass  # Parent cleanup also runs, including child-kill cases.

if __name__=='__main__':main()
