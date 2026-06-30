"""
nexus-hub — MCP "cerebro" que orquesta multiples proyectos.

Comparte la MISMA base de datos que projects-hub (~/.claude-projects-hub/hub.db).
NO toca las tablas de projects-hub (projects, state, handoffs): solo las lee.
Agrega tablas nuevas para coordinar trabajo que cruza varios repos.
"""
import sqlite3
import json
import re
import time
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
    conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_project TEXT,
        to_project TEXT NOT NULL,
        text TEXT NOT NULL,
        status TEXT DEFAULT 'unread',
        kind TEXT DEFAULT 'note',
        created_at TEXT,
        read_at TEXT
    )""")
    # Bitacora del listener autonomo: una corrida por item para idempotencia.
    # Vive en nexus-hub; NO toca las tablas de projects-hub.
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
    # ALTER defensivo: agrega 'kind' a messages de DBs viejas (CREATE no lo hace).
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN kind TEXT DEFAULT 'note'")
    except sqlite3.OperationalError:
        pass  # la columna ya existe
    return conn


def now():
    return datetime.now().isoformat()


def _project_exists(conn, name):
    return conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone() is not None


def _providers_for(conn, project):
    """Conjunto de proyectos que PROVEEN algo que 'project' CONSUME (match por name o
    category, misma logica que resolve_dependencies). Sirve para deducir a quien preguntar."""
    consumes = conn.execute(
        "SELECT name, category FROM capabilities WHERE project=? AND kind='consumes'",
        (project,)).fetchall()
    provs = set()
    for c in consumes:
        rows = conn.execute(
            "SELECT project FROM capabilities WHERE kind='provides' AND project<>? "
            "AND (name=? OR (category<>'' AND category=?))",
            (project, c["name"], c["category"])).fetchall()
        provs.update(r["project"] for r in rows)
    return provs


def _wait_for_answer(from_project, target, qid, timeout):
    """Espera (polling cada 3s) una respuesta de 'target' a 'from_project' posterior a la
    pregunta 'qid'. Devuelve el row (dict) o None si expira. Correlacion simple por id>qid
    (suficiente para un flujo de usuario secuencial); marca la respuesta como leida al
    entregarla, para no duplicarla en el buzon."""
    deadline = time.monotonic() + max(5, timeout)
    while time.monotonic() < deadline:
        time.sleep(3)
        with db() as conn:
            row = conn.execute(
                "SELECT id, text, kind, created_at FROM messages "
                "WHERE to_project=? AND from_project=? AND id>? AND kind<>'question' "
                "ORDER BY id ASC LIMIT 1",
                (from_project, target, qid)).fetchone()
            if row:
                conn.execute("UPDATE messages SET status='read', read_at=? WHERE id=?",
                             (now(), row["id"]))
                return dict(row)
    return None


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


@mcp.tool()
def get_project_context(project: str, max_chars: int = 8000) -> str:
    """Averigua el contexto de OTRO proyecto SIN pedir handoff: devuelve su descripcion,
    ruta, lo que PROVEE/CONSUME y el contenido de su CLAUDE.md (si existe en la raiz). Asi
    una sesion entiende por si misma que hace y que necesita otro sistema y se auto-sirve."""
    with db() as conn:
        row = conn.execute(
            "SELECT name, path, description FROM projects WHERE name=?", (project,)).fetchone()
        if not row:
            return f"Proyecto '{project}' no existe en el hub."
        caps = conn.execute(
            "SELECT kind, name, category, contract, notes FROM capabilities WHERE project=? "
            "ORDER BY kind, name", (project,)).fetchall()
    info = {
        "project": row["name"],
        "path": row["path"],
        "description": row["description"],
        "provides": [dict(c) for c in caps if c["kind"] == "provides"],
        "consumes": [dict(c) for c in caps if c["kind"] == "consumes"],
        "claude_md": None,
    }
    base = Path(row["path"]) if row["path"] else None
    if base:
        for fname in ("CLAUDE.md", "claude.md", "Claude.md"):
            f = base / fname
            try:
                if f.exists():
                    txt = f.read_text(encoding="utf-8", errors="replace")
                    if len(txt) > max_chars:
                        txt = txt[:max_chars] + "\n...[truncado; abre el archivo para el resto]..."
                    info["claude_md"] = txt
                    info["claude_md_file"] = str(f)
                    break
            except OSError:
                pass
    if info["claude_md"] is None:
        info["nota"] = "Sin CLAUDE.md en la raiz; usa 'path', las capacidades y get_checkpoints."
    return json.dumps(info, indent=2, ensure_ascii=False)


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
    contract o notes). Sirve para rutear: '¿quien provee lambdas de pagos?'.
    Tokeniza la consulta (separa por espacios, '_' y '-') y exige que CADA token
    aparezca en algun campo, asi 'validar clave sii' encuentra 'validar_clave_sii'."""
    tokens = [t for t in re.split(r"[\s_\-]+", query.lower().strip()) if t]
    if not tokens:
        return "La consulta esta vacia."
    clauses, params = [], []
    for t in tokens:
        like = f"%{t}%"
        clauses.append("(name LIKE ? OR category LIKE ? OR contract LIKE ? OR notes LIKE ?)")
        params.extend([like, like, like, like])
    where = " AND ".join(clauses)
    with db() as conn:
        rows = conn.execute(
            f"""SELECT project, name, category, contract FROM capabilities
               WHERE kind='provides' AND {where}
               ORDER BY project, name""",
            params).fetchall()
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


# --------------------------------------------------------------------------
# Buzon inter-sesion (mensajes asincronos ligeros entre proyectos)
# --------------------------------------------------------------------------
@mcp.tool()
def post_message(to_project: str, text: str, from_project: str = "", kind: str = "note") -> str:
    """Deja un mensaje asincrono en el buzon de OTRO proyecto. Mas ligero que un handoff
    (solo texto, sin stage/payload): para preguntas, avisos o respuestas entre sesiones de
    distintos proyectos. La otra sesion lo lee con read_messages al arrancar o al consultar.
    kind: 'note' (aviso, def.), 'question' (consulta que el listener auto-respondera) o
    'answer' (respuesta a una consulta previa)."""
    kind = (kind or "note").lower().strip()
    if kind not in ("note", "question", "answer"):
        kind = "note"
    with db() as conn:
        if not _project_exists(conn, to_project):
            return f"Proyecto destino '{to_project}' no existe en el hub."
        conn.execute(
            "INSERT INTO messages(from_project, to_project, text, status, kind, created_at) "
            "VALUES (?,?,?, 'unread', ?, ?)",
            (from_project, to_project, text, kind, now()))
    via = f" (de {from_project})" if from_project else ""
    return f"Mensaje ({kind}) dejado para '{to_project}'{via}."


@mcp.tool()
def read_messages(project: str, include_read: bool = False, mark_read: bool = True) -> str:
    """Lee el buzon de un proyecto (mensajes de otras sesiones). Por defecto solo los NO
    leidos y los marca como leidos. include_read=True trae tambien el historial reciente."""
    with db() as conn:
        if include_read:
            rows = conn.execute(
                """SELECT id, from_project, text, kind, status, created_at, read_at FROM messages
                   WHERE to_project=? ORDER BY created_at DESC LIMIT 50""", (project,)).fetchall()
            out = [dict(r) for r in rows]
        else:
            rows = conn.execute(
                """SELECT id, from_project, text, kind, created_at FROM messages
                   WHERE to_project=? AND status='unread' ORDER BY created_at""", (project,)).fetchall()
            out = [dict(r) for r in rows]
            if mark_read and out:
                conn.executemany(
                    "UPDATE messages SET status='read', read_at=? WHERE id=?",
                    [(now(), r["id"]) for r in out])
    if not out:
        scope = "" if include_read else "nuevos "
        return f"No hay mensajes {scope}para '{project}'."
    return json.dumps(out, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Consultas con auto-respuesta (gatillan al listener autonomo)
# --------------------------------------------------------------------------
@mcp.tool()
def ask_provider(from_project: str, question: str, to_project: str = "",
                 wait: bool = True, timeout: int = 120) -> str:
    """Pregunta algo a OTRO sistema y, por defecto (wait=True), ESPERA la respuesta y te la
    devuelve aca mismo (no tenes que ir al buzon despues). El listener despierta al proveedor
    headless, este investiga read-only y responde; ask_provider hace polling hasta 'timeout'
    segundos y retorna la respuesta. Requiere el daemon (listener) ENCENDIDO. Si 'to_project'
    viene vacio, deduce el proveedor desde el mapa (lo que 'from_project' consume y quien lo
    provee). wait=False solo encola y vuelve al toque. Para empujar trabajo accionable (un
    requerimiento) usa send_handoff; para una duda, usa esto."""
    question = (question or "").strip()
    if not question:
        return "La pregunta esta vacia."
    with db() as conn:
        if not _project_exists(conn, from_project):
            return f"Proyecto origen '{from_project}' no existe en el hub."
        target = (to_project or "").strip()
        if target:
            if not _project_exists(conn, target):
                return f"Proyecto destino '{target}' no existe en el hub."
        else:
            cands = _providers_for(conn, from_project)
            cands.discard(from_project)
            if len(cands) == 1:
                target = cands.pop()
            elif len(cands) > 1:
                return json.dumps({
                    "status": "ambiguo",
                    "candidatos": sorted(cands),
                    "hint": "Repite ask_provider con to_project=<uno de los candidatos>.",
                }, indent=2, ensure_ascii=False)
            else:
                return ("No pude deducir el proveedor desde el mapa (¿'" + from_project +
                        "' no declara lo que consume?). Indica to_project explicito, o declara "
                        "la dependencia con declare_capability para que el ruteo sea automatico.")
        cur = conn.execute(
            "INSERT INTO messages(from_project, to_project, text, status, kind, created_at) "
            "VALUES (?,?,?, 'unread', 'question', ?)",
            (from_project, target, question, now()))
        qid = cur.lastrowid

    if not wait:
        return json.dumps({
            "status": "encolada",
            "from": from_project,
            "to": target,
            "nota": (f"Consulta encolada para '{target}'. El listener la auto-respondera; la "
                     f"respuesta llegara al buzon de '{from_project}' (read_messages)."),
        }, indent=2, ensure_ascii=False)

    ans = _wait_for_answer(from_project, target, qid, timeout)
    if ans:
        return json.dumps({
            "status": "answered",
            "from": target,
            "to": from_project,
            "answer": ans["text"],
        }, indent=2, ensure_ascii=False)
    return json.dumps({
        "status": "pending",
        "from": from_project,
        "to": target,
        "nota": (f"No llego respuesta en {timeout}s. ¿El listener (daemon) esta encendido? La "
                 f"consulta sigue encolada: cuando responda, mirala con read_messages('{from_project}')."),
    }, indent=2, ensure_ascii=False)


@mcp.tool()
def list_auto_runs(project: str = "", limit: int = 50) -> str:
    """Lista las corridas del listener autonomo (tabla auto_runs): que item se auto-resolvio,
    para que proyecto, con que estado (claimed|answered|drafted|skipped|error) y el resultado.
    Si se pasa project, filtra por el proyecto que resolvio. Sirve de observabilidad."""
    with db() as conn:
        if project:
            rows = conn.execute(
                "SELECT * FROM auto_runs WHERE project=? ORDER BY created_at DESC LIMIT ?",
                (project, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM auto_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return "No hay corridas del listener registradas."
    return json.dumps([dict(r) for r in rows], indent=2, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
