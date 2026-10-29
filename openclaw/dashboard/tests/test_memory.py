import io
import json
import os
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {}, clear=True); self.env.start()
        self.life = patch.object(server, 'LIFE_DIR', self.root); self.life.start()
        self.agent = server.Agent(self.root/'dashboard-agent')
        self.service = patch.object(server, 'agent_service', self.agent); self.service.start()
        self.responses = []
    def tearDown(self):
        for worker in list(self.agent.active.values()): worker.join(3)
        self.service.stop(); self.life.stop(); self.env.stop(); self.tmp.cleanup()
    def handler(self, path, body=None):
        h = object.__new__(server.Handler); h.path = path
        h.headers = {'Host':'panel.test', 'Origin':'http://panel.test', 'Content-Type':'application/json'}
        h.body = lambda: body; h.authorize = lambda: (True, None)
        h.send_json = lambda code, data: self.responses.append((code, data))
        return h
    def add(self, **fields):
        b = dict(name='Проект', text='Любит чай', request='abcdefghijklmnop', source='dashboard'); b.update(fields)
        self.handler('/api/memory/add', b).do_POST()
        return self.responses[-1]
    def page(self):
        return self.root/'memory/Проект.md'
    def audit(self):
        return server.memory_audit()
    def configure(self):
        token = self.root/'token'; token.write_text('fake-token'); token.chmod(0o600)
        os.environ.update(SVODKA_WORKSPACE_ENABLED='1', SVODKA_CANONICAL_SESSION='agent:owner:new-session', SVODKA_AGENT_ENABLED='1', SVODKA_AGENT_URL='http://127.0.0.1:12345/v1/responses', SVODKA_AGENT_TOKEN_FILE=str(token), SVODKA_AGENT_ID='owner', SVODKA_AGENT_TRUSTED_PROXY='1')
    def test_add_verify_retry_and_dedup(self):
        code, row = self.add(); self.assertEqual(code, 200)
        data = self.page().read_bytes()
        self.assertEqual(row['version'], server.memory_version(data)); self.assertTrue(row['verified'])
        self.assertEqual(data.decode().splitlines()[row['line']], '- '+server.datetime.now().strftime('%Y-%m-%d')+' · Любит чай')
        self.assertFalse(row['duplicate'])
        self.assertTrue(self.add()[1]['duplicate']); self.assertEqual(data, self.page().read_bytes())
        self.assertEqual([r['op'] for r in self.audit()], ['add'])
        self.assertTrue(self.add(request='different_request1', text='  ЛЮБИТ   чай  ')[1]['duplicate'])
        self.assertEqual(data, self.page().read_bytes()); self.assertEqual(self.audit()[-1]['op'], 'dedup')
        self.assertNotIn('Любит', json.dumps(self.audit(), ensure_ascii=False))
    def test_reject_and_validation(self):
        for fields in ({'text':'sk-abcdefghijk'}, {'name':'sk-abcdefghijk'}, {'text':'<think>private</think>'}, {'name':'<system>'}, {'text':'Системный промпт'}, {'text':'system prompt'}, {'source':'other'}, {'request':'bad'}, {'request':None}):
            code, row = self.add(**fields); self.assertEqual(code, 400)
            self.assertFalse(self.page().exists()); self.assertFalse(self.audit())
        self.assertEqual(self.add(text='<reasoning>')[1]['error'], server.MEMORY_REJECT)
    def test_delete_history(self):
        _, row = self.add(); raw = self.page().read_text().splitlines()[row['line']]
        self.handler('/api/memory/delete', dict(name='Проект', line=row['line'], raw=raw)).do_POST()
        self.assertEqual(self.responses[-1][0], 200)
        history = (self.root/'dashboard-agent/memory-removed.md').read_text()
        self.assertIn(raw, history); self.assertIn('Проект', history)
        self.assertNotIn(raw, self.page().read_text()); self.assertEqual(self.audit()[-1]['op'], 'delete')
    def test_external_append_between_adds(self):
        self.add()
        with self.page().open('a') as f: f.write('- 2026-10-29 · Факт агента\n')
        self.add(text='Другой факт', request='different_request1')
        text = self.page().read_text()
        for fact in ('Любит чай', 'Факт агента', 'Другой факт'): self.assertIn(fact, text)
    def test_status_and_unprotected_probe(self):
        self.add()
        self.handler('/api/memory/status').do_GET(); code, row = self.responses[-1]
        self.assertEqual(code, 200); self.assertEqual(row['store']['pages'], 1); self.assertEqual(row['store']['facts'], 1)
        self.assertEqual(len(row['store']['version']), 16); self.assertIsInstance(row['store']['writable'], bool)
        self.assertEqual(row['search']['mode'], 'keyword'); self.assertFalse(row['search']['semantic'])
        self.assertEqual(len(row['contract']), 4); self.assertEqual(row['audit']['last']['op'], 'add')
        with patch.object(server._workspace_module.Workspace, 'gateway_invoke') as invoke:
            self.handler('/api/memory/status?probe=1').do_GET()
            self.assertFalse(self.responses[-1][1]['tools']['probe']); invoke.assert_not_called()
    def test_probe_discards_gateway_content(self):
        self.configure(); seen = []
        def transport(req, timeout):
            body = json.loads(req.data); seen.append(body)
            return io.BytesIO(json.dumps({'ok':True, 'result':{'details':{'private':'NEVER-RETURN-CONTENT'}}}).encode())
        with patch('urllib.request.build_opener', return_value=SimpleNamespace(open=transport)):
            self.handler('/api/memory/status?probe=1').do_GET()
        row = self.responses[-1][1]
        self.assertTrue(row['tools']['probe']); self.assertTrue(row['tools']['memory_get']['ok'])
        self.assertEqual([r['tool'] for r in seen], ['memory_get', 'sessions_history'])
        self.assertEqual(seen[0]['args'], {'path':'MEMORY.md', 'from':1, 'lines':1})
        self.assertEqual(seen[1]['args']['limit'], 1)
        self.assertNotIn('NEVER-RETURN', json.dumps(row)); self.assertNotIn('fake-token', json.dumps(row))
        with patch.object(server._workspace_module.Workspace, 'gateway_invoke', side_effect=ValueError('NEVER-RETURN-CONTENT')):
            self.handler('/api/memory/status?probe=1').do_GET()
        self.assertFalse(self.responses[-1][1]['tools']['memory_get']['ok'])
        self.assertNotIn('NEVER-RETURN', json.dumps(self.responses[-1]))
    def test_MOCK_cross_session_direct_file_read_and_send_retry(self):
        # MOCK: proves the dashboard/file/session contract, not real agent retrieval.
        self.add(); self.configure(); seen = []; harmless_tools = []
        def transport(req, timeout):
            self.assertEqual(server.urlsplit(req.full_url).path, '/v1/responses')
            self.assertEqual(req.get_header('X-openclaw-session-key'), 'agent:owner:new-session')
            seen.append(json.loads(req.data))
            fact = self.page().read_text().split(' · ')[-1].strip()
            harmless_tools.append('read_memory_page')
            response = io.BytesIO(('data: '+json.dumps({'type':'response.output_text.delta', 'delta':fact})+'\n\ndata: {"type":"response.completed"}\n\n').encode())
            response.headers = Message(); response.headers['Content-Type'] = 'text/event-stream'
            return response
        with patch('urllib.request.build_opener', return_value=SimpleNamespace(open=transport)):
            body = dict(text='Что известно о проекте?', request='new_session_request1')
            self.handler('/api/workspace/send', body).do_POST()
            self.assertEqual(self.responses[-1][0], 202)
            for worker in list(self.agent.active.values()): worker.join(3)
            self.handler('/api/workspace/send', body).do_POST()
            self.assertTrue(self.responses[-1][1]['duplicate'])
        self.assertEqual(len(seen), 1); self.assertEqual(harmless_tools, ['read_memory_page'])
        self.assertIn('Любит чай', self.agent.snapshot()['runs'][0]['output'])


if __name__ == '__main__': unittest.main()
