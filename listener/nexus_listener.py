#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nexus_listener.py — Daemon que AUTO-RESUELVE el trabajo cross-sistema de Nexus.

Sondea la base del hub (~/.claude-projects-hub/hub.db) y, cuando aparece un handoff
o una consulta dirigida a un proyecto OPT-IN, despierta a ese proyecto lanzando un
Claude Code headless (`claude -p`) con cwd = la raiz del proyecto. Ese agente:

  - CONSULTA SIMPLE  -> investiga read-only (Read/Grep/Glob + tools de Nexus) y responde
                        por el buzon (post_message kind='answer'); cierra el handoff.
  - REQUERIMIENTO    -> NO toca codigo ni git: redacta un BORRADOR de alcance (checkpoint)
                        y avisa por el buzon; deja el handoff PENDING para el humano.

Seguridad: el agente corre con una allowlist de tools de SOLO LECTURA + tools del hub
(sin Write/Edit/Bash), permission-mode 'default' (lo no permitido se deniega), timeout y
una corrida por item (tabla auto_runs, idempotente). El motor es la suscripcion de Claude
Code (no usa API key de pago).

Uso (PowerShell, sin cd; ya estas en la carpeta del repo):
    python listener/nexus_listener.py --once --dry-run --backlog   # ver que tomaria
    python listener/nexus_listener.py --once                       # drena backlog nuevo y sale
    python listener/nexus_listener.py                              # daemon (bucle)
    python listener/nexus_listener.py --project respaldos-scraps   # solo un proyecto

Sin dependencias externas (biblioteca estandar).
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path.home() / ".claude-projects-hub" / "hub.db"
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
RUNS_DIR = Path.home() / ".claude-projects-hub" / "listener-runs"

# El daemon corre con pythonw (sin consola): sin este flag, CADA subproceso (git,
# claude) abriria una ventana de consola visible que aparece y se cierra.
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Env de los agentes headless: NEXUS_LISTENER=1 hace que el hook SessionEnd
# (observer/session_observer.py) NO registre estas corridas como sesiones humanas.
AGENT_ENV = {**os.environ, "NEXUS_LISTENER": "1"}

# Cada cuanto revisar en idle si hay fichas por refrescar (evita correr git rev-parse
# por proyecto en cada poll de 15s; el sondeo de items sigue siendo cada poll_interval).
KNOWLEDGE_CHECK_SECONDS = 600

DEFAULTS = {
    "responders": [],          # proyectos que PUEDEN auto-responder (opt-in). Vacio = nadie.
    "poll_interval": 15,       # segundos entre sondeos (modo daemon)
    "timeout": 240,            # segundos maximos por agente headless
    "max_concurrent": 2,       # agentes en paralelo
    "model": "sonnet",         # modelo del resolver (suscripcion; sonnet = buen balance)
    "result_max_chars": 2000,  # cuanto guardar de la salida del agente en auto_runs
    "max_retries": 1,          # reintentos EXTRA de un item que quedo en error (0 = ninguno)
    "retry_cooldown": 300,     # segundos de espera antes de reintentar un error
    "knowledge_refresh_days": 7,   # fallback por edad si el repo no tiene git (0 = desactivado)
    "knowledge_projects": [],  # proyectos con fichas auto-refrescadas (vacio = responders)
    "knowledge_timeout": 600,  # timeout del agente de fichas (explora mas que un Q&A)
    "git_sync_projects": [],   # repos que se actualizan solos (fetch + pull --ff-only con guardas)
    "git_sync_hours": 24,      # cada cuantas horas corre el sync (0 = desactivado)
    "observation_projects": [],     # proyectos cuyas observaciones se resumen (vacio = TODAS)
    "observation_timeout": 120,     # timeout del resumen (tarea de texto puro, sin tools)
    "observations_per_cycle": 2,    # max resumenes por ciclo idle (no monopolizar el idle)
    "observation_dialogue_chars": 12000,  # cuanto dialogo del transcript se le pasa al modelo
}

# Allowlist ESTRECHA de tools del agente: solo lectura + tools del hub necesarias para
# responder/borradorear. SIN Write/Edit/Bash, SIN delete_project/send_handoff/declare_*.
ALLOWED_TOOLS = [
    "Read", "Grep", "Glob",
    "mcp__nexus-hub__get_project_context",
    "mcp__nexus-hub__resolve_dependencies",
    "mcp__nexus-hub__find_providers",
    "mcp__nexus-hub__list_capabilities",
    "mcp__nexus-hub__get_checkpoints",
    "mcp__nexus-hub__checkpoint",
    "mcp__nexus-hub__post_message",
    "mcp__nexus-hub__read_messages",
    "mcp__nexus-hub__list_auto_runs",
    "mcp__nexus-hub__get_knowledge",
    "mcp__nexus-hub__nexus_search",
    "mcp__projects-hub__get_project",
    "mcp__projects-hub__list_projects",
    "mcp__projects-hub__get_state",
    "mcp__projects-hub__get_pending_handoffs",
    "mcp__projects-hub__consume_handoff",
]
# El agente de FICHAS ademas puede escribir conocimiento y mantener el mapa de
# capacidades (ambas tablas del hub, UPSERT idempotente; nunca borra).
KNOWLEDGE_TOOLS = ALLOWED_TOOLS + [
    "mcp__nexus-hub__save_knowledge",
    "mcp__nexus-hub__declare_capability",
]
DISALLOWED_TOOLS = ["Write", "Edit", "NotebookEdit", "Bash"]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def now_iso():
    return datetime.now().isoformat()


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            log(f"AVISO: no pude leer {CONFIG_PATH.name} ({exc}); uso defaults.")
    # overrides por entorno
    if os.environ.get("NEXUS_RESOLVER_MODEL"):
        cfg["model"] = os.environ["NEXUS_RESOLVER_MODEL"]
    return cfg


def find_claude():
    """Ruta absoluta al CLI de Claude Code (resuelta, con extension en Windows)."""
    p = shutil.which("claude")
    if p:
        return p
    base = Path.home() / ".local" / "bin"
    for name in ("claude.exe", "claude.cmd", "claude"):
        cand = base / name
        if cand.exists():
            return str(cand)
    return None


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # Esquema propio del listener, defensivo: no dependemos de que el MCP nexus-hub ya
    # haya migrado la DB (mismo CREATE/ALTER que nexus-hub/server.py).
    conn.execute("""CREATE TABLE IF NOT EXISTS auto_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_type TEXT NOT NULL,
        item_id INTEGER NOT NULL,
        project TEXT NOT NULL,
        status TEXT DEFAULT 'claimed',
        result TEXT,
        created_at TEXT,
        finished_at TEXT,
        UNIQUE(item_type, item_id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS knowledge (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL,
        topic TEXT NOT NULL,
        content TEXT NOT NULL,
        updated_at TEXT,
        UNIQUE(project, topic)
    )""")
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN kind TEXT DEFAULT 'note'")
    except sqlite3.OperationalError:
        pass  # la columna ya existe (o la tabla aun no; el MCP la crea)
    try:
        conn.execute("ALTER TABLE auto_runs ADD COLUMN attempts INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE knowledge ADD COLUMN git_commit TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # Observaciones de sesion (las escribe el hook observer/; aca solo se resumen).
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
    try:
        conn.execute("ALTER TABLE observations ADD COLUMN transcript_path TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


# --------------------------------------------------------------------------
# Git de solo avance (cerebro vivo)
# --------------------------------------------------------------------------
def run_git(path, *args, timeout=60):
    """Corre git -C <path> <args>. Devuelve (rc, stdout limpio). rc=-1 si no se pudo."""
    try:
        proc = subprocess.run(["git", "-C", str(path), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)
        return proc.returncode, (proc.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


def repo_head(path):
    rc, out = run_git(path, "rev-parse", "HEAD", timeout=10)
    return out if rc == 0 else ""


# --------------------------------------------------------------------------
# Deteccion de items a resolver
# --------------------------------------------------------------------------
def pending_items(conn, cfg, project_filter, since_iso):
    """Lista de items (dict) pendientes de auto-resolver para los proyectos opt-in:
    handoffs 'pending' y messages 'unread' kind='question' aun no procesados y, si hay
    watermark, creados despues de 'since_iso'. Un item en auto_runs solo se excluye si
    termino bien O agoto los reintentos O su error es muy reciente (retry_cooldown):
    asi un fallo transitorio no mata el item para siempre."""
    responders = cfg["responders"]
    if not responders:
        return []
    targets = [p for p in responders if (not project_filter or p == project_filter)]
    if not targets:
        return []
    ph = ",".join("?" for _ in targets)
    max_attempts = 1 + max(0, int(cfg.get("max_retries", 0)))
    retry_cutoff = (datetime.now() - timedelta(seconds=cfg.get("retry_cooldown", 300))).isoformat()
    # Excluye del sondeo todo item ya corrido, SALVO errores reintentables "enfriados".
    excl = ("SELECT item_id FROM auto_runs WHERE item_type=? AND NOT "
            "(status='error' AND attempts < ? AND finished_at <= ?)")
    items = []

    q = (f"SELECT id, from_project, to_project, stage, payload, created_at FROM handoffs "
         f"WHERE status='pending' AND to_project IN ({ph}) AND id NOT IN ({excl})")
    params = list(targets) + ["handoff", max_attempts, retry_cutoff]
    if since_iso:
        q += " AND created_at > ?"; params.append(since_iso)
    for r in conn.execute(q, params):
        items.append({"type": "handoff", "id": r["id"], "from": r["from_project"],
                      "to": r["to_project"], "stage": r["stage"] or "",
                      "text": r["payload"] or "", "created_at": r["created_at"]})

    # Nota: los messages en error siguen 'unread' (finish solo marca leidos los exitosos
    # via el propio flujo), pero el filtro real es auto_runs; el status del mensaje se
    # actualiza al cerrar la corrida.
    q = (f"SELECT id, from_project, to_project, text, created_at FROM messages "
         f"WHERE kind='question' AND to_project IN ({ph}) AND id NOT IN ({excl}) "
         f"AND status='unread'")
    params = list(targets) + ["message", max_attempts, retry_cutoff]
    if since_iso:
        q += " AND created_at > ?"; params.append(since_iso)
    for r in conn.execute(q, params):
        items.append({"type": "message", "id": r["id"], "from": r["from_project"],
                      "to": r["to_project"], "stage": "consulta",
                      "text": r["text"] or "", "created_at": r["created_at"]})

    items.sort(key=lambda x: x["created_at"] or "")
    return items


def project_path(conn, name):
    row = conn.execute("SELECT path FROM projects WHERE name=?", (name,)).fetchone()
    return row["path"] if row and row["path"] else None


def claim(conn, item, max_attempts):
    """Reserva el item en auto_runs (idempotente). Devuelve True si lo reservamos nosotros.
    Si el item quedo en 'error' y aun tiene reintentos, lo re-reclama sumando el intento."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO auto_runs(item_type, item_id, project, status, attempts, created_at) "
        "VALUES (?,?,?, 'claimed', 1, ?)",
        (item["type"], item["id"], item["to"], now_iso()))
    if cur.rowcount == 1:
        conn.commit()
        return True
    cur = conn.execute(
        "UPDATE auto_runs SET status='claimed', attempts=attempts+1 "
        "WHERE item_type=? AND item_id=? AND status='error' AND attempts < ?",
        (item["type"], item["id"], max_attempts))
    conn.commit()
    return cur.rowcount == 1


def finish(conn, item, status, result):
    conn.execute(
        "UPDATE auto_runs SET status=?, result=?, finished_at=? WHERE item_type=? AND item_id=?",
        (status, result, now_iso(), item["type"], item["id"]))
    # Un mensaje en error queda 'unread' a proposito: asi puede reintentarse (el filtro
    # anti-duplicado real es auto_runs).
    if item["type"] == "message" and status != "error":
        conn.execute("UPDATE messages SET status='read', read_at=? WHERE id=?",
                     (now_iso(), item["id"]))
    # Log de interaccion (resolver B -> origen A) para el monitoreo / dashboard.
    conn.execute(
        "INSERT INTO interactions(from_project, to_project, intent, capability, outcome, feature, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (item["to"], item["from"], f"auto:{item['stage']}"[:120], "", f"auto-{status}", "", now_iso()))
    conn.commit()


# --------------------------------------------------------------------------
# Construccion y ejecucion del agente headless
# --------------------------------------------------------------------------
def system_prompt(target, asker):
    return (
        f"Sos el RESOLUTOR AUTONOMO del proyecto '{target}' en Nexus. Trabajas SIN "
        f"supervision humana en este momento: se CONSERVADOR.\n\n"
        "REGLAS DURAS (inquebrantables):\n"
        "- SOLO podes LEER e INFORMAR. NUNCA modifiques archivos, NUNCA uses git, NUNCA "
        "ejecutes comandos que cambien estado. No tenes Write/Edit/Bash; no los pidas.\n\n"
        "QUE HACER:\n"
        "1. Clasifica la solicitud:\n"
        "   - CONSULTA SIMPLE (una duda: de donde sale algo, que devuelve un endpoint, si "
        "algo es reutilizable, un contrato): investigala leyendo este repo (Read/Grep/Glob) "
        "y, si depende de otro sistema, usa get_project_context/resolve_dependencies. Luego "
        f"RESPONDE con post_message(to_project='{asker}', from_project='{target}', "
        "text=<respuesta concreta y breve>) (agrega kind='answer' si el tool lo acepta). Si "
        "vino como handoff, cierra con consume_handoff(handoff_id).\n"
        "   - REQUERIMIENTO (pide implementar o cambiar codigo): NO lo implementes. Redacta un "
        "BORRADOR DE ALCANCE (que se tocaria, archivos, contratos, riesgos, esfuerzo) y "
        f"guardalo con checkpoint(project='{target}', summary=<borrador>). Avisa con "
        f"post_message(to_project='{asker}', from_project='{target}', "
        "text=<resumen del borrador + 'pendiente de aprobacion humana'>). NO consumas el "
        "handoff: dejalo PENDING para el humano.\n"
        "2. No inventes: si no encontras algo en el repo o el mapa, dilo. Espanol de Chile, "
        "breve y concreto.\n\n"
        "TU ULTIMA LINEA debe ser exactamente 'RESULTADO: answered' (si respondiste una "
        "consulta) o 'RESULTADO: drafted' (si dejaste un borrador de requerimiento)."
    )


def user_prompt(item):
    if item["type"] == "handoff":
        return (
            f"Te llego un HANDOFF en Nexus dirigido a tu proyecto '{item['to']}'.\n"
            f"- De: {item['from']}\n- Etapa/stage: {item['stage']}\n"
            f"- handoff_id = {item['id']}  (usalo en consume_handoff si lo resolves como consulta)\n"
            f"- Contenido (payload):\n{item['text']}\n\n"
            "Resuelvelo segun tu rol (ver system prompt)."
        )
    return (
        f"Te llego una CONSULTA en Nexus dirigida a tu proyecto '{item['to']}'.\n"
        f"- De: {item['from']}\n- message_id = {item['id']}\n- Pregunta:\n{item['text']}\n\n"
        f"Investiga read-only y responde con post_message(to_project='{item['from']}', "
        f"from_project='{item['to']}', text=...). No llames read_messages "
        "(la pregunta ya esta aca)."
    )


def save_run_log(tag, cmd, rc, stdout, stderr):
    """Persiste la corrida COMPLETA (comando, rc, stdout, stderr) en un archivo, para
    diagnosticar errores que en auto_runs quedan truncados/vacios. Devuelve la ruta."""
    try:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        f = RUNS_DIR / f"{ts}_{tag}.log"
        f.write_text(
            f"# {ts}  rc={rc}\n# cmd: {' '.join(str(c) for c in cmd[:4])} ...\n\n"
            f"## STDOUT\n{stdout or '(vacio)'}\n\n## STDERR\n{stderr or '(vacio)'}\n",
            encoding="utf-8", errors="replace")
        return str(f)
    except OSError:
        return ""


def run_agent(claude_bin, cwd, model, timeout, item):
    """Lanza `claude -p` headless en cwd. Devuelve (status, result_text)."""
    cmd = [
        claude_bin, "-p", user_prompt(item),
        "--append-system-prompt", system_prompt(item["to"], item["from"]),
        "--allowedTools", *ALLOWED_TOOLS,
        "--disallowedTools", *DISALLOWED_TOOLS,
        "--permission-mode", "default",
        "--model", model,
        "--output-format", "json",
        "--add-dir", cwd,
    ]
    tag = f"{item['type']}{item['id']}_{item['to']}"
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              creationflags=CREATE_NO_WINDOW, env=AGENT_ENV)
    except subprocess.TimeoutExpired as exc:
        logf = save_run_log(tag, cmd, "timeout", exc.stdout, exc.stderr)
        return "error", f"timeout tras {timeout}s (log: {logf})"
    except (OSError, ValueError) as exc:
        return "error", f"fallo al lanzar claude: {exc}"

    out = (proc.stdout or "").strip()
    result_text = out
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            result_text = data.get("result") or data.get("text") or out
    except ValueError:
        pass  # no era JSON; nos quedamos con stdout crudo

    if proc.returncode != 0:
        logf = save_run_log(tag, cmd, proc.returncode, proc.stdout, proc.stderr)
        err = (proc.stderr or "").strip()[-600:]
        return "error", f"claude rc={proc.returncode}: {err or result_text[-600:]} (log: {logf})"

    status = "done"
    for line in reversed(result_text.splitlines()):
        s = line.strip().lower()
        if s.startswith("resultado:"):
            tok = s.split(":", 1)[1].strip()
            if "answer" in tok:
                status = "answered"
            elif "draft" in tok:
                status = "drafted"
            break
    return status, result_text


def process_item(cfg, claude_bin, item):
    """Reclama, ejecuta y cierra un item. Una conexion por hilo (sqlite no comparte conn)."""
    max_attempts = 1 + max(0, int(cfg.get("max_retries", 0)))
    conn = db()
    try:
        if not claim(conn, item, max_attempts):
            return f"skip (ya reclamado) {item['type']}#{item['id']}"
        path = project_path(conn, item["to"])
        if not path or not Path(path).exists():
            finish(conn, item, "skipped", f"sin ruta valida para '{item['to']}'")
            return f"skipped (sin ruta) {item['type']}#{item['id']} -> {item['to']}"
        log(f"resolviendo {item['type']}#{item['id']} para '{item['to']}' (de {item['from']})...")
        status, result = run_agent(claude_bin, path, cfg["model"], cfg["timeout"], item)
        finish(conn, item, status, (result or "")[: cfg["result_max_chars"]])
        return f"{status} {item['type']}#{item['id']} -> {item['to']}"
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Fichas de conocimiento (refresh en idle)
# --------------------------------------------------------------------------
def knowledge_system_prompt(project):
    return (
        f"Sos el DOCUMENTADOR AUTONOMO del proyecto '{project}' en Nexus. Tu unico trabajo "
        "es generar/actualizar FICHAS DE CONOCIMIENTO del repo en el hub, para que otras "
        "sesiones respondan consultas sin releer el codigo.\n\n"
        "REGLAS DURAS: SOLO LECTURA del repo (Read/Grep/Glob). NUNCA modifiques archivos ni "
        "uses git. La UNICA escritura permitida es save_knowledge (tabla del hub).\n\n"
        "QUE HACER:\n"
        "1. Explora el repo (CLAUDE.md, README, codigo fuente principal).\n"
        f"2. Guarda 3 a 6 fichas con save_knowledge(project='{project}', topic=..., "
        "content=...). Topics sugeridos (usa los que apliquen):\n"
        "   - 'resumen': que es, stack, objetivo, como se corre.\n"
        "   - 'endpoints-contratos': endpoints/lambdas/funciones publicas con input/output REAL.\n"
        "   - 'datos': tablas/modelos/esquemas principales.\n"
        "   - 'flujos-clave': los 3-5 flujos de negocio centrales y que archivos los implementan.\n"
        "   - 'integraciones': que consume de otros sistemas y que le provee a quien.\n"
        "3. Contenido CONCRETO: rutas de archivo, firmas, contratos reales. Nada de vaguedades. "
        "Espanol de Chile. Cada ficha entre 500 y 3000 caracteres.\n"
        "4. Ademas de las fichas, manten el MAPA de capacidades del hub: por cada "
        "endpoint/lambda/servicio/tabla REAL que este repo OFRECE a otros sistemas, "
        f"declare_capability(project='{project}', kind='provides', name=<kebab-case>, "
        "category=<api|lambda|table|service|event>, contract=<input/output real>); por cada "
        "dependencia de OTRO sistema que este repo usa, lo mismo con kind='consumes'. Solo "
        "capacidades VERIFICADAS en el codigo (es UPSERT idempotente; no borra nada).\n\n"
        "TU ULTIMA LINEA debe ser exactamente 'RESULTADO: refreshed'."
    )


def stale_knowledge(conn, cfg, project_filter, force=False):
    """Proyectos cuyas fichas requieren refresh (cerebro vivo). Criterio:
    1) sin fichas; 2) el HEAD del repo CAMBIO desde la ultima ficha (git-aware: si el
    commit no cambio, las fichas estan al dia sin importar la edad); 3) fallback por
    edad (knowledge_refresh_days) solo cuando no hay info git para comparar."""
    days = int(cfg.get("knowledge_refresh_days", 0))
    if days <= 0 and not force:
        return []
    projs = cfg.get("knowledge_projects") or cfg.get("responders") or []
    if project_filter:
        projs = [p for p in projs if p == project_filter]
    if force:
        return list(projs)
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    out = []
    for p in projs:
        row = conn.execute(
            "SELECT updated_at, git_commit FROM knowledge WHERE project=? "
            "ORDER BY updated_at DESC LIMIT 1", (p,)).fetchone()
        if not row:
            out.append(p)
            continue
        stored = row["git_commit"] or ""
        head = repo_head(project_path(conn, p) or "")
        if head and stored:
            if head != stored:
                out.append(p)  # el repo cambio: la ficha quedo vieja
            continue           # mismo commit: al dia, la edad no importa
        if row["updated_at"] < cutoff:  # sin info git: fallback por edad
            out.append(p)
    return out


def refresh_knowledge(cfg, claude_bin, project):
    """Lanza el agente documentador sobre un proyecto y deja bitacora en auto_runs
    (item_type='knowledge', item_id=epoch: es un log recurrente, no idempotencia)."""
    conn = db()
    try:
        path = project_path(conn, project)
        if not path or not Path(path).exists():
            return f"knowledge skipped (sin ruta) {project}"
        cmd = [
            claude_bin, "-p",
            f"Genera/actualiza las fichas de conocimiento del proyecto '{project}' "
            "segun tu rol (ver system prompt).",
            "--append-system-prompt", knowledge_system_prompt(project),
            "--allowedTools", *KNOWLEDGE_TOOLS,
            "--disallowedTools", *DISALLOWED_TOOLS,
            "--permission-mode", "default",
            "--model", cfg["model"],
            "--output-format", "json",
            "--add-dir", path,
        ]
        log(f"refrescando fichas de conocimiento de '{project}'...")
        started = now_iso()
        try:
            proc = subprocess.run(cmd, cwd=path, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=cfg.get("knowledge_timeout", 600),
                                  creationflags=CREATE_NO_WINDOW, env=AGENT_ENV)
            rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            rc, stdout, stderr = "timeout", exc.stdout, exc.stderr
        except (OSError, ValueError) as exc:
            rc, stdout, stderr = "launch-error", "", str(exc)
        out = (stdout or "").strip()
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                out = data.get("result") or data.get("text") or out
        except ValueError:
            pass
        ok = (rc == 0)
        status = "refreshed" if ok else "error"
        logf = "" if ok else save_run_log(f"knowledge_{project}", cmd, rc, stdout, stderr)
        result = out if ok else f"rc={rc}: {(stderr or out or '')[-400:]} (log: {logf})"
        conn.execute(
            "INSERT OR IGNORE INTO auto_runs(item_type, item_id, project, status, attempts, "
            "result, created_at, finished_at) VALUES ('knowledge', ?, ?, ?, 1, ?, ?, ?)",
            (int(time.time()), project, status, result[: cfg["result_max_chars"]],
             started, now_iso()))
        conn.commit()
        return f"knowledge {status} -> {project}"
    finally:
        conn.close()


def knowledge_cycle(cfg, claude_bin, project_filter, dry_run, attempted, force=False):
    """Refresca a lo mas UN proyecto por ciclo (no compite con los items). 'attempted'
    (dict en memoria) evita martillar el mismo proyecto si falla: reintenta en 6h."""
    conn = db()
    try:
        stale = stale_knowledge(conn, cfg, project_filter, force)
    finally:
        conn.close()
    stale = [p for p in stale if time.monotonic() - attempted.get(p, -10**9) > 6 * 3600]
    if not stale:
        return 0
    if dry_run:
        log(f"DRY-RUN: fichas por refrescar: {', '.join(stale)}")
        return 0
    targets = stale if force else stale[:1]
    for target in targets:
        attempted[target] = time.monotonic()
        log("  " + refresh_knowledge(cfg, claude_bin, target))
    return len(targets)


# --------------------------------------------------------------------------
# Resumen de observaciones de sesion (memoria pasiva, en idle)
# --------------------------------------------------------------------------
# Paridad con observer/session_observer.py: mismos wrappers a saltar y mismo filtro
# de privacidad (lo <private> tampoco puede llegar al resumen).
OBS_NON_PROMPT_PREFIXES = ("<command-name>", "<local-command", "<system-reminder",
                           "<command-message>", "[Request interrupted", "Caveat:")
OBS_PRIVATE_RE = re.compile(r"<private>.*?</private>", re.IGNORECASE | re.DOTALL)


def extract_dialogue(path, max_chars=12000):
    """Dialogo plano del transcript JSONL: prompts del usuario y texto del asistente
    (sin tool results ni wrappers). Si excede max_chars se queda inicio + final (el
    cierre de la sesion suele concentrar las conclusiones). None si no se pudo leer."""
    lines = []
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for raw in fh:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("isMeta"):
                continue
            etype = entry.get("type")
            content = (entry.get("message") or {}).get("content")
            texts = []
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                texts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
            for t in texts:
                t = OBS_PRIVATE_RE.sub("", t or "").strip()
                if not t or any(t.startswith(p) for p in OBS_NON_PROMPT_PREFIXES):
                    continue
                who = "USUARIO" if etype == "user" else "ASISTENTE"
                if etype in ("user", "assistant"):
                    lines.append(f"{who}: {t}")
    text = "\n\n".join(lines)
    if len(text) > max_chars:
        half = max_chars // 2
        text = text[:half] + "\n\n[... dialogo recortado ...]\n\n" + text[-half:]
    return text


def pending_observations(conn, cfg, project_filter):
    """Observaciones 'raw' por resumir, excluyendo las ya corridas en auto_runs
    (item_type='observation'), con la misma logica de reintentos que los items."""
    max_attempts = 1 + max(0, int(cfg.get("max_retries", 0)))
    retry_cutoff = (datetime.now() - timedelta(seconds=cfg.get("retry_cooldown", 300))).isoformat()
    excl = ("SELECT item_id FROM auto_runs WHERE item_type='observation' AND NOT "
            "(status='error' AND attempts < ? AND finished_at <= ?)")
    q = (f"SELECT id, project, session_id, branch, first_prompt, files_touched, "
         f"transcript_path, created_at FROM observations "
         f"WHERE status='raw' AND id NOT IN ({excl})")
    params = [max_attempts, retry_cutoff]
    projs = cfg.get("observation_projects") or []
    if projs:
        ph = ",".join("?" for _ in projs)
        q += f" AND project IN ({ph})"
        params += list(projs)
    if project_filter:
        q += " AND project=?"
        params.append(project_filter)
    q += " ORDER BY created_at"
    return [dict(r) for r in conn.execute(q, params)]


def summarize_observation(cfg, claude_bin, obs):
    """Resume UNA observacion con `claude -p` de texto puro (sin tools ni repo: el
    dialogo ya va en el prompt). raw -> summarized. Bitacora idempotente en auto_runs."""
    conn = db()
    item = {"type": "observation", "id": obs["id"], "to": obs["project"]}
    try:
        if not claim(conn, item, 1 + max(0, int(cfg.get("max_retries", 0)))):
            return f"obs#{obs['id']} skip (ya reclamada)"
        dialogue = extract_dialogue(obs.get("transcript_path") or "",
                                    int(cfg.get("observation_dialogue_chars", 12000)))
        if not dialogue:
            # Sin transcript no hay que resumir; se cierra para no reintentar eterno.
            conn.execute("UPDATE observations SET status='summarized', "
                         "summary='(sin transcript disponible)' WHERE id=?", (obs["id"],))
            conn.execute("UPDATE auto_runs SET status='skipped', result='sin transcript', "
                         "finished_at=? WHERE item_type='observation' AND item_id=?",
                         (now_iso(), obs["id"]))
            conn.commit()
            return f"obs#{obs['id']} skipped (sin transcript) {obs['project']}"
        try:
            files = ", ".join(json.loads(obs.get("files_touched") or "[]")[:15]) or "(ninguno)"
        except ValueError:
            files = "(ilegible)"
        prompt = (
            f"Resume esta sesion de trabajo sobre el proyecto '{obs['project']}' en 3 a 5 "
            "lineas: QUE se hizo, DECISIONES tomadas y PENDIENTES que quedaron. Concreto y "
            "en espanol de Chile; no inventes nada que no este en el dialogo. Responde SOLO "
            "con el resumen (sin titulos ni preambulo).\n\n"
            f"- Rama: {obs.get('branch') or '?'}\n"
            f"- Archivos tocados: {files}\n"
            f"- Dialogo (recortado):\n{dialogue}"
        )
        cmd = [claude_bin, "-p", prompt,
               "--disallowedTools", *DISALLOWED_TOOLS,
               "--permission-mode", "default",
               "--model", cfg["model"],
               "--output-format", "json"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace",
                                  timeout=int(cfg.get("observation_timeout", 120)),
                                  creationflags=CREATE_NO_WINDOW, env=AGENT_ENV)
            rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            rc, stdout, stderr = "timeout", exc.stdout, exc.stderr
        except (OSError, ValueError) as exc:
            rc, stdout, stderr = "launch-error", "", str(exc)
        out = (stdout or "").strip()
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                out = data.get("result") or data.get("text") or out
        except ValueError:
            pass
        if rc != 0 or not out.strip():
            logf = save_run_log(f"obs{obs['id']}_{obs['project']}", cmd, rc, stdout, stderr)
            conn.execute("UPDATE auto_runs SET status='error', result=?, finished_at=? "
                         "WHERE item_type='observation' AND item_id=?",
                         (f"rc={rc}: {(stderr or out or '')[-400:]} (log: {logf})",
                          now_iso(), obs["id"]))
            conn.commit()
            return f"obs#{obs['id']} error (rc={rc}) {obs['project']}"
        summary = out.strip()[:2000]
        conn.execute("UPDATE observations SET summary=?, status='summarized' WHERE id=?",
                     (summary, obs["id"]))
        conn.execute("UPDATE auto_runs SET status='done', result=?, finished_at=? "
                     "WHERE item_type='observation' AND item_id=?",
                     (summary[: cfg["result_max_chars"]], now_iso(), obs["id"]))
        conn.commit()
        return f"obs#{obs['id']} summarized -> {obs['project']}"
    finally:
        conn.close()


def observation_cycle(cfg, claude_bin, project_filter, dry_run, force=False):
    """Resume hasta observations_per_cycle observaciones 'raw' (todas con force)."""
    conn = db()
    try:
        pend = pending_observations(conn, cfg, project_filter)
    finally:
        conn.close()
    if not pend:
        return 0
    if dry_run:
        log(f"DRY-RUN: {len(pend)} observacion(es) por resumir: "
            + ", ".join(f"#{o['id']}/{o['project']}" for o in pend[:10]))
        return 0
    targets = pend if force else pend[: max(1, int(cfg.get("observations_per_cycle", 2)))]
    for obs in targets:
        log("  " + summarize_observation(cfg, claude_bin, obs))
    return len(targets)


# --------------------------------------------------------------------------
# Sync seguro de repos (git de solo avance, opt-in)
# --------------------------------------------------------------------------
def sync_repo(path):
    """Actualiza un repo de forma SEGURA: fetch siempre; pull --ff-only SOLO si el repo
    esta limpio Y parado en la rama default del remoto. Nunca hace commit, merge, push,
    rebase ni checkout: si no puede avanzar limpio, no toca nada y lo reporta."""
    if not path or not (Path(path) / ".git").exists():
        return "skip: sin repo git"
    rc, _ = run_git(path, "fetch", "origin", "--quiet", timeout=120)
    if rc != 0:
        return "error: fetch fallo (remoto/credenciales)"
    # rama default del remoto (origin/HEAD); fallback a main/master
    rc, ref = run_git(path, "symbolic-ref", "refs/remotes/origin/HEAD", "--short")
    default = ref.split("/", 1)[1] if rc == 0 and "/" in ref else ""
    if not default:
        for cand in ("main", "master"):
            rc, _ = run_git(path, "rev-parse", "--verify", f"origin/{cand}")
            if rc == 0:
                default = cand
                break
    if not default:
        return "skip: no pude determinar la rama default del remoto"
    rc, behind = run_git(path, "rev-list", "--count", f"HEAD..origin/{default}")
    behind = int(behind) if rc == 0 and behind.isdigit() else 0
    if behind == 0:
        return f"al dia con origin/{default}"
    _, branch = run_git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != default:
        return f"detras {behind} commit(s) de origin/{default} pero en rama '{branch}': NO se toca"
    rc, dirty = run_git(path, "status", "--porcelain")
    if rc != 0 or dirty:
        return f"detras {behind} commit(s) pero con cambios locales sin commitear: NO se toca"
    rc, _ = run_git(path, "pull", "--ff-only", "--quiet", timeout=180)
    if rc != 0:
        return "error: pull --ff-only fallo (historia divergente); no se toco nada"
    return f"actualizado +{behind} commit(s) desde origin/{default}"


def git_sync_due(conn, cfg):
    """True si toca correr el sync (ultima corrida 'git-sync' hace mas de git_sync_hours)."""
    hours = int(cfg.get("git_sync_hours", 0))
    if hours <= 0 or not cfg.get("git_sync_projects"):
        return False
    row = conn.execute(
        "SELECT MAX(created_at) AS m FROM auto_runs WHERE item_type='git-sync'").fetchone()
    if not row["m"]:
        return True
    return row["m"] < (datetime.now() - timedelta(hours=hours)).isoformat()


def git_sync_cycle(cfg, project_filter, dry_run, force=False):
    """Sincroniza los repos opt-in (git_sync_projects) si corresponde por calendario.
    Deja bitacora agregada en auto_runs (item_type='git-sync'). Tras un pull exitoso el
    HEAD cambia, asi que el proximo knowledge_cycle refresca las fichas solo."""
    conn = db()
    try:
        if not force and not git_sync_due(conn, cfg):
            return 0
        projs = cfg.get("git_sync_projects") or []
        if project_filter:
            projs = [p for p in projs if p == project_filter]
        if not projs:
            return 0
        if dry_run:
            log(f"DRY-RUN: git sync revisaria: {', '.join(projs)}")
            return 0
        results = []
        for p in projs:
            path = project_path(conn, p)
            res = sync_repo(path) if path else "skip: sin ruta en el hub"
            log(f"  git-sync {p}: {res}")
            results.append(f"{p}: {res}")
        conn.execute(
            "INSERT OR IGNORE INTO auto_runs(item_type, item_id, project, status, attempts, "
            "result, created_at, finished_at) VALUES ('git-sync', ?, '*', 'done', 1, ?, ?, ?)",
            (int(time.time()), " | ".join(results)[: cfg["result_max_chars"]],
             now_iso(), now_iso()))
        conn.commit()
        return len(projs)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Bucle principal
# --------------------------------------------------------------------------
def cycle(cfg, claude_bin, project_filter, since_iso, dry_run):
    conn = db()
    try:
        items = pending_items(conn, cfg, project_filter, since_iso)
    finally:
        conn.close()
    if not items:
        return 0
    if dry_run:
        log(f"DRY-RUN: {len(items)} item(s) que se tomarian:")
        for it in items:
            preview = (it["text"] or "").replace("\n", " ")[:80]
            log(f"  - {it['type']}#{it['id']}  {it['from']} -> {it['to']}  [{it['stage']}]  {preview}")
        return 0
    with ThreadPoolExecutor(max_workers=cfg["max_concurrent"]) as ex:
        futs = [ex.submit(process_item, cfg, claude_bin, it) for it in items]
        for f in as_completed(futs):
            log("  " + f.result())
    return len(items)


def main():
    ap = argparse.ArgumentParser(description="Listener autonomo de Nexus (auto-resuelve handoffs/consultas).")
    ap.add_argument("--once", action="store_true", help="procesa el backlog actual y sale")
    ap.add_argument("--dry-run", action="store_true", help="muestra que tomaria, sin lanzar agentes")
    ap.add_argument("--backlog", action="store_true", help="incluye items viejos (ignora el watermark)")
    ap.add_argument("--project", default="", help="procesar solo este proyecto (debe ser opt-in)")
    ap.add_argument("--since", default="", help="watermark ISO explicito (solo items posteriores)")
    ap.add_argument("--refresh-knowledge", action="store_true",
                    help="fuerza el refresh de fichas de conocimiento ahora (ignora frescura)")
    ap.add_argument("--summarize-observations", action="store_true",
                    help="resume TODAS las observaciones 'raw' ahora (sin tope por ciclo)")
    ap.add_argument("--git-sync", action="store_true",
                    help="fuerza el sync de repos (git_sync_projects) ahora (ignora calendario)")
    args = ap.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"No existe la BD del hub: {DB_PATH}")
    cfg = load_config()
    claude_bin = find_claude()
    if not claude_bin and not args.dry_run:
        raise SystemExit("No encontre el CLI 'claude' en PATH ni en ~/.local/bin. Instalalo o ajusta PATH.")

    if not cfg["responders"]:
        log("AVISO: no hay proyectos opt-in en listener/config.json ('responders' vacio). "
            "Nadie auto-respondera hasta que agregues proyectos a esa lista.")

    # Watermark: por defecto solo items NUEVOS (creados tras arrancar), salvo --backlog/--since.
    since_iso = "" if args.backlog else (args.since or now_iso())
    project_filter = args.project.strip()

    mode = "dry-run" if args.dry_run else ("once" if args.once else "daemon")
    log(f"Nexus listener arrancado (modo={mode}, responders={cfg['responders']}, "
        f"model={cfg['model']}, watermark={'(backlog)' if not since_iso else since_iso}).")

    attempted = {}  # cooldown en memoria de refresh de fichas fallidos/recientes

    if args.once or args.dry_run:
        # Orden: sync de repos ANTES de fichas, para que un pull dispare el refresh al tiro.
        g = git_sync_cycle(cfg, project_filter, args.dry_run, force=args.git_sync)
        n = cycle(cfg, claude_bin, project_filter, since_iso, args.dry_run)
        o = observation_cycle(cfg, claude_bin, project_filter, args.dry_run,
                              force=args.summarize_observations)
        k = knowledge_cycle(cfg, claude_bin, project_filter, args.dry_run, attempted,
                            force=args.refresh_knowledge)
        log(f"Listo. {g} repo(s) sincronizado(s), {n} item(s) procesado(s), "
            f"{o} observacion(es) resumida(s), {k} refresh(es) de fichas.")
        return

    try:
        force_kn = args.refresh_knowledge
        force_git = args.git_sync
        next_kn_check = 0.0  # chequeo de fichas throttled (no en cada poll de 15s)
        while True:
            git_sync_cycle(cfg, project_filter, False, force=force_git)
            force_git = False
            n = cycle(cfg, claude_bin, project_filter, since_iso, dry_run=False)
            if n == 0:
                # idle: primero observaciones (baratas: 1 llamada de texto puro c/u,
                # chequeo = 1 SELECT por poll), luego fichas (throttled: corre git).
                observation_cycle(cfg, claude_bin, project_filter, False)
                if force_kn or time.monotonic() >= next_kn_check:
                    knowledge_cycle(cfg, claude_bin, project_filter, False, attempted,
                                    force=force_kn)
                    force_kn = False
                    next_kn_check = time.monotonic() + KNOWLEDGE_CHECK_SECONDS
            time.sleep(cfg["poll_interval"])
    except KeyboardInterrupt:
        log("Detenido por el usuario.")


if __name__ == "__main__":
    main()
