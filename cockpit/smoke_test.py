#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Prueba de humo del cerebro del cockpit: lanza UNA consulta al agente y muestra la
respuesta. Sirve para validar de una sola vez que el Agent SDK, los MCP del hub y
la autenticacion funcionan, sin levantar el server HTTP.

Uso (en una terminal con Claude Code logueado, o con ANTHROPIC_API_KEY seteada):
    cockpit\\.venv\\Scripts\\python.exe cockpit\\smoke_test.py

Si responde algo como "respaldos-scraps provee ..." el cerebro funciona.
Si dice "Not logged in / Please run /login", falta resolver la autenticacion.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import brain  # noqa: E402
from claude_agent_sdk import query, AssistantMessage, ResultMessage  # noqa: E402

PREGUNTA = "que consume el proyecto checkempresa y quien lo provee? responde breve."


async def main():
    prompt = brain.ROL + "\n\nPeticion del usuario: " + PREGUNTA
    print(">>> consultando al agente (modelo:", os.environ.get("NEXUS_MODEL", "opus"), ")\n")
    async for msg in query(prompt=prompt, options=brain._options()):
        if isinstance(msg, AssistantMessage):
            for block in getattr(msg, "content", None) or []:
                if getattr(block, "text", None):
                    print(block.text)
                if getattr(block, "name", None):
                    print("  [tool]", block.name)
        elif isinstance(msg, ResultMessage):
            print("\n--- fin (subtype:", getattr(msg, "subtype", ""),
                  "| costo USD:", getattr(msg, "total_cost_usd", None), ")")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print("ERROR:", type(exc).__name__, str(exc))
