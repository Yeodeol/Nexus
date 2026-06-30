# Listener autónomo de Nexus

Daemon que **auto-resuelve** el trabajo cross-sistema: sondea el hub (`hub.db`) y, cuando
aparece un **handoff** o una **consulta** dirigida a un proyecto *opt-in*, despierta a ese
proyecto lanzando un **Claude Code headless** (`claude -p`) con `cwd` = la raíz del repo.

Cierra las Fases 4–5 del roadmap: el Sistema B se resuelve solo, sin que abras su sesión a
mano. La respuesta vuelve al **buzón** del que preguntó (`read_messages`) y queda en la
bitácora (`auto_runs`, visible con `list_auto_runs` y en el dashboard).

## Qué hace el agente despertado

- **Consulta simple** (una duda: de dónde sale X, qué devuelve Y, si Z es reutilizable, un
  contrato) → investiga **read-only** (Read/Grep/Glob + `get_project_context`) y responde con
  `post_message(kind='answer')`; si vino como handoff, lo cierra con `consume_handoff`.
- **Requerimiento** (pide cambiar código) → **NO** toca código ni git: redacta un **borrador
  de alcance** (`checkpoint`) y avisa por el buzón; deja el handoff **pending** para vos.

## Seguridad

- Allowlist estrecha de tools: **solo lectura** + tools del hub. **Sin** `Write`/`Edit`/`Bash`,
  sin `delete_project`/`send_handoff`/`declare_capability` (ver `ALLOWED_TOOLS` en el `.py`).
- `--permission-mode default` en headless ⇒ lo no permitido se **deniega** (no pregunta).
- **Timeout** por agente, **concurrencia** acotada, **opt-in** por `config.json`, e
  **idempotencia** (tabla `auto_runs`: una corrida por item).
- **Watermark**: por defecto solo procesa items **nuevos** (creados tras arrancar), así no
  re-dispara el backlog viejo. Usá `--backlog` para incluirlo a propósito.
- Motor: la **suscripción** de Claude Code (`claude -p`), sin API key de pago.

## Configuración

Copiá `config.example.json` a `config.json` (este último está *gitignored*) y listá en
`responders` los proyectos que **pueden** auto-responder:

```json
{ "responders": ["respaldos-scraps", "g-back"], "poll_interval": 15, "timeout": 240,
  "max_concurrent": 2, "model": "sonnet" }
```

Override del modelo por entorno: `NEXUS_RESOLVER_MODEL`.

## Uso

```powershell
python listener/nexus_listener.py --once --dry-run --backlog   # ver qué tomaría (no lanza)
python listener/nexus_listener.py --once                       # drena lo nuevo y sale
python listener/nexus_listener.py                              # daemon (bucle, baja latencia)
python listener/nexus_listener.py --project respaldos-scraps   # acota a un proyecto opt-in
```

Para que corra solo (sin consola), registralo como Tarea Programada (`--once` cada N min):

```powershell
.\listener\register_task.ps1 -IntervalMinutes 5
```

### Toggle de 1 clic (encender/apagar a demanda)

Si preferís tenerlo **apagado** y prenderlo solo cuando lo necesitás, `nexus_toggle.ps1` es
un interruptor: el mismo clic **arranca** el daemon en bucle (~tiempo real, sin consola, vía
`pythonw`) si está apagado, o lo **detiene** si está encendido. Avisa el nuevo estado con un
popup. Detecta el estado por la línea de comando del proceso (sin PID-file).

Crea un acceso directo en el escritorio que lo dispare:

```powershell
$ws=New-Object -ComObject WScript.Shell; $l=$ws.CreateShortcut("$([Environment]::GetFolderPath('Desktop'))\Nexus Listener (ON-OFF).lnk"); $l.TargetPath="$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"; $l.Arguments='-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "'+$PSScriptRoot+'\nexus_toggle.ps1"'; $l.WorkingDirectory=$PSScriptRoot; $l.Save()
```

Logs del daemon: `~/.claude-projects-hub/nexus_listener.log` (y `.err.log`). Bajá
`poll_interval` en `config.json` si querés que escuche aún más seguido que cada 15s.

## Cómo lo gatilla una sesión

- **Consulta** (duda): `ask_provider(from_project="A", question="...", to_project="B")`
  → deja un `message kind='question'`; el listener lo toma y B responde a tu buzón.
- **Requerimiento** (trabajo accionable): `send_handoff(...)` como siempre → el listener
  despierta a B, que deja un **borrador** y avisa; el handoff queda pendiente para vos.

## Flags del CLI

| Flag | Efecto |
|---|---|
| `--once` | procesa el backlog actual y sale (ideal para Tarea Programada) |
| `--dry-run` | muestra qué tomaría, sin lanzar agentes |
| `--backlog` | incluye items viejos (ignora el watermark) |
| `--project X` | solo ese proyecto (debe ser opt-in) |
| `--since ISO` | watermark explícito (solo items posteriores) |
