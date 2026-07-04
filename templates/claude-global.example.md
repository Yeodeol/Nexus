<!--
  PLANTILLA — instrucciones globales del "cerebro".

  Copia el bloque de abajo (desde "## Nexus (orquestación multi-proyecto)" hasta el
  final) y pégalo en tu ~/.claude/CLAUDE.md (créalo si no existe). Es lo que hace que
  tu asistente use Nexus SOLO: los MCP le dan las herramientas, pero sin estas
  instrucciones no sabe CUÁNDO usarlas.
-->

## Nexus (orquestación multi-proyecto)

Al iniciar cualquier sesión en un proyecto:
1. Llama `nexus_boot("<nombre-del-proyecto>")` — una sola llamada trae handoffs
   pendientes, buzón, dependencias, estado y fichas. Si el proyecto no está
   registrado, `register_project(name, path, description)` primero y repite.
2. Procesa lo que traiga (handoffs se cierran con `consume_handoff(id)` al resolverlos).

REGLA DURA: si te preguntan (o necesitas) algo cuya respuesta vive en OTRO sistema
("de dónde sale X", "qué devuelve Y", "quién provee Z"), NO respondas "no sé" ni
busques solo en este repo. Consulta Nexus primero, en este orden de costo:
`nexus_search("<términos>")` → `get_knowledge(proyecto[, topic])` →
`get_project_context(proyecto, from_project="<proyecto actual>")` → subagente al
repo del proveedor (último recurso). Indica siempre de qué proyecto/fuente sacaste
la información. Lo aprendido estable se persiste: `save_knowledge` (conocimiento) /
`declare_capability` (capacidades).

Al terminar trabajo que afecte a otro proyecto: `send_handoff(from, to, stage,
payload)` con decisiones, archivos modificados, contratos y pendientes. Deja estado
con `set_state(proyecto, clave, valor)` anteponiendo un tag al valor: `[PEND]`
(por hacer), `[LISTO]` (resuelto), `[ANALISIS]` (nota informativa). Para una duda
puntual a otro sistema usa `ask_provider(from, question, to)` (requiere el
listener encendido; si no, queda encolada).
