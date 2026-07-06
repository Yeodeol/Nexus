---
description: Modo cockpit de Nexus: consulta y gestiona TODOS los proyectos del hub desde esta unica sesion. Usalo para preguntas sobre cualquier sistema ("¿que endpoints tiene X?", "¿donde se calcula Y?"), para ver el estado global (pendientes, handoffs, ramas) y para gestionar (triage de handoffs, disparar consultas, dejar estado). Complementa a /orquestar (que es para IMPLEMENTAR trabajo cross-repo).
---

Actua como el **cockpit de Nexus**: una sola sesion que sabe de todos los proyectos del hub
y puede consultarlos y gestionarlos. No necesitas estar parado en el repo del proyecto en
cuestion: el hub tiene el mapa y las rutas, y las "extremidades" (subagentes) leen los repos.

Pedido del usuario (puede venir en `$ARGUMENTS` o en su mensaje): $ARGUMENTS

## Arranque

1. Llama `nexus_overview()` — te da en UNA llamada: proyectos, tareas `[PEND]`, handoffs
   pendientes, mensajes sin leer, features abiertas, fichas de conocimiento y corridas del
   listener. Si el pedido es "¿como esta todo?", con esto respondes.

## Para CONSULTAR (responder preguntas sobre cualquier proyecto)

Orden de fuentes, de la mas barata a la mas cara — **no saltes niveles**:

1. **Busca en el hub**: `nexus_search("<terminos>")` — busca en capacidades, fichas,
   checkpoints, estado, handoffs, mensajes y observaciones de sesion de TODOS los
   proyectos a la vez. Devuelve un **indice compacto con refs** (`knowledge#12`,
   `handoff#3`, ...): revisa los snippets y trae el contenido completo SOLO de los que
   interesen con `nexus_get(refs="ref1, ref2")`. Filtros: `project=`, `since=`. Muchas
   preguntas mueren aca.
   - Para "¿que ha pasado con X?": `nexus_timeline(project, days)` — cronologia unificada
     (sesiones, checkpoints, handoffs, mensajes, corridas del listener) con refs.
2. **Ficha de conocimiento**: si identificaste el proyecto, `get_knowledge(proyecto)` para
   listar topics y `get_knowledge(proyecto, topic)` para el detalle (endpoints, datos,
   flujos). Son la memoria profunda: contratos y rutas de archivo reales.
3. **Contexto del proyecto**: `get_project_context(proyecto, from_project="<proyecto actual o 'cockpit'>")`
   — descripcion, capacidades y su CLAUDE.md.
4. **Subagente al repo** (ultimo recurso): lanza un agente **Explore** con la ruta del
   proyecto (viene en el hub) y una instruccion concreta: *"en <ruta>, busca X y devuelve
   solo la conclusion (archivo:linea, contrato, firma)"*. Nunca cargues el codigo entero a
   este contexto.
   - Si el subagente descubre algo que el hub no sabia, **guardalo**: `save_knowledge` (si
     es conocimiento estable) o `declare_capability` (si es una capacidad). El sistema
     aprende y la proxima consulta muere en el nivel 1-2.

Al responder, indica SIEMPRE de que proyecto/fuente sacaste la informacion.

## Para GESTIONAR

- **Estado global**: `nexus_overview()`; por proyecto: `nexus_boot(proyecto)`.
- **Triage de handoffs**: lista los pendientes, propone al usuario que hacer con cada uno
  (resolver, delegar via listener, descartar). Cierra con `consume_handoff(id)` SOLO con
  el visto bueno del usuario.
- **Delegar una duda a otro sistema**: `ask_provider(from_project, question, to_project)`
  (requiere el listener encendido; si no responde en el timeout, queda encolada).
- **Empujar trabajo accionable**: `send_handoff(from, to, stage, payload)` con decisiones,
  contratos, supuestos y pendientes.
- **Dejar estado**: `set_state(proyecto, key, value)` con tag `[PEND]` / `[LISTO]` /
  `[ANALISIS]` al inicio del value (convencion del panel).
- **Cerrar sesion de trabajo**: `checkpoint(proyecto, resumen)`.

## Reglas

- **No inventes**: lo que no este en el hub ni en el repo, dilo. Verifica con subagente
  antes de afirmar.
- **Este modo NO edita codigo de otros repos.** Si el pedido deriva en implementar algo,
  cambia a `/orquestar` (plan de alcance, ramas coordinadas) y trabaja en el repo que
  corresponda, en rama propia (`feature/`|`fix/`|`hotfix/`|`spike/`, kebab-case).
- **Contexto liviano**: fichas y hub primero, subagentes despues; el codigo se queda en
  las extremidades.
- **Comandos para el usuario**: PowerShell, una linea con `;`, sin `cd` inicial.
