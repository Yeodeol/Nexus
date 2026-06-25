# 🧠 Nexus

> Un "cerebro" que unifica el contexto de todos tus proyectos. Hablas con **un solo lugar** y él sabe qué proyectos están involucrados, qué provee cada uno y cómo coordinarlos.

**Nexus** es un sistema de orquestación multi-proyecto construido sobre el [Model Context Protocol (MCP)](https://modelcontextprotocol.io). Está pensado para quien trabaja con **muchos repos a la vez** (frontend, backend, funciones serverless, servicios) y pierde tiempo recordando qué hace cada uno, qué expone y cómo se conectan entre sí.

La idea: en vez de tener el contexto disperso en tu cabeza y repetirlo en cada conversación con tu asistente de IA, Nexus lo guarda en una **memoria compartida persistente** y le da a tu asistente las herramientas para **rutear el trabajo solo** entre proyectos.

---

## El problema

Cuando trabajas en varios proyectos en paralelo:

- El **contexto vive fragmentado**: cada repo sabe lo suyo, pero nada conoce el mapa completo.
- Le repites a tu asistente, una y otra vez, "esto está en el repo X", "para los pagos usa el servicio Y".
- Un cambio en un proyecto **impacta a otros** y es fácil olvidarlo.
- Coordinar una misma feature en varios repos (misma rama, mismos pasos) es manual y propenso a errores.

## Qué hace Nexus

- 🗺️ **Mapa de capacidades** — registra qué **provee** y qué **consume** cada proyecto (APIs, funciones, tablas, servicios).
- 🧭 **Ruteo automático** — dado un objetivo en un proyecto, deduce qué otros proyectos están implicados y a quién consultar.
- 🔗 **Handoffs** — transfiere contexto de un proyecto a otro (decisiones, contratos, pendientes).
- 🌿 **Features coordinadas** — crea la **misma rama** (`feature/...`) en varios repos y rastrea su estado (`planned → created → pushed → pr-open → merged`).
- 📊 **Monitoreo** — registra cada interacción entre proyectos para verlo en un panel.
- 💾 **Memoria externa** — todo persiste en una base SQLite, así el contexto de tu chat se mantiene liviano.

---

## Arquitectura

```mermaid
flowchart TD
    U["Tú · un solo chat"] --> B["Cerebro<br/>cliente MCP + skill orquestador"]
    B -->|consulta / registra| N["nexus-hub<br/>capacidades · interacciones · features"]
    B -->|lanza| S["Subagentes<br/>leen cada repo"]
    N --> DB[("hub.db<br/>memoria compartida")]
    P["projects-hub<br/>proyectos · estado · handoffs"] --> DB
    S --> R["Repos: app · api · servicios..."]
    DB --> D["Dashboard de monitoreo"]
```

Nexus son **dos módulos MCP** que comparten una sola base de datos (`~/.claude-projects-hub/hub.db`):

| Módulo | Rol | Tablas |
|---|---|---|
| **`projects-hub`** | Base: catálogo de proyectos, estado y handoffs | `projects`, `state`, `handoffs` |
| **`nexus-hub`** | Extensión orquestadora | `capabilities`, `interactions`, `coordinated_features`, `feature_branches`, `checkpoints` |

> Diseño deliberado: una sola fuente de verdad, sin sincronización entre almacenes. `nexus-hub` **no toca** las tablas de `projects-hub`, solo las lee.

Ver el diseño completo en [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Instalación

Requisitos: **Python 3.10+** y un cliente MCP (p. ej. [Claude Code](https://docs.claude.com/en/docs/claude-code) o Claude Desktop).

```powershell
git clone https://github.com/Yeodeol/Nexus.git
cd Nexus

# Un venv por módulo
python -m venv projects-hub\.venv
python -m venv nexus-hub\.venv
projects-hub\.venv\Scripts\python.exe -m pip install -r projects-hub\requirements.txt
nexus-hub\.venv\Scripts\python.exe  -m pip install -r nexus-hub\requirements.txt
```

Luego registra **ambos** servidores en tu cliente MCP. Ejemplo para Claude Code (`~/.claude.json` → `mcpServers`):

```json
{
  "mcpServers": {
    "projects-hub": {
      "type": "stdio",
      "command": "<ruta>/Nexus/projects-hub/.venv/Scripts/python.exe",
      "args": ["<ruta>/Nexus/projects-hub/server.py"]
    },
    "nexus-hub": {
      "type": "stdio",
      "command": "<ruta>/Nexus/nexus-hub/.venv/Scripts/python.exe",
      "args": ["<ruta>/Nexus/nexus-hub/server.py"]
    }
  }
}
```

Reinicia el cliente y ambos quedan disponibles. Las tablas se crean solas en el primer uso.

---

## Tools

### `projects-hub` (base)
| Tool | Para qué |
|---|---|
| `register_project` | Registra o actualiza un proyecto (nombre, ruta, descripción) |
| `list_projects` / `get_project` | Consulta el catálogo |
| `set_state` / `get_state` | Notas de estado por proyecto (clave-valor) |
| `send_handoff` / `get_pending_handoffs` / `consume_handoff` | Transferencia de contexto entre proyectos |

### `nexus-hub` (orquestación)
| Tool | Para qué |
|---|---|
| `delete_project` | Elimina un proyecto (seguro: no deja datos huérfanos) |
| `declare_capability` | Declara qué **provee** o **consume** un proyecto |
| `list_capabilities` / `find_providers` | Consulta el mapa de capacidades |
| `resolve_dependencies` | Dado un proyecto + intención, dice a quién consultar |
| `log_interaction` / `list_interactions` | Monitoreo de interacciones entre proyectos |
| `create_coordinated_feature` | Crea la misma rama en varios repos + entrega el comando git |
| `update_branch_state` / `get_coordinated_feature` / `list_coordinated_features` | Seguimiento de features cross-repo |
| `checkpoint` / `get_checkpoints` | Memoria externa para no saturar el contexto del chat |

### Ejemplo de uso (genérico)

```
1. declare_capability(project="pagos-svc", kind="provides",
       name="cobro_tarjeta", category="api",
       contract="POST /charge {amount, token} -> {status, id}")

2. declare_capability(project="tienda-web", kind="consumes",
       name="cobro_tarjeta", category="api")

3. resolve_dependencies(project="tienda-web")
   -> "tienda-web necesita 'cobro_tarjeta'; lo provee 'pagos-svc'"

4. create_coordinated_feature(slug="checkout-1click", type="feature",
       description="Compra en 1 clic", projects="tienda-web,pagos-svc")
   -> crea feature/checkout-1click en ambos repos + comando git
```

---

## Roadmap

- [x] **Fase 0** — Núcleo MCP (capacidades, ruteo, features coordinadas, checkpoints)
- [ ] **Fase 1** — Poblado de capacidades de un proyecto piloto
- [ ] **Fase 2** — Skill orquestador (el "cómo pensar" del cerebro)
- [x] **Fase 3** — Dashboard de monitoreo (grafo de interacciones + estado de ramas)
- [ ] **Fase 4** — Sensores externos (p. ej. monitoreo de Slack → bandeja de requerimientos)
- [ ] **Fase 5** — Actuadores asistidos (borradores de respuesta con aprobación humana)

---

## Estado

En construcción (**Fase 3 de 5**). El núcleo (Fase 0) funciona y el **dashboard de monitoreo** (Fase 3) ya está disponible:

```powershell
python dashboard\dashboard.py    # http://localhost:8788
```

Lee `hub.db` en solo lectura (sin dependencias externas) y muestra el grafo de dependencias e interacciones, el ruteo resuelto, las capacidades por proyecto y el estado de las features coordinadas — detalle en [dashboard/README.md](dashboard/README.md). La API de los tools puede cambiar mientras avanza el roadmap.

## Licencia

[MIT](LICENSE) — úsalo, modifícalo y compártelo libremente.
