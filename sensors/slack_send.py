"""Envia mensajes a Slack como el usuario (user token xoxp), sin pasar por el
conector de Claude — por lo tanto SIN la etiqueta "Enviado mediante Claude".

Uso:
    python slack_send.py <channel_id> "<mensaje>"
    python slack_send.py <channel_id> --file <ruta.txt>   (mensaje multilinea)
    python slack_send.py --check                          (valida el token)
    python slack_send.py --auth-url                       (paso 1 OAuth: imprime la URL)
    python slack_send.py --exchange <code>                (paso 2 OAuth: guarda el token)

channel_id: D... (DM), C... (canal) o U... (user id: abre/resuelve el DM solo).
Opcional: --thread <ts> para responder en hilo.

Token: ~/.claude/slack-user-creds.json  ->  {"user_token": "xoxp-..."}
Se crea en api.slack.com/apps (User Token Scopes: chat:write, im:write).
"""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

CREDS = Path.home() / ".claude" / "slack-user-creds.json"
API = "https://slack.com/api/"
SCOPES = "chat:write,im:write,im:history,mpim:history,channels:history,groups:history"
REDIRECT = "https://localhost:3000"


def creds():
    if not CREDS.exists():
        sys.exit(f"No existe {CREDS}")
    return json.loads(CREDS.read_text(encoding="utf-8"))


def auth_url():
    c = creds()
    if not c.get("client_id"):
        sys.exit(f'Falta "client_id" en {CREDS} (Basic Information -> App Credentials)')
    q = urllib.parse.urlencode({
        "client_id": c["client_id"],
        "user_scope": SCOPES,
        "redirect_uri": REDIRECT,
    })
    print(f"https://slack.com/oauth/v2/authorize?{q}")


def exchange(code):
    c = creds()
    for k in ("client_id", "client_secret"):
        if not c.get(k):
            sys.exit(f'Falta "{k}" en {CREDS} (Basic Information -> App Credentials)')
    data = urllib.parse.urlencode({
        "client_id": c["client_id"],
        "client_secret": c["client_secret"],
        "code": code,
        "redirect_uri": REDIRECT,
    }).encode("utf-8")
    req = urllib.request.Request(API + "oauth.v2.access", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read().decode("utf-8"))
    if not out.get("ok"):
        sys.exit(f"Slack error en oauth.v2.access: {out.get('error')}")
    tok = out.get("authed_user", {}).get("access_token", "")
    if not tok.startswith("xoxp-"):
        sys.exit(f"La respuesta no trajo user token (scopes de usuario ausentes): {out}")
    c["user_token"] = tok
    CREDS.write_text(json.dumps(c, indent=2), encoding="utf-8")
    print(f"token guardado en {CREDS}")


def token():
    if not CREDS.exists():
        sys.exit(f"No existe {CREDS}. Crear con: {{\"user_token\": \"xoxp-...\"}}")
    t = json.loads(CREDS.read_text(encoding="utf-8")).get("user_token", "")
    if not t.startswith("xoxp-"):
        sys.exit(f"Token invalido en {CREDS}: debe empezar con xoxp- (User OAuth Token de la app)")
    return t


def call(method, payload):
    req = urllib.request.Request(
        API + method,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read().decode("utf-8"))
    if not out.get("ok"):
        sys.exit(f"Slack error en {method}: {out.get('error')}")
    return out


def main(argv):
    if argv[:1] == ["--auth-url"]:
        auth_url()
        return

    if argv[:1] == ["--exchange"]:
        if len(argv) < 2:
            sys.exit("Falta el code: --exchange <code>")
        exchange(argv[1])
        return

    if argv[:1] == ["--check"]:
        out = call("auth.test", {})
        print(f"ok: user={out.get('user')} ({out.get('user_id')}) team={out.get('team')}")
        return

    if len(argv) < 2:
        sys.exit(__doc__)

    channel, rest = argv[0], argv[1:]
    thread_ts = None
    if "--thread" in rest:
        i = rest.index("--thread")
        thread_ts = rest[i + 1]
        rest = rest[:i] + rest[i + 2:]
    if rest[0] == "--file":
        text = Path(rest[1]).read_text(encoding="utf-8")
    else:
        text = rest[0]

    if channel.startswith("U"):
        channel = call("conversations.open", {"users": channel})["channel"]["id"]

    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    out = call("chat.postMessage", payload)
    print(f"enviado: channel={out['channel']} ts={out['ts']}")


if __name__ == "__main__":
    main(sys.argv[1:])
