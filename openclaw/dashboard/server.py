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

import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_office_path = (_dashboard_dir / 'office.py').resolve(strict=True)
if _office_path.parent != _dashboard_dir:
    raise ImportError('office.py must resolve to a trusted dashboard sibling')
_office_spec = importlib.util.spec_from_file_location('office', _office_path)
_office_module = importlib.util.module_from_spec(_office_spec)
sys.modules[_office_spec.name] = _office_module
_office_spec.loader.exec_module(_office_module)
office_sessions, coder_metadata = _office_module.office_sessions, _office_module.coder_metadata

LIFE_DIR = Path(os.environ.get("LIFE_DIR", Path.home() / ".openclaw/workspace/life")).expanduser()
agent_service = Agent(LIFE_DIR / "dashboard-agent")

VOICE_MAX_BYTES = 10 * 1024 * 1024
VOICE_TIMEOUT = 90
voice_semaphore = threading.Semaphore(1)
VOICE_STAGES = ('t_speech_end', 't_transcript', 't_run_accepted', 't_first_text', 't_first_audio', 't_done')
SECRET_RE = re.compile(r'(sk|gsk|xai|key)[-_][A-Za-z0-9_-]{8,}|Bearer \S+', re.I)
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


# --- AI chat ------------------------------------------------------------
# Claude goes through the Claude Code subscription; when the limits service says it is
# used up, auto mode skips it. Free models go through the local freellmapi gateway, its one
# key lives only in ~/.config/jarvis/freellm-unified-key.

CONF = Path.home() / ".config/jarvis"
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


def listed_models():
    out = []
    cache = read_json(Path(os.environ.get("SVODKA_CODEX_MODELS_FILE", Path.home() / ".codex/models_cache.json")))
    rows = cache.get("models", []) if isinstance(cache, dict) else []
    for m in rows if isinstance(rows, list) else []:
        if not isinstance(m, dict):
            continue
        mid = m.get("slug", "")
        if isinstance(mid, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}", mid) and m.get("visibility") == "list":
            out.append({"id": "chatgpt:"+mid, "title": one_line(m.get("display_name") or mid, 100), "available": bool(codex_bin()) and chatgpt_pause["until"] <= time.time(), "why": "Codex CLI недоступен или действует пауза", "detail": "Кеш Codex · модель перечислена; выполнение не проверено", "provenance": "Codex models_cache.json", "verified_runnable": False})
    source = "переменная окружения оператора"
    if "SVODKA_CLAUDE_MODELS" in os.environ:
        ids = os.environ["SVODKA_CLAUDE_MODELS"].split(",")
    else:
        config = read_json(Path.home() / ".openclaw/openclaw.json")
        node = config
        for key in ("agents", "defaults", "models"):
            node = node.get(key) if isinstance(node, dict) else None
        ids = [key[len("anthropic/"):] for key in node if isinstance(key, str) and key.startswith("anthropic/")] if isinstance(node, dict) else []
        source = "настройки OpenClaw"
    for mid in dict.fromkeys(ids):
        mid = mid.strip()
        if re.fullmatch(r"claude-[a-zA-Z0-9._-]{1,93}", mid):
            out.append({"id":"claude:"+mid, "title":"Claude · "+mid, "available":bool(claude_bin()) and not claude_state()["exhausted"], "why":"Claude недоступен или исчерпана подписка", "detail":source+" · настроена; выполнение не проверено", "provenance":source, "verified_runnable":False})
    return out


def models_payload():
    c = claude_state()
    models = [{"id":"claude", "title":"Claude · по умолчанию CLI", "available":bool(claude_bin()) and not c["exhausted"], "why":"CLI недоступен или исчерпана подписка", "detail":"Модель по умолчанию CLI · модель и выполнение не проверены"},
              {"id":"chatgpt", "title":"ChatGPT · по умолчанию CLI", "available":bool(codex_bin()) and chatgpt_pause["until"] <= time.time(), "why":"CLI недоступен или действует пауза", "detail":"Модель по умолчанию Codex · выполнение не проверено"},
              {"id":"free", "title":"Бесплатная", "available":bool(free_key()) and gate_up(), "why":"Шлюз недоступен", "detail":"Автовыбор шлюза · выполнение не проверено"}]
    auto = next(({"id":m["id"], "title":m["title"]} for m in models if m["available"]), None)
    return {"models":models + listed_models(), "auto":auto, "claude":c, "free_models":gateway_models()}


def route_name(provider, model):
    return {"claude": "подписка Claude", "chatgpt": "подписка ChatGPT · Codex"}.get(provider, f"{model} · шлюз freellmapi")


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


def call_process(provider, messages, system, run, delta, model="auto"):
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
        answer, final, failed = "", "", False
        diagnostic = ""
        with tempfile.TemporaryFile(mode="w+") as stderr:
            proc = None
            timer = None
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True, cwd=tempfile.gettempdir())
                run["proc"] = proc
                timer = threading.Timer(300, proc.kill)
                timer.start()
                if run["stop"].is_set():
                    stop_run(run)
                try:
                    proc.stdin.write((system + "\n\n" if provider == "chatgpt" else "") + transcript(messages))
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
                for line in proc.stdout:
                    if run["stop"].is_set():
                        break
                    if provider == "chatgpt":
                        diagnostic = (diagnostic + line)[-8000:]
                    if provider == "claude":
                        try:
                            obj = json.loads(line)
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
                reason = final + "\n" + diagnostic + "\n" + stderr.read()
                if run["stop"].is_set():
                    return answer
                if proc.returncode or failed or not (final or answer).strip():
                    # Ищем «лимит» только в неудачном запуске и в хвосте вывода: в начале Codex
                    # повторяет промпт, а в удачном ответе слово может встретиться по делу.
                    if re.search(r"limit|quota|usage|лимит", reason[-3000:], re.I):
                        (claude_pause if provider == "claude" else chatgpt_pause)["until"] = time.time() + CLAUDE_PAUSE
                        raise AIError(("Claude" if provider == "claude" else "ChatGPT") + ": лимит подписки исчерпан")
                    tail = reason.strip().splitlines()[-1:] or [""]
                    login = re.search(r"log.?in|auth|sign.?in", reason[-3000:], re.I)
                    raise AIError(("Claude" if provider == "claude" else "ChatGPT") + (": требуется вход" if login else ": " + (one_line(tail[0], 160) or "пустой ответ")))
                if not answer:
                    answer = final
                    delta(answer)
                return answer
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


def call_gateway(model, messages, run, delta):
    body = json.dumps({"model": model, "messages": messages, "stream": True}).encode()
    req = urllib.request.Request(FREE_URL + "/chat/completions", data=body, headers={"Authorization": f"Bearer {free_key()}", "Content-Type": "application/json"})
    answer, actual = "", model
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
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
                actual = str(obj.get("model") or actual)
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
    if not answer.strip() and not run["stop"].is_set():
        raise AIError("Шлюз freellmapi: пустой ответ")
    return answer, actual


def ai_send(chat_id, model_id, text, project, emit):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if model_id not in ("auto", "claude", "chatgpt", "free") and model_id not in {m["id"] for m in listed_models()} and model_id not in {"free:"+m["id"] for m in gateway_models()}:
        raise ValueError()
    # Новый чат пишем на диск только вместе с ответом, чтобы ошибка не оставляла пустых диалогов.
    new = not chat_id
    with ai_lock:
        if chat_id:
            chat = read_chat(chat_id)
        else:
            if project and not any(p["id"] == project for p in projects()):
                raise ValueError()
            chat_id = secrets.token_hex(6)
            chat = {"title": one_line(text, 60), "created": stamp, "updated": stamp, "project": project, "messages": []}
        history = list(chat.get("messages") or [])
        pr = next((p for p in projects() if p["id"] == chat.get("project")), None)
    system = (f"Инструкции проекта «{pr['name']}»: {pr['instructions']}\n\n" if pr and pr.get("instructions") else "") + AI_SYSTEM
    messages, size = [], len(text)
    for m in reversed(history):
        size += len(m.get("text", ""))
        if size > AI_HISTORY_CHARS:
            break
        messages.insert(0, {"role": m["role"], "content": m["text"]})
    messages.append({"role": "user", "content": text})
    available = models_payload()["models"]
    pool = [m["id"] for m in available if m["available"] and ":" not in m["id"]] if model_id == "auto" else [model_id]
    if not pool:
        raise AIError("Нет настроенных маршрутов для авто")
    if model_id != "auto" and not model_id.startswith("free:") and not any(m["id"] == model_id and m["available"] for m in available):
        raise AIError("Выбранный маршрут недоступен; автоматическая замена отключена")
    run_id = secrets.token_hex(12)
    now = time.time()
    provider_title = {"claude": "Claude", "chatgpt": "ChatGPT", "free": "Бесплатная"}[pool[0].split(':', 1)[0]]
    run = {"proc": None, "stop": threading.Event(), "started": now, "updated": now,
           "provider": provider_title, "phase": "waiting"}
    with runs_lock:
        ai_runs[run_id] = run
    errors, answer, title, detail = [], "", "", ""
    def send(obj):
        try:
            emit(obj)
        except (BrokenPipeError, ConnectionResetError, OSError):
            stop_run(run)
    def delta(chunk):
        nonlocal answer
        answer += chunk
        if chunk:
            with runs_lock:
                run.update(phase='streaming', updated=time.time())
        send({"type": "delta", "text": chunk})
    try:
        for n, mid in enumerate(pool):
            provider = mid.split(":", 1)[0]
            model = mid.split(":", 1)[1] if ":" in mid else "auto"
            title = {"claude": "Claude", "chatgpt": "ChatGPT", "free": "Бесплатная"}[provider]
            with runs_lock:
                run.update(provider=title, phase='waiting', updated=time.time())
            detail = route_name(provider, model) + (" · " + model if model != "auto" else " · по умолчанию CLI")
            send({"type": "switch" if n else "start", "chat": chat_id, "run": run_id, "model": title, "detail": detail, "reason": errors[-1] if errors else ""})
            answer = ""
            try:
                if provider == "free":
                    _, actual = call_gateway(model, [{"role": "system", "content": system}] + messages, run, delta)
                    detail = route_name(provider, actual)
                else:
                    call_process(provider, messages, system, run, delta, model)
                break
            except AIError as e:
                errors.append(str(e))
                if run["stop"].is_set():
                    break
                answer = ""
        stopped = run["stop"].is_set()
        if not answer and not stopped:
            send({"type": "error", "error": " · ".join(errors) or "Ответа нет"})
            return
        with ai_lock:
            if not new:
                chat = read_chat(chat_id)
            chat["messages"].append({"role": "user", "text": text, "time": stamp, "model": "", "detail": "", "auto": False})
            if answer:
                chat["messages"].append({"role": "assistant", "text": answer, "time": datetime.now().strftime("%Y-%m-%d %H:%M"), "model": title, "detail": detail, "auto": model_id == "auto", "stopped": stopped})
            chat["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_chat(chat_id, chat)
        send({"type": "done", "chat": {"id": chat_id, **chat}, "errors": errors})
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

    def voice_status(self):
        limits = dict(max_bytes=VOICE_MAX_BYTES, max_seconds=60, vad=True, max_utterance_seconds=30, session_max_minutes=15,
                      realtime=False, realtime_reason='Нативный realtime speech-to-speech не подключён: нужен платный провайдер (см. docs/TASK11-voice-architecture.md)')
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
            return dict(stt=True, reason='Распознавание через OpenClaw; ответы озвучивает браузер', **limits), binary
        return dict(stt=False, reason=reason, **limits), binary

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
        if not 0 <= length <= 64 * 1024:
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
        elif route == "/api/ai/models":
            self.send_json(200, models_payload())
        elif route == "/api/ai/projects":
            with ai_lock:
                self.send_json(200, {"projects": projects()})
        elif route == "/api/ai/chats":
            with ai_lock:
                self.send_json(200, {"chats": list_chats()})
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
        if urlsplit(self.path).path in ('/api/voice/metrics', '/api/client-log'):
            self.voice_diagnostic(urlsplit(self.path).path)
            return
        if urlsplit(self.path).path == "/api/voice/transcribe":
            self.transcribe()
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
        except (ValueError, KeyError, TypeError, StopIteration):
            self.send_json(400, {"error": "Неверные данные"})

    def ai_route(self, b):
        p = self.path
        if p == "/api/ai/send":
            text = str(b["text"]).strip()[:20000]
            if not text:
                raise ValueError()
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
                ai_send(b.get("chat") or "", str(b.get("model") or "auto"), text, b.get("project") or "", emit)
            except (AIError, ValueError, KeyError):
                try:
                    emit({"type": "error", "error": "Не удалось отправить сообщение"})
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
                path(chat_file(b["id"])).unlink(missing_ok=True)
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
            name, file = page_file(b["name"])
            text = one_line(b["text"], 2000)
            if not text:
                raise ValueError()
            if not path(file).exists():
                kind = one_line(b.get("kind"), 40)
                write_text(file, f"# {name}\n" + (f"тип: {kind}\n" if kind else "") + "\n")
            append_line(file, f"- {today} · {text}")
        elif p == "/api/memory/delete":
            _, file = page_file(b["name"])
            replace_line(file, int(b["line"]), b.get("raw"), None)
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
