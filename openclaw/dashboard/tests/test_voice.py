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
        self.agent=patch.object(server,'agent_service',server.Agent(self.root/'agent'));self.agent.start()
        server.client_log_times.clear()
        self.h=object.__new__(server.Handler);self.h.path='/api/voice/transcribe'
        self.h.headers={'Host':'panel.test','Origin':'https://panel.test','X-Svodka-Voice':'1','Content-Type':'audio/webm;codecs=opus','Content-Length':'1024'}
        self.sent=[];self.h.send_json=lambda code,data:self.sent.append((code,data))
    def tearDown(self):
        self.agent.stop();self.token.stop();self.env.stop();self.tmp.cleanup()
    def post(self):
        self.h.rfile=io.BytesIO(b'a'*1024);self.h.do_POST();return self.sent[-1]
    def test_disabled_unprotected(self):
        os.environ['SVODKA_VOICE_ENABLED']='0';self.h.path='/api/voice';self.h.do_GET();self.assertFalse(self.sent[-1][1]['stt'])
        self.h.path='/api/voice/transcribe';self.assertEqual(self.post()[0],503)
        os.environ['SVODKA_VOICE_ENABLED']='1';os.environ.pop('SVODKA_AGENT_TRUSTED_PROXY');self.assertEqual(self.post()[0],503)
    def test_status_and_binary_missing(self):
        self.h.path='/api/voice';self.h.do_GET()
        self.assertTrue(self.sent[-1][1]['stt']);self.assertEqual(self.sent[-1][1]['max_seconds'],60)
        status=self.sent[-1][1];self.assertTrue(status['vad']);self.assertFalse(status['realtime'])
        self.assertEqual(status['max_utterance_seconds'],30);self.assertEqual(status['session_max_minutes'],15)
        self.assertIn('realtime',status['realtime_reason'])
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
    def test_webm_is_converted_to_wav(self):
        import shutil, subprocess
        ffmpeg=shutil.which('ffmpeg')
        if not ffmpeg:self.skipTest('ffmpeg unavailable')
        webm=self.root/'tone.webm'
        subprocess.run([ffmpeg,'-loglevel','error','-f','lavfi','-i','sine=frequency=440:duration=1','-c:a','libopus',str(webm)],check=True)
        data=webm.read_bytes();self.h.headers['Content-Length']=str(len(data));self.h.rfile=io.BytesIO(data);self.h.do_POST()
        self.assertEqual(self.sent[-1][0],200)
        self.assertTrue(json.loads(self.side.read_text())['path'].endswith('audio.wav'))
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

    def diagnostic(self,route,body,raw=None):
        raw=json.dumps(body,ensure_ascii=False).encode() if raw is None else raw
        self.h.path=route;self.h.headers.update({'Content-Type':'application/json','Content-Length':str(len(raw))});self.h.rfile=io.BytesIO(raw)
        self.h.do_POST();return self.sent[-1]
    def test_metrics_validation(self):
        valid=dict(mode='voice',**{k:None for k in server.VOICE_STAGES});valid['t_speech_end']=0;valid['t_done']=600000
        self.assertEqual(self.diagnostic('/api/voice/metrics',valid)[0],200)
        for body in [None,[],{},dict(mode='bad'),dict(mode='text',text='private'),dict(mode='text',t_done=True),dict(mode='text',t_done=-1),dict(mode='text',t_done=600001),dict(mode='text',t_done='2'),dict(mode='text',t_done=float('nan')),dict(mode='text',t_done=float('inf'))]:
            with self.subTest(body=body):self.assertEqual(self.diagnostic('/api/voice/metrics',body)[0],400)
        self.assertEqual(self.diagnostic('/api/voice/metrics',{},raw=b'{"mode":"text","mode":"voice"}')[0],400)
        self.assertEqual(self.diagnostic('/api/voice/metrics',dict(mode='text',t_done=10**400))[0],400)
    def test_metrics_percentiles_and_cap(self):
        empty=server.voice_metrics();self.assertEqual(empty['count'],0);self.assertEqual(empty['stages']['t_done'],dict(n=0,p50=None,p95=None))
        for i in range(1,21):server.voice_metrics(dict(mode='text',t_done=i,t_first_audio=None))
        self.h.path='/api/voice/metrics';self.h.do_GET();report=self.sent[-1][1]
        self.assertEqual(report['count'],20);self.assertEqual(report['stages']['t_done'],dict(n=20,p50=10,p95=19))
        self.assertEqual(report['stages']['t_first_audio']['n'],0)
        # Seed older rows in the same isolated database, then exercise the real insert/prune.
        with server.agent_service.db() as c:c.executemany('INSERT INTO voice_metrics(mode,t_done) VALUES (?,?)',[('voice',i) for i in range(1000)])
        server.voice_metrics(dict(mode='voice',t_done=600000))
        with server.agent_service.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM voice_metrics').fetchone()[0],1000)
            self.assertEqual(c.execute('SELECT t_done FROM voice_metrics ORDER BY id LIMIT 1').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT t_done FROM voice_metrics ORDER BY id DESC LIMIT 1').fetchone()[0],600000)
    def test_diagnostics_origin_protection_and_auth(self):
        for route,body in [('/api/voice/metrics',dict(mode='text')),('/api/client-log',dict(stage='head',message='error'))]:
            headers=self.h.headers.copy()
            for origin in [None,'https://evil.test','null','https://panel.test/path']:
                if origin is None:self.h.headers.pop('Origin',None)
                else:self.h.headers['Origin']=origin
                self.assertEqual(self.diagnostic(route,body)[0],403)
            self.h.headers=headers
            os.environ.pop('SVODKA_AGENT_TRUSTED_PROXY');self.assertEqual(self.diagnostic(route,body)[0],503);os.environ['SVODKA_AGENT_TRUSTED_PROXY']='1'
            token=self.root/'token';token.write_text('x'*30)
            with patch.object(server,'TOKEN_FILE',token):self.assertEqual(self.diagnostic(route,body)[0],401)
        self.h.headers.pop('X-Svodka-Voice');self.assertEqual(self.diagnostic('/api/voice/metrics',dict(mode='text'))[0],403)
    def test_client_log_size_masking_query_strip(self):
        payload=dict(stage='script-start',message='sk_abcdefghijklmnop Bearer password https://user:password@panel.test/a?k=private&token=secret /index.html?k=another\nkey_1234567890',ua='test',t=1234)
        with patch.object(server.sys,'stderr',io.StringIO()) as stderr:
            self.assertEqual(self.diagnostic('/api/client-log',payload)[0],200)
            logged=(server.agent_service.root/'client-log.jsonl').read_text();self.assertIn(logged.strip(),stderr.getvalue())
        for secret in ['abcdefghijklmnop','password','private','secret','another','1234567890','?','user:']:self.assertNotIn(secret,logged)
        row=json.loads(logged);self.assertIn('https://panel.test/a',row['message']);self.assertEqual(row['t'],1234);self.assertEqual(len(logged.splitlines()),1)
        with patch.object(server.sys,'stderr',io.StringIO()):
            self.assertEqual(self.diagnostic('/api/client-log',dict(stage='a'*100,message='x'*600,ua='u'*400))[0],200)
        row=json.loads((server.agent_service.root/'client-log.jsonl').read_text().splitlines()[-1]);self.assertEqual([len(row[k]) for k in ['stage','message','ua']],[60,300,200])
        self.assertEqual(self.diagnostic('/api/client-log',dict(message='x'*2048))[0],413)
        self.assertEqual(self.diagnostic('/api/client-log',None,raw=b' ' * 2048)[0],400)
        self.assertEqual(self.diagnostic('/api/client-log',None,raw=b' ' * 2049)[0],413)
        for body in [None,[],dict(message={}),dict(t=True),dict(t=float('inf')),dict(url='https://secret')]:self.assertEqual(self.diagnostic('/api/client-log',body)[0],400)
        self.assertEqual(self.diagnostic('/api/client-log',dict(t=10**400))[0],400)
    def test_client_log_rate_and_cap(self):
        with patch.object(server.time,'monotonic',return_value=100),patch.object(server.sys,'stderr',io.StringIO()):
            for i in range(20):self.assertEqual(self.diagnostic('/api/client-log',dict(message=str(i)))[0],200)
            self.assertEqual(self.diagnostic('/api/client-log',dict(message='limited'))[0],429)
        target=server.agent_service.root/'client-log.jsonl';target.write_text('\n'.join(json.dumps(dict(message=str(i))) for i in range(200))+'\n')
        with patch.object(server.time,'monotonic',return_value=161),patch.object(server.sys,'stderr',io.StringIO()):
            self.assertEqual(self.diagnostic('/api/client-log',dict(message='latest'))[0],200)
        rows=[json.loads(s) for s in target.read_text().splitlines()];self.assertEqual(len(rows),200);self.assertEqual(rows[0]['message'],'1');self.assertEqual(rows[-1]['message'],'latest')
        self.assertEqual(target.stat().st_mode&0o777,0o600)
