#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
session_context.py — Hook SessionStart de Claude Code: INYECCION de memoria del hub.

Contraparte de session_observer.py (fase 4 de memoria pasiva): al ARRANCAR una sesion
sobre un proyecto registrado en el hub, inyecta via additionalContext un bloque compacto
con lo que el hub sabe: ultimas sesiones (observaciones resumidas), handoffs pendientes
y mensajes sin leer. Es el `nexus_boot` automatico, sin que el modelo tenga que llamarlo.

Costo controlado: el bloque se recorta a `inject_max_chars` (default 1200 chars ≈ ~300
tokens) y se puede apagar con `inject_context: false` en observer/config.json.

Guardas (mismas que el observer):
  - Sesiones headless del listener se saltan (env NEXUS_LISTENER=1).
  - Solo proyectos registrados en el hub (y opt-in segun config 'projects').
  - Nunca rompe la sesion: cualquier error va al log y sale con codigo 0 sin output.

Registro del hook (~/.claude/settings.json), junto al SessionEnd del observer:
  "SessionStart": [{"hooks": [{"type": "command",
    "command": "python \"<ruta-al-repo>/observer/session_context.py\""}]}]

Uso manual (ver que inyectaria):
    python observer/session_context.py --cwd <ruta-de-un-proyecto>
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_observer as so  # reutiliza db(), resolve_project(), config y log

INJECT_DEFAULTS = {
    "inject_context": True,   # apagar con false si el bloque molesta o pesa mucho
    "inject_max_chars": 1200,  # presupuesto duro del bloque (≈300 tokens)
    "inject_observations": 3,  # cuantas sesiones recientes mostrar
}


def load_config():
    cfg = dict(INJECT_DEFAULTS)
    cfg.update(so.load_config())  # comparte observer/config.json (projects, etc.)
    return cfg


def _clip(text, width):
    t = (text or "").replace("\n", " ").strip()
    return t[:width] + ("..." if len(t) > width else "")


def build_context(conn, cfg, project):
    """Bloque compacto de memoria del hub para 'project'. None si no hay nada que decir."""
    lines = []
    try:
        obs = conn.execute(
            "SELECT branch, first_prompt, summary, created_at FROM observations "
            "WHERE project=? ORDER BY created_at DESC LIMIT ?",
            (project, int(cfg.get("inject_observations", 3)))).fetchall()
    except Exception:  # tabla aun no creada u otro problema: sin sesiones
        obs = []
    if obs:
        lines.append("Ultimas sesiones sobre este proyecto:")
        for r in obs:
            fecha = (r["created_at"] or "")[:10]
            detalle = _clip(r["summary"] or r["first_prompt"], 160)
            lines.append(f"- {fecha} [{r['branch'] or '?'}] {detalle}")
    try:
        hand = conn.execute(
            "SELECT from_project, stage, created_at FROM handoffs "
            "WHERE to_project=? AND status='pending' ORDER BY created_at DESC",
            (project,)).fetchall()
    except Exception:
        hand = []
    if hand:
        tops = "; ".join(f"'{h['stage'] or '-'}' (de {h['from_project']})" for h in hand[:3])
        extra = f" y {len(hand) - 3} mas" if len(hand) > 3 else ""
        lines.append(f"Handoffs pendientes ({len(hand)}): {tops}{extra}.")
    try:
        msgs = conn.execute(
            "SELECT count(*) FROM messages WHERE to_project=? AND status='unread'",
            (project,)).fetchone()[0]
    except Exception:
        msgs = 0
    if msgs:
        lines.append(f"Mensajes sin leer en el buzon: {msgs}.")
    if not lines:
        return None
    header = (f"[Nexus] Memoria del hub para el proyecto '{project}' "
              "(contexto automatico; detalle: nexus_boot / nexus_timeline):")
    text = header + "\n" + "\n".join(lines)
    max_chars = int(cfg.get("inject_max_chars", 1200))
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    return text


def main(argv=None):
    if os.environ.get("NEXUS_LISTENER"):
        return 0  # los agentes headless del listener no necesitan este contexto
    parser = argparse.ArgumentParser(description="Inyector de contexto Nexus (SessionStart)")
    parser.add_argument("--cwd", help="cwd de la sesion (modo manual)")
    args = parser.parse_args(argv)
    try:
        if args.cwd:
            cwd = args.cwd
        else:
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
            cwd = payload.get("cwd") or ""
        cfg = load_config()
        if not cfg.get("inject_context", True):
            return 0
        with so.db() as conn:
            project = so.resolve_project(conn, cwd)
            if not so.should_capture(cfg, project):
                return 0
            context = build_context(conn, cfg, project)
        if not context:
            return 0
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }}, ensure_ascii=False))
        so.log(f"context inyectado para '{project}' ({len(context)} chars)")
    except Exception as exc:  # noqa: BLE001 — un hook JAMAS debe botar la sesion
        so.log(f"ERROR contexto: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
