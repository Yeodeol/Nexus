#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
session_observer.py — Hook SessionEnd de Claude Code: memoria PASIVA del hub.

Cada vez que termina una sesion de Claude Code, este script recibe por stdin el
JSON del hook (session_id, transcript_path, cwd), extrae datos DETERMINISTICOS
del transcript (sin llamar a ningun modelo: costo cero, milisegundos) y deja una
fila en la tabla `observations` de hub.db:

  - proyecto (resuelto por cwd contra la tabla `projects` del hub)
  - rama git, archivos tocados (Write/Edit/NotebookEdit), primer prompt del usuario
  - stats (conteo de tools, mensajes, duracion)

El resumen semantico NO se genera aca: lo hace el listener en idle (fase 2),
tomando las filas status='raw' con `claude -p` por suscripcion.

Guardas:
  - Sesiones headless del propio listener se SALTAN (env NEXUS_LISTENER=1).
  - Contenido entre <private>...</private> NUNCA se persiste.
  - Solo captura proyectos registrados en el hub (y opt-in segun config.json).
  - Nunca rompe la sesion: cualquier error va al log y sale con codigo 0.

Registro del hook (~/.claude/settings.json):
  "SessionEnd": [{"hooks": [{"type": "command",
    "command": "python \"<ruta-al-repo>/observer/session_observer.py\""}]}]

Uso manual (para probar sin esperar un SessionEnd real):
    python observer/session_observer.py --transcript <ruta.jsonl> --cwd <ruta> --session-id test

Sin dependencias externas (biblioteca estandar).
"""
import argparse
import json
import os
import re
import subprocess
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path.home() / ".claude-projects-hub" / "hub.db"
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
LOG_PATH = Path.home() / ".claude-projects-hub" / "observer.log"
LOG_MAX_BYTES = 512 * 1024  # el log se trunca al crecer demasiado (hook silencioso)

# Igual que el listener: sin este flag cada subproceso git abriria una consola visible.
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

DEFAULTS = {
    "projects": [],              # opt-in; VACIO = todos los proyectos registrados en el hub
    "max_files": 40,             # tope de archivos tocados que se guardan por sesion
    "first_prompt_max_chars": 400,
    "retention_days": 90,        # prune de observaciones 'raw' mas viejas (0 = nunca borrar)
}

# Tools cuyo input identifica un archivo modificado.
FILE_TOOLS = {"Write": "file_path", "Edit": "file_path",
              "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}

# Prefijos de mensajes de usuario que NO son un prompt real (wrappers del harness).
NON_PROMPT_PREFIXES = ("<command-name>", "<local-command", "<system-reminder",
                       "<command-message>", "[Request interrupted", "Caveat:")

PRIVATE_RE = re.compile(r"<private>.*?</private>", re.IGNORECASE | re.DOTALL)


def now_iso():
    return datetime.now().isoformat()


def log(msg):
    """Bitacora en archivo (el hook corre sin consola). Truncado naive al crecer."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            LOG_PATH.write_text("", encoding="utf-8")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{now_iso()}] {msg}\n")
    except OSError:
        pass  # el log nunca puede botar el hook


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            log(f"AVISO: config.json ilegible ({exc}); uso defaults.")
    return cfg


def db(path=None):
    conn = sqlite3.connect(path or DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # Esquema defensivo (mismo patron que listener y nexus-hub/server.py): no
    # dependemos de que el MCP desplegado ya haya migrado la DB.
    conn.execute("""CREATE TABLE IF NOT EXISTS observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL,
        session_id TEXT NOT NULL,
        cwd TEXT,
        branch TEXT,
        first_prompt TEXT,
        files_touched TEXT,
        stats TEXT,
        summary TEXT DEFAULT '',
        status TEXT DEFAULT 'raw',
        created_at TEXT,
        transcript_path TEXT DEFAULT '',
        UNIQUE(session_id)
    )""")
    # ALTER defensivo para DBs creadas antes de la fase 2 (el listener resume leyendo
    # el transcript, asi que la ruta se persiste).
    try:
        conn.execute("ALTER TABLE observations ADD COLUMN transcript_path TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


# --------------------------------------------------------------------------
# Resolucion de proyecto y opt-in
# --------------------------------------------------------------------------
def _norm(path):
    """Normaliza para comparar rutas (case-insensitive en Windows, separadores unificados)."""
    return os.path.normcase(os.path.normpath(str(path)))


def resolve_project(conn, cwd):
    """Nombre del proyecto del hub cuya ruta contiene a cwd (prefijo mas largo).
    None si cwd no cae dentro de ningun proyecto registrado."""
    if not cwd:
        return None
    cwd_n = _norm(cwd)
    best_name, best_len = None, -1
    try:
        rows = conn.execute("SELECT name, path FROM projects").fetchall()
    except sqlite3.OperationalError:
        return None  # hub recien creado, sin tabla projects todavia
    for r in rows:
        p = _norm(r["path"] or "")
        if not p:
            continue
        if cwd_n == p or cwd_n.startswith(p + os.sep):
            if len(p) > best_len:
                best_name, best_len = r["name"], len(p)
    return best_name


def should_capture(cfg, project):
    """Opt-in: lista vacia = todos los registrados; con nombres = solo esos."""
    if not project:
        return False
    allowed = cfg.get("projects") or []
    return not allowed or project in allowed


# --------------------------------------------------------------------------
# Parseo del transcript (JSONL de Claude Code)
# --------------------------------------------------------------------------
def strip_private(text):
    """Elimina bloques <private>...</private>: ese contenido NUNCA se persiste."""
    return PRIVATE_RE.sub("", text or "").strip()


def _user_text(message):
    """Texto plano de un mensaje de usuario ('' si es tool_result u otro bloque)."""
    content = (message or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def _is_real_prompt(text):
    t = (text or "").strip()
    return bool(t) and not any(t.startswith(p) for p in NON_PROMPT_PREFIXES)


def parse_transcript(path, cfg):
    """Extrae del JSONL: primer prompt real, archivos tocados, conteo de tools,
    rama git y timestamps. Tolerante: una linea ilegible se salta, no bota nada."""
    first_prompt = ""
    files, tool_counts = [], {}
    branch = ""
    user_msgs = 0
    first_ts = last_ts = ""
    max_files = int(cfg.get("max_files", 40))
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError as exc:
        log(f"AVISO: transcript ilegible ({exc})")
        return None
    with fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            ts = entry.get("timestamp") or ""
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            if entry.get("gitBranch"):
                branch = entry["gitBranch"]
            etype = entry.get("type")
            if etype == "user" and not entry.get("isMeta"):
                text = _user_text(entry.get("message"))
                if _is_real_prompt(text):
                    user_msgs += 1
                    if not first_prompt:
                        first_prompt = strip_private(text)  # si era 100% privado queda ''
                        # y el siguiente prompt real la reemplaza en la proxima vuelta
            elif etype == "assistant":
                content = (entry.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "?")
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                    key = FILE_TOOLS.get(name)
                    fpath = (block.get("input") or {}).get(key) if key else None
                    if fpath and fpath not in files and len(files) < max_files:
                        files.append(fpath)
    duration_min = None
    if first_ts and last_ts:
        try:
            t0 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            duration_min = round((t1 - t0).total_seconds() / 60, 1)
        except ValueError:
            pass
    max_chars = int(cfg.get("first_prompt_max_chars", 400))
    return {
        "first_prompt": first_prompt[:max_chars],
        "files": files,
        "stats": {"tools": tool_counts, "user_msgs": user_msgs,
                  "duration_min": duration_min},
        "branch": branch,
    }


def git_branch(cwd):
    """Fallback si el transcript no traia gitBranch."""
    try:
        proc = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=10,
                              creationflags=CREATE_NO_WINDOW)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


# --------------------------------------------------------------------------
# Persistencia
# --------------------------------------------------------------------------
def upsert_observation(conn, obs):
    """UPSERT por session_id: si la sesion se retoma y vuelve a terminar, se
    actualizan los datos y vuelve a 'raw' para que el listener re-resuma.
    El summary previo se conserva (el listener lo pisa al re-resumir)."""
    conn.execute("""
        INSERT INTO observations
            (project, session_id, cwd, branch, first_prompt, files_touched,
             stats, status, created_at, transcript_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'raw', ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            project=excluded.project, cwd=excluded.cwd, branch=excluded.branch,
            first_prompt=excluded.first_prompt, files_touched=excluded.files_touched,
            stats=excluded.stats, status='raw', created_at=excluded.created_at,
            transcript_path=excluded.transcript_path
    """, (obs["project"], obs["session_id"], obs["cwd"], obs["branch"],
          obs["first_prompt"], json.dumps(obs["files"], ensure_ascii=False),
          json.dumps(obs["stats"], ensure_ascii=False), now_iso(),
          obs.get("transcript_path", "")))
    # Si la sesion ya habia sido resumida (auto_runs 'done'), esa corrida vieja
    # bloquearia el re-resumen por idempotencia: se libera. auto_runs es tabla de
    # nexus-hub (escritura permitida); puede no existir aun en hubs recien creados.
    try:
        row = conn.execute("SELECT id FROM observations WHERE session_id=?",
                           (obs["session_id"],)).fetchone()
        if row:
            conn.execute("DELETE FROM auto_runs WHERE item_type='observation' AND item_id=?",
                         (row["id"],))
    except sqlite3.OperationalError:
        pass
    conn.commit()


def prune(conn, cfg):
    """Borra observaciones 'raw' viejas (las 'summarized' son el valor destilado: quedan)."""
    days = int(cfg.get("retention_days", 90))
    if days <= 0:
        return
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    conn.execute("DELETE FROM observations WHERE status='raw' AND created_at < ?", (cutoff,))
    conn.commit()


# --------------------------------------------------------------------------
# Entrada (hook stdin o CLI manual)
# --------------------------------------------------------------------------
def read_payload(argv=None):
    """Payload del hook: JSON por stdin; o flags CLI para pruebas manuales."""
    parser = argparse.ArgumentParser(description="Observer de sesiones de Nexus")
    parser.add_argument("--transcript", help="ruta al transcript JSONL (modo manual)")
    parser.add_argument("--cwd", help="cwd de la sesion (modo manual)")
    parser.add_argument("--session-id", help="id de sesion (modo manual)")
    args = parser.parse_args(argv)
    if args.transcript:
        return {"transcript_path": args.transcript, "cwd": args.cwd or os.getcwd(),
                "session_id": args.session_id or f"manual-{os.getpid()}"}
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else None
    except ValueError:
        return None


def process(payload, cfg, conn):
    """Nucleo testeable: valida, parsea y persiste. Devuelve un string de resultado."""
    session_id = payload.get("session_id") or ""
    transcript = payload.get("transcript_path") or ""
    cwd = payload.get("cwd") or ""
    if not session_id or not transcript:
        return "skip: payload incompleto"
    project = resolve_project(conn, cwd)
    if not should_capture(cfg, project):
        return f"skip: cwd fuera de proyectos capturables ({cwd})"
    parsed = parse_transcript(transcript, cfg)
    if parsed is None:
        return "skip: transcript ilegible"
    if not parsed["first_prompt"] and not parsed["files"]:
        return "skip: sesion trivial (sin prompt ni archivos tocados)"
    obs = {
        "project": project, "session_id": session_id, "cwd": cwd,
        "branch": parsed["branch"] or git_branch(cwd),
        "first_prompt": parsed["first_prompt"],
        "files": parsed["files"], "stats": parsed["stats"],
        "transcript_path": str(transcript),
    }
    upsert_observation(conn, obs)
    prune(conn, cfg)
    return (f"ok: observacion de '{project}' guardada "
            f"(files={len(obs['files'])}, branch={obs['branch'] or '?'})")


def main(argv=None):
    # Las corridas headless del propio listener tambien disparan SessionEnd:
    # se saltan para no llenar el hub con ruido de agentes automaticos.
    if os.environ.get("NEXUS_LISTENER"):
        return 0
    try:
        payload = read_payload(argv)
        if not payload:
            log("skip: sin payload por stdin")
            return 0
        cfg = load_config()
        with db() as conn:
            result = process(payload, cfg, conn)
        log(f"{payload.get('session_id', '?')}: {result}")
    except Exception as exc:  # noqa: BLE001 — un hook JAMAS debe botar la sesion
        log(f"ERROR: {exc!r}")
    return 0  # siempre 0: el hook es best-effort


if __name__ == "__main__":
    sys.exit(main())
