"""Exactly two normal-function checks; no boundary cases or full-suite runner."""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

class ReleaseSmoke(unittest.TestCase):
    def test_demo_and_query(self):
        with tempfile.TemporaryDirectory() as temp:
            db = str(pathlib.Path(temp) / 'demo.sqlite')
            subprocess.run([sys.executable, '-m', 'bankagent', '--db', db, 'demo'], cwd=ROOT, check=True, capture_output=True)
            result = subprocess.run([sys.executable, '-m', 'bankagent', '--db', db, 'kb', 'query', 'finance', '现金流'], cwd=ROOT, check=True, capture_output=True, text=True)
            self.assertTrue(result.stdout.strip())

    def test_normal_hook_route(self):
        with tempfile.TemporaryDirectory() as temp:
            target = pathlib.Path(temp)
            shutil.copy(ROOT / 'integrations/codex-hook/route_hook.py', target)
            shutil.copy(ROOT / 'integrations/codex-hook/routes.example.json', target / 'routes.json')
            event = {'hook_event_name': 'PreToolUse', 'tool_name': 'mcp__codex_app__send_message_to_thread', 'session_id': 'MARKET_CHAT_ID', 'tool_input': {'threadId': 'CONTROLLER_CHAT_ID', 'prompt': 'fixture normal completion'}, 'tool_use_id': 'fixture-normal'}
            subprocess.run([sys.executable, str(target / 'route_hook.py')], input=json.dumps(event), text=True, check=True, capture_output=True)
            self.assertTrue(json.loads((target / 'events.jsonl').read_text())['allowed'])

if __name__ == '__main__':
    unittest.main()
