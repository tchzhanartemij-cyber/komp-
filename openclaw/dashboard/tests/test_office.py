"""Office tests use synthetic data, temporary files and mocked transports only."""
import copy
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from agent import Agent, queue_snapshot
from office import LOG_LIMIT, coder_metadata, model_name, office_sessions, parse_coder_log, redact


def message(kind, *items):
    return json.dumps({'type': kind, 'message': {'content': list(items)}}).encode() + b'\n'


def tool(name, tid='t1', **inputs):
    return message('assistant', {'type': 'tool_use', 'id': tid, 'name': name, 'input': inputs})


def result(tid='t1'):
    return message('user', {'type': 'tool_result', 'tool_use_id': tid, 'content': 'private result'})


class OfficeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_agent_mapping_is_pure_and_uses_latest_event(self):
        agent = {'runs': [{'id': 12, 'status': 'running', 'input': 'Первая строка\nвторая', 'started': 10, 'updated': 50, 'model': 'openai/gpt-6.1-sol'}],
                 'events': [{'run': 12, 'time': 20, 'output_chars': 0}, {'run': 12, 'time': 50, 'output_chars': 8}, {'run': 9, 'time': 60, 'output_chars': 100}]}
        before = copy.deepcopy(agent)
        row = office_sessions(agent, {}, {})[0]
        self.assertEqual(agent, before)
        self.assertEqual((row['key'], row['label'], row['provider']), ('agent:12', 'Сводка #12', 'Сводка-агент'))
        self.assertEqual((row['title'], row['activity'], row['activity_at']), ('Первая строка', 'answering', 50))
        self.assertEqual(row['model'], 'openai/gpt-6.1-sol')
        agent['events'].append({'run': 12, 'time': 51, 'output_chars': 0})
        self.assertEqual(office_sessions(agent, {}, {})[0]['activity'], 'waiting_model')
        agent['runs'][0]['status'] = 'completed'
        agent['runs'][0].pop('model')
        row = office_sessions(agent, {}, {})[0]
        self.assertIsNone(row['activity'])
        self.assertIsNone(row['model'])

    def test_tool_categories_and_latest_activity(self):
        categories = {'Edit': 'code', 'Write': 'code', 'MultiEdit': 'code', 'NotebookEdit': 'code',
                      'Read': 'search_code', 'Grep': 'search_code', 'Glob': 'search_code',
                      'WebSearch': 'web', 'WebFetch': 'web', 'Bash': 'command', 'Task': 'subagent', 'Agent': 'subagent', 'Other': 'tool'}
        for name, activity in categories.items():
            with self.subTest(name=name):
                data = tool('Edit') + tool(name, 'last') + result('last')
                parsed = parse_coder_log(data, 120)
                self.assertEqual(parsed['activity'], activity)
                self.assertEqual(parsed['activity_at'], 120)
        for kind in ('text', 'thinking'):
            parsed = parse_coder_log(tool('Read') + message('assistant', {'type': kind, kind: 'private reasoning'}), 121)
            self.assertEqual(parsed['activity'], 'thinking')
            self.assertNotIn('private', json.dumps(parsed))
        self.assertIsNone(parse_coder_log(message('assistant', {'type': 'text', 'text': ''}), 120)['activity'])

    def test_activity_detail_is_file_name_or_program_only(self):
        cases = [(tool('Edit', file_path='/home/private/repo/index.html'), 'index.html'),
                 (tool('Read', file_path='/srv/x/server.py'), 'server.py'),
                 (tool('Bash', command='cd /private/repo && FLAG=secret python3 -m unittest'), 'python3'),
                 (tool('Bash', command='/usr/bin/git status'), 'git'),
                 (tool('Edit', file_path='/x/' + 'A9_' * 12), None),
                 (tool('Grep', pattern='private'), None),
                 (tool('Bash', command='codex "unterminated'), None),
                 (tool('Edit', file_path='/x/index.html') + message('assistant', {'type': 'text', 'text': 'private'}), None)]
        for data, detail in cases:
            with self.subTest(detail=detail):
                parsed = parse_coder_log(data, 1)
                self.assertEqual(parsed['detail'], detail)
                self.assertNotIn('private', json.dumps(parsed))
                self.assertNotIn('secret', json.dumps(parsed))
        rows = office_sessions({}, {'jobs': [{'id': 1, 'status': 'running', 'detail': 'index.html'},
                                             {'id': 2, 'status': 'done', 'detail': 'index.html'}]}, {})
        self.assertEqual([r['detail'] for r in rows], ['index.html', None])

    def test_task_title_drops_labels_paths_and_tail(self):
        from office import task_title
        self.assertEqual(task_title('ЗАДАЧА: превратить вкладку «Офис» в живой офис. Дальше длинное описание'),
                         'Превратить вкладку «Офис» в живой офис')
        self.assertEqual(task_title('В дашборде «Сводка» (openclaw/dashboard) добавить кнопку'),
                         'В дашборде «Сводка» добавить кнопку')
        long = task_title('Сделать ' + 'очень ' * 20)
        self.assertTrue(len(long) <= 60 and long.endswith('…'))

    def test_pending_tool_and_local_coder_time(self):
        self.assertTrue(parse_coder_log(tool('Bash', 'a', command='npm test'), 1)['tool_pending'])
        self.assertFalse(parse_coder_log(tool('Bash', 'a', command='npm test') + result('a'), 1)['tool_pending'])
        from datetime import datetime
        from office import timestamp
        # coder.py writes naive local "%Y-%m-%d %H:%M" stamps.
        self.assertEqual(timestamp('2026-10-28 18:05'), datetime(2026, 10, 28, 18, 5).timestamp())

    def test_codex_child_only_until_matching_result(self):
        for command in ('codex exec -m gpt-6.1-sol task', 'cd /private/repo && codex exec --model=gpt-6-astra task',
                        'env FLAG=private codex -m openai/gpt-6.1-sol exec task', 'FLAG=private codex exec task'):
            with self.subTest(command=command):
                parsed = parse_coder_log(tool('Bash', command=command) + result('other'), 200)
                self.assertIsInstance(parsed['codex'], dict)
                self.assertNotIn('private', json.dumps(parsed))
                self.assertIsNone(parse_coder_log(tool('Bash', command=command) + result(), 201)['codex'])
        for command in ('echo codex -m fake', 'cat file && codex exec', 'codexx exec', 'codex "unterminated'):
            self.assertIsNone(parse_coder_log(tool('Bash', command=command), 200)['codex'])
        parsed = parse_coder_log(tool('Bash', command='codex exec --model=bad?model'), 200)
        self.assertEqual(parsed['codex'], {'model': None})
        parsed = parse_coder_log(tool('Bash', 'one', command='codex -m first') + tool('Bash', 'two', command='codex -m second') + result('two'), 200)
        self.assertEqual(parsed['codex']['model'], 'first')

    def test_coder_codex_chat_sessions(self):
        jobs = [{'id': 8, 'status': 'running', 'created': '2026-10-28T00:00:00Z', 'updated': 100,
                 'title': 'Исправить поиск', **parse_coder_log(tool('Bash', command='codex exec -m gpt-6.1-sol task'), 200)}]
        chats = {'raw-private-run-id': {'provider': 'ChatGPT', 'started': 10, 'updated': 220, 'phase': 'streaming', 'text': 'private message'}}
        rows = office_sessions({}, {'jobs': jobs}, chats)
        coder, codex, chat = rows
        self.assertEqual((coder['key'], coder['provider'], coder['activity'], coder['updated']), ('coder:8', 'Claude Code', 'command', 200))
        self.assertEqual((codex['key'], codex['parent'], codex['model'], codex['activity']), ('coder:8:codex', 'coder:8', 'gpt-6.1-sol', 'code'))
        self.assertRegex(chat['key'], r'^chat:[a-f0-9]{12}$')
        self.assertEqual((chat['title'], chat['label'], chat['activity']), ('Диалог в ИИ', 'Чат · ChatGPT', 'answering'))
        self.assertNotIn('raw-private', json.dumps(rows))
        self.assertNotIn('private message', json.dumps(rows))
        self.assertEqual(rows, office_sessions({}, {'jobs': jobs}, chats))
        jobs[0]['updated'] = 300
        self.assertEqual(office_sessions({}, {'jobs': jobs}, {})[0]['updated'], 300)
        self.assertEqual(office_sessions({}, {'jobs': jobs}, {})[0]['activity_at'], 200)
        jobs[0]['status'] = 'question'
        rows = office_sessions({}, {'jobs': jobs}, {})
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['activity'])
        chats['raw-private-run-id']['phase'] = 'waiting'
        self.assertEqual(office_sessions({}, {}, chats)[0]['activity'], 'waiting_model')

    def test_logs_are_bounded_newest_readonly_and_optional(self):
        row = {'id': 8, 'status': 'running', 'text': 'Название\nвторая строка'}
        missing = coder_metadata(row, self.root)
        self.assertIsNone(missing['activity'])
        self.assertIsNone(missing['activity_at'])
        old = self.root / '8-old.json'
        old.write_bytes(tool('Edit'))
        os.utime(old, (100, 100))
        new = self.root / '8-new.json'
        data = tool('Bash', command='codex -m old-model') + b'x' * (LOG_LIMIT * 2) + b'\n' + tool('Grep')
        new.write_bytes(data)
        os.utime(new, (200, 200))
        details = coder_metadata(row, self.root)
        self.assertEqual((details['title'], details['activity'], details['activity_at']), ('Название', 'search_code', 200))
        self.assertIsNone(details['codex'])
        self.assertEqual(new.read_bytes(), data)
        # No content read for non-running jobs; unreadable/malformed logs are honest unknowns.
        with patch.object(Path, 'open', side_effect=PermissionError):
            self.assertIsNone(coder_metadata(row, self.root)['activity'])
            self.assertIsNone(coder_metadata({**row, 'status': 'done'}, self.root)['activity'])
        new.write_bytes(b'bad\n[]\nnull\n{"type":"assistant","message":null}\n' + message('assistant', [], {'type': 'tool_use', 'name': []}))
        self.assertEqual(coder_metadata(row, self.root)['activity'], 'tool')
        new.write_bytes(b'x' * (LOG_LIMIT * 2))
        self.assertIsNone(coder_metadata(row, self.root)['activity'])
        new.unlink()
        new.symlink_to(old)
        self.assertEqual(coder_metadata(row, self.root)['activity'], 'code')
        self.assertIsNone(coder_metadata({**row, 'id': '../8'}, self.root)['activity'])

    def test_queue_timeout_requires_explicit_status(self):
        filename = self.root / 'tasks.json'
        filename.write_text(json.dumps({'tasks': [{'id': 1, 'status': 'timeout'}, {'id': 2, 'status': 'unrecognized'}, {'id': 3, 'status': 'running', 'error': 'timeout'}]}))
        rows = office_sessions({}, queue_snapshot(filename), {})
        self.assertEqual([r['status'] for r in rows], ['timeout', 'unknown', 'running'])
        self.assertIsNone(rows[2]['activity'])

    def test_redaction_before_truncation(self):
        secret = 'A9_' * 12
        text = '  Проверить https://private.example/a?secret=yes ' + secret + ' sk-abc TOKEN123 key=value www.example.test  '
        safe = redact(text)
        for raw in ('https://', 'private.example', secret, 'sk-', 'TOKEN123', 'key=', 'www.example'):
            self.assertNotIn(raw, safe)
        self.assertIn('[ссылка]', safe)
        self.assertIn('[скрыто]', safe)
        self.assertNotRegex(safe, r'\s{2}')
        rows = office_sessions({'runs': [{'id': 1, 'status': 'running', 'input': 'Задача ' + secret + '\nскрытая вторая строка'}]},
                               {'jobs': [{'id': 8, 'status': 'queued', 'text': text}]}, {})
        self.assertTrue(all(len(row['title']) <= 60 for row in rows))
        self.assertNotIn(secret, json.dumps(rows))
        self.assertNotIn('вторая', json.dumps(rows, ensure_ascii=False))
        for model in (None, [], 42, 'a b', 'x' * 101, 'x\ny'):
            self.assertIsNone(model_name(model))
        self.assertEqual(model_name('vendor/model-1.0:test'), 'vendor/model-1.0:test')

    def test_office_json_auth_redaction_and_legacy_keys_without_sockets(self):
        access = 'synthetic-access-' + 'a' * 30
        operator = 'synthetic-operator-' + 'b' * 30
        token = self.root / 'access'
        token.write_text(access)
        gateway = self.root / 'operator'
        gateway.write_text(operator)
        gateway.chmod(0o600)
        os.environ.update(SVODKA_AGENT_ENABLED='1', SVODKA_AGENT_URL='http://127.0.0.1:18789/v1/responses',
                          SVODKA_AGENT_ID='main', SVODKA_AGENT_USER='svodka-test', SVODKA_AGENT_TOKEN_FILE=str(gateway))
        a = Agent(self.root / 'state')
        with a.db() as db:
            db.execute("INSERT INTO runs(status,input,output,error,started,updated) VALUES('completed',?,?,?,10,20)", ('Задача ' + operator, operator, operator))
        queue = self.root / 'tasks.json'
        queue.write_text(json.dumps({'tasks': [{'id': 8, 'text': 'Проверить https://private.example ' + access, 'status': 'running', 'updated': 20, 'session': operator, 'result': operator}]}))
        logs = self.root / 'custom-logs'
        logs.mkdir()
        (logs / '8-run.json').write_bytes(tool('Bash', command='codex exec -m gpt-6.1-sol ' + operator + ' /private/repo') + result('unrelated'))
        os.environ.update(SVODKA_CODER_QUEUE_FILE=str(queue), SVODKA_CODER_LOGS_DIR=str(logs))
        h = object.__new__(server.Handler)
        h.path = '/api/office'
        h.headers = {}
        sent = []
        h.send_json = lambda code, payload: sent.append((code, payload))
        with patch.object(server, 'TOKEN_FILE', token), patch.object(server, 'agent_service', a), patch.object(a, 'submit') as submit:
            h.do_GET()
            self.assertEqual(sent[-1][0], 401)
            h.headers = {'Cookie': 'svodka=' + server.cookie_value(access)}
            h.do_GET()
            code, payload = sent[-1]
            self.assertEqual(code, 200)
            self.assertEqual(set(payload), {'server_time', 'agent', 'sessions', 'queue', 'runs', 'events'})
            self.assertEqual(set(payload['agent']), {'available', 'reason'})
            self.assertEqual(len(payload['sessions']), 3)
            self.assertIn('jobs', payload['queue'])
            public = json.dumps(payload, ensure_ascii=False)
            for raw in (access, operator, 'private.example', '/private/repo', 'tool_use', 'private result'):
                self.assertNotIn(raw, public)
            h.path = '/api/agent/send'
            h.headers.update({'Content-Type': 'application/json', 'Host': 'panel.test', 'Origin': 'https://evil.test'})
            h.body = MagicMock(side_effect=AssertionError('must reject before body'))
            h.do_POST()
            self.assertEqual(sent[-1][0], 403)
            h.body.assert_not_called()
            submit.assert_not_called()

    def test_sse_model_migration_and_request_unchanged(self):
        a = Agent(self.root)
        with sqlite3.connect(self.root / 'agent.sqlite3') as db:
            db.execute('CREATE TABLE runs (id INTEGER PRIMARY KEY, status TEXT, input TEXT, output TEXT, error TEXT, started REAL, updated REAL)')
        config = ('http://127.0.0.1:18789/v1/responses', 'synthetic-transport', 'main', 'svodka-test')
        for event in ('response.created', 'response.in_progress', 'response.completed'):
            with self.subTest(event=event), patch.object(a, 'config', return_value=config), patch('agent.threading.Thread'):
                rid = a.submit('hello')
            payload = [dict(type=event, response={'model': 'openai/gpt-6.1-sol'})]
            if event != 'response.completed':
                payload += [dict(type='response.output_text.delta', delta='hello'), dict(type='response.completed', response={'model': 'invalid model'})]
            response = io.BytesIO(b''.join(b'data: ' + json.dumps(obj).encode() + b'\n\n' for obj in payload))
            response.headers = Message()
            response.headers['Content-Type'] = 'text/event-stream'
            with patch('agent.urllib.request.OpenerDirector.open', return_value=response) as transport:
                a.work(rid, 'hello', config)
            row = a.snapshot()['runs'][-1]
            self.assertEqual((row['status'], row['model']), ('completed', 'openai/gpt-6.1-sol'))
            req = transport.call_args.args[0]
            self.assertEqual(json.loads(req.data), {'model': 'openclaw/main', 'user': 'svodka-test', 'input': 'hello', 'stream': True})
            self.assertEqual(req.headers, {'Authorization': 'Bearer synthetic-transport', 'Content-type': 'application/json', 'Accept': 'text/event-stream'})
        # The live Gateway echoes the routing target "openclaw/main"; that is not model provenance.
        for model in (None, 42, [], 'a b', 'x' * 101, 'openclaw/main', 'openclaw'):
            with patch.object(a, 'config', return_value=config), patch('agent.threading.Thread'):
                rid = a.submit('hello')
            response = io.BytesIO(('data: ' + json.dumps({'type': 'response.completed', 'response': {'model': model}}) + '\n\n').encode())
            response.headers = Message()
            response.headers['Content-Type'] = 'text/event-stream'
            with patch('agent.urllib.request.OpenerDirector.open', return_value=response):
                a.work(rid, 'hello', config)
            self.assertIsNone(a.snapshot()['runs'][-1]['model'])

    def test_active_chat_metadata_during_real_send_flow(self):
        seen = []
        def process(provider, messages, system, run, delta, model):
            self.assertEqual(run['provider'], 'Claude')
            self.assertEqual(run['phase'], 'waiting')
            self.assertIsInstance(run['started'], float)
            seen.append(office_sessions({}, {}, server.ai_runs)[0])
            delta('answer')
            seen.append(office_sessions({}, {}, server.ai_runs)[0])
            self.assertEqual(run['phase'], 'streaming')
            self.assertGreaterEqual(run['updated'], run['started'])
        with patch.object(server, 'LIFE_DIR', self.root), patch.object(server, 'models_payload', return_value={'models': [{'id': 'claude', 'available': True}]}), patch.object(server, 'call_process', side_effect=process), patch.object(server, 'save_chat'), patch.object(server, 'ai_runs', {}):
            server.ai_send(None, 'claude', 'private user message', None, lambda obj: None)
            self.assertEqual(server.ai_runs, {})
        self.assertEqual([s['activity'] for s in seen], ['waiting_model', 'answering'])
        self.assertEqual(seen[0]['key'], seen[1]['key'])
        self.assertNotIn('private user message', json.dumps(seen))


if __name__ == '__main__':
    unittest.main()
