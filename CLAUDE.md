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
- **Idempotencia con reintentos** del listener: `auto_runs` (UNIQUE `item_type+item_id`) +
  columna `attempts`; un item en error se reintenta tras `retry_cooldown` hasta
  `1+max_retries` intentos (antes quedaba muerto para siempre). Mensajes en error quedan
  `unread` a propósito (el anti-duplicado real es `auto_runs`).
- **Watermark**: el listener solo procesa items nuevos por defecto (no re-dispara el backlog).
- **Opt-in por proyecto** en `listener/config.json` (gitignored; no filtra nombres reales).
- **Una sola base de datos**, sin sincronizar dos almacenes.
- **Cockpit sobre tools agregadoras, no chat propio:** `nexus_boot` (arranque en 1 llamada),
  `nexus_overview` (visión global) y `nexus_search` (búsqueda global) + skill `/nexus`.
  Para la búsqueda se descartó FTS5 (triggers de sincronización) por LIKE tokenizado en
  Python: el hub tiene cientos de filas, no miles.
- **Fichas de conocimiento** (tabla `knowledge`, UNIQUE project+topic): memoria profunda por
  proyecto; las refresca el listener en idle con un agente cuya única escritura permitida es
  `save_knowledge`.
- **Auto-log de interacciones:** `ask_provider` y `get_project_context(from_project=)`
  insertan en `interactions` solos (7 filas en 2 meses demostraron que el log manual no
  funciona); `log_interaction` queda como complemento.

## 3. Flujos y arquitectura

- **Arranque de sesión:** `nexus_boot(proyecto)` — 1 llamada con handoffs + buzón +
  dependencias + estado + checkpoints + fichas (reemplaza la secuencia de 4-5 tools).
- **Cockpit (sesión única, skill `/nexus`):** cascada de costo para consultas:
  `nexus_search` → `get_knowledge` → `get_project_context` → subagente al repo (último
  recurso). Lo aprendido se persiste (`save_knowledge` / `declare_capability`).
- **Auto-servicio (averiguar):** `get_project_context` / `resolve_dependencies` /
  `find_providers`. Buzón asíncrono: `post_message` / `read_messages` (`kind`:
  `note`/`question`/`answer`).
- **Auto-resolución (despertar):** `listener/nexus_listener.py` sondea `hub.db`; ante un
  handoff o consulta a un proyecto opt-in, lanza `claude -p` con `cwd`=raíz del proyecto.
  Consulta simple → responde al buzón + `consume_handoff`. Requerimiento → `checkpoint`
  (borrador) + aviso; el handoff queda **pending** para el humano.
- **Entrada de consultas:** `ask_provider(from, question, to="")` deja `kind='question'`
  (deduce proveedor si no se indica). El listener la toma y la auto-responde.
- **Fichas en idle:** sin items pendientes, el listener refresca fichas vencidas
  (`knowledge_refresh_days`, 1 por ciclo; `--refresh-knowledge` fuerza todas). Logs
  completos de cada corrida en `~/.claude-projects-hub/listener-runs/`.
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

- **Reiniciar Claude** tras desplegar para exponer las tools nuevas (`nexus_boot`,
  `nexus_overview`, `nexus_search`, `save_knowledge`, `get_knowledge`) a las sesiones.
- **Poblar fichas iniciales:** correr `python listener/nexus_listener.py --once --refresh-knowledge`
  (o dejar el daemon en idle) para generar las primeras fichas de los responders.
- **Triage humano** de los ~13 handoffs `pending` acumulados (visibles en `nexus_overview`).
- **Fase 4:** sensores externos (Slack → bandeja de requerimientos) que generen `ask_provider`
  / handoffs automáticamente.
- Posible: notificación push/Slack cuando el listener auto-responde; vista de `auto_runs` y
  fichas en el dashboard.

<!-- Dependencias entre proyectos (Nexus): si este repo CONSUME de otros, esta sección la
     mantiene tools/sync_nexus_deps.py. -->
