# Widget del orquestador (Nexus)

Una ventana que muestra el **grafo de orquestación en vivo**: los nodos (proyectos)
y sus conexiones se **encienden** cuando ocurre una interacción entre sistemas.

El **cerebro es Claude Code** — ahí haces las peticiones, das los permisos y
consultas los repos (con el skill `/orquestar` y las tools del hub). Cada vez que
se registra una interacción (`log_interaction`), este widget la muestra
encendiéndose en vivo. Sin chat propio, sin Agent SDK, **sin costo de API**.

```
  Claude Code (el cerebro)              Widget (esta ventana)
  ──────────────────────────           ──────────────────────────────
  peticiones · permisos · repos  ──▶    grafo vivo: las burbujas se
  /orquestar (log_interaction)          encienden al registrar interacciones
```

## Uso
```powershell
python cockpit\widget_server.py
```
Abre http://localhost:8780 y déjalo en una ventana aparte mientras trabajas en
Claude Code. Solo requiere **Python 3.10+** (biblioteca estándar; no instala nada).
Para otro puerto: `python cockpit\widget_server.py 9000`.

## Archivos
| Archivo | Qué es |
|---|---|
| `widget_server.py` | Server mínimo (stdlib) que sirve el widget en `:8780` |
| `graph.py` | Arma el grafo desde `hub.db` (solo lectura) + la animación en vivo (`/api/interactions`) |
| `experimental/` | Experimento previo de **chat agéntico propio** (FastAPI + Claude Agent SDK). Archivado: Claude Code cumple ese rol mejor (permisos, subagentes, repos). Ver abajo. |

## Cómo se llena
Trabaja normalmente en Claude Code. Cuando orquestes (resolver dependencias,
consultar proveedores) y se registren interacciones en el hub, el widget las
muestra encendiéndose en ≤2.5 s (hace polling a `/api/interactions`).

## experimental/ (chat propio, archivado)
Un MVP de chat agéntico que reconstruía la conversación con el Agent SDK. Se
archivó porque **Claude Code ya es mejor cerebro** (manejo de permisos, lectura de
repos con subagentes, contexto). Si quieres retomarlo: instala
`experimental/requirements.txt` en un venv y corre
`uvicorn brain:app --app-dir cockpit/experimental`. Usa el mismo `graph.py` del padre.
