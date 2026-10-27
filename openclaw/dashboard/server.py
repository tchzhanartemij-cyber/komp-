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
  ai/<id>.json     dialogs of the «ИИ» tab: {"title", "messages": [{"role", "text", "model", "time"}]}

Jarvis writes them from Telegram; this server shows them and lets you edit them
from any browser. Only the Python standard library is used.
"""

import csv
import hashlib
import hmac
import io
import json
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
from pathlib import Path

LIFE_DIR = Path(os.environ.get("LIFE_DIR", Path.home() / ".openclaw/workspace/life")).expanduser()
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

# The order is the order in the chat's list and the order auto mode tries them in.
AI_MODELS = [
    {"id": "claude", "title": "Claude", "routes": [("claude", "sonnet")]},
    {"id": "free", "title": "Бесплатная (шлюз)", "routes": [("freellm", "auto")]},
]

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
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def claude_state():
    """Подписка Claude: сколько потрачено за 5 часов и неделю и не кончилась ли она."""
    now = time.time()
    until, windows = 0.0, {}
    usage = read_json(CODER_DATA / "usage.json") or {}
    for key in ("five_hour", "seven_day"):
        w = usage.get(key)
        if isinstance(w, dict) and w.get("reset", 0) > now:
            windows[key] = {"pct": round(float(w.get("pct", 0))), "reset": w["reset"]}
            if float(w.get("pct", 0)) >= 99.5:
                until = max(until, w["reset"])
    info = (read_json(CODER_DATA / "limits.json") or {}).get("info") or {}
    if info.get("status") == "rejected":
        resets = [w.get("resetsAt", 0) for w in (info.get("unifiedWindows") or {}).values() if isinstance(w, dict)]
        resets.append(info.get("resetsAt") or 0)
        if max(resets) > now:
            until = max(until, max(resets))
    if claude_pause["until"] > now:
        until = max(until, claude_pause["until"])
    return {"exhausted": until > now, "until": until if until > now else 0, "windows": windows}


def ready_routes(m, claude=None):
    """Маршруты модели, которыми можно воспользоваться прямо сейчас: [(provider, model)].
    Шлюз пробуем всегда, когда есть ключ: если он лежит, владелец увидит понятную ошибку."""
    out = []
    for provider, model in m["routes"]:
        if provider == "claude":
            if claude_bin() and not (claude or claude_state())["exhausted"]:
                out.append((provider, model))
        elif free_key():
            out.append((provider, model))
    return out


def models_payload():
    claude = claude_state()
    models, auto = [], None
    for m in AI_MODELS:
        routes = ready_routes(m, claude)
        why = ""
        if routes and m["id"] == "free" and not gate_up():
            routes, why = [], "шлюз не отвечает"
        elif not routes:
            if m["id"] == "claude":
                why = "лимит подписки исчерпан" if claude["exhausted"] and claude_bin() else "не найден Claude Code"
            else:
                why = "нет ключа " + FREE_KEY
        models.append({"id": m["id"], "title": m["title"], "available": bool(routes), "why": why,
                       "detail": route_name(*routes[0]) if routes else ""})
        if routes and auto is None:
            auto = {"id": m["id"], "title": m["title"]}
    return {"models": models, "auto": auto, "claude": claude}


def route_name(provider, model):
    return "подписка Claude" if provider == "claude" else f"{model} · шлюз freellmapi"


def call_api(model, messages):
    """Спрашивает шлюз; возвращает ответ и модель, которую шлюз выбрал на самом деле."""
    body = json.dumps({"model": model, "messages": messages}).encode()
    try:
        data = free_request("/chat/completions", body)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AIError(f"Шлюз freellmapi отклонил ключ ({FREE_KEY}).")
        try:
            err = json.loads(e.read()).get("error")
            msg = err.get("message") if isinstance(err, dict) else err
        except (ValueError, AttributeError, OSError):
            msg = ""
        raise AIError(f"Шлюз freellmapi: ошибка {e.code} {one_line(msg, 160)}".strip())
    except OSError:
        gate_cache.update(at=time.time(), up=False)
        raise AIError(FREE_DOWN)
    except ValueError:
        raise AIError("Шлюз freellmapi: непонятный ответ")
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AIError("Шлюз freellmapi: пустой ответ")
    if not (text or "").strip():
        raise AIError("Шлюз freellmapi: пустой ответ")
    return text.strip(), str(data.get("model") or model)


def call_claude(model, messages):
    if len(messages) == 1:
        prompt = messages[0]["content"]
    else:
        who = {"user": "Владелец", "assistant": "Ты"}
        prompt = ("Ниже наша переписка. Ответь на последнее сообщение владельца, только сам ответ.\n\n"
                  + "\n\n".join(f"{who[m['role']]}: {m['content']}" for m in messages))
    cmd = [claude_bin(), "-p", "--model", model, "--tools", "", "--system-prompt", AI_SYSTEM,
           "--output-format", "json", "--no-session-persistence"]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT,
                           cwd=tempfile.gettempdir())
    except (OSError, subprocess.SubprocessError) as e:
        raise AIError(f"Claude: не запустился ({type(e).__name__})")
    try:
        out = json.loads(r.stdout)
    except ValueError:
        out = {}
    text = str(out.get("result") or "").strip()
    if r.returncode != 0 or out.get("is_error") or not text:
        reason = text or (r.stderr or r.stdout).strip()
        if re.search(r"limit|usage|quota|лимит", reason, re.I):
            claude_pause["until"] = time.time() + CLAUDE_PAUSE
            raise AIError("Claude: лимит подписки исчерпан")
        raise AIError(f"Claude: {one_line(reason, 160) or 'пустой ответ'}")
    return text


def chat_file(chat_id):
    if not CHAT_ID_RE.match(str(chat_id)):
        raise ValueError()
    return f"ai/{chat_id}.json"


def read_chat(chat_id):
    try:
        chat = json.loads(read_text(chat_file(chat_id)) or "null")
    except ValueError:
        chat = None
    if not isinstance(chat, dict):
        raise KeyError()
    return chat


def list_chats():
    out = []
    folder = LIFE_DIR / "ai"
    for f in folder.glob("*.json") if folder.is_dir() else []:
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
            out.append({"id": f.stem, "title": c.get("title") or "Без названия", "updated": c.get("updated", ""),
                        "count": len(c.get("messages") or [])})
        except (OSError, ValueError, AttributeError):
            continue
    return sorted(out, key=lambda c: c["updated"], reverse=True)


def ai_send(chat_id, model_id, text):
    """Отправляет сообщение и возвращает весь диалог с ответом. Пока модель думает, замков не держим."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    history = []
    if chat_id:
        with ai_lock:
            history = read_chat(chat_id).get("messages") or []
    messages, size = [], len(text)
    for m in reversed(history):
        size += len(m.get("text", ""))
        if size > AI_HISTORY_CHARS:
            break
        messages.insert(0, {"role": m["role"], "content": m["text"]})
    messages.append({"role": "user", "content": text})

    auto = model_id == "auto"
    pool = AI_MODELS if auto else [m for m in AI_MODELS if m["id"] == model_id]
    if not pool:
        raise ValueError()
    errors, answer, used = [], None, None
    for m in pool:
        for provider, model in ready_routes(m):
            try:
                if provider == "claude":
                    answer = call_claude(model, messages)
                else:
                    answer, model = call_api(model, [{"role": "system", "content": AI_SYSTEM}] + messages)
                used = (m, provider, model)
                break
            except AIError as e:
                errors.append(str(e))
        if answer:
            break
    if not answer:
        if not errors:
            raise AIError("Эта модель сейчас недоступна." if not auto else
                          f"Claude недоступен, а для бесплатного шлюза нет ключа ~/.config/jarvis/{FREE_KEY}.")
        raise AIError(errors[0] if len(errors) == 1 else "Не ответила ни одна модель. " + " · ".join(errors[-3:]))

    m, provider, model = used
    with ai_lock:
        if chat_id:
            chat = read_chat(chat_id)
        else:
            chat_id = secrets.token_hex(6)
            chat = {"title": one_line(text, 60), "created": stamp, "messages": []}
        chat["messages"] += [
            {"role": "user", "text": text, "time": stamp},
            {"role": "assistant", "text": answer, "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
             "model": m["title"], "detail": route_name(provider, model), "auto": auto},
        ]
        chat["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        write_text(chat_file(chat_id), json.dumps(chat, ensure_ascii=False, indent=1))
    return {"id": chat_id, **chat, "errors": errors}


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
        if length > 64 * 1024:
            raise ValueError("too large")
        return json.loads(self.rfile.read(length) or b"{}")

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
        elif route == "/api/ai/models":
            self.send_json(200, models_payload())
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
        try:
            b = self.body()
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
            self.send_json(200, ai_send(b.get("chat") or "", str(b.get("model") or "auto"), text))
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
