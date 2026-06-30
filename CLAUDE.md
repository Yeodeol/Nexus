# CLAUDE.md — Nexus

Memoria operativa del repo. Para la narrativa completa ver [README](README.md) y
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## 1. Contexto general

- **Qué es:** sistema de orquestación multi-proyecto sobre **MCP**. Da a Claude Code (el
  "cerebro") un mapa + memoria compartida de todos los proyectos y la capacidad de **rutear y
  auto-resolver** trabajo cross-sistema.
- **Tecnologías:** Python 3.10+ (FastMCP), **SQLite** (`~/.claude-projects-hub/hub.db`),
  Claude Code CLI (`claude -p` headless para el listener). Sin dependencias de pago.
- **Módulos:** `projects-hub` (catálogo, estado, handoffs) y `nexus-hub` (capacidades,
  ruteo, interacciones, features coordinadas, checkpoints, buzón, **listener/auto_runs**).
  Comparten `hub.db`; `nexus-hub` solo **lee** las tablas de `projects-hub`.

## 2. Decisiones técnicas

- **El cerebro es Claude Code, no un chat propio.** El experimento de cockpit/Agent SDK se
  archivó (`cockpit/experimental/`) por el costo de la API key.
- **Listener autónomo con `claude -p` headless** (no Agent SDK): usa la **suscripción**, sin
  API key. Reutiliza al mismo cerebro en modo no-interactivo.
- **Sandbox de solo lectura** para el agente autónomo: allowlist sin `Write`/`Edit`/`Bash`,
  `--permission-mode default` (deniega lo no listado). Nunca edita ni toca git.
- **Idempotencia** del listener vía tabla `auto_runs` (UNIQUE `item_type+item_id`).
- **Watermark**: el listener solo procesa items nuevos por defecto (no re-dispara el backlog).
- **Opt-in por proyecto** en `listener/config.json` (gitignored; no filtra nombres reales).
- **Una sola base de datos**, sin sincronizar dos almacenes.

## 3. Flujos y arquitectura

- **Auto-servicio (averiguar):** `get_project_context` / `resolve_dependencies` /
  `find_providers`. Buzón asíncrono: `post_message` / `read_messages` (`kind`:
  `note`/`question`/`answer`).
- **Auto-resolución (despertar):** `listener/nexus_listener.py` sondea `hub.db`; ante un
  handoff o consulta a un proyecto opt-in, lanza `claude -p` con `cwd`=raíz del proyecto.
  Consulta simple → responde al buzón + `consume_handoff`. Requerimiento → `checkpoint`
  (borrador) + aviso; el handoff queda **pending** para el humano.
- **Entrada de consultas:** `ask_provider(from, question, to="")` deja `kind='question'`
  (deduce proveedor si no se indica). El listener la toma y la auto-responde.
- **Coordinación de ramas:** `create_coordinated_feature` + `update_branch_state`.

## 4. Errores y soluciones

- **`auto_runs` no existía al correr el listener** (el MCP desplegado es viejo). Solución: el
  listener crea su esquema **defensivamente** en `db()` (igual que `server.py`), sin depender
  de que el MCP haya migrado.
- **Repo vs desplegado:** el MCP global carga desde `C:\Users\Administrador\mcp-servers\`, no
  del repo. Por eso `ask_provider`/`post_message(kind=)` requieren **desplegar + reiniciar**
  Claude. El listener+headless funcionan igual hoy (el agente usa el `post_message` viejo).

## 5. Convenciones y reglas

- **Tools nuevas de un MCP existente:** mismo estilo que `nexus-hub/server.py` (UPSERT
  idempotente, validación de inputs, JSON `ensure_ascii=False`).
- **Acceso directo a `hub.db`** (listener, dashboard, tools): permitido en lectura; las
  escrituras nuevas van a tablas de `nexus-hub`, nunca se mutan las de `projects-hub`.
- **Git:** ramas `feature/`|`fix/`|`hotfix/`|`spike/` en kebab-case; nunca trabajar sobre
  `main`; el merge lo hace el responsable (sin auto-merge).
- **Sin dependencias externas** donde se pueda (stdlib).

## 6. Pendientes

- **Desplegar** `nexus-hub/server.py` a `~/mcp-servers/` y reiniciar Claude para exponer
  `ask_provider` / `list_auto_runs` / `post_message(kind=)` a las sesiones.
- **Fase 4:** sensores externos (Slack → bandeja de requerimientos) que generen `ask_provider`
  / handoffs automáticamente.
- Posible: notificación push/Slack cuando el listener auto-responde; vista de `auto_runs` en
  el dashboard.

<!-- Dependencias entre proyectos (Nexus): si este repo CONSUME de otros, esta sección la
     mantiene tools/sync_nexus_deps.py. -->
