"""Analisis de impacto sobre el grafo de ast_graph.py.

Dado un conjunto de archivos cambiados (o un diff de git), calcula el cierre
inverso: quien llama o importa lo cambiado -> que se rompe si lo tocas.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict

REVERSE_TYPES = {"calls", "imports"}


def _changed_from_git(root: str, base: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", root, "diff", f"{base}..HEAD", "--name-only"],
        capture_output=True, text=True, timeout=15,
    )
    if out.returncode != 0:
        return []
    files = [ln.strip() for ln in out.stdout.splitlines() if ln.strip().endswith(".py")]
    rootbase = os.path.basename(os.path.abspath(root))
    norm = []
    for f in files:
        # ponytail: git devuelve rutas relativas al repo; recorto el prefijo de la
        # carpeta analizada si aplica, para casar con el 'file' del grafo
        if "/" in f and f.split("/", 1)[0] == rootbase:
            norm.append(f.split("/", 1)[1])
        else:
            norm.append(f)
    return norm


def impact(graph: dict, changed_files: set[str]) -> dict:
    rev: dict[str, list] = defaultdict(list)
    for e in graph["edges"]:
        if e["type"] in REVERSE_TYPES:
            rev[e["target"]].append((e["source"], e["type"]))

    seed = {n["id"] for n in graph["nodes"] if n["file"] in changed_files}
    affected: dict[str, str] = {}
    frontier = list(seed)
    while frontier:
        cur = frontier.pop()
        for src, etype in rev.get(cur, []):
            if src not in seed and src not in affected:
                affected[src] = etype
                frontier.append(src)

    node_file = {n["id"]: n["file"] for n in graph["nodes"]}
    files = defaultdict(set)
    for nid, etype in affected.items():
        files[node_file.get(nid, "?")].add(etype)

    return {
        "changed_files": sorted(changed_files),
        "seed_nodes": len(seed),
        "affected_nodes": len(affected),
        "affected_files": {f: sorted(v) for f, v in sorted(files.items())},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Analisis de impacto sobre el grafo nexus-understand.")
    ap.add_argument("graph", help="Ruta al understand.json")
    ap.add_argument("--changed", nargs="*", default=[], help="Archivos cambiados (rel al root del grafo)")
    ap.add_argument("--git", nargs="?", const="HEAD~1", default="", help="Deducir cambios desde git diff <base>..HEAD (default base HEAD~1)")
    args = ap.parse_args(argv)

    with open(args.graph, "r", encoding="utf-8") as fh:
        graph = json.load(fh)

    changed = set(c.replace(os.sep, "/") for c in args.changed)
    if args.git:
        changed |= set(_changed_from_git(graph["root"], args.git))
    if not changed:
        print("error: indica --changed <archivos> o --git [base]", file=sys.stderr)
        return 2

    res = impact(graph, changed)
    print(f"cambiados: {res['changed_files']}")
    print(f"nodos semilla: {res['seed_nodes']}  |  nodos afectados: {res['affected_nodes']}")
    print(f"archivos afectados ({len(res['affected_files'])}):")
    for f, why in res["affected_files"].items():
        print(f"  - {f}  ({'/'.join(why)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
