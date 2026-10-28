"""Managed team and Claude proof regressions, using isolated synthetic fixtures."""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from office import LOG_LIMIT, coder_metadata, parse_coder_log


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'SVODKA_CLAUDE_MODELS': ''}, clear=True)
        self.env.start()
        self.life = patch.object(server, 'LIFE_DIR', self.root)
        self.life.start()

    def tearDown(self):
        self.life.stop()
        self.env.stop()
        self.tmp.cleanup()

    def binary(self, actual='claude-sonnet-4'):
        binary = self.root / 'claude'
        events = [dict(type='system', subtype='init', model=actual), dict(type='result', result='answer')]
        binary.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n' + '\n'.join('print(' + repr(json.dumps(e)) + ')' for e in events) + '\n')
        binary.chmod(0o700)
        return str(binary)

    def test_aliases_mismatch_and_proof(self):
        with patch.object(server, 'claude_bin', return_value=self.binary()), patch.object(server, 'claude_state', return_value={'exhausted': False}), patch.object(server, 'gateway_models', return_value=[]), patch.object(server, 'codex_bin', return_value=None), patch.object(server, 'free_key', return_value=None):
            rows = server.models_payload()['models']
            self.assertEqual([r['id'] for r in rows[:4]], ['claude', 'claude:opus', 'claude:sonnet', 'claude:haiku'])
            for alias in ('opus', 'sonnet', 'haiku'):
                events = []
                server.ai_send('', 'claude:' + alias, 'hello', '', events.append)
                msg = events[-1]['chat']['messages'][-1]
                self.assertEqual(msg['requested'], alias)
                self.assertEqual(msg['actual'], 'claude-sonnet-4')
                self.assertEqual(msg['mismatch'], alias != 'sonnet')
                self.assertIn('запрошено ' + alias + ' · фактически claude-sonnet-4', msg['detail'])
            with self.assertRaises(ValueError):
                server.ai_send('', 'claude:unknown', 'hello', '', lambda e: None)
            proof = json.loads((self.root / 'ai/model-proof.json').read_text())
            self.assertEqual(set(proof['claude:opus']), {'actual', 'at', 'ok', 'error'})
            proof['claude:opus']['secret'] = 'private'
            proof['claude:bad'] = dict(actual='bad model!', at=1, ok=True, error='')
            (self.root / 'ai/model-proof.json').write_text(json.dumps(proof))
            rows = server.models_payload()['models']
            self.assertNotIn('secret', next(r for r in rows if r['id'] == 'claude:opus')['proof'])
            self.assertNotIn('claude:bad', server.model_proofs())

    def test_model_stream_and_head(self):
        data = b'{"type":"system","subtype":"init","model":"claude-opus-4"}\n'
        data += b'{"type":"assistant","message":{"model":"bad model!"}}\n'
        self.assertEqual(parse_coder_log(data, 1)['model'], 'claude-opus-4')
        data += b'{"type":"assistant","message":{"model":"claude-sonnet-4"}}\n'
        self.assertEqual(parse_coder_log(data, 1)['model'], 'claude-sonnet-4')
        (self.root / '1-run.json').write_bytes(data + b'x' * (LOG_LIMIT * 2) + b'\n')
        row = coder_metadata({'id': 1, 'status': 'done'}, self.root)
        self.assertEqual(row['model'], 'claude-sonnet-4')
        self.assertIsNone(row['activity'])
        with patch.object(server, 'claude_bin', return_value=self.binary('bad model!')):
            run = {'stop': threading.Event()}
            server.call_process('claude', [], '', run, lambda text: None)
            self.assertIsNone(run.get('actual_model'))

    def test_team_route_redacts_and_authorizes(self):
        queue = self.root / 'tasks.json'
        queue.write_text(json.dumps({'tasks': [dict(id=1, text='Title sk-secret token=private\nPRIVATE_TEXT', result='PRIVATE_RESULT', session='PRIVATE_SESSION', status='new-state', created=1, updated=2)]}))
        os.environ['SVODKA_CODER_QUEUE_FILE'] = str(queue)
        handler = object.__new__(server.Handler)
        handler.path = '/api/team'
        handler.headers = {}
        handler.wfile = io.BytesIO()
        sent = []
        handler.send_json = lambda code, data: sent.append((code, data))
        with patch.object(server, 'load_token', return_value=None):
            handler.do_GET()
        payload = sent[-1][1]
        self.assertEqual(payload['runs'][0]['outcome'], 'неизвестно')
        for private in ('PRIVATE_TEXT', 'PRIVATE_RESULT', 'PRIVATE_SESSION', 'sk-secret', 'token=private', '"text"', '"result"', '"session"', str(self.root)):
            self.assertNotIn(private, json.dumps(payload))
        handler.deny = lambda: sent.append((401, {}))
        with patch.object(server, 'load_token', return_value='protected'):
            handler.do_GET()
        self.assertEqual(sent[-1][0], 401)

    def test_codex_history_for_finished_run(self):
        events = [dict(type='assistant', message=dict(content=[dict(type='tool_use', name='Bash', id=str(i), input=dict(command='codex exec -m ' + model))])) for i, model in enumerate(('gpt-b', 'gpt-a', 'gpt-b'))]
        (self.root / '1-run.json').write_text('\n'.join(json.dumps(e) for e in events))
        row = coder_metadata(dict(id=1, status='done'), self.root)
        self.assertEqual(row['codex_runs'], 3)
        self.assertEqual(row['codex_models'], ['gpt-a', 'gpt-b'])
        self.assertIsNone(row['codex'])
        self.assertFalse(row['tool_pending'])
