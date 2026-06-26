# Plan — Chat unificado de Nexus ("cockpit")

> ⚠️ **REORIENTADO.** Tras probar el MVP se decidió **no** construir un chat propio: el cerebro es **Claude Code** (maneja permisos, subagentes y lectura de repos mejor) y lo que Nexus aporta es el **widget del orquestador en vivo** (`cockpit/widget_server.py`) + el modelo **auto-servicio** (`get_project_context`, buzón). El chat agéntico que describe este plan se archivó en `cockpit/experimental/`. Este documento queda como **registro de diseño**; para el estado actual ver el [README](../README.md).

> **Visión #3 / Fase 6 del roadmap (original).** Documento de diseño. La implementación era por fases; este documento la guiaba.

## Objetivo

Una interfaz web propia donde el usuario conversa en lenguaje natural, un agente Claude **orquesta** el trabajo cross-repo usando las tools del hub, y el **grafo vivo** reacciona al lado. Es el salto de "Claude Code en terminal" a un *cockpit* de producto: una sola puerta donde hablas y el cerebro rutea, consulta proveedores y coordina.

No reemplaza a Claude Code — es **otra puerta de entrada al mismo cerebro** (el hub y sus tools).

## Arquitectura

```
   Tú escribes en el chat
            │
            ▼
 ┌───────────────────────────────────────┐
 │  UI web: chat  +  grafo vivo al lado   │   (navegador)
 └─────────────────┬─────────────────────┘
                   │  /chat  (streaming SSE)
                   ▼
 ┌───────────────────────────────────────┐
 │  Backend "cerebro" (FastAPI)           │
 │  Claude Agent SDK  +  tools del hub    │
 └─────────────────┬─────────────────────┘
                   │  MCP (stdio)
                   ▼
        projects-hub + nexus-hub  →  hub.db
                   │
                   ▼  (cada log_interaction)
          el grafo vivo se enciende
```

| Capa | Qué es | Stack |
|---|---|---|
| **Frontend (cockpit)** | 2 columnas: izquierda chat (input + historial + streaming), derecha el grafo vivo embebido | HTML/CSS/JS vanilla, misma paleta; reusa el panel/grafo |
| **Backend (cerebro)** | endpoint `/chat` (SSE). Pasa el mensaje a Claude vía Agent SDK con los MCP del hub conectados; Claude orquesta y responde en streaming | Python + FastAPI + Agent SDK |
| **Memoria/estado** | `hub.db` (ya existe) — no se duplica | SQLite compartida |
| **Grafo vivo** | ya reacciona a cada `log_interaction` (visión #2) | el cockpit lo embebe |

## Fases incrementales

- **Fase A — Cerebro headless** *(el corazón; mayor valor y riesgo)*
  FastAPI + Agent SDK + MCP del hub (projects-hub, nexus-hub). Endpoint `/chat` sin streaming.
  Prueba real: *"¿qué consume checkempresa?"* → Claude llama `resolve_dependencies` y responde. Sin UI (se prueba con `curl`).

- **Fase B — Streaming + UI de chat**
  SSE en `/chat`; UI con caja de chat, historial y streaming de tokens.

- **Fase C — Cockpit completo**
  Layout de 2 columnas con el grafo vivo embebido; las interacciones que genere el agente encienden las burbujas en tiempo real.

- **Fase D — Pulido**
  Mostrar en el chat "qué está haciendo" (las tool-calls en vivo), manejo de errores, persistir la conversación, auth si se expone.

## Dónde vive

Nuevo módulo `cockpit/` en el repo **Nexus** (open-source, genérico). Usa la versión genérica del grafo (`dashboard.py`), no un panel personal. Mantiene la convención del proyecto: Python + stdlib donde se pueda, dependencias mínimas y declaradas.

## Prerequisitos (bloqueantes para iniciar la Fase A)

- 🔑 **`ANTHROPIC_API_KEY`** con saldo — **costo por uso**, distinto de la suscripción de Claude Code. Es el principal bloqueante.
- 📦 **Agent SDK de Anthropic** (Python) instalado en un venv dedicado del cockpit.
- Los MCP del hub (ya en `mcp-servers/`) conectados al SDK como subprocesos stdio, igual que en la config de Claude Code.

## Decisiones y riesgos a resolver

- **Modelo por defecto**: Sonnet (más barato) vs Opus (mejor calidad). Configurable por variable de entorno.
- **Subagentes desde el SDK**: confirmar en la Fase A cómo el Agent SDK lanza las "extremidades" (subagentes que leen cada repo). **A verificar contra la documentación del SDK al implementar — no asumir la API.**
- **Seguridad**: las tools del hub son seguras (leen/escriben `hub.db`). Las acciones git las sigue confirmando el humano (nunca trabajar sobre `main`, merge por el responsable) — igual que el flujo actual.
- **Streaming**: SSE para tokens y para eventos de tool-use (que alimentan el "qué está haciendo").
- **Coexistencia**: el cockpit no elimina Claude Code.

## Estado

Documento de diseño aprobado. Implementación pendiente, por fases (A → B → C → D), en sesiones dedicadas, una vez disponible la API key y el entorno.
