#!/usr/bin/env python3
"""«Сводка»: a small web server over the files the OpenClaw life-ledger skill keeps.

The data lives in plain files in LIFE_DIR (default ~/.openclaw/workspace/life):
  expenses.csv     date,amount,currency,category,note
  tasks.md         personal to-dos   - [ ] title (до 2026-10-30) / - [x] title (сделано 2026-10-25)
  plans.md         personal plans and goals, same line format as tasks.md
  notes.md         personal notes    - 2026-10-25 14:30 · text
  work-tasks.md    work to-dos, same format as tasks.md
  work-notes.md    work notes, same format as notes.md
  food.csv         date,time,item,grams,kcal,protein,fat,carbs,source
  settings.json    {"kcalGoal": 2200}
  memory/<Имя>.md  one page per person, place, project or topic, Obsidian style:
                   "# Дима", "тип: человек", then facts "- 2026-10-25 · text with [[links]]"
  ai/<id>.json     dialogs of the «ИИ» tab: {"title", "project", "messages": [{"role", "text", "model", "time"}]}
  ai/projects.json projects of the «ИИ» tab: {"projects": [{"id", "name", "description", "instructions"}]}

Jarvis writes them from Telegram; this server shows them and lets you edit them
from any browser. Only the Python standard library is used.
"""

# -I deliberately excludes the script directory. Load only the resolved sibling,
# never a module selected through cwd, environment, or an expanded sys.path.
import importlib.util
import sys
from pathlib import Path
_dashboard_dir = Path(__file__).resolve().parent
_agent_path = (_dashboard_dir / "agent.py").resolve(strict=True)
if _agent_path.parent != _dashboard_dir:
    raise ImportError("agent.py must resolve to a trusted dashboard sibling")
_agent_spec = importlib.util.spec_from_file_location("agent", _agent_path)
_agent_module = importlib.util.module_from_spec(_agent_spec)
sys.modules[_agent_spec.name] = _agent_module  # decorators may inspect their module
_agent_spec.loader.exec_module(_agent_module)
Agent, Unavailable, queue_snapshot = _agent_module.Agent, _agent_module.Unavailable, _agent_module.queue_snapshot
_workspace_path = (_dashboard_dir / 'workspace.py').resolve(strict=True)
if _workspace_path.parent != _dashboard_dir:
    raise ImportError('workspace.py must resolve to a trusted dashboard sibling')
_workspace_spec = importlib.util.spec_from_file_location('svodka_workspace', _workspace_path)
_workspace_module = importlib.util.module_from_spec(_workspace_spec)
sys.modules[_workspace_spec.name] = _workspace_module
_workspace_spec.loader.exec_module(_workspace_module)

_attachments_path = (_dashboard_dir / 'attachments.py').resolve(strict=True)
if _attachments_path.parent != _dashboard_dir:
    raise ImportError('attachments.py must resolve to a trusted dashboard sibling')
_attachments_spec = importlib.util.spec_from_file_location('svodka_attachments', _attachments_path)
attachments = importlib.util.module_from_spec(_attachments_spec)
sys.modules[_attachments_spec.name] = attachments
_attachments_spec.loader.exec_module(attachments)


import csv
import base64
import hashlib
import hmac
import io
import json
import math
import os
import queue
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit, unquote
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_office_path = (_dashboard_dir / 'office.py').resolve(strict=True)
if _office_path.parent != _dashboard_dir:
    raise ImportError('office.py must resolve to a trusted dashboard sibling')
_office_spec = importlib.util.spec_from_file_location('office', _office_path)
_office_module = importlib.util.module_from_spec(_office_spec)
sys.modules[_office_spec.name] = _office_module
_office_spec.loader.exec_module(_office_module)
office_sessions, coder_metadata, coder_feed = _office_module.office_sessions, _office_module.coder_metadata, _office_module.coder_feed

LIFE_DIR = Path(os.environ.get("LIFE_DIR", Path.home() / ".openclaw/workspace/life")).expanduser()
agent_service = Agent(LIFE_DIR / "dashboard-agent")
workspace_service = None

VOICE_MAX_BYTES = 10 * 1024 * 1024
VOICE_TIMEOUT = 90
voice_semaphore = threading.Semaphore(1)
VOICE_STAGES = ('t_speech_end', 't_transcript', 't_run_accepted', 't_first_text', 't_first_audio', 't_done')
SECRET_RE = re.compile(r'(sk|gsk|xai|key)[-_][\w-]{8,}|Bearer \S+', re.I)
client_log_lock = threading.Lock()
client_log_times = []


def diagnostic_text(value, limit):
    value = SECRET_RE.sub('***', value)
    # Never journal URL credentials, query strings (including access links), or fragments.
    def clean_url(match):
        try:
            u = urlsplit(match[0])
            return (u.scheme + '://' if u.scheme else '//') + (u.netloc.rsplit('@', 1)[-1]) + u.path
        except ValueError:
            return '<url>'
    value = re.sub(r'(?:[a-z][a-z0-9+.-]*:)?//[^\s<>"\']+', clean_url, value, flags=re.I)
    value = re.sub(r'[?#][^\s<>"\']*', '', value)
    value = re.sub(r'\b(?:token|api[_-]?key|authorization|k)\s*[=:]\s*[^\s,;]+', 'token=***', value, flags=re.I)
    return one_line(value, limit)


def voice_metrics(body=None):
    if body is not None:
        if not isinstance(body, dict) or set(body) - set(VOICE_STAGES) - {'mode'} or body.get('mode') not in ('voice', 'text'):
            raise ValueError()
        for key in VOICE_STAGES:
            n = body.get(key)
            if n is not None and (type(n) not in (int, float) or not 0 <= n <= 600000 or not math.isfinite(n)):
                raise ValueError()
    with agent_service.lock, agent_service.db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS voice_metrics (id INTEGER PRIMARY KEY, mode TEXT, ' + ','.join(k+' REAL' for k in VOICE_STAGES) + ')')
        if body is not None:
            c.execute('INSERT INTO voice_metrics (mode,' + ','.join(VOICE_STAGES) + ') VALUES (?,?,?,?,?,?,?)', [body['mode']] + [body.get(k) for k in VOICE_STAGES])
            c.execute('DELETE FROM voice_metrics WHERE id NOT IN (SELECT id FROM voice_metrics ORDER BY id DESC LIMIT 1000)')
            return {'ok': True}
        rows = c.execute('SELECT * FROM voice_metrics').fetchall()
    stages = {}
    for key in VOICE_STAGES:
        values = sorted(r[key] for r in rows if r[key] is not None)
        stages[key] = dict(n=len(values), p50=values[math.ceil(len(values)*.5)-1] if values else None,
                          p95=values[math.ceil(len(values)*.95)-1] if values else None)
    return dict(count=len(rows), stages=stages)

HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASHBOARD_PORT", "8787"))
STATIC = Path(__file__).resolve().parent / "index.html"
ICON = Path(__file__).resolve().parent / "icon.png"

# Personal access link. If the token file exists, every request needs the link
# (https://host/?k=TOKEN) once; the server then sets a year-long cookie. With no token
# file the server stays open (the reverse proxy is expected to protect it).
TOKEN_FILE = Path(os.environ.get("DASHBOARD_TOKEN_FILE", Path.home() / ".config/jarvis/dashboard-token")).expanduser()
COOKIE = "svodka"

EXPENSE_FIELDS = ["date", "amount", "currency", "category", "note"]
FOOD_FIELDS = ["date", "time", "item", "grams", "kcal", "protein", "fat", "carbs", "source"]
FOOD_NUMBERS = ["grams", "kcal", "protein", "fat", "carbs"]
DEFAULT_SETTINGS = {"kcalGoal": 2200}
# Which file each list lives in; the page names lists, never file paths.
TASK_FILES = {"tasks": "tasks.md", "plans": "plans.md", "work": "work-tasks.md"}
NOTE_FILES = {"notes": "notes.md", "work": "work-notes.md"}

TASK_RE = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+(.*?)\s*$")
DUE_RE = re.compile(r"\(до (\d{4}-\d{2}-\d{2})\)")
DONE_RE = re.compile(r"\(сделано (\d{4}-\d{2}-\d{2})\)")
NOTE_RE = re.compile(r"^\s*[-*]\s+(\d{4}-\d{2}-\d{2})(?: (\d{2}:\d{2}))?\s*·\s*(.*?)\s*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PAGE_NAME_RE = re.compile(r"[\w .,()«»'’&+-]{1,80}")
KIND_RE = re.compile(r"^\s*тип:\s*(.+?)\s*$")

lock = threading.Lock()


def path(name):
    return LIFE_DIR / name


def read_text(name):
    try:
        return path(name).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_text(name, text):
    """Write atomically, so Jarvis never reads a half-written file."""
    target = path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, target)


def one_line(value, limit=500):
    return " ".join(str(value or "").split())[:limit]


# --- expenses -----------------------------------------------------------

def read_expenses():
    rows = []
    for i, row in enumerate(csv.DictReader(io.StringIO(read_text("expenses.csv")))):
        try:
            amount = float(str(row.get("amount", "")).replace(",", ".").replace(" ", ""))
        except ValueError:
            continue
        rows.append({
            "id": i,
            "date": (row.get("date") or "").strip(),
            "amount": amount,
            "currency": (row.get("currency") or "RUB").strip(),
            "category": (row.get("category") or "Другое").strip(),
            "note": (row.get("note") or "").strip(),
        })
    return rows


def write_expenses(rows):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=EXPENSE_FIELDS, lineterminator="\n")
    w.writeheader()
    for r in rows:
        amount = r["amount"]
        w.writerow({**{k: r[k] for k in EXPENSE_FIELDS if k != "amount"},
                    "amount": int(amount) if float(amount).is_integer() else amount})
    write_text("expenses.csv", out.getvalue())


# --- food ---------------------------------------------------------------

def number(value):
    try:
        return float(str(value or "").replace(",", ".").replace(" ", ""))
    except ValueError:
        return 0.0


def read_food():
    rows = []
    for i, row in enumerate(csv.DictReader(io.StringIO(read_text("food.csv")))):
        rows.append({
            "id": i,
            "date": (row.get("date") or "").strip(),
            "time": (row.get("time") or "").strip(),
            "item": (row.get("item") or "").strip(),
            "source": (row.get("source") or "").strip(),
            **{k: number(row.get(k)) for k in FOOD_NUMBERS},
        })
    return rows


def write_food(rows):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=FOOD_FIELDS, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: (int(r[k]) if k in FOOD_NUMBERS and float(r[k]).is_integer() else r[k]) for k in FOOD_FIELDS})
    write_text("food.csv", out.getvalue())


def read_settings():
    try:
        saved = json.loads(read_text("settings.json") or "{}")
    except ValueError:
        saved = {}
    return {**DEFAULT_SETTINGS, **(saved if isinstance(saved, dict) else {})}


# --- tasks, plans -------------------------------------------------------

def read_tasks(name):
    tasks = []
    for i, line in enumerate(read_text(name).splitlines()):
        m = TASK_RE.match(line)
        if not m:
            continue
        body = m.group(2)
        due = DUE_RE.search(body)
        done_on = DONE_RE.search(body)
        title = DONE_RE.sub("", DUE_RE.sub("", body)).strip()
        tasks.append({
            "line": i,
            "raw": line,
            "title": title,
            "done": m.group(1).lower() == "x",
            "due": due.group(1) if due else "",
            "doneOn": done_on.group(1) if done_on else "",
        })
    return tasks


def format_task(title, done, due, done_on):
    text = f"- [{'x' if done else ' '}] {title}"
    if due:
        text += f" (до {due})"
    if done and done_on:
        text += f" (сделано {done_on})"
    return text


# --- notes --------------------------------------------------------------

def read_notes(name):
    notes = []
    for i, line in enumerate(read_text(name).splitlines()):
        m = NOTE_RE.match(line)
        if m:
            notes.append({"line": i, "raw": line, "date": m.group(1), "time": m.group(2) or "", "text": m.group(3)})
    return notes


# --- memory pages -------------------------------------------------------

def page_file(name):
    name = one_line(name, 80)
    if not PAGE_NAME_RE.fullmatch(name) or name.startswith("."):
        raise ValueError()
    return name, f"memory/{name}.md"


def read_memory():
    pages = []
    folder = LIFE_DIR / "memory"
    if not folder.is_dir():
        return pages
    for f in sorted(folder.glob("*.md")):
        kind, facts = "", []
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines()):
            m = NOTE_RE.match(line)
            k = KIND_RE.match(line)
            if m:
                facts.append({"line": i, "raw": line, "date": m.group(1), "text": m.group(3)})
            elif k and not kind:
                kind = k.group(1)
            elif line.strip() and not line.startswith("#"):
                facts.append({"line": i, "raw": line, "date": "", "text": line.strip().lstrip("-* ").strip()})
        pages.append({"name": f.stem, "kind": kind, "facts": facts})
    return pages


MEMORY_SERVICE_RE = re.compile(r'<\s*/?\s*(think|thinking|reasoning|system)\b|system\s+prompt|системный\s+промпт', re.I)
MEMORY_REJECT = 'Похоже на секрет или служебный текст — в память не сохраняется'


def memory_version(data):
    return hashlib.sha256(data).hexdigest()[:16]


def memory_audit():
    rows = []
    for line in read_text('dashboard-agent/memory-audit.jsonl').splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except ValueError:
            pass
    return rows


def memory_journal(file, text):
    target = path('dashboard-agent/' + file)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('a', encoding='utf-8') as f:
        f.write(text + '\n')
        f.flush()
        os.fsync(f.fileno())


def memory_record(op, source, request, page, line, text, before, after):
    memory_journal('memory-audit.jsonl', json.dumps(dict(ts=datetime.now().isoformat(timespec='seconds'),
        op=op, source=source, request=request, page=page, line=line,
        text_sha256=memory_version(text.encode()), version_before=before, version_after=after), ensure_ascii=False))


def memory_metadata(b):
    request, source = b.get('request'), b.get('source', 'dashboard')
    if ('request' in b and (not isinstance(request, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', request))) or source not in ('dashboard', 'voice'):
        raise ValueError()
    return request, source


def memory_status():
    pages = read_memory()
    digest = hashlib.sha256()
    for f in sorted((LIFE_DIR / 'memory').glob('*.md')):
        digest.update(f.name.encode()); digest.update(b'\0'); digest.update(f.read_bytes()); digest.update(b'\0')
    folder = LIFE_DIR / 'memory'
    parent = folder
    while not parent.exists():
        parent = parent.parent
    writable = os.access(parent, os.W_OK) and all(os.access(f, os.W_OK) for f in folder.glob('*.md'))
    rows = memory_audit()
    contract = [
        dict(data='Факты о людях/местах/проектах', authority='life/memory/<Имя>.md', writers='Сводка + агент (навык life-ledger)', agent_access='Агент читает файлы напрямую'),
        dict(data='Профиль и предпочтения', authority='USER.md/SOUL.md рабочего пространства', writers='Только агент', agent_access='Панель не читает и не правит'),
        dict(data='Долгая память агента', authority='MEMORY.md, memory/*.md, индекс memory_search', writers='Агент', agent_access='Панель не читает'),
        dict(data='Переписка', authority='Канонический сеанс задачи 12 через sessions_history', writers='Панель + агент', agent_access='Панель не копирует в память')]
    return dict(store=dict(pages=len(pages), facts=sum(len(p['facts']) for p in pages), version=digest.hexdigest()[:16], writable=writable),
        audit=dict(records=len(rows), last={k:rows[-1].get(k) for k in ('ts','op','source')} if rows else None),
        search=dict(mode='keyword', semantic=False, reason='Семантический поиск OpenClaw не подтверждён: индекс построен как fts-only, ожидается text-embedding-3-small (диагностика 29.10). Панель и агент находят факты Сводки прямым чтением страниц и поиском по ключевым словам.'),
        contract=contract, tools=dict(probe=False))


# --- AI chat ------------------------------------------------------------
# Claude goes through the Claude Code subscription; when the limits service says it is
# used up, auto mode skips it. Free models go through the local freellmapi gateway, its one
# key lives only in ~/.config/jarvis/freellm-unified-key.

CONF = Path.home() / ".config/jarvis"
ELEVEN_FILE = CONF / "elevenlabs.env"   # ELEVENLABS_API_KEY=..., optional ELEVENLABS_VOICE_ID / ELEVENLABS_MODEL
ELEVEN_VOICE = "JBFqnCBsd6RMkjVDRZzb"   # premade multilingual male voice; speaks Russian with flash v2.5
eleven_pause = {"until": 0.0, "reason": ""}
# Live voice: one OpenClaw Talk realtime call at a time, held open by talk_helper.mjs.
# The Gateway client secret never reaches the browser; only the SDP offer and answer pass through.
talk_calls = {}
talk_lock = threading.Lock()
TALK_HELPER = Path(__file__).resolve().parent / "talk_helper.mjs"


def talk_reader(proc, lines):
    for line in proc.stdout:
        lines.put(line)
    lines.put(None)


def talk_next(call, timeout):
    """Next JSON message from a call helper, or None when it ended or went quiet."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            line = call["lines"].get(timeout=max(0.05, deadline - time.monotonic()))
        except queue.Empty:
            return None
        if line is None:
            return None
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict):
            return msg


def talk_end(sid=None):
    with talk_lock:
        ended = [talk_calls.pop(k) for k in list(talk_calls) if sid is None or k == sid]
    for call in ended:
        proc = call["proc"]
        try:
            proc.stdin.write(b'{"close":true}\n')
            proc.stdin.close()
            proc.wait(timeout=4)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            proc.kill()
tts_semaphore = threading.Semaphore(2)


def eleven_config():
    values = {}
    try:
        for line in ELEVEN_FILE.read_text().splitlines():
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    key = values.get("ELEVENLABS_API_KEY") or os.environ.get("ELEVENLABS_API_KEY", "")
    voice = values.get("ELEVENLABS_VOICE_ID") or ELEVEN_VOICE
    model = values.get("ELEVENLABS_MODEL") or "eleven_flash_v2_5"
    if not re.fullmatch(r"[A-Za-z0-9]{10,40}", voice) or not re.fullmatch(r"[a-z0-9_]{3,40}", model):
        return "", ELEVEN_VOICE, "eleven_flash_v2_5"
    return key.strip(), voice, model


def model_name_safe(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,80}", value) else None


def tts_status():
    if not eleven_config()[0]:
        return False, "Голос ElevenLabs не настроен — озвучивает браузер"
    if eleven_pause["until"] > time.time():
        return False, eleven_pause["reason"]
    return True, "Голос ElevenLabs"


def eleven_speak(text):
    """MP3 for one short phrase. Pauses ElevenLabs for a while on quota or key problems."""
    key, voice, model = eleven_config()
    body = json.dumps({"text": text, "model_id": model, "language_code": "ru",
                       "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.15, "use_speaker_boost": True}}).encode()
    req = urllib.request.Request(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_64", data=body,
                                 headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read(3 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        quota = e.code in (401, 402, 403, 429)
        eleven_pause.update(until=time.time() + (600 if quota else 60),
                            reason="Минуты ElevenLabs закончились или ключ не подходит — озвучивает браузер" if quota else "ElevenLabs временно недоступен — озвучивает браузер")
    except (urllib.error.URLError, OSError, ValueError):
        eleven_pause.update(until=time.time() + 60, reason="ElevenLabs временно недоступен — озвучивает браузер")
    return None
CODER_DATA = Path.home() / ".openclaw/workspace/coder"   # limits from coder.py (usage.json, limits.json)
CHAT_ID_RE = re.compile(r"^[0-9a-f]{12}$")
AI_SYSTEM = ("Ты собеседник владельца в его личном чате. Отвечай на языке вопроса, по делу, "
             "без эмодзи. Оформление простое: абзацы и списки через дефис.")
AI_HISTORY_CHARS = 60000     # сколько последних символов переписки отдавать модели
CLAUDE_TIMEOUT = 300
CLAUDE_PAUSE = 30 * 60       # Claude сказал «лимит»: не трогаем его полчаса

# freellmapi speaks the OpenAI chat format; model "auto" lets it pick a free model itself.
FREE_URL = "http://127.0.0.1:3187/v1"
FREE_KEY = "freellm-unified-key"
FREE_DOWN = "Бесплатный шлюз freellmapi недоступен (127.0.0.1:3187). Проверь, что он запущен."

ai_lock = threading.Lock()
claude_pause = {"until": 0.0}
gate_cache = {"at": 0.0, "up": False}


class AIError(Exception):
    pass


def free_key():
    try:
        k = (CONF / FREE_KEY).read_text().strip()
    except OSError:
        return None
    return k or None


def free_request(path, body=None, timeout=180):
    headers = {"Authorization": f"Bearer {free_key()}", "Content-Type": "application/json", "User-Agent": "Svodka/1"}
    with urllib.request.urlopen(urllib.request.Request(FREE_URL + path, data=body, headers=headers), timeout=timeout) as r:
        return json.loads(r.read())


def gate_up():
    """Отвечает ли шлюз; спрашиваем не чаще раза в минуту."""
    if time.time() - gate_cache["at"] > 60:
        try:
            free_request("/models", timeout=2)
            up = True
        except (OSError, ValueError):
            up = False
        gate_cache.update(at=time.time(), up=up)
    return gate_cache["up"]


def claude_bin():
    try:
        for line in (CONF / "coder.env").read_text().splitlines():
            if line.strip().startswith("CLAUDE_BIN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    found = shutil.which("claude")
    if found:
        return found
    for p in (Path.home() / ".local/bin/claude", Path.home() / ".claude/local/claude"):
        if p.exists():
            return str(p)
    return None


def read_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def claude_state():
    """Подписка Claude: сколько потрачено за 5 часов и неделю и не кончилась ли она."""
    now = time.time()
    until, windows = 0.0, {}
    def mapping(value):
        return value if isinstance(value, dict) else {}
    def numeric(value):
        try:
            value = float(value)
            return value if value >= 0 and value < float('inf') else 0.0
        except (TypeError, ValueError, OverflowError):
            return 0.0
    usage = mapping(read_json(CODER_DATA / "usage.json"))
    for key in ("five_hour", "seven_day"):
        w = mapping(usage.get(key))
        reset, pct = numeric(w.get("reset")), numeric(w.get("pct"))
        if reset > now:
            windows[key] = {"pct": round(pct), "reset": reset}
            if pct >= 99.5:
                until = max(until, reset)
    info = mapping(mapping(read_json(CODER_DATA / "limits.json")).get("info"))
    if info.get("status") == "rejected":
        resets = [numeric(mapping(w).get("resetsAt")) for w in mapping(info.get("unifiedWindows")).values()]
        resets.append(numeric(info.get("resetsAt")))
        until = max(until, max(resets))
    if claude_pause["until"] > now:
        until = max(until, claude_pause["until"])
    return {"exhausted": until > now, "until": until if until > now else 0, "windows": windows}


def codex_bin():
    return shutil.which("codex") or (str(Path.home() / ".npm-global/bin/codex") if (Path.home() / ".npm-global/bin/codex").is_file() else None)


chatgpt_pause = {"until": 0.0}
free_models_cache = {"at": 0.0, "models": []}
ai_runs = {}
runs_lock = threading.Lock()
probe_lock = threading.Lock()
probe_cache_lock = threading.Lock()
probe_cache = {}
PROBE_TTL = 6 * 3600


# freellmapi пишет у всех моделей owned_by "freellmapi" и не говорит, через какой провайдер
# (Groq, OpenRouter...) она идёт, поэтому группируем по разработчику модели по её имени.
FREE_GROUPS = [
    (r"^(auto|fusion|default|free-router|claude-)|-auto$|auto-route", "Подбор шлюзом"),
    (r"^(gemini|gemma)", "Google"), (r"^(qwen|qwq)", "Qwen"), (r"^(gpt|o\d)", "OpenAI"),
    (r"^(mistral|ministral|codestral|devstral|voxtral|open-mistral)", "Mistral"), (r"^llama", "Meta Llama"),
    (r"^deepseek", "DeepSeek"), (r"^glm", "GLM"), (r"^nemotron", "NVIDIA"), (r"^(command|aya)", "Cohere"),
    (r"^groq", "Groq"), (r"^minimax", "MiniMax"),
]


def free_group(mid, m):
    provider = m.get("provider")
    if isinstance(provider, str) and provider:
        return provider
    for pattern, title in FREE_GROUPS:
        if re.search(pattern, mid, re.I):
            return title
    return "Другие"


def gateway_models():
    if not free_key():
        return []
    if time.time() - free_models_cache["at"] > 300:
        models = []
        try:
            for m in free_request("/models", timeout=3).get("data", []):
                if m.get("available") is False:
                    continue
                mid = str(m["id"])
                models.append({"id": mid, "name": one_line(m.get("name"), 120), "provider": free_group(mid, m)})
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        free_models_cache.update(at=time.time(), models=models)
    return free_models_cache["models"]


def claude_title(mid):
    parts = mid.removeprefix('claude-').split('-')
    return 'Claude ' + ' '.join(p.capitalize() for p in parts if not p.isdigit()) + (' ' + '.'.join(p for p in parts if p.isdigit()) if any(p.isdigit() for p in parts) else '')


def probe_status(mid):
    # Scope memory cache by data root as well as model (also permits isolated tests).
    with probe_cache_lock:
        key = (str(LIFE_DIR), mid)
        row = probe_cache.get(key)
        if row is None:
            saved = read_json(LIFE_DIR / 'ai/probes.json')
            row = saved.get(mid) if isinstance(saved, dict) else None
            if isinstance(row, dict):
                probe_cache[key] = row
        if not isinstance(row, dict) or not isinstance(row.get('at'), (float, int)) or time.time() - row['at'] >= PROBE_TTL:
            return dict(status='unknown', actual_model=None, at=None, error=None)
        return dict(status='ok' if row.get('ok') else 'failed', actual_model=row.get('actual_model'), at=row['at'], error=row.get('error'))


def model_row(mid, title, route, available, why, detail, image=False, **extra):
    return dict(id=mid, title=title, route=route, available=available, why=why, detail=detail,
                caps=dict(image=image, pdf=True, text=True), probe=probe_status(mid), **extra)


def listed_models():
    out = []
    ids = os.environ.get('SVODKA_CLAUDE_MODELS', 'claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5').split(',')
    for mid in dict.fromkeys(x.strip() for x in ids):
        if re.fullmatch(r'claude-[a-zA-Z0-9._-]{1,93}', mid):
            out.append(model_row('claude:'+mid, claude_title(mid), 'claude', bool(claude_bin()) and not claude_state()['exhausted'],
                                 'Claude недоступен или исчерпана подписка', 'Claude Code CLI · подписка', True,
                                 provenance='SVODKA_CLAUDE_MODELS' if 'SVODKA_CLAUDE_MODELS' in os.environ else 'список Claude', verified_runnable=False))
    cache = read_json(Path(os.environ.get('SVODKA_CODEX_MODELS_FILE', Path.home() / '.codex/models_cache.json')))
    rows = cache.get('models', []) if isinstance(cache, dict) else []
    for m in rows if isinstance(rows, list) else []:
        if not isinstance(m, dict):
            continue
        mid = m.get('slug', '')
        if isinstance(mid, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}', mid) and m.get('visibility') == 'list':
            modalities = m.get('input_modalities', [])
            out.append(model_row('chatgpt:'+mid, one_line(m.get('display_name') or mid, 100), 'chatgpt', bool(codex_bin()) and chatgpt_pause['until'] <= time.time(),
                                 'Codex CLI недоступен или действует пауза', 'Codex CLI · подписка', isinstance(modalities, list) and 'image' in modalities,
                                 provenance='Codex models_cache.json', verified_runnable=False))
    return out


def models_payload():
    c = claude_state()
    free_available = bool(free_key()) and gate_up()
    models = listed_models() + [model_row('free', 'Бесплатная', 'free', free_available, 'Шлюз недоступен', 'freellmapi')]
    # Auto prefers Opus, then Codex, then the free gateway. Other Claude choices stay explicit.
    candidates = [m for m in models if m['route'] == 'claude' and 'opus' in m['id']] + [m for m in models if m['route'] != 'claude']
    auto = next((dict(m, label='Авто → '+m['title']) for m in candidates if m['available']), None)
    free = [dict(model_row('free:'+m['id'], m.get('name') or m['id'], 'free', free_available, 'Шлюз недоступен', 'freellmapi'), id=m['id'], name=m.get('name'), provider=m.get('provider')) for m in gateway_models()]
    return dict(models=models, auto=auto, claude=c, free_models=free)


def model_pool(model_id):
    payload = models_payload()
    rows = payload['models'] + [dict(m, id='free:'+m['id']) for m in payload['free_models']]
    if model_id in ('claude', 'chatgpt'):
        model_id = next((m['id'] for m in rows if m['route'] == model_id), '')
    if model_id == 'auto':
        pool = [m for m in rows if m['available'] and ((m['route'] == 'claude' and 'opus' in m['id']) or m['route'] == 'chatgpt' or m['id'] == 'free')]
        if not pool:
            raise AIError('Нет настроенных маршрутов для авто')
        return pool
    selected = next((m for m in rows if m['id'] == model_id), None)
    if selected is None:
        raise ValueError('Неизвестная модель')
    return [selected]


def route_name(provider, model=None):
    return {'claude': 'Claude Code CLI · подписка', 'chatgpt': 'Codex CLI · подписка', 'free': 'freellmapi'}[provider]


def ai_probe(model_id):
    if not probe_lock.acquire(blocking=False):
        raise attachments.AttachmentError('Уже выполняется проверка модели', 409)
    try:
        selected = model_pool(model_id)[0]
        mid = selected['id']
        status = probe_status(mid)
        if status['status'] != 'unknown':
            with probe_cache_lock:
                return dict(probe_cache[(str(LIFE_DIR), mid)], cached=True)
        run = dict(stop=threading.Event(), proc=None)
        started = time.monotonic()
        actual, error = None, None
        def capture_model(value):
            nonlocal actual
            actual = value
        run['meta'] = capture_model
        try:
            model = mid.split(':', 1)[1] if ':' in mid else 'auto'
            messages = [dict(role='user', content='Ответь одним словом: ок')]
            if selected['route'] == 'free':
                _, actual = call_gateway(model, messages, run, lambda text: None, timeout=60)
            else:
                _, actual = call_process(selected['route'], messages, AI_SYSTEM, run, lambda text: None, model, timeout=60)
        except AIError as e:
            error = str(e)
        except Exception:
            error = 'Проверка модели не удалась'
        finally:
            stop_run(run)
        result = dict(ok=error is None, actual_model=actual, transport=route_name(selected['route']), ms=int((time.monotonic()-started)*1000), error=error, at=time.time())
        with probe_cache_lock:
            saved = read_json(LIFE_DIR / 'ai/probes.json')
            saved = saved if isinstance(saved, dict) else {}
            saved[mid] = result
            folder = LIFE_DIR / 'ai'
            folder.mkdir(parents=True, exist_ok=True)
            attachments.private_write(folder / 'probes.json', json.dumps(saved, ensure_ascii=False).encode())
            probe_cache[(str(LIFE_DIR), mid)] = result
        return result
    finally:
        probe_lock.release()


def chat_file(chat_id):
    if not CHAT_ID_RE.fullmatch(str(chat_id)):
        raise ValueError()
    return f"ai/{chat_id}.json"


def read_chat(chat_id):
    chat = read_json(path(chat_file(chat_id)))
    if not isinstance(chat, dict):
        raise KeyError()
    chat.setdefault("project", "")
    return chat


def save_chat(chat_id, chat):
    write_text(chat_file(chat_id), json.dumps(chat, ensure_ascii=False, indent=1))


def projects():
    return (read_json(path("ai/projects.json")) or {}).get("projects", [])


def save_projects(items):
    write_text("ai/projects.json", json.dumps({"projects": items}, ensure_ascii=False, indent=1))


def list_chats():
    out = []
    for f in path("ai").glob("*.json"):
        if not CHAT_ID_RE.fullmatch(f.stem):
            continue
        try:
            c = read_chat(f.stem)
            out.append({"id": f.stem, "title": c.get("title") or "Без названия", "updated": c.get("updated", ""), "project": c.get("project", ""), "count": len(c.get("messages") or [])})
        except (OSError, ValueError, KeyError):
            pass
    return sorted(out, key=lambda c: c["updated"], reverse=True)


def stop_run(run):
    run["stop"].set()
    proc = run.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def transcript(messages):
    return "Ответь на последнее сообщение владельца, только сам ответ.\n\n" + "\n\n".join(("Владелец" if m["role"] == "user" else "Ты") + ": " + m["content"] for m in messages)


def call_process(provider, messages, system, run, delta, model="auto", images=None, timeout=300):
    if model != "auto" and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}", model):
        raise AIError("Недопустимая модель")
    binary = claude_bin() if provider == "claude" else codex_bin()
    if not binary:
        raise AIError("Не найден " + ("Claude Code" if provider == "claude" else "Codex CLI"))
    with tempfile.TemporaryDirectory() as folder:
        output = Path(folder) / "answer"
        cmd = [binary, "-p", "--tools", "", "--system-prompt", system, "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--no-session-persistence"] if provider == "claude" else [binary, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only", "--color", "never", "-o", str(output), "-"]
        if model != "auto":
            cmd[1:1] = ["--model=" + model] if provider == "claude" else []
            if provider == "chatgpt":
                cmd[2:2] = ["--model=" + model]
        images = images or []
        prompt = (system + "\n\n" if provider == "chatgpt" else "") + transcript(messages)
        if provider == 'claude' and images:
            cmd += ['--input-format', 'stream-json']
            content = [{'type': 'image', 'source': {'type': 'base64', 'media_type': img['mime'], 'data': base64.b64encode(Path(img['path']).read_bytes()).decode('ascii')}} for img in images]
            content.append({'type': 'text', 'text': prompt})
            prompt = json.dumps({'type': 'user', 'message': {'role': 'user', 'content': content}}, ensure_ascii=False) + '\n'
        elif provider == 'chatgpt':
            # Keep subscription auth, without loading operator MCP servers, plugins or hooks.
            cmd[2:2] = ['--ignore-user-config', '-c', 'web_search="disabled"']
            for feature in ('shell_tool', 'unified_exec', 'apps', 'plugins', 'hooks', 'multi_agent',
                            'multi_agent_v2', 'browser_use', 'computer_use', 'image_generation',
                            'view_image', 'goals', 'sleep_tool', 'tool_suggest', 'code_mode', 'code_mode_host'):
                cmd[2:2] = ['-c', 'features.' + feature + '=false']
            for n, img in enumerate(images):
                dest = Path(folder) / ('image-'+str(n)+Path(img['name']).suffix)
                shutil.copyfile(img['path'], dest)
                dest.chmod(0o600)
                cmd[2:2] = ['-i', str(dest)]
        answer, final, failed = "", "", False
        diagnostic = ""
        actual = None
        def actual_model(value):
            nonlocal actual
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._:/-]{1,100}', value) and not SECRET_RE.search(value) and value != actual:
                actual = value
                if run.get('meta'):
                    run['meta'](actual)
        with tempfile.TemporaryFile(mode="w+") as stderr:
            proc = None
            timer = None
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True, cwd=folder)
                run["proc"] = proc
                expired = threading.Event()
                def expire():
                    expired.set()
                    proc.kill()
                timer = threading.Timer(timeout, expire)
                timer.start()
                if run["stop"].is_set():
                    stop_run(run)
                try:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
                for line in proc.stdout:
                    if run["stop"].is_set():
                        break
                    if provider == "chatgpt":
                        diagnostic = (diagnostic + line)[-8000:]
                        match = re.match(r'^model:\s*(\S+)', line)
                        if match:
                            actual_model(match[1])
                    if provider == "claude":
                        try:
                            obj = json.loads(line)
                            if obj.get('type') == 'system' and obj.get('subtype') == 'init':
                                actual_model(obj.get('model'))
                            event = obj.get("event", {})
                            d = event.get("delta", {})
                            if obj.get("type") == "stream_event" and event.get("type") == "content_block_delta" and d.get("type") == "text_delta":
                                answer += d.get("text", "")
                                delta(d.get("text", ""))
                            if obj.get("type") == "result":
                                final, failed = str(obj.get("result") or ""), bool(obj.get("is_error"))
                        except (ValueError, AttributeError):
                            pass
                proc.wait()
                if provider == "chatgpt":
                    final = output.read_text() if output.exists() else ""
                stderr.seek(0)
                error_output = stderr.read()
                if provider == 'chatgpt':
                    match = re.search(r'^model:\s*(\S+)', error_output, re.M)
                    if match:
                        actual_model(match[1])
                reason = final + "\n" + diagnostic + "\n" + error_output
                if run["stop"].is_set():
                    return answer, actual
                if expired.is_set():
                    raise AIError('Превышено время ответа модели')
                if proc.returncode or failed or not (final or answer).strip():
                    # Ищем «лимит» только в неудачном запуске и в хвосте вывода: в начале Codex
                    # повторяет промпт, а в удачном ответе слово может встретиться по делу.
                    if re.search(r"limit|quota|usage|лимит", reason[-3000:], re.I):
                        (claude_pause if provider == "claude" else chatgpt_pause)["until"] = time.time() + CLAUDE_PAUSE
                        raise AIError(("Claude" if provider == "claude" else "ChatGPT") + ": лимит подписки исчерпан")
                    login = re.search(r"log.?in|auth|sign.?in", reason[-3000:], re.I)
                    # CLI diagnostics can contain credentials; expose only controlled reasons.
                    raise AIError(("Claude" if provider == "claude" else "Codex") + (": требуется вход" if login else ": ошибка выполнения или пустой ответ"))
                if not answer:
                    answer = final
                    delta(answer)
                return answer, actual
            except OSError:
                raise AIError("Не удалось запустить " + provider)
            finally:
                if timer:
                    timer.cancel()
                if proc:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()
                    if proc.stdout:
                        proc.stdout.close()
                    if proc.stdin and not proc.stdin.closed:
                        proc.stdin.close()
                run["proc"] = None


def call_gateway(model, messages, run, delta, timeout=180):
    body = json.dumps({"model": model, "messages": messages, "stream": True}).encode()
    req = urllib.request.Request(FREE_URL + "/chat/completions", data=body, headers={"Authorization": f"Bearer {free_key()}", "Content-Type": "application/json"})
    answer, actual = "", None
    timer = None
    expired = threading.Event()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            if sock is not None:
                def expire():
                    expired.set()
                    try:
                        sock.shutdown(2)
                    except OSError:
                        pass
                timer = threading.Timer(timeout, expire)
                timer.start()
            for raw in response:
                if run["stop"].is_set():
                    break
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                obj = json.loads(payload)
                value = obj.get('model')
                if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._:/-]{1,100}', value) and not SECRET_RE.search(value) and actual != value:
                    actual = value
                    if run.get('meta'):
                        run['meta'](actual)
                choices = obj.get("choices") or []
                chunk = (choices[0].get("delta", {}).get("content") or "") if choices else ""
                if isinstance(chunk, str) and chunk:
                    answer += chunk
                    delta(chunk)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AIError("Шлюз freellmapi отклонил ключ.")
        raise AIError(f"Шлюз freellmapi: ошибка {e.code}")
    except OSError:
        raise AIError(FREE_DOWN)
    except (ValueError, KeyError, TypeError):
        raise AIError("Шлюз freellmapi: непонятный ответ")
    finally:
        if timer:
            timer.cancel()
    if expired.is_set():
        raise AIError('Превышено время ответа модели')
    if not answer.strip() and not run["stop"].is_set():
        raise AIError("Шлюз freellmapi: пустой ответ")
    return answer, actual


def ai_send(chat_id, model_id, text, project, emit, attachments_ids=None, **kwargs):
    # Accept the public attachments keyword without shadowing the storage module.
    ids = kwargs.pop('attachments', attachments_ids if attachments_ids is not None else [])
    if kwargs:
        raise ValueError()
    pool = model_pool(model_id)
    store = attachments.Store(LIFE_DIR)
    new = not chat_id
    if new:
        chat_id = secrets.token_hex(6)
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    with ai_lock:
        if new:
            if project and not any(p['id'] == project for p in projects()):
                raise ValueError()
            chat = dict(title=one_line(text, 60) or 'Вложения', created=stamp, updated=stamp, project=project, messages=[])
        else:
            chat = read_chat(chat_id)
        history = list(chat.get('messages') or [])
        pr = next((p for p in projects() if p['id'] == chat.get('project')), None)
    with store.reserve(ids, chat_id, pool[0]) as files:
        system = (f"Инструкции проекта «{pr['name']}»: {pr['instructions']}\n\n" if pr and pr.get('instructions') else '') + AI_SYSTEM
        messages, size = [], len(text)
        for m in reversed(history):
            content = m.get('text', '') + ''.join('\n[вложение: '+a['name']+']' for a in m.get('attachments', []))
            size += len(content)
            if size > AI_HISTORY_CHARS:
                break
            messages.insert(0, dict(role=m['role'], content=content))
        messages.append(dict(role='user', content=text + store.prompt(files)))
        images = [dict(m, path=str(store.file(m['id']))) for m in files if m['kind'] == 'image']
        # Auto may only fall back to models capable of handling the same request.
        pool = [m for m in pool if not images or m['caps']['image']]
        run_id = secrets.token_hex(12)
        now = time.time()
        run = dict(proc=None, stop=threading.Event(), started=now, updated=now,
                   provider={'claude': 'Claude', 'chatgpt': 'ChatGPT', 'free': 'Бесплатная'}[pool[0]['route']], phase='waiting', chat=chat_id)
        with runs_lock:
            ai_runs[run_id] = run
        errors, answer, actual, fallback = [], '', None, ''
        selected = pool[0]
        def send(obj):
            try:
                emit(obj)
            except OSError:
                stop_run(run)
        def delta(chunk):
            nonlocal answer
            answer += chunk
            if chunk:
                with runs_lock:
                    run.update(phase='streaming', updated=time.time())
            send(dict(type='delta', text=chunk))
        def meta(value):
            nonlocal actual
            actual = value
            send(dict(type='meta', actual_model=value))
        run['meta'] = meta
        try:
            for n, selected in enumerate(pool):
                if run['stop'].is_set():
                    break
                mid, provider = selected['id'], selected['route']
                model = mid.split(':', 1)[1] if ':' in mid else 'auto'
                transport = route_name(provider)
                fallback = 'резерв: '+selected['title']+', '+errors[-1] if n else ''
                with runs_lock:
                    run.update(provider={'claude':'Claude', 'chatgpt':'ChatGPT', 'free':'Бесплатная'}[provider], phase='waiting', updated=time.time())
                send(dict(type='switch' if n else 'start', chat=chat_id, run=run_id, id=mid, title=selected['title'], selected_id=mid, selected_title=selected['title'], model=selected['title'], transport=transport, detail=transport, reason=fallback, mode='chat'))
                answer, actual = '', None
                try:
                    if not selected['available']:
                        raise AIError(selected['why']+'; автоматическая замена отключена')
                    if provider == 'free':
                        result, value = call_gateway(model, [dict(role='system', content=system)] + messages, run, delta)
                    else:
                        result, value = call_process(provider, messages, system, run, delta, model, images=images)
                    if value and value != actual:
                        meta(value)
                    if not answer and result:
                        delta(result)
                    break
                except AIError as e:
                    errors.append(str(e))
                    if run['stop'].is_set():
                        break
                    answer = ''
            stopped = run['stop'].is_set()
            if not answer and not stopped:
                send(dict(type='error', error=' · '.join(errors) or 'Ответа нет'))
                return
            with ai_lock:
                if not new:
                    chat = read_chat(chat_id)
                chat['messages'].append(dict(role='user', text=text, time=stamp, attachments=[store.public(m) for m in files]))
                if answer:
                    chat['messages'].append(dict(role='assistant', text=answer, time=datetime.now().strftime('%Y-%m-%d %H:%M'), model=selected['title'], model_id=selected['id'], actual_model=actual, transport=route_name(selected['route']), mode='chat', auto=model_id == 'auto', fallback=fallback, stopped=stopped))
                chat['updated'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                previous = [m['chat'] for m in files]
                store.attach(files, chat_id)
                try:
                    save_chat(chat_id, chat)
                except Exception:
                    for m, old in zip(files, previous):
                        m['chat'] = old
                        store.save_meta(m)
                    raise
            send(dict(type='done', chat=dict(chat, id=chat_id), errors=errors, fallback=fallback))
        finally:
            stop_run(run)
            with runs_lock:
                ai_runs.pop(run_id, None)


# --- HTTP ---------------------------------------------------------------

class Conflict(Exception):
    pass


def replace_line(name, line_no, expected, new_line):
    """Change one line, but only if it is still the line the page saw."""
    lines = read_text(name).splitlines()
    if line_no >= len(lines) or lines[line_no] != expected:
        raise Conflict()
    if new_line is None:
        del lines[line_no]
    else:
        lines[line_no] = new_line
    write_text(name, "\n".join(lines) + ("\n" if lines else ""))


def append_line(name, line):
    text = read_text(name)
    if text and not text.endswith("\n"):
        text += "\n"
    write_text(name, text + line + "\n")



def load_token():
    try:
        t = TOKEN_FILE.read_text().strip()
    except OSError:
        return None
    return t if len(t) >= 24 else None


def cookie_value(token):
    return hmac.new(token.encode(), b"svodka-session", hashlib.sha256).hexdigest()


LOGIN_PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Сводка</title>
<body style="font-family:-apple-system,system-ui,sans-serif;background:#eef0f5;color:#1c1c22;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0">
<div style="max-width:320px;text-align:center;padding:24px"><h2>Сводка</h2>
<p>Нужна личная ссылка доступа. Открой «Сводку» по ссылке, которую прислал помощник в Telegram.</p></div>"""

class Handler(BaseHTTPRequestHandler):
    server_version = "Svodka/1"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


    def same_origin(self, required=False):
        origin = self.headers.get('Origin')
        if origin is None:
            return not required
        try:
            u = urlsplit(origin)
            expected = urlsplit(u.scheme + '://' + self.headers.get('Host', ''))
            return bool(u.scheme in ('http', 'https') and u.hostname and not u.username and not u.password
                        and not u.path and not u.query and not u.fragment and u.hostname == expected.hostname
                        and (u.port or (443 if u.scheme == 'https' else 80)) == (expected.port or (443 if u.scheme == 'https' else 80)))
        except ValueError:
            return False

    def talk_start(self):
        def fail(code, msg):
            self.send_json(code, {'error': msg})
        if not self.same_origin(required=True):
            return fail(403, 'Источник запроса не совпадает с панелью')
        if not self.agent_protected():
            return fail(503, 'Голос запрещён: требуется защита панели')
        node = shutil.which('node') or '/usr/bin/node'
        sdk = os.environ.get('SVODKA_OPENCLAW_SDK') or str(Path.home() / '.npm-global/lib/node_modules/openclaw/dist/plugin-sdk/gateway-runtime.js')
        token = os.environ.get('SVODKA_AGENT_TOKEN_FILE', '')
        if not (Path(node).is_file() and Path(sdk).is_file() and token and TALK_HELPER.is_file()):
            return fail(503, 'Живой голос не настроен на сервере')
        try:
            key = agent_service.canonical() or 'main'
        except Unavailable:
            key = 'main'
        talk_end()  # one call at a time: a new tap replaces an abandoned call
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(Path.home()), 'TOKEN_FILE': token, 'OPENCLAW_SDK': sdk,
               'SESSION_KEY': key, 'GATEWAY_URL': os.environ.get('SVODKA_AGENT_URL', 'http://127.0.0.1:18789/')}
        try:
            proc = subprocess.Popen([node, str(TALK_HELPER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=str(TALK_HELPER.parent))
        except OSError:
            return fail(503, 'Не удалось запустить живой голос')
        call = {'proc': proc, 'lines': queue.Queue()}
        threading.Thread(target=talk_reader, args=(proc, call['lines']), daemon=True).start()
        msg = talk_next(call, 20)
        if not msg or not msg.get('ready'):
            proc.kill()
            return fail(502, (msg or {}).get('error') or 'Джарвис не открыл голосовую сессию')
        sid = secrets.token_hex(12)
        with talk_lock:
            talk_calls[sid] = call
        self.send_json(200, {'id': sid, 'model': model_name_safe(msg.get('model')), 'voice': model_name_safe(msg.get('voice'))})

    def talk_offer(self):
        def fail(code, msg):
            self.send_json(code, {'error': msg})
        if not self.same_origin(required=True):
            return fail(403, 'Источник запроса не совпадает с панелью')
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/sdp':
            return fail(415, 'Требуется application/sdp')
        sid = parse_qs(urlsplit(self.path).query).get('id', [''])[0]
        with talk_lock:
            call = talk_calls.get(sid)
        if not call or call['proc'].poll() is not None:
            return fail(410, 'Голосовая сессия устарела, начните заново')
        try:
            n = int(self.headers.get('Content-Length', ''))
        except ValueError:
            return fail(400, 'Неверная длина')
        if not 10 <= n <= 64 * 1024:
            return fail(413, 'Неверный размер SDP')
        sdp = self.rfile.read(n).decode('utf-8', 'replace')
        try:
            call['proc'].stdin.write((json.dumps({'offer': sdp}) + '\n').encode())
            call['proc'].stdin.flush()
        except (OSError, ValueError):
            return fail(502, 'Голосовая сессия закрылась')
        msg = talk_next(call, 25)
        if not msg or not isinstance(msg.get('answer'), str):
            talk_end(sid)
            return fail(502, (msg or {}).get('error') or 'Голосовой сервер не ответил')
        answer = msg['answer'].encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/sdp')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def talk_stop(self):
        if not self.same_origin(required=True):
            return self.send_json(403, {'error': 'Источник запроса не совпадает с панелью'})
        talk_end(parse_qs(urlsplit(self.path).query).get('id', [''])[0] or None)
        self.send_json(200, {'ok': True})

    def voice_status(self):
        limits = dict(max_bytes=VOICE_MAX_BYTES, max_seconds=60, vad=True, max_utterance_seconds=30, session_max_minutes=15,
                      realtime=False, realtime_reason='Нативный realtime speech-to-speech не подключён: нужен платный провайдер')
        binary = None
        candidate = os.environ.get('SVODKA_OPENCLAW_BIN', '')
        if candidate and Path(candidate).is_absolute() and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            binary = candidate
        else:
            binary = shutil.which('openclaw')
            fallback = Path.home() / '.npm-global/bin/openclaw'
            if not binary and fallback.is_file():
                binary = str(fallback)
        if os.environ.get('SVODKA_VOICE_ENABLED') != '1':
            reason = 'Распознавание отключено: требуется разрешение оператора SVODKA_VOICE_ENABLED=1'
        elif not self.agent_protected():
            reason = 'Распознавание запрещено: требуется защита панели токеном или доверенным прокси'
        elif not binary:
            reason = 'Исполняемый файл OpenClaw не найден'
        else:
            tts, tts_reason = tts_status()
            return dict(stt=True, reason='Распознавание через OpenClaw · ' + tts_reason, tts=tts, tts_reason=tts_reason, **limits), binary
        return dict(stt=False, reason=reason, tts=False, tts_reason=reason, **limits), binary

    def speak(self):
        def fail(code, msg):
            self.send_json(code, {'error': msg})
        if not self.same_origin(required=True):
            return fail(403, 'Источник запроса не совпадает с панелью')
        if self.headers.get('X-Svodka-Voice') != '1':
            return fail(403, 'Требуется X-Svodka-Voice: 1')
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
            return fail(415, 'Требуется application/json')
        if not self.agent_protected():
            return fail(503, 'Озвучка запрещена: требуется защита панели')
        try:
            b = self.body()
            text = b.get('text') if isinstance(b, dict) else None
        except (ValueError, KeyError, TypeError):
            text = None
        if not isinstance(text, str) or not text.strip() or len(text) > 600:
            return fail(400, 'Нужна фраза до 600 символов')
        ok, reason = tts_status()
        if not ok:
            return fail(503, reason)
        if not tts_semaphore.acquire(timeout=25):
            return fail(429, 'Озвучка занята')
        try:
            audio = eleven_speak(text.strip())
        finally:
            tts_semaphore.release()
        if not audio:
            return fail(503, tts_status()[1])
        self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)

    def transcribe(self):
        def fail(code, msg):
            self.send_json(code, {'error': msg})
        if not self.same_origin(required=True):
            return fail(403, 'Источник запроса не совпадает с панелью')
        if self.headers.get('X-Svodka-Voice') != '1':
            return fail(403, 'Требуется X-Svodka-Voice: 1')
        state, binary = self.voice_status()
        if not state['stt']:
            return fail(503, state['reason'])
        extensions = {'audio/webm':'webm', 'audio/mp4':'m4a', 'audio/x-m4a':'m4a', 'audio/aac':'aac', 'audio/ogg':'ogg', 'audio/wav':'wav', 'audio/x-wav':'wav', 'audio/mpeg':'mp3'}
        mime = self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
        if mime not in extensions:
            return fail(415, 'Неподдерживаемый формат аудио')
        try:
            n = int(self.headers.get('Content-Length', ''))
        except ValueError:
            return fail(400, 'Неверная длина аудио')
        if not 1024 <= n <= VOICE_MAX_BYTES:
            return fail(413, 'Размер аудио должен быть от 1024 байт до 10 МиБ')
        audio = self.rfile.read(n)
        if len(audio) != n:
            return fail(400, 'Аудио получено не полностью')
        if not voice_semaphore.acquire(blocking=False):
            return fail(429, 'Распознавание уже идёт')
        try:
            with tempfile.TemporaryDirectory(prefix='svodka-voice-') as directory:
                f = Path(directory) / ('audio.' + extensions[mime])
                fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as out:
                    out.write(audio)
                # OpenClaw returns no transcript for WebM/Ogg (Chrome, Android): hand it WAV instead.
                ffmpeg = shutil.which('ffmpeg')
                if extensions[mime] in ('webm', 'ogg') and ffmpeg:
                    wav = Path(directory) / 'audio.wav'
                    try:
                        subprocess.run([ffmpeg, '-loglevel', 'error', '-nostdin', '-i', str(f), '-ac', '1', '-ar', '16000', str(wav)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=directory, timeout=30, check=True)
                        f = wav
                    except (subprocess.SubprocessError, OSError):
                        pass
                try:
                    result = subprocess.run([binary, 'infer', 'audio', 'transcribe', '--file', str(f), '--language', 'ru', '--json'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=directory, timeout=VOICE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    return fail(504, 'Распознавание не уложилось в 90 с')
                except OSError:
                    return fail(503, 'Не удалось запустить OpenClaw')
                stdout = result.stdout.decode('utf-8', errors='replace')
                stderr = result.stderr.decode('utf-8', errors='replace')
                try:
                    obj = json.loads(stdout)
                except ValueError:
                    obj = {}
                if not isinstance(obj, dict):
                    obj = {}
                outputs = obj.get('outputs')
                first = outputs[0] if isinstance(outputs, list) and outputs and isinstance(outputs[0], dict) else {}
                text = first.get('text')
                if obj.get('ok') is True and isinstance(text, str) and text.strip():
                    return self.send_json(200, dict(text=text[:20000], provider=one_line(obj.get('provider'), 60), model=one_line(obj.get('model'), 100)))
                if 'No audio transcription provider' in stdout + stderr:
                    return fail(502, 'Провайдер распознавания речи в OpenClaw не настроен или не готов')
                lines = [line for line in stderr.splitlines() if line.strip()]
                detail = lines[-1] if lines else str(obj.get('error') or 'Некорректный ответ OpenClaw')
                detail = detail.replace(directory, '<аудио>')
                detail = SECRET_RE.sub('***', detail)
                return fail(502, 'Ошибка распознавания: ' + one_line(detail, 200))
        finally:
            voice_semaphore.release()

    def agent_protected(self):
        return bool(load_token()) or os.environ.get("SVODKA_AGENT_TRUSTED_PROXY") == "1"

    def voice_diagnostic(self, route):
        if not self.same_origin(required=True):
            return self.send_json(403, {'error': 'Источник запроса не совпадает с панелью'})
        if route == '/api/voice/metrics' and self.headers.get('X-Svodka-Voice') != '1':
            return self.send_json(403, {'error': 'Требуется X-Svodka-Voice: 1'})
        if not self.agent_protected():
            return self.send_json(503, {'error': 'Требуется защита панели токеном или доверенным прокси'})
        try:
            length = int(self.headers.get('Content-Length', ''))
            if not 0 < length <= 2048:
                return self.send_json(413, {'error': 'Допустимо не более 2 КиБ'})
            b = self.body()
            if route == '/api/voice/metrics':
                if not isinstance(b, dict):
                    raise ValueError()
                return self.send_json(200, voice_metrics(b))
            if not isinstance(b, dict) or set(b) - {'stage', 'message', 'ua', 't'} or any(not isinstance(b.get(k, ''), str) for k in ('stage', 'message', 'ua')):
                raise ValueError()
            t = b.get('t', 0)
            if type(t) not in (int, float) or not 0 <= t <= 1e15 or not math.isfinite(t):
                raise ValueError()
            row = {k: diagnostic_text(b.get(k, ''), n) for k, n in [('stage', 60), ('message', 300), ('ua', 200)]}
            row['t'] = t
            line = json.dumps(row, ensure_ascii=False)
            with client_log_lock:
                now = time.monotonic()
                client_log_times[:] = [t for t in client_log_times if now-t < 60]
                if len(client_log_times) >= 20:
                    return self.send_json(429, {'error': 'Не более 20 сообщений в минуту'})
                client_log_times.append(now)
                agent_service.root.mkdir(parents=True, exist_ok=True)
                target = agent_service.root / 'client-log.jsonl'
                old = target.read_text(encoding='utf-8').splitlines()[-199:] if target.exists() else []
                fd, temporary = tempfile.mkstemp(dir=agent_service.root, prefix='.client-log-')
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as f:
                        f.write('\n'.join(old + [line]) + '\n')
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                print('svodka client-log ' + line, file=sys.stderr, flush=True)
            self.send_json(200, {'ok': True})
        except (ValueError, TypeError):
            self.send_json(400, {'error': 'Неверные данные'})
        except OSError:
            self.send_json(503, {'error': 'Диагностика временно недоступна'})

    def agent_snapshot(self):
        state = agent_service.snapshot()
        if not self.agent_protected():
            state.update(available=False, reason="Запуск запрещён: требуется защита панели токеном или явное доверие защищённому прокси")
        return state

    def authorize(self):
        """Returns (allowed, set_cookie_value). Open when there is no token file."""
        token = load_token()
        if token is None:
            return True, None
        query = parse_qs(urlsplit(self.path).query)
        given = (query.get("k") or [""])[0]
        if given:
            if hmac.compare_digest(given.encode(), token.encode()):
                return True, cookie_value(token)
            time.sleep(1)
            return False, None
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and hmac.compare_digest(value.encode(), cookie_value(token).encode()):
                return True, None
        return False, None

    def deny(self):
        if urlsplit(self.path).path in ("/", "/index.html"):
            data = LOGIN_PAGE.encode()
            self.send_response(401)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_json(401, {"error": "нужна ссылка доступа"})

    def end_headers_cookie(self, cookie):
        if cookie:
            self.send_header("Set-Cookie", f"{COOKIE}={cookie}; Path=/; Max-Age=31536000; HttpOnly; Secure; SameSite=Lax")

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        maximum = 512 * 1024 if urlsplit(getattr(self,'path','')).path.startswith('/api/workspace/') else 64 * 1024
        if not 0 <= length <= maximum:
            raise ValueError("too large")
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result
        def invalid_constant(value):
            raise ValueError("invalid JSON constant")
        return json.loads(self.rfile.read(length) or b"{}", object_pairs_hook=unique_object,
                          parse_constant=invalid_constant)

    def workspace_route(self, route, post=False):
        if not self.agent_protected():
            return self.send_json(503, {'error':'Требуется защита панели токеном или доверенным прокси'})
        workspace = _workspace_module.Workspace(agent_service, SECRET_RE)
        # Persistent adapter owns the shared tail cache and revision lock.
        global workspace_service
        with lock:
            if workspace_service is None or workspace_service.agent is not agent_service:
                workspace_service = workspace
            workspace = workspace_service
        if post:
            if self.headers.get('Content-Type','').split(';',1)[0].strip().lower() != 'application/json':
                return self.send_json(415, {'error':'Требуется application/json'})
            if not self.same_origin(required=True):
                return self.send_json(403, {'error':'Источник запроса не совпадает с панелью'})
        status = workspace.status()
        if route == '/api/workspace' and not post:
            sessions, tasks, error = [], [], None
            if status['available']:
                state = self.agent_snapshot()
                queue = queue_snapshot(os.environ.get('SVODKA_CODER_QUEUE_FILE'))
                try:
                    sessions = workspace.sessions()
                except ValueError as e:
                    error = str(e)  # dashboard runs and coder queue stay visible without Gateway sessions
                tasks = workspace.tasks(state, queue, sessions)
            try:
                key = agent_service.canonical()
            except Unavailable:
                key = None
            payload = dict(status=status, session={'canonical':key is not None,'key':key}, sessions=sessions, tasks=tasks, hidden_subagents=workspace.visible_sessions(sessions)[1])
            if error:
                payload['error'] = error
            return self.send_json(200,payload)
        if not status['available']:
            return self.send_json(503, {'error':status['reason']})
        try:
            if not post and route == '/api/workspace/history':
                q = parse_qs(urlsplit(self.path).query)
                return self.send_json(200, workspace.history(int(q.get('offset',['0'])[0]), int(q.get('limit',['30'])[0])))
            b = self.body() if post else {}
            if not isinstance(b,dict):
                raise ValueError('Неверные данные')
            if route == '/api/workspace/send' and post:
                text, request = b.get('text'), b.get('request')
                if not isinstance(text,str) or not 0<len(text.strip())<=20000 or not isinstance(request,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}',request):
                    raise ValueError('Неверные данные')
                rid = agent_service.submit('[Сводка:'+request+'] '+text.strip(),request)
                duplicate = getattr(rid,'duplicate',False)
                return self.send_json(200 if duplicate else 202,dict(id=int(rid),duplicate=duplicate))
            if route == '/api/workspace/revision' and post:
                return self.send_json(201,workspace.create_revision(b.get('target'),b.get('original'),b.get('text')))
            if route == '/api/workspace/revision/send' and post:
                if b.get('confirm') is not True or type(b.get('id')) is not int:
                    raise ValueError('Требуется confirm:true и id черновика')
                result = workspace.send_revision(b['id'])
                return self.send_json(200 if result['duplicate'] else 202,result)
            return self.send_json(404,{'error':'not found'})
        except Unavailable as e:
            return self.send_json(503,{'error':str(e)})
        except (ValueError, TypeError) as e:
            return self.send_json(400 if post else 503,{'error':str(e) if isinstance(e,ValueError) else 'Неверные данные'})

    def do_GET(self):
        route = urlsplit(self.path).path
        if route == "/apple-touch-icon.png" or route == "/icon.png":
            try:
                data = ICON.read_bytes()
            except OSError:
                self.send_json(404, {"error": "not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        allowed, cookie = self.authorize()
        if not allowed:
            self.deny()
            return
        if route.startswith("/api/workspace"):
            return self.workspace_route(route)
        if route == "/manifest.webmanifest":
            token = load_token()
            start = "/?k=" + token if token else "/"
            manifest = {"name": "Сводка", "short_name": "Сводка", "start_url": start, "scope": "/",
                        "display": "standalone", "background_color": "#eef0f5", "theme_color": "#eef0f5",
                        "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png"}]}
            body = json.dumps(manifest, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif route in ("/", "/index.html"):
            data = STATIC.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers_cookie(cookie)
            self.end_headers()
            self.wfile.write(data)
        elif route == "/api/data":
            with lock:
                payload = {
                    "expenses": read_expenses(),
                    "tasks": {k: read_tasks(f) for k, f in TASK_FILES.items()},
                    "notes": {k: read_notes(f) for k, f in NOTE_FILES.items()},
                    "food": read_food(),
                    "memory": read_memory(),
                    "settings": read_settings(),
                    "today": datetime.now().strftime("%Y-%m-%d"),
                }
            self.send_json(200, payload)
        elif route == '/api/memory/status':
            with lock:
                payload = memory_status()
            if parse_qs(urlsplit(self.path).query).get('probe') == ['1']:
                workspace = _workspace_module.Workspace(agent_service, SECRET_RE)
                state = workspace.status()
                if not self.agent_protected() or not state['available']:
                    payload['tools'] = dict(probe=False, reason=state['reason'] if not state['available'] else 'Требуется защита панели токеном или доверенным прокси')
                else:
                    payload['tools'] = dict(probe=True)
                    for tool, args in (('memory_get', dict(path='MEMORY.md', **{'from':1}, lines=1)),
                                       ('sessions_history', dict(sessionKey=agent_service.canonical(), limit=1, includeTools=False))):
                        started = time.monotonic()
                        error = None
                        try:
                            workspace.gateway_invoke(tool, args)  # content is discarded, only reachability is reported
                        except Exception as e:
                            # Only the adapter's fixed diagnostics (401/403/404/offline); never Gateway text.
                            known = ('Шлюз отклонил авторизацию', 'Политика шлюза запретила инструмент',
                                     'Инструмент недоступен по политике шлюза', 'Ошибка HTTP шлюза',
                                     'Шлюз недоступен или вернул некорректный ответ')
                            error = str(e) if str(e) in known else 'Инструмент недоступен'
                        payload['tools'][tool] = dict(ok=error is None, error=error, ms=int((time.monotonic()-started)*1000))
            self.send_json(200, payload)
        elif route == "/api/voice":
            self.send_json(200, self.voice_status()[0])
        elif route == "/api/voice/metrics":
            self.send_json(200, voice_metrics())
        elif route == "/api/agent":
            self.send_json(200, self.agent_snapshot())
        elif route == "/api/office":
            state = self.agent_snapshot()
            filename = os.environ.get('SVODKA_CODER_QUEUE_FILE')
            logs_dir = os.environ.get('SVODKA_CODER_LOGS_DIR') or (str(Path(filename).parent / 'logs') if filename else '')
            queue = queue_snapshot(filename, lambda row: coder_metadata(row, logs_dir))
            with runs_lock:
                chats = {key: {k: row.get(k) for k in ('started', 'updated', 'provider', 'phase')} for key, row in ai_runs.items()}
            self.send_json(200, {"server_time": time.time(), "sessions": office_sessions(state, queue, chats),
                                 "agent": {k: state[k] for k in ('available', 'reason')},
                                 "runs": [{k:r[k] for k in ("id", "status", "started", "updated")} for r in state["runs"]],
                                 "events": state["events"], "queue": queue})
        elif route == "/api/office/events":
            # Public work feed of one coder task: tool steps and visible replies only.
            key = parse_qs(urlsplit(self.path).query).get('key', [''])[0]
            m = re.fullmatch(r'coder:([A-Za-z0-9_-]{1,64})', key)
            filename = os.environ.get('SVODKA_CODER_QUEUE_FILE')
            if not m or not filename:
                return self.send_json(404, {"error": "Лента есть только у задач кодера"})
            logs_dir = os.environ.get('SVODKA_CODER_LOGS_DIR') or str(Path(filename).parent / 'logs')
            queue = queue_snapshot(filename)
            job = next((j for j in queue.get('jobs', []) if str(j.get('id')) == m.group(1)), None)
            if job is None:
                return self.send_json(404, {"error": "Задача не найдена в очереди"})
            feed = None if job['status'] == 'queued' else coder_feed(m.group(1), logs_dir)
            self.send_json(200, {"server_time": time.time(), "key": key, "status": job['status'],
                                 "available": feed is not None or job['status'] == 'queued',
                                 **(feed or {"model": None, "events": [], "log_at": None})})
        elif route == "/api/ai/models":
            self.send_json(200, models_payload())
        elif route == "/api/ai/projects":
            with ai_lock:
                self.send_json(200, {"projects": projects()})
        elif route == "/api/ai/chats":
            canonical = _workspace_module.Workspace(agent_service, SECRET_RE).status()
            if not self.agent_protected():
                canonical = dict(available=False, reason='Требуется защита панели токеном или доверенным прокси')
            with ai_lock:
                self.send_json(200, {"chats": list_chats(), 'canonical': dict(canonical, key_label='Telegram · основной диалог')})
        elif route == '/api/ai/file':
            try:
                fid = parse_qs(urlsplit(self.path).query).get('id', [''])[0]
                body, headers = attachments.Store(LIFE_DIR).download(fid)
                self.send_response(200)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except attachments.AttachmentError as e:
                self.send_json(e.status, {'error': str(e)})
        elif route == "/api/ai/chat":
            try:
                chat_id = (parse_qs(urlsplit(self.path).query).get("id") or [""])[0]
                with ai_lock:
                    self.send_json(200, {"id": chat_id, **read_chat(chat_id)})
            except (ValueError, KeyError):
                self.send_json(404, {"error": "Диалог не найден"})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        allowed, _ = self.authorize()
        if not allowed:
            self.deny()
            return
        if urlsplit(self.path).path.startswith('/api/ai/'):
            if not self.same_origin():
                return self.send_json(403, {'error': 'Источник запроса не совпадает с панелью'})
            if urlsplit(self.path).path == '/api/ai/upload':
                return self.ai_upload()
            if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                return self.send_json(415, {'error': 'Требуется application/json'})
        if urlsplit(self.path).path.startswith('/api/workspace'):
            return self.workspace_route(urlsplit(self.path).path, True)
        if urlsplit(self.path).path in ('/api/voice/metrics', '/api/client-log'):
            self.voice_diagnostic(urlsplit(self.path).path)
            return
        if urlsplit(self.path).path == "/api/voice/transcribe":
            self.transcribe()
            return
        if urlsplit(self.path).path == "/api/voice/speak":
            self.speak()
            return
        if urlsplit(self.path).path == "/api/jarvis/talk":
            self.talk_start()
            return
        if urlsplit(self.path).path == "/api/jarvis/offer":
            self.talk_offer()
            return
        if urlsplit(self.path).path == "/api/jarvis/end":
            self.talk_stop()
            return
        if urlsplit(self.path).path == "/api/agent/send":
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self.send_json(415, {"error": "Требуется application/json"})
                return
            if not self.same_origin():
                self.send_json(403, {"error": "Источник запроса не совпадает с панелью"})
                return
        try:
            b = self.body()
            if urlsplit(self.path).path == "/api/agent/send":
                if not isinstance(b, dict) or set(b) not in ({"text"}, {"text", "request"}) or not isinstance(b["text"], str) or not 0 < len(b["text"].strip()) <= 20000:
                    raise ValueError()
                if 'request' in b and (not isinstance(b['request'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', b['request'])):
                    raise ValueError()
                try:
                    if not self.agent_protected():
                        raise Unavailable("Запуск запрещён: панель не защищена токеном; требуется отдельное доверие защищённому прокси")
                    rid = agent_service.submit(b["text"].strip(), b['request']) if 'request' in b else agent_service.submit(b["text"].strip())
                    duplicate = getattr(rid, 'duplicate', False)
                    self.send_json(200 if duplicate else 202, {"id": rid, **({"duplicate": True} if duplicate else {})})
                except Unavailable as e:
                    self.send_json(503, {"error": str(e)})
                return
            if self.path.startswith("/api/ai/"):
                self.ai_route(b)
                return
            with lock:
                self.route(b)
        except Conflict:
            self.send_json(409, {"error": "Запись уже изменилась. Обнови страницу."})
        except AIError as e:
            self.send_json(502, {"error": str(e)})
        except attachments.AttachmentError as e:
            self.send_json(e.status, {'error': str(e)})
        except (ValueError, KeyError, TypeError, StopIteration):
            self.send_json(400, {"error": "Неверные данные"})

    def ai_upload(self):
        try:
            length = int(self.headers.get('Content-Length', ''))
            if length < 0:
                raise ValueError()
            if length > attachments.MAX_BYTES:
                raise attachments.AttachmentError('Файл больше 10 МБ', 413)
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError()
            name = unquote(self.headers.get('X-File-Name', 'файл.txt'), errors='strict')
            self.send_json(201, attachments.Store(LIFE_DIR).upload(data, name))
        except attachments.AttachmentError as e:
            self.close_connection = True
            self.send_json(e.status, {'error': str(e)})
        except (ValueError, UnicodeError):
            self.close_connection = True
            self.send_json(400, {'error': 'Неверный файл или размер запроса'})

    def ai_route(self, b):
        p = self.path
        if not isinstance(b, dict):
            raise ValueError()
        if p == '/api/ai/probe':
            if not isinstance(b.get('model'), str):
                raise ValueError()
            self.send_json(200, ai_probe(b['model']))
        elif p == '/api/ai/file/delete':
            attachments.Store(LIFE_DIR).delete(b.get('id'))
            self.send_json(200, {'ok': True})
        elif p == "/api/ai/send":
            if not isinstance(b.get('text'), str) or len(b['text']) > 20000:
                raise ValueError()
            text = b['text'].strip()
            ids = b.get('attachments', [])
            if not text and not ids:
                raise ValueError()
            pool = model_pool(str(b.get('model') or 'auto'))
            attachments.Store(LIFE_DIR).validate(ids, b.get('chat') or '', pool[0])
            if b.get('chat'):
                read_chat(b['chat'])
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            def emit(obj):
                self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
                self.wfile.flush()
            try:
                ai_send(b.get("chat") or "", str(b.get("model") or "auto"), text, b.get("project") or "", emit, attachments=ids)
            except (AIError, ValueError, KeyError) as e:
                try:
                    emit({"type": "error", "error": str(e) if isinstance(e, (AIError, attachments.AttachmentError)) else "Не удалось отправить сообщение"})
                except OSError:
                    pass
        elif p == "/api/ai/stop":
            with runs_lock:
                run = ai_runs.get(b.get("run"))
                if run:
                    stop_run(run)
            self.send_json(200, {"ok": True})
        elif p in ("/api/ai/rename", "/api/ai/move"):
            with ai_lock:
                chat = read_chat(b["id"])
                if p.endswith("rename"):
                    title = str(b["title"]).strip()
                    if not 1 <= len(title) <= 80:
                        raise ValueError()
                    chat["title"] = title
                else:
                    project = b.get("project") or ""
                    if project and not any(x["id"] == project for x in projects()):
                        raise ValueError()
                    chat["project"] = project
                save_chat(b["id"], chat)
            self.send_json(200, {"ok": True})
        elif p == "/api/ai/project/save":
            name = str(b.get("name") or "").strip()
            description, instructions = str(b.get("description") or ""), str(b.get("instructions") or "")
            if not 1 <= len(name) <= 80 or len(description) > 500 or len(instructions) > 8000:
                raise ValueError()
            with ai_lock:
                items = projects()
                item = next((x for x in items if x["id"] == b.get("id")), None)
                if b.get("id") and not item:
                    raise KeyError()
                if item is None:
                    item = {"id": secrets.token_hex(6), "created": datetime.now().isoformat(timespec="seconds")}
                    items.append(item)
                item.update(name=name, description=description, instructions=instructions)
                save_projects(items)
            self.send_json(200, {"project": item})
        elif p == "/api/ai/project/delete":
            with ai_lock:
                items = projects()
                if not any(x["id"] == b["id"] for x in items):
                    raise KeyError()
                for c in list_chats():
                    if c["project"] == b["id"]:
                        chat = read_chat(c["id"])
                        chat["project"] = ""
                        save_chat(c["id"], chat)
                save_projects([x for x in items if x["id"] != b["id"]])
            self.send_json(200, {"ok": True})
        elif p == "/api/ai/delete":
            with ai_lock:
                with runs_lock:
                    if any(r.get('chat') == b['id'] for r in ai_runs.values()):
                        raise attachments.AttachmentError('Сначала остановите ответ в чате', 409)
                path(chat_file(b["id"])).unlink(missing_ok=True)
                attachments.Store(LIFE_DIR).delete_chat(b['id'])
            self.send_json(200, {"ok": True})
        else:
            self.send_json(404, {"error": "not found"})

    def route(self, b):
        today = datetime.now().strftime("%Y-%m-%d")
        p = self.path
        if p == "/api/expenses/add":
            amount = float(b["amount"])
            if not amount > 0:
                raise ValueError()
            date = b.get("date") or today
            if not DATE_RE.match(date):
                raise ValueError()
            rows = read_expenses()
            rows.append({"date": date, "amount": amount, "currency": one_line(b.get("currency") or "RUB", 8),
                         "category": one_line(b.get("category") or "Другое", 40), "note": one_line(b.get("note"))})
            write_expenses(rows)
        elif p == "/api/expenses/delete":
            rows = read_expenses()
            i = int(b["id"])
            if i >= len(rows) or rows[i]["date"] != b.get("date") or rows[i]["amount"] != float(b.get("amount")):
                raise Conflict()
            del rows[i]
            write_expenses(rows)
        elif p == "/api/tasks/add":
            name = TASK_FILES[b.get("list", "tasks")]
            # single brackets would read as a checkbox; [[links]] stay as they are
            title = re.sub(r"(?<!\[)\[(?!\[)|(?<!\])\](?!\])", lambda m: "(" if m.group() == "[" else ")", one_line(b["title"]))
            due = b.get("due") or ""
            if not title or (due and not DATE_RE.match(due)):
                raise ValueError()
            append_line(name, format_task(title, False, due, ""))
        elif p == "/api/tasks/toggle":
            name = TASK_FILES[b.get("list", "tasks")]
            t = next(t for t in read_tasks(name) if t["line"] == int(b["line"]))
            if t["raw"] != b.get("raw"):
                raise Conflict()
            done = not t["done"]
            replace_line(name, t["line"], t["raw"], format_task(t["title"], done, t["due"], today if done else ""))
        elif p == "/api/tasks/delete":
            replace_line(TASK_FILES[b.get("list", "tasks")], int(b["line"]), b.get("raw"), None)
        elif p == "/api/notes/add":
            text = one_line(b["text"], 2000)
            if not text:
                raise ValueError()
            append_line(NOTE_FILES[b.get("list", "notes")], f"- {today} {datetime.now().strftime('%H:%M')} · {text}")
        elif p == "/api/notes/delete":
            replace_line(NOTE_FILES[b.get("list", "notes")], int(b["line"]), b.get("raw"), None)
        elif p == "/api/memory/add":
            request, source = memory_metadata(b)
            if any(SECRET_RE.search(str(b.get(k, ''))) or MEMORY_SERVICE_RE.search(str(b.get(k, ''))) for k in ('name', 'text', 'kind')):
                return self.send_json(400, {'error': MEMORY_REJECT})
            name, file = page_file(b['name'])
            text = one_line(b['text'], 2000)
            if not text:
                raise ValueError()
            target = path(file)
            target.parent.mkdir(parents=True, exist_ok=True)
            data = target.read_bytes() if target.exists() else b''
            before = memory_version(data)
            lines = data.decode('utf-8').splitlines()
            previous = next((r for r in memory_audit() if request and r.get('request') == request), None)
            normalize = lambda value: ' '.join(value.split()).casefold()
            idx = next((i for i, line in enumerate(lines) if normalize(NOTE_RE.match(line).group(3) if NOTE_RE.match(line) else line.strip().lstrip('-* ').strip()) == normalize(text) and line.strip() and not line.startswith('#') and not KIND_RE.match(line)), None)
            duplicate = previous is not None or idx is not None
            if previous:
                name, file = page_file(previous['page'])
                data = path(file).read_bytes()
                return self.send_json(200, dict(ok=True, duplicate=True, page=name, line=previous['line'], version=memory_version(data), verified=True))
            if not duplicate:
                raw = f'- {today} · {text}'
                # Append in place: never replace a page another writer may have appended to.
                with target.open('ab+') as f:
                    f.seek(0); data = f.read()
                    before = memory_version(data)
                    prefix = (b'\n' if data and not data.endswith(b'\n') else b'')
                    if not data:
                        kind = one_line(b.get('kind'), 40)
                        prefix = (f'# {name}\n' + (f'тип: {kind}\n' if kind else '') + '\n').encode()
                    f.write(prefix + raw.encode() + b'\n')
                    f.flush(); os.fsync(f.fileno())
                data = target.read_bytes()
                lines = data.decode('utf-8').splitlines()
                if raw not in lines:
                    raise Conflict()
                idx = len(lines) - 1 - lines[::-1].index(raw)
            else:
                data = target.read_bytes()
            version = memory_version(data)
            memory_record('dedup' if duplicate else 'add', source, request, name, idx, text, before, version)
            return self.send_json(200, dict(ok=True, duplicate=duplicate, page=name, line=idx, version=version, verified=True))
        elif p == "/api/memory/delete":
            request, source = memory_metadata(b)
            name, file = page_file(b['name'])
            data = path(file).read_bytes()
            lines = data.decode('utf-8').splitlines()
            idx = int(b['line'])
            if idx < 0 or idx >= len(lines) or lines[idx] != b.get('raw'):
                raise Conflict()
            raw = lines[idx]
            memory_journal('memory-removed.md', f'### {today} · {name}\n{raw}\n')
            replace_line(file, idx, raw, None)
            memory_record('delete', source, request, name, idx, raw, memory_version(data), memory_version(path(file).read_bytes()))
        elif p == "/api/food/add":
            item = one_line(b["item"], 200)
            kcal = number(b.get("kcal"))
            if not item or not 0 < kcal < 10000:
                raise ValueError()
            rows = read_food()
            rows.append({"date": today, "time": datetime.now().strftime("%H:%M"), "item": item, "source": "вручную",
                         **{k: max(0.0, number(b.get(k))) for k in FOOD_NUMBERS}, "kcal": kcal})
            write_food(rows)
        elif p == "/api/food/delete":
            rows = read_food()
            i = int(b["id"])
            if i >= len(rows) or rows[i]["date"] != b.get("date") or rows[i]["item"] != b.get("item"):
                raise Conflict()
            del rows[i]
            write_food(rows)
        elif p == "/api/settings":
            goal = int(number(b["kcalGoal"]))
            if not 800 <= goal <= 6000:
                raise ValueError()
            write_text("settings.json", json.dumps({**read_settings(), "kcalGoal": goal}, ensure_ascii=False) + "\n")
        else:
            self.send_json(404, {"error": "not found"})
            return
        self.send_json(200, {"ok": True})


if __name__ == "__main__":
    LIFE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Сводка on http://{HOST}:{PORT}, data in {LIFE_DIR}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
