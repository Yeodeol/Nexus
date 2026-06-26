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
| `messages` | `id`, `from_project`, `to_project`, `text`, `status`, `created_at`, `read_at` | Buzón asíncrono entre proyectos/sesiones |

## Flujo de orquestación

1. Le das un objetivo al cerebro: *"implementa X en el proyecto A"*.
2. El cerebro llama a `resolve_dependencies(A)` → obtiene qué consume A y qué proyectos lo proveen.
3. Por cada dependencia, lanza un **subagente** al proyecto proveedor para traer el contrato real o ver si ya existe algo reutilizable.
4. Registra lo ocurrido con `log_interaction` (alimenta el monitoreo).
5. Si el trabajo cruza repos, crea una `coordinated_feature` (misma rama en todos) y/o un `handoff` con el contexto.

El ruteo no es magia: es **razonamiento sobre un mapa bien mantenido**. Mientras más capacidades declaradas, menos hace falta dirigir al cerebro a mano.

## Auto-servicio y comunicación entre sesiones

Los módulos MCP de Nexus están en la config **global** del cliente, así que **toda sesión, en cualquier proyecto, los tiene**. El modelo es de auto-servicio:

- **Averiguar sin handoff:** cuando una sesión necesita algo de otro sistema, usa `resolve_dependencies` / `find_providers` para ubicarlo y `get_project_context(otro)` para traer su descripción, capacidades y su `CLAUDE.md`. Así entiende el otro proyecto por sí misma.
- **Buzón entre sesiones:** `post_message` / `read_messages` deja mensajes asíncronos (preguntas, avisos, respuestas). Las sesiones son procesos independientes; el buzón persiste en `hub.db` y se lee al arrancar o al consultar.
- **Handoffs** se reservan para *empujar* trabajo entregable que el otro debe accionar.

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
| 4 | Sensores externos (p. ej. Slack → bandeja de requerimientos) |
| 5 | Actuadores asistidos (borradores con aprobación humana) |
