"""Extrae un grafo de conocimiento de un arbol Python usando solo stdlib (ast).

Produce nodes (file/class/function) + edges (contains/imports/calls) con
docstrings y firmas. Sin dependencias externas, sin Node, sin Tree-sitter.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from collections import defaultdict

DEFAULT_IGNORE_DIRS = {
    ".git", "__pycache__", "venv", ".venv", "env", ".env",
    "node_modules", "dist", "build", ".mypy_cache", ".pytest_cache",
    ".trash", ".ua", ".understand-anything", "back_up",
}
DOC_MAX = 500


def _rel(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _dotted(rel: str) -> str:
    no_ext = rel[:-3] if rel.endswith(".py") else rel
    if no_ext.endswith("/__init__"):
        no_ext = no_ext[: -len("/__init__")]
    return no_ext.replace("/", ".")


def _sig(node: ast.AST) -> str:
    try:
        return "(" + ast.unparse(node.args) + ")"
    except Exception:
        return "(...)"


def _doc(node: ast.AST) -> str:
    d = ast.get_docstring(node, clean=True)
    if not d:
        return ""
    d = " ".join(d.split())
    return d[:DOC_MAX]


def _call_name(func: ast.AST):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _direct_calls(body) -> set:
    names, stack = set(), list(body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(n, ast.Call):
            nm = _call_name(n.func)
            if nm:
                names.add(nm)
        stack.extend(ast.iter_child_nodes(n))
    return names


def _git_commit(root: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _iter_py_files(root: str, ignore_tests: bool):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_IGNORE_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            if ignore_tests and ("test" in fn.lower()):
                continue
            yield os.path.join(dirpath, fn)


def build_graph(root: str, ignore_tests: bool = True) -> dict:
    root = os.path.abspath(root)
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    defs_index: dict[str, list[str]] = defaultdict(list)
    file_defs: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    dir_defs: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    module_index: dict[str, str] = {}
    func_calls: dict[str, set] = {}
    parse_errors: list[str] = []

    def _register(name, nid, rel):
        defs_index[name].append(nid)
        file_defs[rel][name].append(nid)
        dir_defs[os.path.dirname(rel)][name].append(nid)

    def add_node(nid, ntype, name, rel, line, sig="", doc=""):
        nodes[nid] = {
            "id": nid, "type": ntype, "name": name, "file": rel,
            "line": line, "signature": sig, "doc": doc, "summary": "",
        }

    def process_body(stmts, rel, parent_id, prefix):
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = prefix + stmt.name
                nid = f"function:{rel}:{qual}"
                add_node(nid, "function", qual, rel,
                         [stmt.lineno, getattr(stmt, "end_lineno", stmt.lineno)],
                         _sig(stmt), _doc(stmt))
                edges.append({"source": parent_id, "target": nid, "type": "contains"})
                _register(stmt.name, nid, rel)
                func_calls[nid] = _direct_calls(stmt.body)
                process_body(stmt.body, rel, nid, qual + ".")
            elif isinstance(stmt, ast.ClassDef):
                qual = prefix + stmt.name
                nid = f"class:{rel}:{qual}"
                add_node(nid, "class", qual, rel,
                         [stmt.lineno, getattr(stmt, "end_lineno", stmt.lineno)],
                         "", _doc(stmt))
                edges.append({"source": parent_id, "target": nid, "type": "contains"})
                _register(stmt.name, nid, rel)
                process_body(stmt.body, rel, nid, qual + ".")

    files = sorted(_iter_py_files(root, ignore_tests))
    file_imports: dict[str, list[str]] = {}

    for path in files:
        rel = _rel(path, root)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            parse_errors.append(f"{rel}: {exc.__class__.__name__}")
            continue

        fid = f"file:{rel}"
        add_node(fid, "file", rel, rel,
                 [1, getattr(tree, "end_lineno", 1) or 1], "", _doc(tree))
        module_index[_dotted(rel)] = fid
        func_calls[fid] = _direct_calls(tree.body)

        imps: list[str] = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imps += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                imps.append(n.module)
                imps += [f"{n.module}.{a.name}" for a in n.names]
        file_imports[fid] = imps

        process_body(tree.body, rel, fid, "")

    def resolve_module(cand, filedir):
        # scope de carpeta primero: `from utils` en DIR/main.py -> DIR/utils.py
        if filedir:
            hit = module_index.get(f"{filedir.replace('/', '.')}.{cand}")
            if hit:
                return hit
        hit = module_index.get(cand)
        if hit:
            return hit
        matches = [v for k, v in module_index.items()
                   if k == cand or k.endswith("." + cand)]
        return matches[0] if len(matches) == 1 else None

    def resolve_call(nm, rel, filedir):
        for scope in (file_defs[rel].get(nm), dir_defs[filedir].get(nm), defs_index.get(nm)):
            if scope and len(scope) == 1:
                return scope[0], "ok"
            if scope:
                return None, "ambiguous"
        return None, "external"

    for fid, imps in file_imports.items():
        filedir = os.path.dirname(nodes[fid]["file"])
        seen = set()
        for cand in imps:
            target = resolve_module(cand, filedir)
            if target and target != fid and target not in seen:
                seen.add(target)
                edges.append({"source": fid, "target": target, "type": "imports"})

    ambiguous = external = 0
    for src, names in func_calls.items():
        rel = nodes[src]["file"]
        filedir = os.path.dirname(rel)
        for nm in names:
            target, why = resolve_call(nm, rel, filedir)
            if target and target != src:
                edges.append({"source": src, "target": target, "type": "calls"})
            elif why == "ambiguous":
                ambiguous += 1
            elif why == "external":
                external += 1

    by_type: dict[str, int] = defaultdict(int)
    for nd in nodes.values():
        by_type[nd["type"]] += 1
    edge_type: dict[str, int] = defaultdict(int)
    for ed in edges:
        edge_type[ed["type"]] += 1

    return {
        "version": "nexus-understand/1",
        "root": root.replace(os.sep, "/"),
        "git_commit": _git_commit(root),
        "nodes": list(nodes.values()),
        "edges": edges,
        "stats": {
            "files_parsed": len(files) - len(parse_errors),
            "parse_errors": parse_errors,
            "nodes_by_type": dict(by_type),
            "edges_by_type": dict(edge_type),
            "calls_ambiguous_skipped": ambiguous,
            "calls_external_skipped": external,
        },
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Grafo AST de un arbol Python (stdlib).")
    ap.add_argument("folder", help="Carpeta a analizar")
    ap.add_argument("--out", default="", help="Ruta del JSON de salida (default: <folder>/.nexus/understand.json)")
    ap.add_argument("--include-tests", action="store_true", help="No excluir archivos *test*")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.folder):
        print(f"error: no es carpeta: {args.folder}", file=sys.stderr)
        return 2

    graph = build_graph(args.folder, ignore_tests=not args.include_tests)
    out = args.out or os.path.join(args.folder, ".nexus", "understand.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(graph, fh, ensure_ascii=False, indent=2)

    s = graph["stats"]
    print(f"OK -> {out}")
    print(f"  nodes: {len(graph['nodes'])} {s['nodes_by_type']}")
    print(f"  edges: {len(graph['edges'])} {s['edges_by_type']}")
    print(f"  parse_errors: {len(s['parse_errors'])}  "
          f"calls ambiguos/externos saltados: {s['calls_ambiguous_skipped']}/{s['calls_external_skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
