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
import tempfile
import threading
import time
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
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        allowed, _ = self.authorize()
        if not allowed:
            self.deny()
            return
        try:
            b = self.body()
            with lock:
                self.route(b)
        except Conflict:
            self.send_json(409, {"error": "Запись уже изменилась. Обнови страницу."})
        except (ValueError, KeyError, TypeError, StopIteration):
            self.send_json(400, {"error": "Неверные данные"})

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
