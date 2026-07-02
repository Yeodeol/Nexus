# Arquitectura de Nexus

## Filosofía: cerebro + extremidades + memoria

Nexus parte de una metáfora simple:

- **El cerebro** piensa y decide. Es tu cliente MCP (Claude Code, por ejemplo) guiado por un skill orquestador. No guarda todo el conocimiento en su contexto: lo consulta de la memoria externa cuando lo necesita.
- **Las extremidades** ejecutan. Son subagentes que entran a cada repo, leen lo que hace falta y devuelven **solo la conclusión** (no el código entero). Así el contexto del cerebro se mantiene liviano.
- **La memoria** persiste. Es una base SQLite (`hub.db`) compartida por los dos módulos MCP. Sobrevive entre sesiones.

## Las dos capas MCP

```
┌─────────────────────────────────────────────┐
│  Cliente MCP (cerebro) + skill orquestador   │
└───────────────┬──────────────┬───────────────┘
                │              │
        ┌───────▼──────┐  ┌────▼─────────┐
        │ projects-hub │  │  nexus-hub   │
        │   (base)     │  │ (extensión)  │
        └───────┬──────┘  └────┬─────────┘
                │              │
              ┌─▼──────────────▼─┐
              │     hub.db       │   (~/.claude-projects-hub/hub.db)
              └──────────────────┘
```

- **`projects-hub`** es la base: el catálogo de proyectos, su estado y los handoffs.
- **`nexus-hub`** es la extensión orquestadora: añade el mapa de capacidades, el ruteo, las interacciones, las features coordinadas y los checkpoints.

`nexus-hub` **comparte** la misma base de datos pero **no modifica** las tablas de `projects-hub`: solo las lee. Esto evita duplicar el catálogo y mantener dos almacenes sincronizados.

## Esquema de datos

### Tablas de `projects-hub`
| Tabla | Campos | Para qué |
|---|---|---|
| `projects` | `name` (PK), `path`, `description`, `status`, `updated_at` | Catálogo de proyectos |
| `state` | `project`, `key`, `value`, `updated_at` (PK: project+key) | Notas de estado clave-valor |
| `handoffs` | `id`, `from_project`, `to_project`, `stage`, `payload`, `status`, `created_at`, `consumed_at` | Transferencia de contexto |

### Tablas de `nexus-hub`
| Tabla | Campos | Para qué |
|---|---|---|
| `capabilities` | `id`, `project`, `kind` (`provides`/`consumes`), `name`, `category`, `contract`, `notes`, `updated_at` | Qué provee/consume cada proyecto |
| `interactions` | `id`, `from_project`, `to_project`, `intent`, `capability`, `outcome`, `feature`, `created_at` | Log de interacciones (monitoreo) |
| `coordinated_features` | `id`, `slug`, `branch`, `type`, `description`, `status`, timestamps | Features que cruzan repos |
| `feature_branches` | `id`, `feature_id`, `project`, `branch`, `state`, `pr_url`, `updated_at` | Estado de la rama por repo |
| `checkpoints` | `id`, `project`, `summary`, `created_at` | Resúmenes de avance (memoria externa) |
| `messages` | `id`, `from_project`, `to_project`, `text`, `status`, `kind`, `created_at`, `read_at` | Buzón asíncrono entre proyectos/sesiones (`kind`: `note`/`question`/`answer`) |
| `knowledge` | `id`, `project`, `topic`, `content`, `updated_at`, `git_commit` (UNIQUE project+topic) | Fichas de conocimiento por proyecto (memoria profunda). `git_commit` = HEAD del repo al guardar: el refresh se decide por cambio de commit |
| `auto_runs` | `id`, `item_type`, `item_id`, `project`, `status`, `result`, `attempts`, `created_at`, `finished_at` | Bitácora del listener autónomo (idempotencia + reintentos de errores) |

## Flujo de orquestación

1. Le das un objetivo al cerebro: *"implementa X en el proyecto A"*.
2. El cerebro llama a `resolve_dependencies(A)` → obtiene qué consume A y qué proyectos lo proveen.
3. Por cada dependencia, lanza un **subagente** al proyecto proveedor para traer el contrato real o ver si ya existe algo reutilizable.
4. Registra lo ocurrido con `log_interaction` (alimenta el monitoreo).
5. Si el trabajo cruza repos, crea una `coordinated_feature` (misma rama en todos) y/o un `handoff` con el contexto.

El ruteo no es magia: es **razonamiento sobre un mapa bien mantenido**. Mientras más capacidades declaradas, menos hace falta dirigir al cerebro a mano.

## Auto-servicio y comunicación entre sesiones

Los módulos MCP de Nexus están en la config **global** del cliente, así que **toda sesión, en cualquier proyecto, los tiene**. El modelo es de auto-servicio:

- **Arranque en 1 llamada:** `nexus_boot(proyecto)` reemplaza la secuencia de inicio (handoffs pendientes + buzón + dependencias + estado): menos latencia y menos contexto quemado en cada sesión.
- **Averiguar sin handoff:** cuando una sesión necesita algo de otro sistema, usa `nexus_search` (búsqueda global en todo el hub), `get_knowledge` (fichas), `resolve_dependencies` / `find_providers` para ubicarlo y `get_project_context(otro)` para traer su descripción, capacidades y su `CLAUDE.md`. Así entiende el otro proyecto por sí misma.
- **Buzón entre sesiones:** `post_message` / `read_messages` deja mensajes asíncronos (preguntas, avisos, respuestas). Las sesiones son procesos independientes; el buzón persiste en `hub.db` y se lee al arrancar o al consultar.
- **Handoffs** se reservan para *empujar* trabajo entregable que el otro debe accionar.
- **Monitoreo sin esfuerzo:** `ask_provider` y `get_project_context(..., from_project=)` auto-registran la interacción en `interactions`; el grafo del dashboard se alimenta solo, sin depender de `log_interaction` manual.

## Cockpit: una sesión para todos los proyectos

El skill **`/nexus`** convierte cualquier sesión en el cockpit: `nexus_overview()` da la
visión global (pendientes `[PEND]`, handoffs, features, fichas, listener) y las consultas
se resuelven en cascada de costo: **1)** `nexus_search` sobre el hub → **2)** fichas
(`get_knowledge`) → **3)** `get_project_context` → **4)** subagente al repo (última
opción). Lo aprendido en el paso 4 se persiste (`save_knowledge` / `declare_capability`):
el sistema aprende y la próxima consulta muere en los pasos 1-2.

## Fichas de conocimiento (memoria profunda)

La tabla `knowledge` guarda fichas por proyecto y topic (`resumen`,
`endpoints-contratos`, `datos`, `flujos-clave`, `integraciones`): contenido concreto con
rutas de archivo, firmas y contratos reales. Las genera y refresca el **listener en idle**
(agente headless read-only cuyas únicas escrituras permitidas son `save_knowledge` y
`declare_capability` — de paso mantiene el mapa de capacidades). Cualquier sesión también
puede guardarlas a mano. Esto convierte al hub de "mapa" en memoria consultable: la
mayoría de las preguntas se responden sin releer los repos.

## Cerebro vivo: fichas git-aware + sync de repos

Dos mecanismos cierran el ciclo "el repo cambia → el cerebro se actualiza":

1. **Refresh por cambio, no por edad.** Cada ficha guarda el `git_commit` (HEAD) del repo
   al momento de crearse. En idle, el listener compara ese commit con el HEAD actual
   (`git rev-parse`, solo lectura): si difieren, re-documenta; si son iguales, la ficha
   está al día sin importar cuándo se escribió (cero corridas desperdiciadas).
   `knowledge_refresh_days` queda como fallback por edad para rutas sin git.
2. **Sync seguro de repos** (opt-in `git_sync_projects`, cada `git_sync_hours`, pensado
   para repos donde el usuario es consumidor pasivo — p. ej. los que mantiene otro
   equipo): `git fetch` siempre; `git pull --ff-only` **solo si** el repo está limpio
   **y** parado en la rama default del remoto. Nunca hace commit, merge, push, rebase ni
   checkout: si no puede avanzar limpio, reporta ("detrás N commits, en rama X: no se
   toca") y sigue. Bitácora agregada en `auto_runs` (`item_type='git-sync'`).

Encadenados: el sync diario avanza el repo → el HEAD cambia → el siguiente ciclo idle
refresca las fichas → las consultas responden con la data del último cambio, sin
intervención humana.

## Auto-resolución: el listener autónomo

El auto-servicio resuelve el *averiguar*; el **listener** resuelve el *despertar*. Sin él, un
handoff o una consulta a otro sistema quedan esperando a que alguien abra esa sesión a mano.

```
Sesión A  --send_handoff / ask_provider-->  hub.db
                                              │
                    nexus_listener.py (daemon, sondea ~15s)
                                              │  item NUEVO para B (opt-in), no procesado
                                              ▼
              claude -p   (cwd = ruta de B, tools read-only + Nexus)
                · CONSULTA  -> investiga read-only -> post_message(answer) + consume_handoff
                · REQUERIM. -> NO toca código/git -> checkpoint(borrador) + post_message; handoff queda PENDING
                                              │
                          auto_runs (idempotencia) + interactions (dashboard)
```

**Decisiones clave:**

- **Motor `claude -p` headless**, no el Agent SDK: corre con la suscripción de Claude Code
  (sin API key de pago) y reutiliza al mismo "cerebro". El experimento del cockpit se frenó
  justamente por el costo del SDK; esto lo evita.
- **Sandbox de solo lectura**: allowlist estrecha (`Read`/`Grep`/`Glob` + tools del hub para
  responder/borradorear), **sin** `Write`/`Edit`/`Bash`, y `--permission-mode default` ⇒ en
  headless lo no permitido se **deniega** (no pregunta). El agente nunca edita ni toca git.
- **Idempotencia con reintentos** vía `auto_runs` (UNIQUE `item_type+item_id` + columna
  `attempts`): una corrida por item, pero un item en **error** se reintenta (tras
  `retry_cooldown`, hasta `1+max_retries` intentos) en vez de quedar muerto para siempre.
- **Log completo por corrida** en `~/.claude-projects-hub/listener-runs/` (stdout+stderr),
  porque el `result` truncado de `auto_runs` no basta para diagnosticar fallas.
- **Refresh de fichas en idle**: cuando no hay items, el listener refresca las fichas de
  conocimiento vencidas (una por ciclo), con un agente cuya única escritura es `save_knowledge`.
- **Watermark**: por defecto solo procesa items creados tras arrancar, para no re-disparar el
  backlog viejo (`--backlog` lo incluye a propósito).
- **Opt-in por proyecto** (`listener/config.json`): arranca conservador; el usuario habilita
  qué sistemas pueden auto-responder.
- **El listener escribe directo en `hub.db`** (como el dashboard), respetando que solo *lee*
  las tablas de `projects-hub`; su bitácora `auto_runs` vive en el espacio de `nexus-hub`.

## Manejo del contexto

El límite de contexto del chat se respeta con tres defensas:

1. **Subagentes** que aíslan la lectura pesada y devuelven solo conclusiones.
2. **Memoria en `hub.db`**, no en el historial del chat.
3. **Checkpoints** (`checkpoint`/`get_checkpoints`) para que una sesión nueva retome sin arrastrar todo.

## Coordinación de ramas

`create_coordinated_feature` genera una rama compartida con la convención `{type}/{slug}` (`feature`, `fix`, `hotfix`, `spike` + kebab-case) y siembra el estado `planned` en cada repo. Entrega además el comando para crear la rama en todos los repos sin cambiar de carpeta (usando `git -C`). El avance se sigue con `update_branch_state`: `planned → created → committed → pushed → pr-open → merged`. Cuando todas las ramas quedan `merged`, la feature se marca `merged` automáticamente.

## Decisiones de diseño

- **El cerebro es tu cliente MCP (Claude Code), no un chat propio.** Nexus no reconstruye la conversación ni el manejo de permisos/repos: se apoya en Claude Code y le aporta mapa, memoria y herramientas. (Hubo un experimento de chat propio con el Agent SDK; se archivó en `cockpit/experimental/`.)
- **Averiguar antes que pedir.** Para conocer el contexto de otro proyecto se usa el auto-servicio (`get_project_context`, `resolve_dependencies`); los handoffs quedan para *empujar* trabajo accionable.
- **Una sola base de datos** en vez de dos almacenes que sincronizar.
- **Borrado seguro**: `delete_project` no elimina un proyecto con datos asociados a menos que se pida explícitamente (`purge_data=True`), para no dejar huérfanos.
- **Extensión, no reemplazo**: `nexus-hub` se apoya en `projects-hub` y lo respeta.

## Roadmap

| Fase | Entrega |
|---|---|
| 0 | Núcleo MCP ✅ |
| 1 | Poblado de capacidades de un proyecto piloto ✅ |
| 2 | Skill orquestador (comportamiento del cerebro) ✅ |
| 3 | Dashboard de monitoreo ✅ |
| 5 | Actuadores asistidos: listener autónomo (auto-responde + borradores con aprobación humana) ✅ |
| 4 | Sensores externos (p. ej. Slack → bandeja de requerimientos) |
