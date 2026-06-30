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
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

DB_PATH = Path.home() / ".claude-projects-hub" / "hub.db"
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

DEFAULTS = {
    "responders": [],          # proyectos que PUEDEN auto-responder (opt-in). Vacio = nadie.
    "poll_interval": 15,       # segundos entre sondeos (modo daemon)
    "timeout": 240,            # segundos maximos por agente headless
    "max_concurrent": 2,       # agentes en paralelo
    "model": "sonnet",         # modelo del resolver (suscripcion; sonnet = buen balance)
    "result_max_chars": 2000,  # cuanto guardar de la salida del agente en auto_runs
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
    "mcp__projects-hub__get_project",
    "mcp__projects-hub__list_projects",
    "mcp__projects-hub__get_state",
    "mcp__projects-hub__get_pending_handoffs",
    "mcp__projects-hub__consume_handoff",
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
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN kind TEXT DEFAULT 'note'")
    except sqlite3.OperationalError:
        pass  # la columna ya existe (o la tabla aun no; el MCP la crea)
    conn.commit()
    return conn


# --------------------------------------------------------------------------
# Deteccion de items a resolver
# --------------------------------------------------------------------------
def pending_items(conn, responders, project_filter, since_iso):
    """Lista de items (dict) pendientes de auto-resolver para los proyectos opt-in:
    handoffs 'pending' y messages 'unread' kind='question' aun no procesados (no en
    auto_runs) y, si hay watermark, creados despues de 'since_iso'."""
    if not responders:
        return []
    targets = [p for p in responders if (not project_filter or p == project_filter)]
    if not targets:
        return []
    ph = ",".join("?" for _ in targets)
    items = []

    q = (f"SELECT id, from_project, to_project, stage, payload, created_at FROM handoffs "
         f"WHERE status='pending' AND to_project IN ({ph}) "
         f"AND id NOT IN (SELECT item_id FROM auto_runs WHERE item_type='handoff')")
    params = list(targets)
    if since_iso:
        q += " AND created_at > ?"; params.append(since_iso)
    for r in conn.execute(q, params):
        items.append({"type": "handoff", "id": r["id"], "from": r["from_project"],
                      "to": r["to_project"], "stage": r["stage"] or "",
                      "text": r["payload"] or "", "created_at": r["created_at"]})

    q = (f"SELECT id, from_project, to_project, text, created_at FROM messages "
         f"WHERE status='unread' AND kind='question' AND to_project IN ({ph}) "
         f"AND id NOT IN (SELECT item_id FROM auto_runs WHERE item_type='message')")
    params = list(targets)
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


def claim(conn, item):
    """Reserva el item en auto_runs (idempotente). Devuelve True si lo reservamos nosotros."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO auto_runs(item_type, item_id, project, status, created_at) "
        "VALUES (?,?,?, 'claimed', ?)",
        (item["type"], item["id"], item["to"], now_iso()))
    conn.commit()
    return cur.rowcount == 1


def finish(conn, item, status, result):
    conn.execute(
        "UPDATE auto_runs SET status=?, result=?, finished_at=? WHERE item_type=? AND item_id=?",
        (status, result, now_iso(), item["type"], item["id"]))
    if item["type"] == "message":
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
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return "error", f"timeout tras {timeout}s"
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
        err = (proc.stderr or "").strip()[-600:]
        return "error", f"claude rc={proc.returncode}: {err or result_text[-600:]}"

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
    conn = db()
    try:
        if not claim(conn, item):
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
# Bucle principal
# --------------------------------------------------------------------------
def cycle(cfg, claude_bin, project_filter, since_iso, dry_run):
    conn = db()
    try:
        items = pending_items(conn, cfg["responders"], project_filter, since_iso)
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

    if args.once or args.dry_run:
        n = cycle(cfg, claude_bin, project_filter, since_iso, args.dry_run)
        log(f"Listo. {n} item(s) procesado(s).")
        return

    try:
        while True:
            cycle(cfg, claude_bin, project_filter, since_iso, dry_run=False)
            time.sleep(cfg["poll_interval"])
    except KeyboardInterrupt:
        log("Detenido por el usuario.")


if __name__ == "__main__":
    main()
