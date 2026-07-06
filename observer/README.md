# observer — memoria pasiva de sesiones

Dos hooks de Claude Code que cierran el ciclo de memoria del hub:

- **`session_observer.py` (SessionEnd):** deja una **observación** en `hub.db` cada vez
  que termina una sesión sobre un proyecto registrado: rama, archivos tocados, primer
  prompt y stats. Todo **determinístico** (sin llamar a ningún modelo: costo cero). El
  resumen semántico lo genera después el listener en idle (`raw` → `summarized`).
- **`session_context.py` (SessionStart):** al arrancar una sesión, **inyecta** vía
  `additionalContext` un bloque compacto (~1200 chars máx) con lo que el hub sabe del
  proyecto: últimas sesiones resumidas, handoffs pendientes y mensajes sin leer. Es el
  `nexus_boot` automático. Se apaga con `inject_context: false`.

Idea adaptada de [claude-mem](https://github.com/thedotmack/claude-mem) (captura por
hooks), implementada al estilo Nexus: stdlib puro, una sola base (`hub.db`), sin
daemon extra ni base vectorial.

## Instalación

1. `setup.ps1` crea `observer/config.json` desde el ejemplo (o cópialo a mano).
2. Registrar los hooks en `~/.claude/settings.json` (fusionar con los hooks existentes):

```json
{
  "hooks": {
    "SessionEnd": [
      {"hooks": [{"type": "command",
        "command": "python \"C:/ruta/al/repo/observer/session_observer.py\""}]}
    ],
    "SessionStart": [
      {"hooks": [{"type": "command",
        "command": "python \"C:/ruta/al/repo/observer/session_context.py\""}]}
    ]
  }
}
```

La inyección de SessionStart es opcional: si solo quieres capturar, registra solo el
`SessionEnd`.

## Config (`observer/config.json`, gitignored)

| Clave | Default | Qué hace |
|---|---|---|
| `projects` | `[]` | Opt-in. Vacío = captura **todos** los proyectos registrados en el hub; con nombres, solo esos. |
| `max_files` | `40` | Tope de archivos tocados guardados por sesión. |
| `first_prompt_max_chars` | `400` | Recorte del primer prompt. |
| `retention_days` | `90` | Prune de observaciones `raw` viejas (0 = nunca). Las `summarized` se conservan. |
| `inject_context` | `true` | Activa/apaga la inyección de SessionStart. |
| `inject_max_chars` | `1200` | Presupuesto duro del bloque inyectado (~300 tokens). |
| `inject_observations` | `3` | Cuántas sesiones recientes se muestran. |

## Guardas

- **Sesiones headless del listener se saltan** (`NEXUS_LISTENER=1` en el env del agente).
- **`<private>...</private>`** en un prompt nunca se persiste; si el prompt completo era
  privado, se usa el siguiente prompt real.
- Solo captura `cwd` que caigan dentro de un proyecto registrado; el resto se ignora.
- Nunca rompe la sesión: cualquier error va a `~/.claude-projects-hub/observer.log` y
  el hook sale con código 0.

## Probar y evaluar

```powershell
python observer/session_observer.py --transcript "<ruta a un .jsonl de ~/.claude/projects/...>" --cwd "<raiz del proyecto>" --session-id prueba
python observer/session_context.py --cwd "<raiz del proyecto>"
python -m unittest observer.test_session_observer observer.test_session_context -v
```

Ver lo capturado: tool `list_observations` del MCP `nexus-hub` (requiere desplegar +
reiniciar Claude) o directo:

```powershell
python -c "import sqlite3, pathlib; [print(dict(r)) for r in sqlite3.connect(pathlib.Path.home()/'.claude-projects-hub'/'hub.db').execute('SELECT id, project, branch, substr(first_prompt,1,60), status, created_at FROM observations ORDER BY id DESC LIMIT 10')]"
```
