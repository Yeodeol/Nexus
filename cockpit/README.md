# Nexus Cockpit (Fase A — cerebro headless)

Backend "cerebro" del **chat unificado** (visión #3). Expone un chat agéntico
sobre las tools del hub (`projects-hub` + `nexus-hub`) usando el **Claude Agent SDK**.

> Estado: **Fase A** — esqueleto del backend. La UI y el grafo vivo embebido
> llegan en las fases B/C. Ver el diseño en [../docs/PLAN_CHAT_UNIFICADO.md](../docs/PLAN_CHAT_UNIFICADO.md).

## Requisitos
- Python 3.10+
- `ANTHROPIC_API_KEY` en el entorno (con saldo — **costo por uso**)
- Los MCP del hub instalados con su `.venv` (projects-hub, nexus-hub)

## Instalación
```powershell
python -m venv cockpit\.venv
cockpit\.venv\Scripts\python.exe -m pip install -r cockpit\requirements.txt
```

## Configuración (variables de entorno)
| Variable | Para qué | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | autenticación del agente | (requerida) |
| `NEXUS_MCP_DIR` | carpeta con `projects-hub/` y `nexus-hub/` | `~/mcp-servers` |
| `NEXUS_MODEL` | modelo del agente (`opus` / `sonnet`) | `opus` |

## Correr
```powershell
$env:ANTHROPIC_API_KEY="sk-ant-..."; cockpit\.venv\Scripts\python.exe -m uvicorn brain:app --app-dir cockpit --port 8800
```

## Probar
Diagnóstico sin gastar tokens (verifica SDK + API key + rutas):
```powershell
curl http://localhost:8800/health
```
Chat agéntico (streaming SSE):
```powershell
curl -N -X POST http://localhost:8800/chat -H "Content-Type: application/json" -d '{\"message\": \"que consume checkempresa y quien lo provee?\"}'
```
Esperado: el agente llama a `resolve_dependencies` y responde que `respaldos-scraps`
provee esas capacidades.

## Notas
- `/health` responde aunque el SDK no esté instalado (para diagnosticar el entorno).
- La estructura de los mensajes del SDK se lee de forma defensiva; al integrarlo en
  vivo se ajusta contra el paquete real instalado.
