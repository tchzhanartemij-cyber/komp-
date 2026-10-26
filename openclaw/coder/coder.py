#!/usr/bin/env python3
"""Очередь задач по коду для Джарвиса.

Джарвис кладёт задачу из Telegram командой `add`, служба `worker` берёт задачи по одной,
запускает Claude Code в отдельной копии репозитория на ветке jarvis/task-N, пушит ветку
и присылает отчёт в Telegram. В основную ветку попадает только по команде `merge`.

  coder.py add [--repo komp-] "что сделать"
  coder.py list [--all]
  coder.py show N
  coder.py answer N "ответ на вопрос"
  coder.py redo N "что переделать"
  coder.py merge N
  coder.py cancel N
  coder.py repos
  coder.py connect <репозиторий> "что это за проект"
  coder.py worker            (запускает служба jarvis-coder)
"""
import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

HOME = Path.home()
DATA = HOME / ".openclaw/workspace/coder"
TASKS = DATA / "tasks.json"
LOCK = DATA / ".lock"
LOGS = DATA / "logs"
WORK = HOME / "work"
CONF = HOME / ".config/jarvis"
TOKEN_FILE = CONF / "github-token"
ENV_FILE = CONF / "coder.env"
REPOS_FILE = CONF / "repos.txt"   # подключённые проекты: «имя | описание» на строку

OWNER = "tchzhanartemij-cyber"
DEFAULT_REPO = "komp-"
DEFAULT_REPOS = {DEFAULT_REPO: "Джарвис, «Сводка» (openclaw/dashboard), скрипты"}
TASK_TIMEOUT = 2 * 60 * 60  # один заход не дольше двух часов
REFS_DIR = ".jarvis-refs"   # кадры и картинки к задаче внутри копии репозитория, в git не попадают
LIMIT_RETRY = 30 * 60       # кончился лимит подписки: пробуем снова через полчаса
POLL = 10

ACTIVE = ("queued", "running", "waiting_limit", "question")
STATUS_RU = {
    "queued": "в очереди",
    "running": "выполняется",
    "waiting_limit": "ждёт лимит Claude",
    "question": "ждёт твоего ответа",
    "done": "готово, ждёт «принять»",
    "nochange": "готово, без изменений",
    "failed": "не получилось",
    "cancelled": "отменена",
    "merged": "принята в основную ветку",
}

# Что Claude Code может делать сам. Пушит, сливает и ходит в сеть руками только раннер.
ALLOWED_TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep", "WebSearch", "WebFetch",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(git add:*)", "Bash(git commit:*)", "Bash(git mv:*)", "Bash(git rm:*)",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
    "Bash(mkdir:*)", "Bash(mv:*)", "Bash(cp:*)",
    "Bash(npm:*)", "Bash(npx:*)", "Bash(node:*)", "Bash(pnpm:*)", "Bash(yarn:*)",
    "Bash(python3:*)", "Bash(pytest:*)", "Bash(bash -n:*)",
]
DENIED_TOOLS = [
    "Bash(git push:*)", "Bash(git remote:*)", "Bash(git config:*)",
    "Bash(curl:*)", "Bash(wget:*)", "Bash(ssh:*)", "Bash(scp:*)", "Bash(sudo:*)",
]

PROMPT = """Ты работаешь над репозиторием {repo} по задаче владельца. Задача пришла из Telegram.

Задача №{id}:
{text}

Правила:
- Ты уже на ветке {branch}. Не переключай ветки, не пушь, не трогай настройки git.
- Сначала разберись в коде, потом делай минимальное изменение, которое решает задачу.
- Если в проекте есть тесты, линтер или сборка, запусти их и почини то, что сломал.
- Закоммить работу одним или несколькими коммитами с понятными сообщениями на русском.
- Не трогай секреты, токены и файлы с паролями.
- Если задача непонятна настолько, что любое решение будет угадыванием, ничего не меняй
  и ответь одной строкой, начинающейся с «ВОПРОС:», и сам вопрос.

В конце ответь коротко по-русски, это уйдёт владельцу в Telegram:
что сделано, как проверено, что осталось или вызывает сомнения."""

FOLLOWUP = {
    "answer": "Ответ владельца на твой вопрос по задаче №{id}:\n{text}\n\nПродолжай работу по тем же правилам.",
    "redo": "Владелец посмотрел результат по задаче №{id} и просит переделать:\n{text}\n\n"
            "Продолжай на той же ветке по тем же правилам.",
    "continue": "Работа по задаче №{id} прервалась: {text}. Всё сделанное уже закоммичено на этой ветке. "
                "Продолжай с того места, где остановился, по тем же правилам.",
}

REFS_PROMPT = """

Владелец приложил образцы ({count} шт.) в папке {path}/ (в git не попадает, не коммить её).
Это кадры из видео или картинки того, как должно выглядеть. Открой каждый файл инструментом Read
и посмотри. Анимации на кадрах не видно: как именно всё движется, описано в тексте задачи."""


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


@contextmanager
def store():
    DATA.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(TASKS.read_text()) if TASKS.exists() else {"next": 1, "tasks": []}
        yield data
        tmp = TASKS.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(tmp, TASKS)


def find(data, task_id):
    for t in data["tasks"]:
        if t["id"] == task_id:
            return t
    sys.exit(f"Задачи {task_id} нет.")


def update(task_id, **fields):
    with store() as data:
        t = find(data, task_id)
        t.update(fields, updated=now())
        return dict(t)


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def full_name(repo):
    """komp- -> tchzhanartemij-cyber/komp-; чужой владелец пишется как owner/repo."""
    if not re.fullmatch(r"([A-Za-z0-9-]+/)?[A-Za-z0-9._-]+", repo):
        raise ValueError(f"странное имя репозитория: {repo}")
    return repo if "/" in repo else f"{OWNER}/{repo}"


def load_repos():
    repos = dict(DEFAULT_REPOS)
    if REPOS_FILE.exists():
        for line in REPOS_FILE.read_text().splitlines():
            name, _, desc = line.partition("|")
            if name.strip():
                repos[name.strip()] = desc.strip()
    return repos


def github(path):
    req = urllib.request.Request(f"https://api.github.com/{path}", headers={
        "Authorization": f"Bearer {TOKEN_FILE.read_text().strip()}",
        "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def short(text, n=80):
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------- команды для Джарвиса ----------

def cmd_add(args):
    text = " ".join(args.text).strip()
    if not text:
        sys.exit("Пустая задача.")
    repos = load_repos()
    if args.repo not in repos:
        sys.exit(f"Проект {args.repo} не подключён. Подключены: {', '.join(repos)}. "
                 f"Подключить: connect <репозиторий> \"что это\".")
    refs = [Path(r).expanduser() for r in args.ref or []]
    missing = [str(r) for r in refs if not r.is_file()]
    if missing:
        sys.exit(f"Нет таких файлов: {', '.join(missing)}")
    not_before = start_time(args.at) if args.at else 0
    with store() as data:
        task_id = data["next"]
        data["next"] += 1
        names = []
        if refs:
            dest = DATA / "refs" / str(task_id)
            dest.mkdir(parents=True, exist_ok=True)
            for i, r in enumerate(refs, 1):
                name = f"{i:02d}-{re.sub(r'[^A-Za-z0-9._-]', '_', r.name)}"
                shutil.copy2(r, dest / name)
                names.append(name)
        data["tasks"].append({
            "id": task_id, "repo": args.repo, "text": text, "status": "queued",
            "created": now(), "updated": now(), "branch": f"jarvis/task-{task_id}",
            "session": None, "followup": None, "action": "work", "result": "", "link": "",
            "refs": names, "not_before": not_before,
        })
        ahead = sum(1 for t in data["tasks"] if t["status"] in ("queued", "running") and t["id"] != task_id)
    if not_before:
        wait = f"начну в {datetime.fromtimestamp(not_before).strftime('%d.%m %H:%M')}"
    else:
        wait = "начинаю сразу" if ahead == 0 else f"перед ней в очереди: {ahead}"
    extra = f", образцов: {len(names)}" if names else ""
    print(f"Задача {task_id} принята ({args.repo}{extra}), {wait}.")


def start_time(hhmm):
    """«02:00» -> ближайшие 02:00 по времени сервера."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", hhmm.strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        sys.exit("Время пиши как ЧЧ:ММ, например 02:00.")
    t = datetime.now().replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
    if t <= datetime.now():
        t += timedelta(days=1)
    return t.timestamp()


def cmd_list(args):
    with store() as data:
        tasks = list(data["tasks"])
    active = [t for t in tasks if t["status"] in ACTIVE]
    rest = [t for t in tasks if t["status"] not in ACTIVE]
    shown = active + (rest if args.all else rest[-5:])
    if not shown:
        print("Задач нет.")
    for t in sorted(shown, key=lambda t: t["id"]):
        status = STATUS_RU.get(t["status"], t["status"])
        if t["status"] == "queued" and t.get("not_before", 0) > time.time():
            status += datetime.fromtimestamp(t["not_before"]).strftime(", начну в %H:%M")
        print(f"{t['id']}. [{status}] {t['repo']}: {short(t['text'])}")


def cmd_show(args):
    with store() as data:
        t = dict(find(data, args.id))
    print(f"Задача {t['id']} ({t['repo']}), {STATUS_RU.get(t['status'], t['status'])}, обновлена {t['updated']}")
    print(f"Текст: {t['text']}")
    print(f"Ветка: {t['branch']}")
    if t["link"]:
        print(f"Изменения: {t['link']}")
    if t["result"]:
        print(f"\n{t['result']}")


def followup(args, kind):
    text = " ".join(args.text).strip()
    with store() as data:
        t = find(data, args.id)
        if t["status"] in ("running", "queued"):
            sys.exit(f"Задача {t['id']} ещё {STATUS_RU[t['status']]}, дождись отчёта.")
        if t["status"] == "merged":
            sys.exit(f"Задача {t['id']} уже принята. Дай новую задачу.")
        t.update(status="queued", followup={"kind": kind, "text": text}, action="work", updated=now())
    print(f"Задача {args.id} снова в очереди.")


def cmd_merge(args):
    with store() as data:
        t = find(data, args.id)
        if t["status"] != "done":
            sys.exit(f"Задачу {t['id']} нельзя принять: {STATUS_RU.get(t['status'], t['status'])}.")
        t.update(status="queued", action="merge", updated=now())
    print(f"Принимаю задачу {args.id}: сливаю ветку в основную, пришлю итог.")


def cmd_repos(args):
    for name, desc in load_repos().items():
        print(f"{name}: {desc}" if desc else name)


def cmd_connect(args):
    repo = args.repo.strip()
    desc = " ".join(args.desc).replace("|", "/").strip()
    try:
        info = github(f"repos/{full_name(repo)}")
    except ValueError as e:
        sys.exit(str(e))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sys.exit(f"Токен не видит {full_name(repo)}. Проверь имя или добавь репозиторий в токен "
                     f"на GitHub: Settings → Developer settings → Fine-grained tokens → Edit → Repository access.")
        sys.exit(f"GitHub ответил ошибкой {e.code}.")
    if not info.get("permissions", {}).get("push"):
        sys.exit(f"Токен видит {info['full_name']}, но не может писать: нужно Contents: Read and write.")
    repos = load_repos()
    repos[repo] = desc
    CONF.mkdir(parents=True, exist_ok=True)
    REPOS_FILE.write_text("".join(f"{n} | {d}\n" for n, d in repos.items() if n not in DEFAULT_REPOS))
    print(f"Проект {repo} подключён ({info['full_name']}, основная ветка {info['default_branch']}).")


def cmd_cancel(args):
    with store() as data:
        t = find(data, args.id)
        if t["status"] == "running":
            t["cancel"] = True
            print(f"Останавливаю задачу {t['id']}.")
        elif t["status"] in ACTIVE:
            t.update(status="cancelled", updated=now())
            print(f"Задача {t['id']} отменена.")
        else:
            print(f"Задача {t['id']} уже {STATUS_RU.get(t['status'], t['status'])}.")


# ---------- исполнитель ----------

def git(repo_dir, *args, check=True):
    r = subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[-500:]}")
    return r.stdout.strip()


CRED_HELPER = ('!f() { test "$1" = get && echo username=x-access-token && '
               f'echo "password=$(cat {TOKEN_FILE})"; }}; f')


def ensure_repo(repo):
    name = full_name(repo)
    repo_dir = WORK / repo.replace("/", "__")
    if not (repo_dir / ".git").exists():
        WORK.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-c", f"credential.helper={CRED_HELPER}", "clone",
                        f"https://github.com/{name}.git", str(repo_dir)],
                       check=True, capture_output=True, text=True)
    git(repo_dir, "config", "credential.helper", CRED_HELPER)
    git(repo_dir, "config", "user.name", "Jarvis")
    git(repo_dir, "config", "user.email", "jarvis@users.noreply.github.com")
    git(repo_dir, "fetch", "origin", "--prune")
    git(repo_dir, "remote", "set-head", "origin", "--auto")
    base = git(repo_dir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").split("/", 1)[1]
    git(repo_dir, "reset", "--hard")
    git(repo_dir, "clean", "-fd")
    return repo_dir, base


def remote_has(repo_dir, branch):
    return git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", check=False) != ""


def run_claude(task, repo_dir, env):
    claude = env.get("CLAUDE_BIN", "claude")
    fu = task.get("followup")
    if fu and task.get("session"):
        prompt = FOLLOWUP[fu["kind"]].format(id=task["id"], text=fu["text"])
        resume = ["--resume", task["session"]]
    else:
        text = task["text"] + (f"\n\nДополнение владельца: {fu['text']}" if fu and fu["kind"] != "continue" else "")
        prompt = PROMPT.format(repo=task["repo"], id=task["id"], text=text, branch=task["branch"])
        if task.get("refs"):
            prompt += REFS_PROMPT.format(count=len(task["refs"]), path=f"{REFS_DIR}/task-{task['id']}")
        resume = []
    cmd = [claude, "-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits",
           *resume, "--allowedTools", *ALLOWED_TOOLS, "--disallowedTools", *DENIED_TOOLS]
    LOGS.mkdir(parents=True, exist_ok=True)
    out_path = LOGS / f"{task['id']}-{int(time.time())}.json"
    err_path = out_path.with_suffix(".err")
    with open(out_path, "w") as out, open(err_path, "w") as err:
        proc = subprocess.Popen(cmd, cwd=repo_dir, stdout=out, stderr=err,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        started = time.time()
        while proc.poll() is None:
            time.sleep(5)
            with store() as data:
                cancel = find(data, task["id"]).get("cancel")
            if cancel or time.time() - started > TASK_TIMEOUT:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(30)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                return {"stopped": "cancel" if cancel else "timeout"}
    raw = out_path.read_text().strip()
    try:
        res = json.loads(raw)
    except ValueError:
        tail = (raw or err_path.read_text()).strip()[-800:]
        return {"is_error": True, "result": tail or "Claude Code ничего не ответил."}
    return res if isinstance(res, dict) else {"is_error": True, "result": raw[-800:]}


def save_progress(repo_dir, base, branch, task_id, push=True):
    """Коммитит недоделанное и (по умолчанию) пушит ветку, чтобы ничего не потерять."""
    if git(repo_dir, "status", "--porcelain"):
        git(repo_dir, "add", "-A")
        git(repo_dir, "commit", "-m", f"Jarvis: задача {task_id}")
    ahead = int(git(repo_dir, "rev-list", "--count", f"origin/{base}..HEAD"))
    if push and ahead:
        git(repo_dir, "push", "-u", "origin", branch)
    return ahead


def work(task, env):
    repo_dir, base = ensure_repo(task["repo"])
    branch = task["branch"]
    start = f"origin/{branch}" if remote_has(repo_dir, branch) else f"origin/{base}"
    git(repo_dir, "checkout", "-B", branch, start)
    if task.get("refs"):
        exclude = repo_dir / ".git/info/exclude"
        if f"/{REFS_DIR}/" not in exclude.read_text():
            with open(exclude, "a") as f:
                f.write(f"\n/{REFS_DIR}/\n")
        shutil.copytree(DATA / "refs" / str(task["id"]), repo_dir / REFS_DIR / f"task-{task['id']}",
                        dirs_exist_ok=True)

    res = run_claude(task, repo_dir, env)
    if res.get("stopped") == "cancel":
        update(task["id"], status="cancelled", cancel=False)
        return f"⏹ Задача {task['id']} остановлена."
    if res.get("stopped") == "timeout":
        ahead = save_progress(repo_dir, base, branch, task["id"])
        update(task["id"], status="failed", result="Не уложился в два часа.",
               followup={"kind": "continue", "text": "закончилось время на один заход"})
        kept = f" Сделанное сохранил в ветке {branch} ({ahead} коммитов)." if ahead else ""
        return (f"⚠️ Задача {task['id']}: не уложился в два часа, остановил.{kept}\n"
                f"Продолжить: «переделать {task['id']}: продолжай».")

    text = (res.get("result") or "").strip()
    session = res.get("session_id") or task.get("session")
    if res.get("is_error"):
        if re.search(r"limit|usage|quota|лимит", text, re.I):
            save_progress(repo_dir, base, branch, task["id"])
            update(task["id"], status="waiting_limit", session=session, result=text,
                   next_try=time.time() + LIMIT_RETRY,
                   followup={"kind": "continue", "text": "закончился лимит подписки"})
            return (f"⏳ Задача {task['id']}: закончился лимит Claude. Сделанное сохранил, "
                    f"продолжу сам, проверяю каждые 30 минут.")
        update(task["id"], status="failed", session=session, result=text)
        return f"⚠️ Задача {task['id']} не получилась:\n{short(text, 1500)}"

    ahead = save_progress(repo_dir, base, branch, task["id"], push=False)

    if text.startswith("ВОПРОС:") and not ahead:
        update(task["id"], status="question", session=session, result=text, followup=None)
        return (f"❓ Задача {task['id']}: {text[len('ВОПРОС:'):].strip()}\n\n"
                f"Ответь: «ответ {task['id']}: ...»")
    if not ahead:
        update(task["id"], status="nochange", session=session, result=text, followup=None)
        return f"ℹ️ Задача {task['id']}: изменений в коде нет.\n\n{short(text, 3000)}"

    git(repo_dir, "push", "-u", "origin", branch)
    link = f"https://github.com/{full_name(task['repo'])}/compare/{base}...{branch}"
    update(task["id"], status="done", session=session, result=text, link=link, followup=None)
    return (f"✅ Задача {task['id']} ({task['repo']}): {short(task['text'])}\n\n{short(text, 3000)}\n\n"
            f"Ветка {branch}, коммитов: {ahead}\nИзменения: {link}\n\n"
            f"Дальше: «принять {task['id']}», «переделать {task['id']}: ...» или «отменить {task['id']}».")


def merge(task):
    repo_dir, base = ensure_repo(task["repo"])
    branch = task["branch"]
    git(repo_dir, "checkout", "-B", base, f"origin/{base}")
    r = subprocess.run(["git", "merge", "--no-ff", f"origin/{branch}", "-m",
                        f"Jarvis: принята задача {task['id']}"], cwd=repo_dir, capture_output=True, text=True)
    if r.returncode != 0:
        git(repo_dir, "merge", "--abort", check=False)
        update(task["id"], status="done", action="work")
        return (f"⚠️ Задачу {task['id']} не получилось слить: конфликт с основной веткой. "
                f"Скажи «переделать {task['id']}: подтяни свежую основную ветку», и я поправлю.")
    git(repo_dir, "push", "origin", base)
    git(repo_dir, "push", "origin", "--delete", branch, check=False)
    update(task["id"], status="merged", action="work")
    return f"🟢 Задача {task['id']} принята и слита в {base}."


def notify(env, text):
    target = env.get("TG_TARGET")
    if not target:
        print("TG_TARGET не задан, отчёт не отправлен:\n" + text, file=sys.stderr)
        return
    r = subprocess.run([env.get("OPENCLAW_BIN", "openclaw"), "message", "send", "--channel", "telegram",
                        "--target", target, "--message", text[:4000]], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"Не отправилось в Telegram: {(r.stderr or r.stdout).strip()}", file=sys.stderr)


def claim():
    with store() as data:
        for t in data["tasks"]:
            ready = (t["status"] == "queued" and t.get("not_before", 0) <= time.time()) or (
                t["status"] == "waiting_limit" and t.get("next_try", 0) <= time.time())
            if ready:
                t.update(status="running", cancel=False, updated=now())
                return dict(t)
    return None


def cmd_worker(args):
    # После перезапуска сервера недоделанные задачи возвращаются в очередь.
    with store() as data:
        for t in data["tasks"]:
            if t["status"] == "running":
                t["status"] = "queued"
    while True:
        env = load_env()
        task = claim()
        if not task:
            time.sleep(POLL)
            continue
        try:
            msg = merge(task) if task.get("action") == "merge" else work(task, env)
        except Exception as e:  # раннер не должен падать из-за одной задачи
            update(task["id"], status="failed", result=str(e))
            msg = f"⚠️ Задача {task['id']}: ошибка раннера: {short(str(e), 1500)}"
        print(msg, flush=True)
        notify(env, msg)


def main():
    p = argparse.ArgumentParser(description="Очередь задач по коду для Джарвиса")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--repo", default=DEFAULT_REPO)
    a.add_argument("--ref", action="append", help="кадр или картинка-образец, можно несколько раз")
    a.add_argument("--at", help="не начинать раньше ЧЧ:ММ (например, 02:00)")
    a.add_argument("text", nargs="+")
    a.set_defaults(fn=cmd_add)
    a = sub.add_parser("list")
    a.add_argument("--all", action="store_true")
    a.set_defaults(fn=cmd_list)
    for name, fn in (("show", cmd_show), ("merge", cmd_merge), ("cancel", cmd_cancel)):
        a = sub.add_parser(name)
        a.add_argument("id", type=int)
        a.set_defaults(fn=fn)
    for name, kind in (("answer", "answer"), ("redo", "redo")):
        a = sub.add_parser(name)
        a.add_argument("id", type=int)
        a.add_argument("text", nargs="+")
        a.set_defaults(fn=lambda args, k=kind: followup(args, k))
    sub.add_parser("repos").set_defaults(fn=cmd_repos)
    a = sub.add_parser("connect")
    a.add_argument("repo")
    a.add_argument("desc", nargs="+")
    a.set_defaults(fn=cmd_connect)
    sub.add_parser("worker").set_defaults(fn=cmd_worker)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
