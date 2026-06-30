---
description: Orquesta trabajo que cruza varios proyectos usando nexus-hub: detecta dependencias, consulta a los proyectos proveedores via subagentes, coordina ramas y handoffs. Usalo cuando un objetivo en un proyecto pueda involucrar a otros (ej. "implementa X en tienda-web").
---

Vas a orquestar un trabajo que puede cruzar varios proyectos, usando **nexus-hub** (+ projects-hub) como cerebro y memoria. Tu rol es el **cerebro**: decides a quien consultar y coordinas; las **extremidades** son subagentes que leen cada repo.

Objetivo del usuario (puede venir en `$ARGUMENTS` o en su mensaje): $ARGUMENTS

## Procedimiento

1. **Identifica el proyecto objetivo.** Del objetivo del usuario, deduce sobre que proyecto es (ej. "tienda-web"). Si no esta claro, pregunta en UNA linea. Verifica que exista con `get_project` / `list_projects`.

2. **Mapea dependencias.** Llama `resolve_dependencies(project, intent)` con el proyecto y una frase de la intencion. Te devuelve:
   - `dependencies[]`: que CONSUME el proyecto y, por cada cosa, `provided_by[]` (que otros proyectos lo proveen).
   - `recent_interactions[]`: que se hizo antes con esos proyectos.
   - Si `dependencies` viene vacio, el mapa aun no esta poblado: usa `find_providers("<lo que busca el objetivo>")` y, si hace falta, declara lo que falte con `declare_capability` antes de seguir.

3. **Consulta a los proveedores (extremidades).** Por cada dependencia con `provided_by`, lanza un **subagente** (Agent / Explore) al repo del proveedor con una instruccion concreta: *"busca si ya existe X, trae el contrato real (input/output) y si es reutilizable o hay que extenderlo"*. El subagente devuelve **solo la conclusion**, no el codigo entero (asi el contexto se mantiene liviano). Si hay varios proveedores independientes, lanzalos en paralelo.

4. **Registra cada interaccion.** Tras consultar a un proveedor, llama `log_interaction(from_project=<objetivo>, to_project=<proveedor>, intent=..., capability=..., outcome=...)`. `outcome`: `reused` | `extended` | `created` | `consulted` | `noop`. Esto alimenta el monitoreo.

5. **Coordina si el trabajo cruza repos.** Si el cambio toca mas de un repo:
   - Crea la feature compartida: `create_coordinated_feature(slug, type, description, projects="a,b")`. Devuelve el one-liner PowerShell (git -C) para crear la rama en todos los repos — entregaselo al usuario.
   - Transfiere contexto puntual con `send_handoff(from_project, to_project, stage, payload)` (decisiones, contrato, supuestos, pendientes).
   - A medida que avanza cada repo, actualiza `update_branch_state(slug, project, state)`.

6. **Sintetiza el plan de alcance.** Entrega al usuario, claro y por proyecto: que se toca en cada repo, que se REUTILIZA (ya existe), que hay que CREAR/EXTENDER, contratos involucrados, y riesgos. No empieces a codear hasta que el usuario apruebe el alcance.

7. **Cierra.** Al terminar (o al pausar), deja memoria externa para no saturar el contexto:
   - `checkpoint(project, summary)` con el resumen del avance.
   - `set_state(project, key, value)` con el tag de estado (`[PEND]` / `[LISTO]` / `[ANALISIS]`).

## Reglas

- **No inventes.** Lo que no este en el mapa o en el codigo, verificalo con un subagente antes de afirmarlo.
- **Mapa incompleto = oportunidad de aprender.** Si descubres una capacidad/dependencia nueva, declarala (`declare_capability`) para que la proxima vez el ruteo sea automatico. El sistema se pule solo.
- **Convenciones git:** ramas `feature/` `fix/` `hotfix/` `spike/` en kebab-case; nunca trabajar sobre main/master/develop; el merge lo hace el responsable (no auto-merge).
- **Contexto liviano:** apoyate en subagentes (aislan la lectura pesada) y en checkpoints (persisten el avance). No arrastres codigo entero al contexto del cerebro.
- **Comandos shell para el usuario:** PowerShell, una sola linea con `;`, sin `cd` al inicio.

## Auto-resolución (listener)

Si el daemon `listener/nexus_listener.py` está corriendo, los `send_handoff` y las consultas
`ask_provider` dirigidas a un proyecto **opt-in** se **auto-resuelven**: el sistema destino
despierta solo (Claude headless) y, para una **consulta simple**, deja la respuesta en el
**buzón** del que preguntó (revisá con `read_messages` al arrancar). Para un **requerimiento**,
deja un **borrador de alcance** (`checkpoint`) y el handoff queda **pendiente** para el humano.

Por eso, para *averiguar algo* de otro sistema preferí `ask_provider(from, question, to)` y
esperá la respuesta en tu buzón; reservá `send_handoff` para *empujar* trabajo accionable.
Mirá qué se auto-resolvió con `list_auto_runs`.

## Si el MCP nexus-hub no responde
Verifica que sus tools esten cargados. Si no, el usuario debe reiniciar Claude (los MCP cargan al inicio). Mientras tanto, puedes leer/escribir el estado directo en `~/.claude-projects-hub/hub.db` (tabla `capabilities`, `interactions`, etc.) como fallback.
