import json, os, subprocess, sys, tempfile, time, urllib.request
srv = sys.argv[1]
with tempfile.TemporaryDirectory(prefix='svodka-t13-') as life:
    env = {k: v for k, v in os.environ.items() if not k.startswith('SVODKA_')}
    env.update(LIFE_DIR=life, DASHBOARD_PORT='18913', DASHBOARD_HOST='127.0.0.1', DASHBOARD_TOKEN_FILE=life + '/no-token')
    p = subprocess.Popen([sys.executable, '-I', '-B', srv], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.5)
        base = 'http://127.0.0.1:18913'
        def call(path, body=None, origin='http://127.0.0.1:18913'):
            t = time.monotonic()
            req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None, {'Content-Type': 'application/json', 'Origin': origin})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read()), int((time.monotonic() - t) * 1000)
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b'{}'), int((time.monotonic() - t) * 1000)
        fact = dict(name='Т13 тест', text='синтетический факт t13-ζ42', kind='тема', request='t13smoke_request_01', source='dashboard')
        out = {}
        out['add'] = call('/api/memory/add', fact)
        out['retry_same_request'] = call('/api/memory/add', fact)
        out['dedup_other_request'] = call('/api/memory/add', {**fact, 'request': 't13smoke_request_02'})
        out['secret_rejected'] = call('/api/memory/add', {**fact, 'text': 'token sk-abcdefghijklmnop', 'request': 't13smoke_request_03'})
        data = call('/api/data')[1]
        page = [p for p in data['memory'] if p['name'] == 'Т13 тест']
        out['read_after_write_facts'] = [f['text'] for f in page[0]['facts']] if page else None
        st = call('/api/memory/status?probe=1')
        out['status'] = dict(code=st[0], store=st[1]['store'], search_mode=st[1]['search']['mode'], semantic=st[1]['search']['semantic'], audit=st[1]['audit'], tools=st[1]['tools'], ms=st[2])
        for k in ('add', 'retry_same_request', 'dedup_other_request', 'secret_rejected'):
            c, b, ms = out[k]; out[k] = dict(code=c, duplicate=b.get('duplicate'), verified=b.get('verified'), error=b.get('error'), ms=ms)
        out['audit_lines'] = open(life + '/dashboard-agent/memory-audit.jsonl').read().count('\n')
        print(json.dumps(out, ensure_ascii=False, indent=1))
    finally:
        p.terminate(); p.wait(5)
