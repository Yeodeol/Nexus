---
name: nexus-understand
description: Mapa fino intra-repo (Python, via ast) para Nexus - grafo nodes/edges + analisis de impacto, sin Node ni deps externas. Uso - /nexus-understand build <proyecto|ruta> | ask <proyecto> <pregunta> | impact <proyecto> [--git|--changed ...] | stale <proyecto>.
---

# /nexus-understand

Construye y consulta un **grafo de conocimiento intra-repo** de un proyecto Python
del hub, usando solo la stdlib (`ast`). Es el "microscopio" que complementa el
ruteo cross-repo de Nexus: Nexus dice QUIEN provee, este mapa dice DONDE/QUE
adentro del proveedor.

- Scripts (repo Nexus): `<NEXUS>/understand/ast_graph.py`, `impact.py`, `enrich.py`
  (donde `<NEXUS>` es la ruta local del repo Nexus en tu máquina).
- Los mapas se guardan en el hub, NO en el repo analizado:
  `~/.claude-projects-hub/understand/<proyecto>.json`.
- Cada mapa guarda el `git_commit` del repo al construirse (para detectar si quedo viejo).

## Resolver `$PROJECT` y `$MAP`

1. Parsea `$ARGUMENTS`: el primer token es el subcomando (`build`/`ask`/`impact`/`stale`),
   el segundo es `<proyecto|ruta>`.
2. Si `<proyecto>` es una ruta existente, usala como `PROJECT_ROOT` y deriva
   `PROJECT_NAME` del basename. Si es un nombre, resuelvelo con la tool
   `get_project(<nombre>)` de projects-hub para obtener `path`.
3. `MAP = ~/.claude-projects-hub/understand/<PROJECT_NAME>.json`.
   Para acotar a una subcarpeta (ej. `<repo>/<subcarpeta>`), pasa la ruta
   directa y usa un `PROJECT_NAME` compuesto (ej. `<repo>--<subcarpeta>`).

## build

```
python "<UNDERSTAND_DIR>/ast_graph.py" "<PROJECT_ROOT>" --out "<MAP>"
```
Reporta al usuario los stats que imprime (nodes/edges por tipo, parse_errors,
calls ambiguos/externos saltados). Los `ambiguos` son llamadas a nombres definidos
en >1 lambda que no se pueden fijar sin trazar imports (techo conocido, se cuentan
pero no se inventan); los `externos` son stdlib/boto3/requests (correcto saltarlos).

**Resumenes semanticos (opcional, `--summaries` — cuesta tokens):** por defecto los
nodos traen `doc` (docstring, gratis) y `summary` vacio. Si el usuario pide
`--summaries`, despacha subagentes (lotes de ~20 archivos) que para cada nodo `file`
escriban un `summary` de 1-2 frases leyendo el archivo real; mergea los `summary` de
vuelta al JSON. No lo hagas sin que lo pidan.

## ask `<proyecto>` `<pregunta>`

Responde SIN leer el repo, usando solo el mapa (patron de revelacion progresiva):
1. Verifica que `<MAP>` existe; si no, dile al usuario que corra `build` primero.
2. Grep en `<MAP>` los keywords de la pregunta sobre `name`/`doc`/`summary`/`file`.
   Anota los `id` que matchean.
3. Para esos `id`, grep en `edges` para traer el subgrafo 1-hop (que importan/llaman
   y quien los importa/llama).
4. Responde citando archivos y funciones concretas del mapa. Si no hay match, dilo y
   sugiere terminos presentes en el mapa.

## impact `<proyecto>` `[--git [base] | --changed f1 f2 ...]`

```
python "<UNDERSTAND_DIR>/impact.py" "<MAP>" --git [base]
python "<UNDERSTAND_DIR>/impact.py" "<MAP>" --changed <archivos rel al root>
```
Reporta los archivos afectados y por que (calls/imports). Sirve antes de tocar un
archivo compartido: "si cambio esto, que se rompe".

## stale `<proyecto>`

Compara `git_commit` guardado en `<MAP>` contra el HEAD actual del repo
(`git -C <PROJECT_ROOT> rev-parse HEAD`). Si difieren, avisa que el mapa quedo viejo
y sugiere `build` de nuevo. (Nexus es git-aware: el mapa vale mientras el commit no cambie.)

Donde `<UNDERSTAND_DIR>` = la carpeta `understand/` del repo Nexus en tu máquina.
