#!/usr/bin/env python3
"""Сторож памяти: предупреждает в Telegram, пока потолки служб не начали мешать.

Раз в минуту смотрит на службы из apply.sh и на сервер в целом и пишет владельцу, когда:
  - служба заняла 80% своего потолка (раз в час на службу);
  - внутри службы система убила процесс из-за потолка;
  - на всём сервере осталось меньше 10% памяти (раз в час).
Запускает служба jarvis-memwatch под jarvis.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
ENV_FILE = HOME / ".config/jarvis/coder.env"
STATE = HOME / ".openclaw/workspace/limits-state.json"
UID = os.getuid()
USER_CG = Path(f"/sys/fs/cgroup/user.slice/user-{UID}.slice/user@{UID}.service/app.slice")
SYSTEM_CG = Path("/sys/fs/cgroup/system.slice")

SERVICES = {   # служба: (где лежит, как называть владельцу)
    "openclaw-gateway": (USER_CG, "Джарвис (с Claude, Кодексом и браузером)"),
    "jarvis-coder": (USER_CG, "очередь задач по коду"),
    "cloudcli": (USER_CG, "cloudcli"),
    "freellmapi": (USER_CG, "шлюз бесплатных моделей"),
    "jarvis-limits": (USER_CG, "счётчик лимитов Claude"),
    "life-dashboard": (SYSTEM_CG, "«Сводка»"),
}
WARN_SHARE = 0.8
SERVER_LOW = 0.1
REPEAT = 60 * 60
POLL = 60


def read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return None


def mb(n):
    return f"{n / 1048576:.0f} МБ"


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def notify(text):
    env = load_env()
    if not env.get("TG_TARGET"):
        print(text, flush=True)
        return
    r = subprocess.run([env.get("OPENCLAW_BIN", "openclaw"), "message", "send", "--channel", "telegram",
                        "--target", env["TG_TARGET"], "--message", text], capture_output=True, text=True)
    print(text if r.returncode == 0 else f"не отправилось: {r.stderr.strip()}\n{text}", flush=True)


def server_available():
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        info[k] = int(v.split()[0]) * 1024
    return info["MemAvailable"], info["MemTotal"]


def check(state):
    now = time.time()
    for name, (root, title) in SERVICES.items():
        cg = root / f"{name}.service"
        cur, top = read(cg / "memory.current"), read(cg / "memory.max")
        if cur is None:
            continue
        s = state.setdefault(name, {})
        events = dict(line.split() for line in (read(cg / "memory.events") or "").splitlines() if line)
        kills = int(events.get("oom_kill", 0))
        if kills > s.get("kills", kills):
            notify(f"⚠️ Память: в службе «{title}» кончилась память, система остановила один процесс "
                   f"(обычно Кодекс, браузер или тяжёлая сборка). Служба продолжает работать. "
                   f"Если это была задача, повтори её. Если повторяется часто, скажи, поднимем потолок.")
        s["kills"] = kills
        if top and top.isdigit():
            share = int(cur) / int(top)
            if share >= WARN_SHARE and now - s.get("warned", 0) > REPEAT:
                notify(f"🟡 Память: «{title}» заняла {mb(int(cur))} из {mb(int(top))} ({share:.0%}). "
                       f"Скоро упрётся в потолок, тогда самый тяжёлый процесс в ней будет остановлен. "
                       f"Лучше не запускать сейчас новые тяжёлые задачи.")
                s["warned"] = now
    avail, total = server_available()
    srv = state.setdefault("_server", {})
    if avail / total < SERVER_LOW and now - srv.get("warned", 0) > REPEAT:
        notify(f"🔴 Память сервера: свободно {mb(avail)} из {mb(total)}. Сервер может начать тормозить.")
        srv["warned"] = now


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    once = "--once" in sys.argv
    while True:
        check(state)
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state))
        if once:
            return
        time.sleep(POLL)


if __name__ == "__main__":
    main()
