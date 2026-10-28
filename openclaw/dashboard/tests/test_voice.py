import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server

class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        binary=self.root/'fake';self.side=self.root/'seen'
        binary.write_text('''#!/usr/bin/env python3
import sys, os, json, time
from pathlib import Path
f=Path(sys.argv[sys.argv.index('--file')+1])
Path(os.environ['VOICE_SEEN']).write_text(json.dumps(dict(argv=sys.argv[1:],path=str(f),exists=f.is_file(),mode=f.stat().st_mode & 0o777,dirmode=f.parent.stat().st_mode & 0o777)))
if os.environ.get('VOICE_SLEEP'): time.sleep(2)
if os.environ.get('VOICE_ERROR'): print(os.environ['VOICE_ERROR']+' '+str(f),file=sys.stderr)
print(os.environ.get('VOICE_JSON','{}'))
''');binary.chmod(0o700)
        self.env=patch.dict(os.environ,dict(SVODKA_VOICE_ENABLED='1',SVODKA_AGENT_TRUSTED_PROXY='1',SVODKA_OPENCLAW_BIN=str(binary),VOICE_SEEN=str(self.side),VOICE_JSON=json.dumps(dict(ok=True,outputs=[dict(text='Привет')],provider='fake',model='test'))),clear=True);self.env.start()
        self.token=patch.object(server,'TOKEN_FILE',self.root/'missing');self.token.start()
        self.h=object.__new__(server.Handler);self.h.path='/api/voice/transcribe'
        self.h.headers={'Host':'panel.test','Origin':'https://panel.test','X-Svodka-Voice':'1','Content-Type':'audio/webm;codecs=opus','Content-Length':'1024'}
        self.sent=[];self.h.send_json=lambda code,data:self.sent.append((code,data))
    def tearDown(self):
        self.token.stop();self.env.stop();self.tmp.cleanup()
    def post(self):
        self.h.rfile=io.BytesIO(b'a'*1024);self.h.do_POST();return self.sent[-1]
    def test_disabled_unprotected(self):
        os.environ['SVODKA_VOICE_ENABLED']='0';self.h.path='/api/voice';self.h.do_GET();self.assertFalse(self.sent[-1][1]['stt'])
        self.h.path='/api/voice/transcribe';self.assertEqual(self.post()[0],503)
        os.environ['SVODKA_VOICE_ENABLED']='1';os.environ.pop('SVODKA_AGENT_TRUSTED_PROXY');self.assertEqual(self.post()[0],503)
    def test_status_and_binary_missing(self):
        self.h.path='/api/voice';self.h.do_GET()
        self.assertTrue(self.sent[-1][1]['stt']);self.assertEqual(self.sent[-1][1]['max_seconds'],60)
        os.environ['SVODKA_OPENCLAW_BIN']='/missing'
        with patch.object(server.shutil,'which',return_value=None),patch.object(server.Path,'home',return_value=self.root):
            self.h.do_GET();self.assertFalse(self.sent[-1][1]['stt'])
            self.h.path='/api/voice/transcribe';self.assertEqual(self.post()[0],503)
    def test_truncated_body_and_malformed_output(self):
        self.h.rfile=io.BytesIO(b'a');self.h.do_POST();self.assertEqual(self.sent[-1][0],400)
        for output in ['not json', '[]', '{"ok":true,"outputs":[null]}', '{"ok":true,"outputs":[{"text":" "}]}']:
            os.environ['VOICE_JSON']=output;self.assertEqual(self.post()[0],502)
    def test_checks(self):
        for key,value,code in [('Origin',None,403),('Origin','https://evil.test',403),('X-Svodka-Voice',None,403),('Content-Type','text/plain',415),('Content-Length','10',413),('Content-Length',str(server.VOICE_MAX_BYTES+1),413),('Content-Length','bad',400)]:
            old=self.h.headers.copy()
            if value is None:self.h.headers.pop(key)
            else:self.h.headers[key]=value
            self.assertEqual(self.post()[0],code);self.h.headers=old
        self.assertFalse(self.side.exists())
    def test_success(self):
        self.assertEqual(self.post(),(200,dict(text='Привет',provider='fake',model='test')))
        seen=json.loads(self.side.read_text());self.assertTrue(seen['exists']);self.assertEqual(seen['mode'],0o600);self.assertEqual(seen['dirmode'],0o700)
        self.assertEqual(seen['argv'][:3],['infer','audio','transcribe']);self.assertEqual(seen['argv'][-3:],['--language','ru','--json'])
        self.assertFalse(Path(seen['path']).parent.exists())
    def test_errors(self):
        os.environ['VOICE_JSON']='{}';os.environ['VOICE_ERROR']='No audio transcription provider';self.assertIn('не настроен',self.post()[1]['error'])
        os.environ['VOICE_ERROR']='gsk_ABCDEFGHIJKLMNOP Bearer supersecret'
        code,data=self.post();self.assertEqual(code,502)
        for secret in ['ABCDEFGHIJKLMNOP','supersecret','svodka-voice-']:self.assertNotIn(secret,data['error'])
        self.assertIn('<аудио>',data['error'])
    def test_timeout_busy_start(self):
        os.environ['VOICE_SLEEP']='1'
        with patch.object(server,'VOICE_TIMEOUT',0.1):self.assertEqual(self.post()[0],504)
        self.assertFalse(Path(json.loads(self.side.read_text())['path']).exists())
        server.voice_semaphore.acquire()
        try:self.assertEqual(self.post()[0],429)
        finally:server.voice_semaphore.release()
        with patch.object(server.subprocess,'run',side_effect=OSError()):self.assertEqual(self.post()[0],503)
        self.assertTrue(server.voice_semaphore.acquire(blocking=False));server.voice_semaphore.release()
    def test_auth(self):
        token=self.root/'token';token.write_text('x'*30)
        with patch.object(server,'TOKEN_FILE',token):self.assertEqual(self.post()[0],401)
    def test_dedupe(self):
        a=server.Agent(self.root/'agent');self.h.path='/api/agent/send';self.h.headers['Content-Type']='application/json';self.h.body=lambda:dict(text='hello',request='0123456789abcdef')
        with patch.object(server,'agent_service',a),patch.object(a,'config',return_value=()),patch.object(server._agent_module.threading,'Thread') as worker:
            self.h.do_POST();code,first=self.sent[-1];self.assertEqual(code,202)
            self.h.do_POST();code,second=self.sent[-1];self.assertEqual(code,200);self.assertEqual(first['id'],second['id']);self.assertTrue(second['duplicate']);self.assertEqual(worker.call_count,1)
            self.h.body=lambda:dict(text='hello',request='bad');self.h.do_POST();self.assertEqual(self.sent[-1][0],400)
            a.update(first['id'],'completed','ok');self.h.body=lambda:dict(text='old');self.h.do_POST();self.assertEqual(self.sent[-1][0],202)
        self.assertNotIn('request',a.snapshot()['runs'][0])
