import sys
import os
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from agent import Agent, Unavailable, queue_snapshot

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {}, clear=True); self.env.start()
    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()
    def config(self, a):
        f=self.root/'secret'; f.write_text('test-operator-secret'); f.chmod(0o600)
        os.environ.update(SVODKA_AGENT_ENABLED='1', SVODKA_AGENT_URL='http://127.0.0.1:18789/v1/responses',SVODKA_AGENT_ID='main',SVODKA_AGENT_USER='svodka-test',SVODKA_AGENT_TOKEN_FILE=str(f))
    def test_disabled_and_routing(self):
        a=Agent(self.root); self.assertFalse(a.snapshot()['available'])
        with self.assertRaises(Unavailable): a.submit('hello')
        self.config(a)
        for bad in ['https://example.com/v1/responses','http://localhost/v1/responses','http://127.0.0.1/tools/invoke','http://127.0.0.1/v1/responses?session=x']:
            os.environ['SVODKA_AGENT_URL']=bad
            with self.assertRaises(Unavailable): a.config()
    def test_private_token(self):
        a=Agent(self.root); self.config(a); (self.root/'secret').chmod(0o644)
        with self.assertRaises(Unavailable): a.config()
    def test_models_sanitized(self):
        f=self.root/'models'; f.write_text(json.dumps({'identity':'SECRET','models':[{'slug':'gpt-6-astra','display_name':'Astra','visibility':'list','secret':'SECRET'},{'slug':'hidden','visibility':'hide'}]}))
        os.environ['SVODKA_CODEX_MODELS_FILE']=str(f)
        os.environ['SVODKA_CLAUDE_MODELS']=''
        with patch.object(server,'codex_bin',return_value='/test'):
            m=server.listed_models()
        self.assertEqual(m[0]['id'],'chatgpt:gpt-6-astra'); self.assertFalse(m[0]['verified_runnable']);self.assertNotIn('SECRET',json.dumps(m));self.assertEqual(len(m),1)
    def test_configured_claude_and_pause(self):
        config = {'secret':'SECRET', 'agents':{'defaults':{'models':{'anthropic/claude-opus-5-5':{'secret':'SECRET'}, 'openai/other':{}}}}}
        with patch.object(server, 'read_json', return_value=config), patch.object(server, 'claude_bin', return_value='/test'), patch.object(server, 'claude_state', return_value={'exhausted':False}):
            rows=server.listed_models()
            self.assertEqual([r['id'] for r in rows], ['claude:claude-opus-5-5', 'claude:claude-sonnet-5-5', 'claude:claude-haiku-5-5'])
            self.assertFalse(rows[0]['verified_runnable'])
            self.assertNotIn('SECRET',json.dumps(rows))
            os.environ['SVODKA_CLAUDE_MODELS']='claude-other'
            self.assertEqual(server.listed_models()[0]['id'],'claude:claude-other')
        os.environ['SVODKA_CLAUDE_MODELS']=''
        with patch.object(server, 'read_json', return_value={'models':[{'slug':'test','visibility':'list'}]}), patch.object(server,'codex_bin',return_value='/test'), patch.dict(server.chatgpt_pause, until=server.time.time()+60):
            self.assertFalse(server.listed_models()[0]['available'])

    def test_claude_exact_cli_slug_and_quota(self):
        proc=MagicMock()
        import io
        proc.stdout=io.StringIO('{"type":"result","result":"ok"}\n')
        proc.returncode=0
        with patch.object(server,'claude_bin',return_value='/test/claude'), patch.object(server.subprocess,'Popen',return_value=proc) as popen, patch.object(server.threading,'Timer'):
            answer=server.call_process('claude', [], '', {'stop':threading.Event()}, lambda text:None, 'claude-opus-5-5')
            args=popen.call_args.args[0]
            self.assertIn('--model=claude-opus-5-5',args)
            self.assertEqual(answer,('ok', None))
        os.environ['SVODKA_CLAUDE_MODELS']='claude-opus-5-5'
        with patch.object(server,'claude_bin',return_value='/test'), patch.object(server,'claude_state',return_value={'exhausted':True}):
            self.assertFalse(next(m for m in server.listed_models() if m['id'].startswith('claude:'))['available'])

    def test_explicit_models_caps_auto_and_no_probe(self):
        cache=self.root/'models.json'
        cache.write_text(json.dumps({'models':[{'slug':'gpt-6-astra','display_name':'GPT 6 Astra','visibility':'list','input_modalities':['text','image']},{'slug':'text-only','visibility':'list','input_modalities':['text']},{'slug':'hidden','visibility':'hide'}]}))
        os.environ['SVODKA_CODEX_MODELS_FILE']=str(cache)
        with patch.object(server,'LIFE_DIR',self.root),patch.object(server,'claude_bin',return_value='/fake/claude'),patch.object(server,'codex_bin',return_value='/fake/codex'),patch.object(server,'claude_state',return_value={'exhausted':False}),patch.object(server,'free_key',return_value=None),patch.object(server,'call_process') as call:
            payload=server.models_payload();rows=payload['models']
            self.assertEqual([r['title'] for r in rows[:3]],['Claude Opus 5.5','Claude Sonnet 5.5','Claude Haiku 5.5'])
            self.assertNotIn('claude',[r['id'] for r in rows]);self.assertNotIn('chatgpt',[r['id'] for r in rows]);self.assertEqual(payload['auto']['label'],'Авто → Claude Opus 5.5')
            self.assertEqual([r['caps']['image'] for r in rows],[True,True,True,True,False,False]);self.assertTrue(all(r['caps']['pdf'] and r['caps']['text'] for r in rows));self.assertTrue(all(r['probe']['status']=='unknown' for r in rows));call.assert_not_called()
            with patch.object(server,'claude_bin',return_value=None):self.assertEqual(server.models_payload()['auto']['id'],'chatgpt:gpt-6-astra')
            self.assertEqual(server.model_pool('chatgpt')[0]['id'],'chatgpt:gpt-6-astra')

    def test_unified_chat_ui_with_node(self):
        import shutil, subprocess
        if not shutil.which('node'):self.skipTest('node unavailable')
        subprocess.run(['node',str(Path(server.__file__).parent/'tests/ai_ui.cjs')],check=True)

    def test_malformed_models_payload(self):
        for data in ([], 'bad', 42, {'models':None}, {'models':[None, [], {'slug':[]}]}):
            with self.subTest(data=data), patch.object(server,'read_json',return_value=data), patch.object(server,'free_key',return_value=None):
                self.assertIsInstance(server.models_payload()['models'],list)

    def test_agent_handler_fail_closed(self):
        import io
        a=Agent(self.root/'state');self.config(a)
        h=object.__new__(server.Handler);h.path='/api/agent/send';h.headers={'Content-Type':'application/json'}
        h.body=lambda: {'text':'hello'}
        sent=[];h.send_json=lambda code,data:sent.append((code,data))
        with patch.object(server,'TOKEN_FILE',self.root/'missing'), patch.object(server,'agent_service',a), patch('agent.threading.Thread'):
            h.do_POST()
            self.assertEqual(sent[-1][0],503)
            self.assertEqual(a.snapshot()['runs'],[])
            self.assertFalse(h.agent_snapshot()['available'])
            os.environ['SVODKA_AGENT_TRUSTED_PROXY']='1'
            h.do_POST()
            self.assertEqual(sent[-1][0],202)

    def test_queue_allowlist(self):
        f=self.root/'tasks';f.write_text(json.dumps({'tasks':[{'id':1,'status':'running','created':'today','text':'SECRET','session':'SECRET','result':'SECRET'}]}))
        q=queue_snapshot(f);self.assertTrue(q['available']);self.assertNotIn('SECRET',json.dumps(q));self.assertFalse(queue_snapshot(None)['available'])
    def test_sse_persistence_and_error(self):
        a=Agent(self.root);self.config(a)
        seen=[]
        class Mock(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                self.wfile.write(b'event: response.output_text.delta\ndata: {"delta":"hello"}\n\ndata: {"type":"response.completed"}\n\ndata: [DONE]\n\n')
        try: http=ThreadingHTTPServer(('127.0.0.1',0),Mock)
        except PermissionError: self.skipTest('Sandbox prohibits local sockets')
        t=threading.Thread(target=http.serve_forever,daemon=True);t.start()
        os.environ['SVODKA_AGENT_URL']=f'http://127.0.0.1:{http.server_port}/v1/responses'
        try:
            # Run synchronously for deterministic assertion, with real HTTP SSE.
            with patch('agent.threading.Thread'):
                rid=a.submit('hello')
            a.work(rid,'hello',a.config())
            r=Agent(self.root).snapshot()['runs'][0]
            self.assertEqual(r['status'],'completed');self.assertEqual(r['output'],'hello');self.assertEqual(seen[0]['user'],'svodka-test');self.assertEqual(seen[0]['model'],'openclaw/main');self.assertNotIn('secret',json.dumps(r))
            with patch('agent.urllib.request.OpenerDirector.open', side_effect=RuntimeError('test-operator-secret')):
                a.work(rid,'hello',a.config())
            self.assertNotIn('test-operator-secret',json.dumps(a.snapshot()));self.assertEqual(a.snapshot()['runs'][0]['status'],'failed')
        finally:http.shutdown();http.server_close()
    def test_mocked_sse_and_restart(self):
        import io
        from email.message import Message
        a=Agent(self.root);self.config(a)
        with patch('agent.threading.Thread'): rid=a.submit('hello')
        response=io.BytesIO(b'event: response.output_text.delta\ndata: {"delta":"hello"}\n\ndata: {"type":"response.completed"}\n\ndata: [DONE]\n\n')
        response.headers=Message();response.headers['Content-Type']='text/event-stream'
        with patch('agent.urllib.request.OpenerDirector.open',return_value=response) as call:
            a.work(rid,'hello',a.config())
        request=call.call_args.args[0]
        self.assertEqual(json.loads(request.data)['user'],'svodka-test')
        self.assertEqual(a.snapshot()['runs'][0]['output'],'hello')
        self.assertTrue(any(e['status']=='running' and e['output_chars']==5 for e in a.snapshot()['events']))
        self.assertEqual(Agent(self.root).snapshot()['runs'][0]['status'],'completed')
        with patch('agent.urllib.request.OpenerDirector.open',side_effect=RuntimeError('test-operator-secret')):
            a.work(rid,'hello',a.config())
        self.assertEqual(a.snapshot()['runs'][0]['status'],'failed')
        self.assertNotIn('test-operator-secret',json.dumps(a.snapshot()))
        with patch('agent.threading.Thread'): rid=a.submit('second')
        self.assertEqual(Agent(self.root).snapshot()['runs'][-1]['status'],'interrupted')

    def test_auth_without_socket(self):
        token=self.root/'access';token.write_text('x'*30)
        h=object.__new__(server.Handler);h.path='/api/agent';h.headers={}
        with patch.object(server,'TOKEN_FILE',token):
            self.assertEqual(h.authorize(),(False,None))
            h.headers={'Cookie':'svodka='+server.cookie_value('x'*30)}
            self.assertEqual(h.authorize(),(True,None))
            h.path='/?k='+'x'*30;h.headers={}
            self.assertEqual(h.authorize(),(True,server.cookie_value('x'*30)))

    def test_incomplete_and_missing_terminal(self):
        import io
        from email.message import Message
        a=Agent(self.root);self.config(a)
        for payload, expected in [(b'data: {"type":"response.incomplete"}\n\n','incomplete'),(b'data: [DONE]\n\n','failed'),(b'data: {"type":"response.failed","error":"test-operator-secret"}\n\n','failed')]:
            with patch('agent.threading.Thread'): rid=a.submit('test')
            response=io.BytesIO(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'+payload);response.headers=Message();response.headers['Content-Type']='text/event-stream'
            with patch('agent.urllib.request.OpenerDirector.open',return_value=response):a.work(rid,'test',a.config())
            saved=Agent(self.root).snapshot()
            self.assertEqual(saved['runs'][-1]['status'],expected)
            self.assertEqual(saved['runs'][-1]['output'],'partial')
            self.assertTrue(any(e['run']==rid and e['status']==expected for e in saved['events']))
            self.assertNotIn('test-operator-secret',json.dumps(a.snapshot()))

    def test_auth_office_and_reject_override(self):
        token=self.root/'access';token.write_text('x'*30)
        a=Agent(self.root/'state')
        with patch.object(server,'TOKEN_FILE',token),patch.object(server,'agent_service',a):
            try: http=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
            except PermissionError: self.skipTest('Sandbox prohibits local sockets')
            threading.Thread(target=http.serve_forever,daemon=True).start()
            base=f'http://127.0.0.1:{http.server_port}'
            try:
                with self.assertRaises(urllib.error.HTTPError) as cm:urllib.request.urlopen(base+'/api/office')
                self.assertEqual(cm.exception.code,401)
                headers={'Cookie':'svodka='+server.cookie_value('x'*30),'Content-Type':'application/json'}
                req=urllib.request.Request(base+'/api/office',headers=headers)
                self.assertEqual(json.load(urllib.request.urlopen(req))['runs'],[])
                req=urllib.request.Request(base+'/api/agent/send',headers=headers,data=b'{"text":"hi","session":"main"}')
                with self.assertRaises(urllib.error.HTTPError) as cm:urllib.request.urlopen(req)
                self.assertEqual(cm.exception.code,400)
            finally:http.shutdown();http.server_close()

    def test_agent_origin_and_content_type_before_body(self):
        h=object.__new__(server.Handler); h.path='/api/agent/send'
        sent=[]; h.send_json=lambda code,data:sent.append((code,data))
        h.body=MagicMock(return_value={'text':'hello'})
        with patch.object(h,'authorize',return_value=(True,None)), patch.object(h,'agent_protected',return_value=True), patch.object(server.agent_service,'submit',return_value=1) as submit:
            for headers, code in [({},415),({'Content-Type':'text/plain'},415),({'Content-Type':'application/json','Host':'panel.test','Origin':'https://evil.test'},403),({'Content-Type':'application/json','Host':'panel.test','Origin':'null'},403),({'Content-Type':'application/json','Host':'panel.test','Origin':'https://panel.test:444'},403),({'Content-Type':'application/json','Host':'panel.test','Origin':'https://panel.test/path'},403)]:
                h.headers=headers; h.do_POST(); self.assertEqual(sent[-1][0],code)
            h.body.assert_not_called(); submit.assert_not_called()
            for origin in (None,'https://panel.test','https://panel.test:443'):
                h.headers={'Content-Type':'application/json; charset=utf-8','Host':'panel.test'}
                if origin is not None:h.headers['Origin']=origin
                h.do_POST(); self.assertEqual(sent[-1][0],202)

    def test_strict_json_body(self):
        import io
        h=object.__new__(server.Handler)
        for payload in (b'{"text":"x","text":"y"}',b'{"text":NaN}',b'{"text":Infinity}'):
            h.headers={'Content-Length':str(len(payload))};h.rfile=io.BytesIO(payload)
            with self.assertRaises(ValueError):h.body()
        h.headers={'Content-Length':'-1'};h.rfile=MagicMock()
        with self.assertRaises(ValueError):h.body()
        h.rfile.read.assert_not_called()

    def test_snapshot_bounds_and_connections_closed(self):
        import sqlite3
        a=Agent(self.root)
        with a.lock,a.db() as c:
            for i in range(25):
                c.execute("INSERT INTO runs(status,input,output,error,started,updated) VALUES('completed',?,?, '',0,0)",('в'*20000,'я'*70000))
        with self.assertRaises(sqlite3.ProgrammingError):c.execute('SELECT 1')
        rows=a.snapshot()['runs'];self.assertEqual(len(rows),20);self.assertEqual(rows[0]['id'],6)
        self.assertTrue(all(r['truncated'] and len(r['output'].encode())<=65536 for r in rows))
        with self.assertRaises(RuntimeError):
            with a.db() as c:raise RuntimeError()
        with self.assertRaises(sqlite3.ProgrammingError):c.execute('SELECT 1')

    def run_mock_stream(self,a,payload):
        import io
        from email.message import Message
        with patch('agent.threading.Thread'):rid=a.submit('test')
        response=io.BytesIO(payload);response.headers=Message();response.headers['Content-Type']='text/event-stream'
        with patch('agent.urllib.request.OpenerDirector.open',return_value=response):a.work(rid,'test',a.config())
        return rid

    def test_sse_bounds_tail_and_throttle(self):
        import agent
        a=Agent(self.root);self.config(a)
        payload=b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'*1000+b'data: {"type":"response.completed"}'
        with patch.object(a,'update',wraps=a.update) as update,patch('agent.time.monotonic',return_value=0):
            self.run_mock_stream(a,payload)
        self.assertLessEqual(update.call_count,3)
        self.assertEqual(a.snapshot()['runs'][-1]['output'],'x'*1000)
        self.assertEqual(a.snapshot()['runs'][-1]['status'],'completed')
        for payload in (b'x'*(agent.LINE_LIMIT+1), (b'data: '+b'x'*60000+b'\n')*20):
            self.run_mock_stream(a,payload)
            self.assertEqual(a.snapshot()['runs'][-1]['status'],'failed')

    def test_sse_total_deadline_and_worker_recovery(self):
        import agent
        a=Agent(self.root);self.config(a)
        ticks=iter(range(10000))
        with patch('agent.TOTAL_TIMEOUT',3),patch('agent.time.monotonic',side_effect=lambda:next(ticks)):
            self.run_mock_stream(a,b': keepalive\n'*100)
        self.assertEqual(a.snapshot()['runs'][-1]['status'],'failed')
        with patch.object(a,'update',side_effect=RuntimeError('disk full')):
            rid=self.run_mock_stream(a,b'data: {"type":"response.completed"}\n\n')
        self.assertNotIn(rid,a.active)
        self.assertEqual(a.snapshot()['runs'][-1]['status'],'interrupted')
        with patch('agent.threading.Thread'):self.assertGreater(a.submit('next'),rid)

    def test_deadline_interrupts_blocking_readline(self):
        import agent
        import types
        from email.message import Message
        a=Agent(self.root);self.config(a)
        with patch('agent.threading.Thread'):rid=a.submit('test')
        expired=threading.Event()
        sock=MagicMock();sock.shutdown.side_effect=lambda how:expired.set()
        class Response:
            headers=Message()
            fp=types.SimpleNamespace(raw=types.SimpleNamespace(_sock=sock))
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def readline(self,limit):
                self.limit=limit
                if not expired.wait(2):raise AssertionError('deadline did not interrupt read')
                return b''
        response=Response();response.headers['Content-Type']='text/event-stream'
        with patch('agent.TOTAL_TIMEOUT',0.03),patch('agent.urllib.request.OpenerDirector.open',return_value=response):
            a.work(rid,'test',a.config())
        sock.shutdown.assert_called_once()
        self.assertEqual(response.limit,agent.LINE_LIMIT+1)
        self.assertEqual(a.snapshot()['runs'][-1]['status'],'failed')

    def test_cli_flag_injection(self):
        for provider in ('claude','chatgpt'):
            for model in ('--help','-x','a b','a\nb'):
                with patch.object(server.subprocess,'Popen') as popen:
                    with self.assertRaises(server.AIError):server.call_process(provider,[],'',{},lambda text:None,model)
                    popen.assert_not_called()
        with patch.object(server,'read_json',return_value={'models':[{'slug':'--help','visibility':'list'}]}),patch.dict(os.environ,SVODKA_CLAUDE_MODELS=''):
            self.assertEqual(server.listed_models(),[])

    def test_layout_logic_with_node(self):
        import shutil, subprocess
        if not shutil.which('node'):self.skipTest('node unavailable')
        html=(Path(server.__file__).parent/'index.html').read_text()
        setup=html[html.index('let agentSeparate=false;'):html.index('agentForm.onsubmit=')]
        show=html[html.index('function showPage()'):html.index("addEventListener('hashchange', showPage)")]
        script="""
const assert=require('assert');
const nodes=new Map();function $(id){if(!nodes.has(id))nodes.set(id,{hidden:false,append(){},setAttribute(){},removeAttribute(){},classList:{add(){},remove(){}}});return nodes.get(id);}
const agentPanel={hidden:false}; const card={hidden:false};
const document={querySelector:()=>card,body:{dataset:{}}};
const localStorage={getItem:()=>null,setItem(){}};
const location={search:'',hash:'#agent'};
const PAGES=['life','work','graph','ai','agents','agent'];let current='',leaveTimer;
const RM={matches:true};function renderBanner(){} function refreshAgent(){} function aiShown(){}
"""+setup+show+"""
showPage();assert.equal(current,'ai');assert.equal(agentPanel.hidden,false);assert.equal(card.hidden,true);
agentSeparate=true;setAgentLayout();$('mode-chat').onclick();assert.equal(agentPanel.hidden,false);
$('mode-agent').onclick();assert.equal(location.hash,'agent');assert.equal(agentPanel.hidden,false);
$('agent-layout-toggle').onclick();assert.equal(location.hash,'ai');
"""
        subprocess.run(['node','-e',script],check=True,capture_output=True)

if __name__=='__main__':unittest.main()
