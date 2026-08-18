"""Observer de Slack para Nexus: vigila los chats del whitelist SIN gastar tokens
de LLM. Corre como proceso en background de una sesion de Claude Code; cuando
detecta mensajes nuevos de OTRA persona en un chat vigilado, imprime el payload
JSON y termina — eso despierta a la sesion, que procesa solo lo que llego.

Uso:
    python slack_watch.py --wait [--max-seconds N]   (bloquea hasta detectar; exit 0=hay mensajes, 3=timeout, 2=error)
    python slack_watch.py --once                     (un chequeo y sale; exit 0=hay, 3=nada)
    python slack_watch.py --init                     (marca los watermarks en AHORA, sin disparar backlog)

Config (whitelist, poll_seconds, self_user_id): ~/.claude/nexus_slack_config.json
Watermark propio del observer:                   ~/.claude/nexus_slack_watch_state.json
Token (user xoxp, requiere scopes *:history):    ~/.claude/slack-user-creds.json
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG = Path.home() / ".claude" / "nexus_slack_config.json"
STATE = Path.home() / ".claude" / "nexus_slack_watch_state.json"
CREDS = Path.home() / ".claude" / "slack-user-creds.json"
API = "https://slack.com/api/"
MAX_CONSECUTIVE_ERRORS = 10


def token():
    if not CREDS.exists():
        sys.exit(f"No existe {CREDS}")
    t = json.loads(CREDS.read_text(encoding="utf-8")).get("user_token", "")
    if not t.startswith("xoxp-"):
        sys.exit(f"Token invalido en {CREDS}: debe empezar con xoxp-")
    return t


def call(method, params):
    url = API + method + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read().decode("utf-8"))
    if not out.get("ok"):
        raise RuntimeError(f"Slack error en {method}: {out.get('error')}")
    return out


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def inbound(messages, watermark, self_id):
    """Mensajes de otra persona, sin subtipos (joins/edits), posteriores al watermark."""
    return sorted(
        (m for m in messages
         if m.get("user") and m["user"] != self_id
         and not m.get("subtype")
         and float(m.get("ts", 0)) > float(watermark)),
        key=lambda m: float(m["ts"]),
    )


def check(cfg, state):
    hits = []
    for w in cfg.get("watch", []):
        ch = w.get("dm_channel") or w.get("channel_id")
        if not ch:
            continue
        wm = state.get(ch)
        if wm is None:
            state[ch] = f"{time.time():.6f}"
            continue
        out = call("conversations.history", {"channel": ch, "oldest": wm, "limit": 20})
        msgs = inbound(out.get("messages", []), wm, cfg.get("self_user_id", ""))
        if msgs:
            state[ch] = msgs[-1]["ts"]
            hits.append({
                "name": w.get("name", ch),
                "channel_id": w.get("channel_id"),
                "dm_channel": w.get("dm_channel"),
                "mode": w.get("mode", "supervised"),
                "notes": w.get("notes", ""),
                "messages": [{"user": m["user"], "text": m.get("text", ""), "ts": m["ts"]}
                             for m in msgs],
            })
    return hits


def save_state(state):
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def main(argv):
    if not CONFIG.exists():
        sys.exit(f"No existe {CONFIG}")
    cfg = load_json(CONFIG, {})
    state = load_json(STATE, {})

    if argv[:1] == ["--init"]:
        now = f"{time.time():.6f}"
        for w in cfg.get("watch", []):
            ch = w.get("dm_channel") or w.get("channel_id")
            if ch:
                state[ch] = now
        save_state(state)
        print(f"watermarks inicializados en ahora para {len(cfg.get('watch', []))} chats")
        return 0

    if argv[:1] == ["--once"]:
        try:
            hits = check(cfg, state)
        except (RuntimeError, urllib.error.URLError) as e:
            print(str(e), file=sys.stderr)
            return 2
        save_state(state)
        if hits:
            print(json.dumps(hits, ensure_ascii=False, indent=2))
            return 0
        print("sin mensajes nuevos", file=sys.stderr)
        return 3

    if argv[:1] == ["--wait"]:
        max_seconds = 0
        if "--max-seconds" in argv:
            max_seconds = int(argv[argv.index("--max-seconds") + 1])
        poll = int(cfg.get("poll_seconds", 120))
        start = time.time()
        errors = 0
        while True:
            try:
                hits = check(cfg, state)
                errors = 0
            except (RuntimeError, urllib.error.URLError) as e:
                errors += 1
                print(f"[{errors}/{MAX_CONSECUTIVE_ERRORS}] {e}", file=sys.stderr)
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    return 2
                hits = []
            save_state(state)
            if hits:
                print(json.dumps(hits, ensure_ascii=False, indent=2))
                return 0
            if max_seconds and time.time() - start > max_seconds:
                print("timeout sin novedad", file=sys.stderr)
                return 3
            time.sleep(poll)

    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
