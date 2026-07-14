# understand/ — mapa fino intra-repo para Nexus

El "microscopio" que complementa el ruteo cross-repo de Nexus. Nexus dice **quién**
provee algo entre proyectos; este módulo dice **dónde/qué** adentro de un repo Python.

Todo **stdlib** (`ast`): sin Node, sin Tree-sitter, sin pnpm, sin plugin, sin deps
externas. Nace de evaluar [Understand-Anything](https://github.com/Egonex-AI/Understand-Anything):
en vez de arrastrar su motor multi-lenguaje (Node + Tree-sitter) para mapear repos que
son 100% Python, el `ast` de la stdlib da la misma estructura gratis.

## Piezas

- **`ast_graph.py`** — escanea una carpeta Python → grafo `nodes` (file/class/function con
  firma + docstring) + `edges` (`contains`/`imports`/`calls`). Resolución con **scope de
  carpeta**: `from utils import x` en `DIR/main.py` resuelve a `DIR/utils.py` (así funcionan
  repos de lambdas aislados, cada carpeta con su propio `utils.py`/`main.py`).
- **`impact.py`** — cierre inverso sobre `calls`/`imports`: dado un archivo cambiado (o un
  `git diff`), qué lo llama/importa → "si toco esto, qué se rompe".
- **`nexus-understand.md`** — skill `/nexus-understand` (copiar a `~/.claude/commands/`) que
  orquesta `build` / `ask` / `impact` / `stale` y guarda los mapas en el hub.

## Uso directo (sin skill)

```
python ast_graph.py <carpeta> --out <mapa.json>
python impact.py <mapa.json> --changed <archivo_rel>
python impact.py <mapa.json> --git HEAD~1
python -m unittest test_ast_graph
```

## Almacenamiento

Los mapas viven en el hub, **no** en el repo analizado:
`~/.claude-projects-hub/understand/<proyecto>.json`. Cada mapa guarda el `git_commit`
del repo al construirse → git-aware (vale mientras el HEAD no cambie; `stale` lo compara).

## Techos conocidos (deliberados)

- **Resolución de `calls` por nombre**: llamadas a un nombre definido en >1 lambda no se
  fijan sin trazar el binding de imports → se **cuentan** como `ambiguos`, no se inventan.
- **`calls` externos** (stdlib/boto3/requests) se saltan a propósito.
- **`summary` semántico** es opcional (flag `--summaries` en el skill, cuesta tokens); por
  defecto el mapa usa los docstrings (`doc`), que son gratis.

## Resultado de referencia

En un repo de ~270 archivos Python: 0 errores de parseo → ~1900 nodos
(file/class/function) y ~3250 edges (contains/imports/calls). El pase de
`--summaries` cubre el 100% de los nodos `file` combinando docstrings + LLM.
