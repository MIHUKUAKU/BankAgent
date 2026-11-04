"""Minimal adapter to official Codex PreToolUse. Desktop retains chat ownership.
A contact-route guardrail, not an OS boundary or business publication approval.
"""
import hashlib,json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def main():
    event=json.load(sys.stdin)
    if event.get('hook_event_name')!='PreToolUse':return
    name=event.get('tool_name','')
    if not name.endswith('send_message_to_thread'):return
    sender=event.get('session_id');args=event.get('tool_input',{})
    recipient=args.get('threadId')
    policy=json.loads((ROOT/'routes.json').read_text())
    # Unknown/non-project chats are outside this project hook's scope.
    if sender not in policy['roles']:return
    allowed=recipient in policy['routes'].get(sender,[])
    detail=dict(time=time.time(),sender=sender,recipient=recipient,tool=name,tool_use_id=event.get('tool_use_id'),allowed=allowed,prompt_sha256=hashlib.sha256(str(args.get('prompt','')).encode()).hexdigest(),scope='automatic_contact_route_only')
    with (ROOT/'events.jsonl').open('a') as f:f.write(json.dumps(detail,ensure_ascii=False)+'\n')
    if not allowed:
        print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'deny','permissionDecisionReason':'BankAgent 自动联系路线不允许此收件人。执行员联系本企固定审核员；审核员退原执行员或完成通知总部。用户直接在任何原聊天输入不受本钩子限制。'}},ensure_ascii=False))
    # Empty successful output preserves normal host authorization, never bypasses it.
if __name__=='__main__':
    try:main()
    except Exception:
        # Explicit protocol denial for adapter errors; the host's own timeout/crash
        # fail-open behavior remains documented and is not claimed solved.
        print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'deny','permissionDecisionReason':'BankAgent route hook could not load its project policy.'}}))
