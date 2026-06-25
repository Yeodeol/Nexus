"""
nexus-hub — MCP "cerebro" que orquesta multiples proyectos.

Comparte la MISMA base de datos que projects-hub (~/.claude-projects-hub/hub.db).
NO toca las tablas de projects-hub (projects, state, handoffs): solo las lee.
Agrega tablas nuevas para coordinar trabajo que cruza varios repos.
"""
import sqlite3
import json
import re
from pathlib import Path
from datetime import datetime
from mcp.server.fastmcp import FastMCP

HUB_DIR = Path.home() / ".claude-projects-hub"
HUB_DIR.mkdir(exist_ok=True)
DB_PATH = HUB_DIR / "hub.db"

mcp = FastMCP("nexus-hub")

VALID_KINDS = ("provides", "consumes")
VALID_BRANCH_TYPES = ("feature", "fix", "hotfix", "spike")
VALID_BRANCH_STATES = ("planned", "created", "committed", "pushed", "pr-open", "merged")


def db():
    # timeout para tolerar que projects-hub escriba la misma DB al mismo tiempo.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # Tablas NUEVAS de nexus-hub. Las de projects-hub (projects/state/handoffs) NO se tocan.
    conn.execute("""CREATE TABLE IF NOT EXISTS capabilities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        category TEXT,
        contract TEXT,
        notes TEXT,
        updated_at TEXT,
        UNIQUE(project, kind, name)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS interactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_project TEXT NOT NULL,
        to_project TEXT NOT NULL,
        intent TEXT,
        capability TEXT,
        outcome TEXT,
        feature TEXT,
        created_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS coordinated_features (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        branch TEXT,
        type TEXT,
        description TEXT,
        status TEXT DEFAULT 'open',
        created_at TEXT,
        updated_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS feature_branches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        feature_id INTEGER NOT NULL,
        project TEXT NOT NULL,
        branch TEXT NOT NULL,
        state TEXT DEFAULT 'planned',
        pr_url TEXT,
        updated_at TEXT,
        UNIQUE(feature_id, project)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL,
        summary TEXT NOT NULL,
        created_at TEXT
    )""")
    return conn


def now():
    return datetime.now().isoformat()


def _project_exists(conn, name):
    return conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone() is not None


def _slugify(text):
    s = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# --------------------------------------------------------------------------
# Gestion de proyectos
# --------------------------------------------------------------------------
@mcp.tool()
def delete_project(name: str, purge_data: bool = False) -> str:
    """Elimina un proyecto del hub. SEGURO por defecto: si tiene datos asociados
    (state, handoffs, capabilities, interactions, ramas) NO borra y avisa, para no
    dejar huerfanos. Con purge_data=True borra el proyecto y TODOS sus datos."""
    with db() as conn:
        if not _project_exists(conn, name):
            return f"Proyecto '{name}' no existe en el hub."
        counts = {
            "state": conn.execute("SELECT count(*) FROM state WHERE project=?", (name,)).fetchone()[0],
            "handoffs": conn.execute("SELECT count(*) FROM handoffs WHERE from_project=? OR to_project=?", (name, name)).fetchone()[0],
            "capabilities": conn.execute("SELECT count(*) FROM capabilities WHERE project=?", (name,)).fetchone()[0],
            "interactions": conn.execute("SELECT count(*) FROM interactions WHERE from_project=? OR to_project=?", (name, name)).fetchone()[0],
            "feature_branches": conn.execute("SELECT count(*) FROM feature_branches WHERE project=?", (name,)).fetchone()[0],
        }
        total = sum(counts.values())
        if total > 0 and not purge_data:
            return (f"'{name}' tiene datos asociados: {json.dumps(counts)}. No se borro para no dejar "
                    f"huerfanos. Consolida/reasigna esos datos primero, o llama de nuevo con "
                    f"purge_data=True para borrar el proyecto y todo lo suyo.")
        if purge_data:
            conn.execute("DELETE FROM state WHERE project=?", (name,))
            conn.execute("DELETE FROM handoffs WHERE from_project=? OR to_project=?", (name, name))
            conn.execute("DELETE FROM capabilities WHERE project=?", (name,))
            conn.execute("DELETE FROM interactions WHERE from_project=? OR to_project=?", (name, name))
            conn.execute("DELETE FROM feature_branches WHERE project=?", (name,))
        conn.execute("DELETE FROM projects WHERE name=?", (name,))
    suffix = " y todos sus datos" if purge_data else ""
    return f"Proyecto '{name}'{suffix} eliminado del hub."


# --------------------------------------------------------------------------
# Capacidades (que provee / consume cada proyecto)
# --------------------------------------------------------------------------
@mcp.tool()
def declare_capability(project: str, kind: str, name: str, category: str = "",
                       contract: str = "", notes: str = "") -> str:
    """Declara una capacidad de un proyecto. kind='provides' (lo ofrece) o
    'consumes' (lo necesita). name = identificador (ej 'lambda_cobro_tarjeta').
    category agrupa ('lambda','api','table','service','event'). contract = texto/JSON
    con input/output/endpoint. Idempotente (UPSERT por project+kind+name)."""
    kind = kind.lower().strip()
    if kind not in VALID_KINDS:
        return f"kind invalido '{kind}'. Usa uno de: {', '.join(VALID_KINDS)}."
    with db() as conn:
        if not _project_exists(conn, project):
            return f"Proyecto '{project}' no existe. Registralo primero (register_project de projects-hub)."
        conn.execute(
            """INSERT INTO capabilities(project, kind, name, category, contract, notes, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(project, kind, name) DO UPDATE SET
                 category=excluded.category, contract=excluded.contract,
                 notes=excluded.notes, updated_at=excluded.updated_at""",
            (project, kind, name, category, contract, notes, now()))
    return f"Capacidad '{name}' ({kind}) declarada para '{project}'."


@mcp.tool()
def list_capabilities(project: str = "", kind: str = "", category: str = "") -> str:
    """Lista capacidades, filtrable por project, kind ('provides'/'consumes') y category."""
    q = "SELECT project, kind, name, category, contract, notes, updated_at FROM capabilities WHERE 1=1"
    params = []
    if project:
        q += " AND project=?"; params.append(project)
    if kind:
        q += " AND kind=?"; params.append(kind.lower().strip())
    if category:
        q += " AND category=?"; params.append(category)
    q += " ORDER BY project, kind, name"
    with db() as conn:
        rows = conn.execute(q, params).fetchall()
    if not rows:
        return "No hay capacidades que coincidan."
    return json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False)


@mcp.tool()
def find_providers(query: str) -> str:
    """Busca que proyectos PROVEEN algo que coincide con 'query' (en name, category,
    contract o notes). Sirve para rutear: '¿quien provee lambdas de pagos?'."""
    like = f"%{query}%"
    with db() as conn:
        rows = conn.execute(
            """SELECT project, name, category, contract FROM capabilities
               WHERE kind='provides' AND (name LIKE ? OR category LIKE ? OR contract LIKE ? OR notes LIKE ?)
               ORDER BY project, name""",
            (like, like, like, like)).fetchall()
    if not rows:
        return f"Ningun proyecto provee algo que coincida con '{query}'."
    return json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Routing: a quien hay que consultar
# --------------------------------------------------------------------------
@mcp.tool()
def resolve_dependencies(project: str, intent: str = "") -> str:
    """Dado un proyecto (y opcionalmente la intencion del trabajo), arma el mapa para
    orquestar: que CONSUME el proyecto, que otros proyectos PROVEEN eso, e
    interacciones pasadas. El cerebro usa esto para decidir a quien consultar."""
    with db() as conn:
        if not _project_exists(conn, project):
            return f"Proyecto '{project}' no existe en el hub."
        consumes = conn.execute(
            "SELECT name, category, contract, notes FROM capabilities WHERE project=? AND kind='consumes'",
            (project,)).fetchall()
        deps = []
        for c in consumes:
            providers = conn.execute(
                """SELECT project, name, category, contract FROM capabilities
                   WHERE kind='provides' AND project<>?
                     AND (name=? OR (category<>'' AND category=?))
                   ORDER BY project""",
                (project, c["name"], c["category"])).fetchall()
            deps.append({"needs": dict(c), "provided_by": [dict(p) for p in providers]})
        past = conn.execute(
            """SELECT from_project, to_project, intent, capability, outcome, feature, created_at
               FROM interactions WHERE from_project=? OR to_project=?
               ORDER BY created_at DESC LIMIT 20""",
            (project, project)).fetchall()
    result = {
        "project": project,
        "intent": intent,
        "dependencies": deps,
        "recent_interactions": [dict(r) for r in past],
        "hint": ("El cerebro decide a quien consultar segun 'provided_by'. Si 'dependencies' "
                 "esta vacio, declara lo que el proyecto consume con declare_capability."),
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Interacciones (monitoreo)
# --------------------------------------------------------------------------
@mcp.tool()
def log_interaction(from_project: str, to_project: str, intent: str = "",
                    capability: str = "", outcome: str = "", feature: str = "") -> str:
    """Registra una interaccion entre proyectos (para monitoreo). outcome sugerido:
    'consulted','reused','created','extended','noop'. feature = slug de la feature
    coordinada si aplica."""
    with db() as conn:
        conn.execute(
            """INSERT INTO interactions(from_project, to_project, intent, capability, outcome, feature, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (from_project, to_project, intent, capability, outcome, feature, now()))
    return f"Interaccion registrada: {from_project} -> {to_project} ({outcome or 'consulted'})."


@mcp.tool()
def list_interactions(project: str = "", limit: int = 50) -> str:
    """Lista interacciones (para dashboard/monitoreo). Si se pasa project, filtra por
    from o to."""
    with db() as conn:
        if project:
            rows = conn.execute(
                """SELECT * FROM interactions WHERE from_project=? OR to_project=?
                   ORDER BY created_at DESC LIMIT ?""",
                (project, project, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM interactions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return "No hay interacciones registradas."
    return json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Features coordinadas (mismas ramas en varios repos)
# --------------------------------------------------------------------------
@mcp.tool()
def create_coordinated_feature(slug: str, type: str, description: str, projects: str) -> str:
    """Crea una feature que cruza varios repos. 'projects' es una lista separada por
    comas (ej 'tienda-web,pagos-svc'). Genera la rama compartida
    '{type}/{slug}' y siembra el estado 'planned' por cada repo. Devuelve ademas el
    one-liner PowerShell (git -C, sin cd) para crear la rama en todos los repos.
    type: feature|fix|hotfix|spike."""
    btype = type.lower().strip()
    if btype not in VALID_BRANCH_TYPES:
        return f"type invalido '{btype}'. Usa uno de: {', '.join(VALID_BRANCH_TYPES)}."
    slug = _slugify(slug)
    if not slug:
        return "slug invalido (quedo vacio tras normalizar a kebab-case)."
    branch = f"{btype}/{slug}"
    proj_list = [p.strip() for p in projects.split(",") if p.strip()]
    if not proj_list:
        return "Indica al menos un proyecto en 'projects'."
    with db() as conn:
        paths, missing = {}, []
        for p in proj_list:
            row = conn.execute("SELECT path FROM projects WHERE name=?", (p,)).fetchone()
            if row:
                paths[p] = row["path"]
            else:
                missing.append(p)
        if missing:
            return f"Estos proyectos no existen en el hub: {', '.join(missing)}. Registralos primero."
        try:
            cur = conn.execute(
                """INSERT INTO coordinated_features(slug, branch, type, description, status, created_at, updated_at)
                   VALUES (?,?,?,?, 'open', ?, ?)""",
                (slug, branch, btype, description, now(), now()))
            feature_id = cur.lastrowid
        except sqlite3.IntegrityError:
            return f"Ya existe una feature coordinada con slug '{slug}'. Mirala con get_coordinated_feature."
        for p in proj_list:
            conn.execute(
                """INSERT INTO feature_branches(feature_id, project, branch, state, updated_at)
                   VALUES (?,?,?, 'planned', ?)""",
                (feature_id, p, branch, now()))
    oneliner = "; ".join([f'git -C "{paths[p]}" checkout -b {branch}' for p in proj_list])
    return json.dumps({
        "feature": slug,
        "branch": branch,
        "projects": proj_list,
        "powershell_crear_ramas": oneliner,
        "nota": "El merge de cada repo lo hace el responsable. Actualiza el avance con update_branch_state.",
    }, indent=2, ensure_ascii=False)


@mcp.tool()
def update_branch_state(slug: str, project: str, state: str, pr_url: str = "") -> str:
    """Actualiza el estado de la rama de un proyecto dentro de una feature coordinada.
    state: planned|created|committed|pushed|pr-open|merged. Si todas las ramas quedan
    'merged', la feature pasa a 'merged' automaticamente."""
    state = state.lower().strip()
    if state not in VALID_BRANCH_STATES:
        return f"state invalido '{state}'. Usa uno de: {', '.join(VALID_BRANCH_STATES)}."
    with db() as conn:
        feat = conn.execute("SELECT id FROM coordinated_features WHERE slug=?", (slug,)).fetchone()
        if not feat:
            return f"No existe feature coordinada '{slug}'."
        n = conn.execute(
            """UPDATE feature_branches SET state=?, pr_url=?, updated_at=?
               WHERE feature_id=? AND project=?""",
            (state, pr_url, now(), feat["id"], project)).rowcount
        if n == 0:
            return f"El proyecto '{project}' no esta en la feature '{slug}'."
        states = [r["state"] for r in conn.execute(
            "SELECT state FROM feature_branches WHERE feature_id=?", (feat["id"],)).fetchall()]
        extra = ""
        if states and all(s == "merged" for s in states):
            conn.execute("UPDATE coordinated_features SET status='merged', updated_at=? WHERE id=?",
                         (now(), feat["id"]))
            extra = " Todas las ramas mergeadas: feature marcada como 'merged'."
    return f"'{project}' en '{slug}' -> {state}.{extra}"


@mcp.tool()
def get_coordinated_feature(slug: str) -> str:
    """Devuelve una feature coordinada con el estado de la rama en cada repo."""
    with db() as conn:
        feat = conn.execute("SELECT * FROM coordinated_features WHERE slug=?", (slug,)).fetchone()
        if not feat:
            return f"No existe feature coordinada '{slug}'."
        branches = conn.execute(
            """SELECT project, branch, state, pr_url, updated_at FROM feature_branches
               WHERE feature_id=? ORDER BY project""", (feat["id"],)).fetchall()
    result = dict(feat)
    result["branches"] = [dict(b) for b in branches]
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def list_coordinated_features(status: str = "") -> str:
    """Lista features coordinadas, filtrable por status (open|in-progress|merged|closed)."""
    with db() as conn:
        if status:
            rows = conn.execute(
                """SELECT slug, branch, type, status, updated_at FROM coordinated_features
                   WHERE status=? ORDER BY updated_at DESC""", (status,)).fetchall()
        else:
            rows = conn.execute(
                """SELECT slug, branch, type, status, updated_at FROM coordinated_features
                   ORDER BY updated_at DESC""").fetchall()
    if not rows:
        return "No hay features coordinadas."
    return json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Checkpoints (memoria externa para no saturar el contexto del chat)
# --------------------------------------------------------------------------
@mcp.tool()
def checkpoint(project: str, summary: str) -> str:
    """Guarda un resumen/checkpoint de avance de un proyecto, para que una sesion nueva
    retome sin arrastrar todo el historial. Recuperalo con get_checkpoints."""
    with db() as conn:
        conn.execute("INSERT INTO checkpoints(project, summary, created_at) VALUES (?,?,?)",
                     (project, summary, now()))
    return f"Checkpoint guardado para '{project}'."


@mcp.tool()
def get_checkpoints(project: str, limit: int = 5) -> str:
    """Devuelve los ultimos checkpoints de un proyecto (mas recientes primero)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT summary, created_at FROM checkpoints WHERE project=? ORDER BY created_at DESC LIMIT ?",
            (project, limit)).fetchall()
    if not rows:
        return f"No hay checkpoints para '{project}'."
    return json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
