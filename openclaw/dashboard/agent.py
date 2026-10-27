"""Dashboard-owned persistent runs. No Gateway config discovery or tool proxy."""
from contextlib import contextmanager
import socket
import http.client
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlsplit

class Unavailable(Exception):
    pass

TOTAL_TIMEOUT = 600
LINE_LIMIT = 64 * 1024
EVENT_LIMIT = 1024 * 1024

class Agent:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.started = False
        self.active = {}

    @contextmanager
    def db(self):
        self.root.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.root / 'agent.sqlite3', timeout=15)
        try:
            with c:
                self.initialize(c)
                yield c
        finally:
            c.close()

    def initialize(self, c):
        c.row_factory = sqlite3.Row
        c.execute('CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, status TEXT, input TEXT, output TEXT, error TEXT, started REAL, updated REAL)')
        c.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, run INTEGER, status TEXT, time REAL)')
        columns = {r[1] for r in c.execute('PRAGMA table_info(events)')}
        if 'output_chars' not in columns:
            c.execute('ALTER TABLE events ADD COLUMN output_chars INTEGER DEFAULT 0')
        if not self.started:
            c.execute("UPDATE runs SET status='interrupted', error='Сервер панели перезапущен; автоматический повтор не выполнялся' WHERE status='running'")
            c.commit()
            self.started = True


    def config(self):
        if os.environ.get('SVODKA_AGENT_ENABLED') != '1':
            raise Unavailable('Агент отключён: требуется разрешение оператора и отдельно включённая точка ответов шлюза.')
        url = os.environ.get('SVODKA_AGENT_URL', '')
        u = urlsplit(url)
        if u.scheme != 'http' or u.hostname not in ('127.0.0.1', '::1') or u.path != '/v1/responses' or u.query or u.fragment or u.username or u.password:
            raise Unavailable('Адрес агента должен явно указывать локальную точку /v1/responses')
        agent = os.environ.get('SVODKA_AGENT_ID', '')
        user = os.environ.get('SVODKA_AGENT_USER', '')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', agent) or not re.fullmatch(r'svodka-[a-zA-Z0-9_-]{1,80}', user):
            raise Unavailable('Требуются фиксированный идентификатор агента и пользователь с префиксом svodka-')
        try:
            f = Path(os.environ['SVODKA_AGENT_TOKEN_FILE'])
            if f.stat().st_mode & 0o077:
                raise OSError()
            token = f.read_text().strip()
            if not token or '\n' in token or '\r' in token:
                raise OSError()
        except (KeyError, OSError):
            raise Unavailable('Требуется закрытый файл токена сервера с правами 0600')
        return url, token, agent, user

    def snapshot(self):
        with self.lock, self.db() as c:
            self.recover(c)
            runs = [dict(r) for r in c.execute('SELECT id,status,substr(input,1,20000) AS input,substr(output,1,16384) AS output,length(output)>16384 AS truncated,substr(error,1,1000) AS error,started,updated FROM runs ORDER BY id DESC LIMIT 20')][::-1]
        try:
            self.config()
            available, reason = True, 'Настроен; точка подключения и выполнение модели пока не проверены'
        except Unavailable as e:
            available, reason = False, str(e)
        with self.lock, self.db() as c:
            events = [dict(r) for r in c.execute('SELECT run,status,time,output_chars FROM events ORDER BY id DESC LIMIT 100')]
        return dict(available=available, reason=reason, runs=runs, events=events)

    def recover(self, c):
        # A dead worker may have failed to persist its terminal state. Never retry work.
        for row in c.execute("SELECT id FROM runs WHERE status='running'").fetchall():
            worker = self.active.get(row['id'])
            if worker is None or not worker.is_alive():
                c.execute("UPDATE runs SET status='interrupted',error=?,updated=? WHERE id=?",
                          ('Поток выполнения остановлен; автоматический повтор не выполнялся', time.time(), row['id']))
                self.active.pop(row['id'], None)

    def submit(self, text):
        config = self.config()
        with self.lock:
            return self._submit_locked(text, config)

    def _submit_locked(self, text, config):
        with self.db() as c:
            self.recover(c)
            if c.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                raise Unavailable('В панели уже выполняется запуск')
            now = time.time()
            rid = c.execute("INSERT INTO runs(status,input,output,error,started,updated) VALUES('running',?,'','',?,?)", (text, now, now)).lastrowid
            c.execute('INSERT INTO events(run,status,time) VALUES(?,?,?)', (rid, 'running', now))
            worker = threading.Thread(target=self.work, args=(rid, text, config), daemon=True)
            self.active[rid] = worker
        try:
            worker.start()
        except Exception:
            with self.lock:
                self.active.pop(rid, None)
            raise
        return rid

    def update(self, rid, status, output, error=''):
        with self.lock, self.db() as c:
            previous = c.execute('SELECT status,output FROM runs WHERE id=?', (rid,)).fetchone()
            if previous and (previous['status'] != status or previous['output'] != output):
                c.execute('INSERT INTO events(run,status,time,output_chars) VALUES(?,?,?,?)', (rid, status, time.time(), len(output)))
            c.execute('UPDATE runs SET status=?,output=?,error=?,updated=? WHERE id=?', (status, output, error, time.time(), rid))

    def work(self, rid, text, config):
        url, token, agent, user = config
        output, status = '', 'running'
        deadline = time.monotonic() + TOTAL_TIMEOUT
        last_write = float('-inf')
        timer = None
        try:
            req = urllib.request.Request(url, data=json.dumps(dict(model='openclaw/'+agent, user=user, input=text, stream=True)).encode(), headers={'Authorization':'Bearer '+token, 'Content-Type':'application/json', 'Accept':'text/event-stream'})
            # No redirects: never forward the operator credential to another URL.
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            def arm_deadline(sock):
                nonlocal timer
                def expire():
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                timer = threading.Timer(max(0, deadline-time.monotonic()), expire)
                timer.daemon = True
                timer.start()

            class DeadlineConnection(http.client.HTTPConnection):
                def connect(self):
                    super().connect()
                    # Arm before reading HTTP headers, which may also be drip-fed.
                    arm_deadline(self.sock)

            class DeadlineHTTP(urllib.request.HTTPHandler):
                def http_open(self, request):
                    return self.do_open(DeadlineConnection, request)

            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), DeadlineHTTP())
            with opener.open(req, timeout=TOTAL_TIMEOUT) as response:
                # Interrupt a blocking/drip-fed readline at the absolute deadline.
                sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                if sock is not None and timer is None:
                    arm_deadline(sock)
                if response.headers.get_content_type() != 'text/event-stream':
                    raise Unavailable('Gateway did not return SSE')
                event, data, data_size = '', [], 0
                while True:
                    if time.monotonic() >= deadline:
                        raise Unavailable('Превышено время выполнения')
                    raw = response.readline(LINE_LIMIT + 1)
                    if time.monotonic() >= deadline:
                        raise Unavailable('Превышено время выполнения')
                    if len(raw) > LINE_LIMIT:
                        raise Unavailable('Gateway event too large')
                    if not raw and not data:
                        break
                    line = raw.decode('utf-8').rstrip('\r\n')
                    if line.startswith('event:'):
                        event = line[6:].strip()
                    elif line.startswith('data:'):
                        data_size += len(raw)
                        if data_size > EVENT_LIMIT:
                            raise Unavailable('Gateway event too large')
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        payload = '\n'.join(data); data = []; data_size = 0
                        if payload == '[DONE]':
                            break
                        obj = json.loads(payload)
                        kind = obj.get('type', event)
                        event = ''
                        if kind == 'response.output_text.delta':
                            output += obj.get('delta', '')
                            if len(output) > 2000000:
                                raise Unavailable('Response exceeds dashboard limit')
                            now = time.monotonic()
                            if now - last_write >= 0.4:
                                self.update(rid, status, output)
                                last_write = now
                        elif kind in ('response.completed','response.incomplete','response.failed'):
                            status = {'response.completed':'completed','response.incomplete':'incomplete','response.failed':'failed'}[kind]
                            self.update(rid, status, output, 'Ответ завершён не полностью' if status == 'incomplete' else 'Ошибка выполнения шлюза' if status == 'failed' else '')
                            break
                    elif not line:
                        event = ''
                if status == 'running':
                    raise Unavailable('Gateway stream ended without a terminal event')
            self.update(rid, status, output, 'Ошибка выполнения шлюза' if status == 'failed' else 'Ответ завершён не полностью' if status == 'incomplete' else '')
        except urllib.error.HTTPError as e:
            try:
                self.update(rid, 'failed', output, {401:'Шлюз отклонил авторизацию',403:'Доступ к шлюзу запрещён',404:'Точка ответов шлюза недоступна; оператор должен включить её отдельно',405:'Точка ответов шлюза отключена или не поддерживается'}.get(e.code, 'Ошибка HTTP шлюза '+str(e.code)))
            except Exception:
                pass
            finally:
                e.close()
        except Exception:
            # Never persist upstream exception/body: it may contain operator secrets.
            try:
                self.update(rid, 'failed', output, 'Шлюз недоступен или получен некорректный либо незавершённый поток SSE')
            except Exception:
                pass  # snapshot/submit recover the dead worker once storage is writable.
        finally:
            if timer is not None:
                timer.cancel()
            with self.lock:
                self.active.pop(rid, None)


def queue_snapshot(filename):
    if not filename:
        return dict(available=False, jobs=[], reason='Очередь кодера для чтения не настроена')
    try:
        d = json.loads(Path(filename).read_text())
        jobs = []
        allowed = {'queued','running','waiting_limit','question','done','nochange','failed','cancelled','merged'}
        for row in d['tasks'][-100:]:
            if not isinstance(row, dict):
                continue
            job = {k: row[k] for k in ('id','created','updated') if isinstance(row.get(k), (str,int,float))}
            job['status'] = row.get('status') if row.get('status') in allowed else 'unknown'
            jobs.append(job)
        return dict(available=True, jobs=jobs, reason='Только чтение; названия, запросы, результаты, пути и сессии скрыты')
    except (OSError, ValueError, KeyError, TypeError):
        return dict(available=False, jobs=[], reason='Очередь кодера недоступна')
