import io
import urllib.error
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server
Workspace = server._workspace_module.Workspace

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.seen=[]; self.error=0; self.key='agent:owner:main'
        self.rows=[{'key':self.key,'channel':'telegram','participants':['private'],'delivery':{'secret':'private'}}, {'key':'agent:owner:subagent:one','parentSessionKey':self.key,'label':'worker'}, {'key':'agent:other:main'}, {'key':'agent:owner:telegram:group:42'}]
        self.messages=[{'role':'system','content':'private'}, {'role':'tool','content':'private'}, {'role':'user','timestamp':1700000000000,'__openclaw':{'id':'m1'},'content':[{'type':'text','text':'[Сводка:abcdefghijklmnopqrstuv] hello <think>private</think><relevant-memories>private</relevant-memories> sk-abcdefghijk <|control|><tool_call>private'},{'type':'image','name':'photo','data':'private','url':'https://private'}]}, {'role':'assistant','timestamp':'2023-11-14T22:13:21Z','content':'answer'}]
        outer=self
        class Gateway(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.seen.append((self.path,{k.lower():v for k,v in self.headers.items()},body))
                if outer.error:
                    self.send_response(outer.error);self.end_headers();self.wfile.write(b'Bearer NEVER-RETURN-THIS');return
                self.send_response(200)
                if self.path=='/v1/responses':
                    self.send_header('Content-Type','text/event-stream');self.end_headers();self.wfile.write(b'data: {"type":"response.completed"}\n\n');return
                self.end_headers()
                details={'messages':outer.messages,'hasMore':True,'nextOffset':30,'droppedMessages':1,'contentRedacted':True} if body['tool']=='sessions_history' else {'sessions':outer.rows}
                self.wfile.write(json.dumps({'ok':True,'result':{'details':details}}).encode())
        self.transport = None
        try:
            self.gateway=ThreadingHTTPServer(('127.0.0.1',0),Gateway)
        except PermissionError:
            self.gateway = None
        if self.gateway:
            self.thread=threading.Thread(target=self.gateway.serve_forever,daemon=True);self.thread.start()
            port = self.gateway.server_port
        else:
            # Restricted CI: retain Request/envelope/SSE checks without OS sockets.
            from email.message import Message
            from types import SimpleNamespace
            def open_request(req, timeout):
                body=json.loads(req.data);path=server.urlsplit(req.full_url).path
                self.seen.append((path,{k.lower():v for k,v in req.headers.items()},body))
                if self.error:
                    raise urllib.error.HTTPError(req.full_url,self.error,'hidden',{},io.BytesIO(b'Bearer NEVER-RETURN-THIS'))
                if path=='/v1/responses':
                    response=io.BytesIO(b'data: {"type":"response.completed"}\n\n')
                    response.headers=Message();response.headers['Content-Type']='text/event-stream'
                    return response
                details={'messages':self.messages,'hasMore':True,'nextOffset':30,'droppedMessages':1,'contentRedacted':True} if body['tool']=='sessions_history' else {'sessions':self.rows}
                return io.BytesIO(json.dumps({'ok':True,'result':{'details':details}}).encode())
            self.transport=patch('urllib.request.build_opener',return_value=SimpleNamespace(open=open_request));self.transport.start()
            port=12345
        token=self.root/'token';token.write_text('fake-token');token.chmod(0o600)
        self.env=patch.dict(os.environ,{'SVODKA_WORKSPACE_ENABLED':'1','SVODKA_CANONICAL_SESSION':self.key,'SVODKA_AGENT_ENABLED':'1','SVODKA_AGENT_URL':f'http://127.0.0.1:{port}/v1/responses','SVODKA_AGENT_TOKEN_FILE':str(token),'SVODKA_AGENT_ID':'owner'},clear=True);self.env.start()
        self.agent=server.Agent(self.root/'agent');self.w=Workspace(self.agent)
    def tearDown(self):
        for worker in list(self.agent.active.values()): worker.join(3)
        if self.gateway:
            self.gateway.shutdown();self.gateway.server_close();self.thread.join()
        if self.transport:self.transport.stop()
        self.env.stop();self.tmp.cleanup()
    def test_unicode_secret_redaction(self):
        for pattern in (server.SECRET_RE, server._workspace_module.SECRET_RE):
            self.assertEqual(pattern.sub('***', 'sk-синтетическийключ1234567890'), '***')

    def test_disabled_and_validation(self):
        del os.environ['SVODKA_WORKSPACE_ENABLED']
        self.assertEqual(self.w.status(),{'available':False,'reason':'Переписка отключена: требуется разрешение оператора SVODKA_WORKSPACE_ENABLED=1'})
        os.environ['SVODKA_WORKSPACE_ENABLED']='1';del os.environ['SVODKA_CANONICAL_SESSION']
        self.assertIn('SVODKA_CANONICAL_SESSION',self.w.status()['reason'])
        for key in ('','agent:owner:subagent:a','agent:owner:cron:a','agent:owner:acp:a','bad'):
            os.environ['SVODKA_CANONICAL_SESSION']=key
            with self.assertRaises(server.Unavailable):self.agent.canonical()
    def test_canonical_submit(self):
        self.agent.submit('hello','abcdefghijklmnopqrstuv')
        for worker in list(self.agent.active.values()):worker.join(3)
        path,headers,body=self.seen[0]
        self.assertEqual(headers.get('x-openclaw-session-key'),self.key);self.assertNotIn('user',body)
        self.assertEqual(self.agent.snapshot()['session'],{'canonical':True,'key':self.key})
    def test_history_sessions_cache(self):
        self.messages.append(dict(self.messages[2]))
        h=self.w.history();self.assertEqual(len(h['messages']),2);self.assertEqual(h['hidden_count'],2)
        m=h['messages'][0];self.assertEqual(m['source'],'dashboard');self.assertEqual(m['origin_request'],'abcdefghijklmnopqrstuv')
        self.assertEqual(m['attachments'],[{'kind':'image','name':'photo'}]);self.assertEqual(m['text'],'hello  *** ')
        self.assertNotIn('private',json.dumps(h));self.assertEqual(h['next_offset'],30);self.assertTrue(h['gap']);self.assertTrue(h['redacted'])
        n=len(self.seen);self.w.history();self.assertEqual(len(self.seen),n)
        self.w.history(30);self.assertEqual(self.seen[-2][2]['args']['offset'],30)
        rows=self.w.sessions();self.assertEqual(len(rows),2);self.assertNotIn('participants',rows[0]);self.assertNotIn('delivery',rows[0])
        with self.assertRaises(ValueError):self.w.gateway_invoke('exec',{})
    def test_revision_idempotence(self):
        draft=self.w.create_revision('m1','original','new');self.assertEqual(self.seen,[]);self.assertEqual(draft['status'],'draft')
        a=self.w.send_revision(draft['id']);b=self.w.send_revision(draft['id'])
        self.assertEqual(a['id'],b['id']);self.assertTrue(b['duplicate'])
        for worker in list(self.agent.active.values()):worker.join(3)
        self.assertEqual(len(self.seen),1)
        c=self.w.db()
        try:self.assertEqual(c.execute('SELECT original FROM revisions').fetchone()[0],'original')
        finally:c.close()
        self.assertIn('Исходный запрос:\noriginal\nНовая редакция:\nnew',self.seen[0][2]['input'])
    def test_gateway_error_private(self):
        self.error=401
        with self.assertRaisesRegex(ValueError,'^Шлюз отклонил авторизацию$'):self.w.sessions()
    def test_tasks(self):
        tasks=self.w.tasks({'runs':[{'id':1,'status':'running','updated':time.time()-1000}]},{'jobs':[{'id':2,'status':'question'},{'id':3,'status':'merged'}]},[])
        self.assertEqual([t['status'] for t in tasks],['stale','waiting_approval','completed']);self.assertTrue(all(not t['cancel']['supported'] for t in tasks))
    def test_main_alias_and_child_filter(self):
        os.environ['SVODKA_CANONICAL_SESSION']='main'
        self.rows += [{'key':'main'}, {'key':'agent:owner:subagent:second','spawnedBy':'main'}]
        self.assertEqual({r['key'] for r in self.w.sessions()}, {'main',self.key,'agent:owner:subagent:one','agent:owner:subagent:second'})
    def test_sanitization_ids_and_bounds(self):
        for tag in ('think','thinking','reasoning','relevant-memories','relevant_memories','tool_call','function_call','tool_calls','function_calls'):
            self.assertEqual(self.w.clean('visible<'+tag+'>private</'+tag+'>tail'),'visibletail')
            self.assertEqual(self.w.clean('visible<'+tag+'>private'),'visible')
        self.messages=[{'role':'assistant','timestamp':1000,'content':'x'*9000}, {'role':'user','content':'Bearer fake-secret'}]
        h=self.w.history();self.assertEqual(h['messages'][0]['ts'],1.0)
        self.assertTrue(h['messages'][0]['truncated']);self.assertEqual(len(h['messages'][0]['text']),8000)
        self.assertTrue(h['messages'][0]['synthetic_id']);self.assertEqual(h['messages'][1]['text'],'***')
        again=self.w.history(1);self.assertEqual(h['messages'][0]['id'],again['messages'][0]['id'])
        for offset,limit in [(-1,30),(0,0),(0,51)]:
            with self.assertRaises(ValueError):self.w.history(offset,limit)
    def test_cache_concurrent_tabs(self):
        results=[]
        threads=[threading.Thread(target=lambda:results.append(self.w.history())) for _ in range(5)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(3)
        self.assertEqual(len(results),5);self.assertEqual(len(self.seen),2)
    def test_successful_send_and_revision_routes(self):
        self.responses=[]
        with patch.object(server,'agent_service',self.agent),patch.object(server,'load_token',return_value='panel-token'):
            request='abcdefghijklmnopqrstuv'
            h=self.handler('/api/workspace/send',{'text':'hello','request':request});h.authorize=lambda:(True,None)
            h.do_POST();first=self.responses[-1];self.assertEqual(first[0],202)
            h.do_POST();self.assertEqual(self.responses[-1][0],200);self.assertEqual(first[1]['id'],self.responses[-1][1]['id'])
            for worker in list(self.agent.active.values()):worker.join(3)
            self.assertEqual(self.seen[0][2]['input'],'[Сводка:'+request+'] hello')
            h=self.handler('/api/workspace/revision',{'target':'m1','original':'unchanged','text':'updated'});h.authorize=lambda:(True,None);h.do_POST()
            self.assertEqual(self.responses[-1][0],201);draft=self.responses[-1][1]
            self.assertEqual(len(self.seen),1)
            h=self.handler('/api/workspace/revision/send',{'id':draft['id'],'confirm':True});h.authorize=lambda:(True,None);h.do_POST()
            self.assertEqual(self.responses[-1][0],202);h.do_POST();self.assertEqual(self.responses[-1][0],200)
    def test_unprotected_routes(self):
        self.responses=[]
        with patch.object(server,'load_token',return_value=None):
            for path in ('/api/workspace','/api/workspace/history','/api/workspace/send','/api/workspace/revision','/api/workspace/revision/send'):
                h=self.handler(path);h.workspace_route(path,path.endswith(('send','revision')))
                self.assertEqual(self.responses[-1][0],503)
    def handler(self,path,body=None,origin='http://panel.test',ctype='application/json'):
        h=object.__new__(server.Handler);h.path=path;h.headers={'Host':'panel.test','Origin':origin,'Content-Type':ctype};h.body=lambda:body
        h.send_json=lambda code,data:self.responses.append((code,data));return h
    def test_routes_security(self):
        self.responses=[]
        with patch.object(server,'agent_service',self.agent),patch.object(server,'load_token',return_value='panel-token'):
            for path in ('/api/workspace','/api/workspace/history','/api/workspace/send','/api/workspace/revision','/api/workspace/revision/send'):
                h=self.handler(path)
                if path.endswith(('send','revision')):h.do_POST()
                else:h.do_GET()
                self.assertEqual(self.responses[-1][0],401)
            for path in ('/api/workspace/send','/api/workspace/revision','/api/workspace/revision/send'):
                for origin,ctype,code in [('http://evil.test','application/json',403),('http://panel.test','text/plain',415)]:
                    h=self.handler(path,{},origin,ctype);h.authorize=lambda:(True,None);h.do_POST();self.assertEqual(self.responses[-1][0],code)
            h=self.handler('/api/workspace/revision/send',{'id':1});h.authorize=lambda:(True,None);h.do_POST();self.assertEqual(self.responses[-1][0],400)
            self.error=401;h=self.handler('/api/workspace');h.authorize=lambda:(True,None);h.agent_snapshot=lambda:{'runs':[]};h.do_GET()
            self.assertEqual(self.responses[-1][0],200);self.assertEqual(self.responses[-1][1]['error'],'Шлюз отклонил авторизацию')

if __name__=='__main__':unittest.main()
