
import sqlite3
import json
from pathlib import Path
from datetime import datetime
from mcp.server.fastmcp import FastMCP

HUB_DIR = Path.home() / ".claude-projects-hub"
HUB_DIR.mkdir(exist_ok=True)
DB_PATH = HUB_DIR / "hub.db"

mcp = FastMCP("projects-hub")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS projects (name TEXT PRIMARY KEY, path TEXT NOT NULL, description TEXT, status TEXT DEFAULT 'active', updated_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS handoffs (id INTEGER PRIMARY KEY AUTOINCREMENT, from_project TEXT NOT NULL, to_project TEXT NOT NULL, stage TEXT, payload TEXT NOT NULL, status TEXT DEFAULT 'pending', created_at TEXT, consumed_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS state (project TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated_at TEXT, PRIMARY KEY (project, key))")
    return conn


@mcp.tool()
def register_project(name: str, path: str, description: str = "") -> str:
    """Registra o actualiza un proyecto en el hub."""
    with db() as conn:
        conn.execute("INSERT INTO projects(name, path, description, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET path=excluded.path, description=excluded.description, updated_at=excluded.updated_at", (name, path, description, datetime.now().isoformat()))
    return f"Proyecto '{name}' registrado en {path}"


@mcp.tool()
def list_projects() -> str:
    """Lista todos los proyectos registrados."""
    with db() as conn:
        rows = conn.execute("SELECT name, path, description, status, updated_at FROM projects ORDER BY updated_at DESC").fetchall()
    if not rows:
        return "No hay proyectos registrados aun."
    return json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False)


@mcp.tool()
def get_project(name: str) -> str:
    """Obtiene detalles de un proyecto."""
    with db() as conn:
        row = conn.execute("SELECT name, path, description, status, updated_at FROM projects WHERE name = ?", (name,)).fetchone()
    if not row:
        return f"Proyecto '{name}' no encontrado."
    return json.dumps(dict(row), indent=2, ensure_ascii=False)


@mcp.tool()
def send_handoff(from_project: str, to_project: str, stage: str, payload: str) -> str:
    """Envia un handoff de un proyecto a otro."""
    with db() as conn:
        conn.execute("INSERT INTO handoffs(from_project, to_project, stage, payload, created_at) VALUES (?, ?, ?, ?, ?)", (from_project, to_project, stage, payload, datetime.now().isoformat()))
    return f"Handoff enviado de '{from_project}' a '{to_project}' (etapa: {stage})"


@mcp.tool()
def get_pending_handoffs(project: str) -> str:
    """Obtiene los handoffs pendientes para un proyecto."""
    with db() as conn:
        rows = conn.execute("SELECT id, from_project, stage, payload, created_at FROM handoffs WHERE to_project = ? AND status = 'pending' ORDER BY created_at ASC", (project,)).fetchall()
    if not rows:
        return f"No hay handoffs pendientes para '{project}'."
    return json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False)


@mcp.tool()
def consume_handoff(handoff_id: int) -> str:
    """Marca un handoff como consumido."""
    with db() as conn:
        conn.execute("UPDATE handoffs SET status='consumed', consumed_at=? WHERE id=?", (datetime.now().isoformat(), handoff_id))
    return f"Handoff {handoff_id} marcado como consumido."


@mcp.tool()
def set_state(project: str, key: str, value: str) -> str:
    """Guarda un valor de estado para un proyecto."""
    with db() as conn:
        conn.execute("INSERT INTO state(project, key, value, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(project, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (project, key, value, datetime.now().isoformat()))
    return f"Estado '{key}' guardado para '{project}'."


@mcp.tool()
def get_state(project: str, key: str = "") -> str:
    """Lee el estado guardado de un proyecto."""
    with db() as conn:
        if key:
            row = conn.execute("SELECT key, value, updated_at FROM state WHERE project=? AND key=?", (project, key)).fetchone()
            return json.dumps(dict(row), indent=2, ensure_ascii=False) if row else "No encontrado."
        rows = conn.execute("SELECT key, value, updated_at FROM state WHERE project=? ORDER BY updated_at DESC", (project,)).fetchall()
    return json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False) if rows else "Sin estado."


if __name__ == "__main__":
    mcp.run()