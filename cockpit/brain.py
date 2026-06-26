#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nexus Cockpit - backend "cerebro" (Fase A del chat unificado, vision #3).

Expone un endpoint /chat que recibe un mensaje en lenguaje natural y deja que un
agente Claude orqueste usando las tools del hub (projects-hub + nexus-hub) via el
Claude Agent SDK. La respuesta se devuelve en streaming (SSE).

Requisitos:
  - Python 3.10+
  - pip install -r requirements.txt   (fastapi, uvicorn, claude-agent-sdk, pydantic)
  - variable de entorno ANTHROPIC_API_KEY (costo por uso)

Config (variables de entorno):
  - NEXUS_MCP_DIR : carpeta que contiene projects-hub/ y nexus-hub/ (def. ~/mcp-servers)
  - NEXUS_MODEL   : modelo del agente (def. "sonnet"; "opus" para mas calidad)

Uso:
  uvicorn brain:app --app-dir cockpit --port 8800
  curl -N -X POST localhost:8800/chat -H "Content-Type: application/json" \
       -d '{"message": "que consume checkempresa y quien lo provee?"}'

NOTA: este es el esqueleto de la Fase A. La estructura exacta de los mensajes del
SDK se lee de forma defensiva (getattr) para tolerar variaciones; al integrar en
vivo se ajusta contra el paquete instalado.
"""
import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# Import perezoso del SDK: /health debe responder aunque el paquete no este
# instalado todavia (asi se diagnostica el entorno antes de gastar tokens).
try:
    from claude_agent_sdk import (
        query,
        ClaudeAgentOptions,
        AssistantMessage,
        ResultMessage,
        SystemMessage,
    )
    _SDK_OK = True
    _SDK_ERR = ""
except Exception as exc:  # pragma: no cover - depende del entorno
    _SDK_OK = False
    _SDK_ERR = str(exc)


ROL = (
    "Eres el cerebro orquestador de Nexus. Tienes tools del hub (projects-hub y "
    "nexus-hub) para consultar el mapa de proyectos y capacidades. Usa "
    "resolve_dependencies, find_providers y list_capabilities para rutear, y "
    "log_interaction para registrar lo que ocurre entre proyectos. Responde claro "
    "y en espanol de Chile. No inventes: si algo no esta en el mapa, dilo."
)


def _venv_python(base: Path) -> str:
    """Python del venv del MCP server (Windows o POSIX)."""
    win = base / ".venv" / "Scripts" / "python.exe"
    nix = base / ".venv" / "bin" / "python"
    return str(win if win.exists() else nix)


def _mcp_servers() -> dict:
    """Config stdio de los dos MCP del hub para el Agent SDK."""
    root = Path(os.environ.get("NEXUS_MCP_DIR", Path.home() / "mcp-servers"))
    servers = {}
    for name in ("projects-hub", "nexus-hub"):
        base = root / name
        servers[name] = {"command": _venv_python(base), "args": [str(base / "server.py")]}
    return servers


def _options():
    return ClaudeAgentOptions(
        mcp_servers=_mcp_servers(),
        allowed_tools=["mcp__projects-hub__*", "mcp__nexus-hub__*"],
        model=os.environ.get("NEXUS_MODEL", "sonnet"),
    )


app = FastAPI(title="Nexus Cockpit", version="0.1.0")


class ChatIn(BaseModel):
    message: str


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.get("/health")
def health():
    """Diagnostico sin gastar tokens: SDK instalado, API key presente, modelo."""
    return {
        "ok": True,
        "sdk_installed": _SDK_OK,
        "sdk_error": _SDK_ERR,
        "api_key_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "model": os.environ.get("NEXUS_MODEL", "sonnet"),
        "mcp_dir": str(os.environ.get("NEXUS_MCP_DIR", Path.home() / "mcp-servers")),
    }


@app.post("/chat")
async def chat(inp: ChatIn):
    async def stream():
        if not _SDK_OK:
            yield _sse({"type": "error", "text": f"claude-agent-sdk no instalado: {_SDK_ERR}"})
            return
        if not os.environ.get("ANTHROPIC_API_KEY"):
            yield _sse({"type": "error", "text": "Falta ANTHROPIC_API_KEY en el entorno."})
            return
        prompt = f"{ROL}\n\nPeticion del usuario: {inp.message}"
        try:
            async for msg in query(prompt=prompt, options=_options()):
                if isinstance(msg, SystemMessage):
                    yield _sse({"type": "system", "subtype": getattr(msg, "subtype", "")})
                elif isinstance(msg, AssistantMessage):
                    for block in getattr(msg, "content", None) or []:
                        text = getattr(block, "text", None)
                        if text:
                            yield _sse({"type": "text", "text": text})
                        name = getattr(block, "name", None)
                        if name:
                            yield _sse({"type": "tool", "name": name})
                elif isinstance(msg, ResultMessage):
                    yield _sse({
                        "type": "result",
                        "subtype": getattr(msg, "subtype", ""),
                        "text": getattr(msg, "result", ""),
                        "cost_usd": getattr(msg, "total_cost_usd", None),
                    })
        except Exception as exc:  # pragma: no cover - errores de runtime del agente
            yield _sse({"type": "error", "text": str(exc)})

    return StreamingResponse(stream(), media_type="text/event-stream")
