# Guía de inicio — de cero a operativo

Esta guía deja Nexus funcionando en ~20 minutos: instalación, conexión al cliente MCP,
instrucciones del "cerebro", poblado inicial y verificación. Para el diseño ver
[ARCHITECTURE.md](ARCHITECTURE.md); para el detalle del daemon ver
[listener/README.md](../listener/README.md).

## Requisitos

- **Python 3.10+** y **git** en el PATH.
- Un cliente MCP: [Claude Code](https://docs.claude.com/en/docs/claude-code) (recomendado;
  el listener lo usa como motor headless) o Claude Desktop.

## Camino rápido (Windows) — `setup.ps1`

Si estás en Windows, el script hace lo mecánico de los pasos 1, 2 y parte del 3-4:

```powershell
git clone https://github.com/Yeodeol/Nexus.git
cd Nexus
./setup.ps1        # crea venvs, instala deps, copia skills, crea config.json y genera el bloque mcpServers
```

Al terminar imprime los pasos manuales que quedan (registrar el MCP, pegar el bloque del
cerebro, reiniciar y poblar el hub). No toca tu `~/.claude.json`: te deja el bloque listo en
`mcp-servers.generated.json` para que lo pegues tú. Las **plantillas** que vas a usar están en
[`templates/`](../templates): `mcp-servers.example.json`, `claude-global.example.md` y
`seed-projects.example.json`. En Mac/Linux, sigue los pasos manuales de abajo.

## 1. Instalar

```powershell
git clone https://github.com/Yeodeol/Nexus.git
cd Nexus
python -m venv projects-hub\.venv
python -m venv nexus-hub\.venv
projects-hub\.venv\Scripts\python.exe -m pip install -r projects-hub\requirements.txt
nexus-hub\.venv\Scripts\python.exe  -m pip install -r nexus-hub\requirements.txt
```

Registra **ambos** servidores en la config **global** de tu cliente (así toda sesión, en
cualquier proyecto, los tiene). En Claude Code: `~/.claude.json` → `mcpServers` (ver el
bloque JSON del [README](../README.md#instalación)). Reinicia el cliente: las tablas de
`~/.claude-projects-hub/hub.db` se crean solas en el primer uso.

## 2. Instalar los skills

```powershell
Copy-Item skills\nexus.md "$HOME\.claude\commands\nexus.md"
Copy-Item skills\orquestar.md "$HOME\.claude\commands\orquestar.md"
```

- **`/nexus`** — cockpit: consultar y gestionar todos los proyectos desde una sesión.
- **`/orquestar`** — implementar trabajo que cruza repos (ramas coordinadas, handoffs).

## 3. Instrucciones globales del cerebro (paso clave)

Los MCP le dan a tu asistente las *herramientas*, pero nadie le dice **cuándo usarlas**.
Eso se resuelve con instrucciones globales. Agrega este bloque a tu `~/.claude/CLAUDE.md`
(créalo si no existe) — es la diferencia entre "las tools existen" y "el cerebro las usa
solo":

```markdown
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
```

## 4. Poblar el hub

En una sesión de tu cliente (cualquier carpeta):

1. **Registra cada proyecto** (nombre lógico estable + ruta absoluta + descripción):
   `register_project("mi-api", "C:/repos/mi-api", "Backend de pagos — FastAPI")`.
   > 💡 Atajo: copia [`templates/seed-projects.example.json`](../templates/seed-projects.example.json)
   > a `seed-projects.json`, edítalo con tus proyectos y pídele a Claude: *"lee
   > `seed-projects.json` y registra cada proyecto con `register_project` y sus capacidades
   > con `declare_capability`"*. Hace los pasos 1 y 2 de una.
2. **Siembra 2-3 capacidades** por proyecto para que el ruteo funcione desde el día uno:
   `declare_capability("mi-api", "provides", "cobro-tarjeta", "api", "POST /charge {amount} -> {id}")`
   y lo que cada proyecto consume (`kind="consumes"`). No busques completitud: el
   documentador del listener mantiene el mapa solo después.
3. **Configura el listener**: copia `listener/config.example.json` a `listener/config.json`
   y define:
   - `responders`: proyectos que pueden **auto-responder** consultas (opt-in).
   - `knowledge_projects`: proyectos a **documentar** con fichas (vacío = responders).
   - `git_sync_projects`: repos que se **actualizan solos** (`pull --ff-only` con guardas —
     ponlos solo donde eres consumidor pasivo, no donde desarrollas).
4. **Genera las primeras fichas** (un agente headless por proyecto, 3-6 min c/u):
   ```powershell
   python listener\nexus_listener.py --once --refresh-knowledge
   ```
5. **Deja el daemon corriendo** (auto-respuestas + fichas frescas + git sync):
   ```powershell
   python listener\nexus_listener.py
   ```
   Sin consola: `listener\nexus_toggle.ps1` (interruptor on/off) o
   `listener\register_task.ps1` (tarea programada).

## 5. Verificar

- `nexus_overview()` en una sesión → debe listar tus proyectos y fichas.
- `/nexus ¿cómo funciona <algo> en <proyecto>?` → debe responder **desde las fichas**
  citando la fuente, sin abrir el repo.
- `list_auto_runs()` → bitácora de lo que el listener hizo solo.

## Qué corre solo a partir de aquí

| Cuándo | Qué |
|---|---|
| Cada sesión | `nexus_boot` trae el contexto en 1 llamada |
| Cada 15 s (daemon) | consultas/handoffs nuevos → agente headless read-only responde |
| En idle, cada 10 min | si el HEAD de un repo cambió → re-documenta sus fichas |
| Cada 24 h | `git_sync_projects`: fetch + pull ff-only (solo repos limpios en rama default) |

El ciclo completo: el repo cambia → el sync/commit nuevo lo detecta → las fichas se
refrescan → cualquier consulta responde con la última versión. Sin intervención manual.
