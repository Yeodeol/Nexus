# nexus-understand — análisis profundo y decisión

Registro del estudio de [Understand-Anything](https://github.com/Egonex-AI/Understand-Anything)
(UA) y del módulo propio que salió de ahí (`understand/`). Sirve como memoria del
*por qué*: qué hace UA por dentro, qué se descartó, qué se rescató y cómo quedó integrado
en Nexus.

## 1. Qué es Understand-Anything y cómo funciona por dentro

UA es un **plugin de agente** (Claude Code / Cursor / Copilot) que convierte **un repo**
en un grafo de conocimiento navegable (`.ua/knowledge-graph.json`, versionado en git) y
lo consulta con slash commands (`/understand`, `/understand-chat`, `/understand-diff`…).

**`/understand` es orquestación por prompt, no un motor propio.** El "cerebro" que ejecuta
es el propio agente (la suscripción), despachando subagentes; los scripts `.mjs`/`.py`
bundleados son el pegamento **determinístico** (Tree-sitter, batching, merge, validación).
Pipeline de 7 fases:

| Fase | Qué hace | Motor |
|---|---|---|
| 0 Pre-flight | resuelve raíz, commit hash, decide full vs incremental (`git diff last..HEAD`) | determinístico |
| 1 Scan | inventario de archivos, lenguajes, import map (Tree-sitter) | subagente + `.mjs` |
| 1.5 Batch | agrupa archivos en lotes semánticos | `.mjs` |
| 2 Analyze | **N subagentes en paralelo** leen cada archivo → nodos/edges JSON | LLM (el grueso del costo) |
| 3-5 | review + asigna capas (layers) + arma tour de onboarding | subagentes |
| 6 Review | valida el grafo (Node determinístico, o LLM con `--review`) | script |
| 7 Save | escribe el grafo + fingerprints Tree-sitter (para incrementales) + `meta.json` con el commit | determinístico |

Esquema del grafo: **13 tipos de nodo** (file/function/class/module/config/document/
service/table/endpoint/pipeline/schema/resource) y **38 tipos de edge** (imports, calls,
contains, depends_on, tested_by, configures…), más `layers` y `tour`.

## 2. El hallazgo clave (valida a Nexus)

- **La consulta NO usa embeddings.** `/understand-chat` hace **grep sobre el JSON + sigue
  edges 1 salto + responde**. Hay un `embedding-search.ts`, pero **ninguna fase genera
  embeddings** → queda muerto; el search real es Fuse.js (léxico). UA llegó
  independientemente a la misma conclusión que Nexus (retrieval léxico + estructura, sin
  RAG) — refuerza la decisión de descartar embeddings del 2026-07-02.
- **Refresh incremental por cambio de HEAD** (git-diff de archivos), no por edad → es el
  mismo patrón "cerebro vivo" git-aware de Nexus.

## 3. Por qué NO se adoptó UA

1. **Toolchain:** exige Node ≥22 + pnpm ≥10 + Tree-sitter (wasm multi-lenguaje). La máquina
   tenía Node 16 y sin pnpm.
2. **Instalación bloqueada:** `/plugin marketplace add` no está disponible en el entorno.
3. **Almacén paralelo:** su grafo por-repo es otro almacén, contra el principio de Nexus
   ("una sola DB, sin deps externas donde se pueda") — el mismo motivo por el que ya se
   había descartado claude-mem.
4. **Sobra para el caso:** los repos objetivo son **100% Python**. UA arrastra Tree-sitter
   por soportar 20 lenguajes; el módulo `ast` de la stdlib da la misma estructura gratis.

## 4. Qué se construyó (nexus-understand)

Módulo `understand/` en el repo Nexus, **solo stdlib** (sin Node/Tree-sitter/pnpm/plugin):

- **`ast_graph.py`** — grafo `nodes` (file/class/function con firma + docstring) + `edges`
  (`contains`/`imports`/`calls`) vía `ast`. **Resolución con scope de carpeta:** un
  `from utils import x` en `DIR/main.py` resuelve a `DIR/utils.py` primero — así funcionan
  los repos de lambdas aislados (cada carpeta con su propio `utils.py`/`main.py`), que con
  resolución global quedaban desconectados.
- **`impact.py`** — cierre inverso sobre `calls`/`imports`: dado un archivo cambiado (o un
  `git diff`), qué lo llama/importa → "si toco esto, qué se rompe" (el `/understand-diff`).
- **`enrich.py`** — rellena `summary` de nodos file: primera frase del docstring (gratis) +
  merge opcional de un pase LLM (subagentes) para archivos sin docstring.
- **skill `/nexus-understand`** — `build` / `ask` / `impact` / `stale`. Los mapas se guardan
  en el hub (`~/.claude-projects-hub/understand/<proyecto>.json`), **no** en el repo
  analizado; cada mapa guarda el `git_commit` (git-aware; `stale` compara contra HEAD).

### Techos deliberados (documentados, no escondidos)

- **Resolución de `calls` por nombre:** una llamada a un nombre definido en >1 lugar que no
  se puede fijar por scope (mismo archivo → misma carpeta → global único) se **cuenta** como
  ambigua, no se inventa el edge. Trazar el binding real de imports sería el upgrade.
- **`calls` externos** (stdlib/boto3/requests) se saltan a propósito.
- **`summary` LLM es opcional** (`--summaries`): por defecto el mapa vive de docstrings.

## 5. Cómo encaja en Nexus

Es el **microscopio intra-repo** que complementa al **router cross-repo**: Nexus dice
**quién** provee algo entre proyectos; el mapa dice **dónde/qué** adentro del proveedor. En
la cascada de `/nexus`, antes de gastar un subagente en frío contra un repo, si existe su
mapa se consulta primero (`ask`), y `impact` habilita el análisis de cambios que Nexus no
tenía. Capacidad declarada en el hub (`nexus-hub` provides `understand`).

Comparado con UA: se obtuvo el grafo fino + impacto **sin Node/pnpm/plugin/almacén
paralelo**, en el stack de Nexus. Lo que se cede: multi-lenguaje (no se necesita, es puro
Python) y el dashboard visual de UA.

## 6. Referencia

Detalle de uso y comandos: [`understand/README.md`](../understand/README.md).
