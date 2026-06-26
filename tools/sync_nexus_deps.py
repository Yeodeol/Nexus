#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_nexus_deps.py — Mantiene una seccion "Dependencias entre proyectos (Nexus)" en el
CLAUDE.md de cada proyecto, generada desde el mapa del hub (hub.db).

Para que el auto-averiguar sea AUTONOMO: el CLAUDE.md de un proyecto se carga SIEMPRE al
abrir una sesion ahi, asi que si lleva escrito que consume X de Y, Claude ya lo sabe sin
que el usuario se lo diga, y solo necesita get_project_context(Y) para el detalle.

La seccion se delimita con marcadores y se ACTUALIZA idempotentemente (no duplica ni pisa
el resto del CLAUDE.md). Solo toca proyectos que CONSUMEN algo con proveedor conocido y que
YA tienen un CLAUDE.md (no crea archivos nuevos).

Uso:
    python tools/sync_nexus_deps.py                      # DRY-RUN: muestra que cambiaria
    python tools/sync_nexus_deps.py --apply              # escribe/actualiza los CLAUDE.md
    python tools/sync_nexus_deps.py --project checkempresa [--apply]   # solo uno

Sin dependencias externas (biblioteca estandar).
"""
import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path.home() / ".claude-projects-hub" / "hub.db"
START = "<!-- NEXUS:DEPS:START (generado por tools/sync_nexus_deps.py; no editar a mano) -->"
END = "<!-- NEXUS:DEPS:END -->"


def build_deps():
    """Devuelve {proyecto: {'path':..., 'deps': {proveedor: set(capacidades)}}} para los
    proyectos que CONSUMEN algo provisto por otro (match por name o category, como
    resolve_dependencies)."""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    projects = {r["name"]: r["path"] for r in con.execute("SELECT name, path FROM projects")}
    caps = [dict(r) for r in con.execute("SELECT project, kind, name, category FROM capabilities")]
    con.close()

    prov_by_name, prov_by_cat = {}, {}
    for c in caps:
        if c["kind"] == "provides":
            prov_by_name.setdefault(c["name"], set()).add(c["project"])
            if c["category"]:
                prov_by_cat.setdefault(c["category"], set()).add(c["project"])

    out = {}
    for c in caps:
        if c["kind"] != "consumes":
            continue
        proj = c["project"]
        provs = set(prov_by_name.get(c["name"], set()))
        if c["category"]:
            provs |= prov_by_cat.get(c["category"], set())
        provs.discard(proj)
        if not provs:
            continue
        entry = out.setdefault(proj, {"path": projects.get(proj), "deps": {}})
        for p in provs:
            entry["deps"].setdefault(p, set()).add(c["name"])
    return out


def render_section(project, deps):
    lines = [
        START, "",
        "## Dependencias entre proyectos (Nexus)", "",
        f"`{project}` CONSUME datos/servicios de otros sistemas que NO viven en este repo.",
        "Cuando te pregunten o necesites algo que dependa de ellos, usa Nexus ANTES de buscar",
        f'solo aqui o decir "no esta": `resolve_dependencies("{project}")` y luego',
        "`get_project_context(\"<proveedor>\")` para traer su CLAUDE.md y contratos.", "",
        "| Para (capacidades) | Lo provee |", "|---|---|",
    ]
    for prov in sorted(deps):
        caps = ", ".join(sorted(deps[prov]))
        lines.append(f"| {caps} | **{prov}** |")
    lines += ["", END]
    return "\n".join(lines)


def upsert(md_text, section):
    """Inserta o reemplaza el bloque entre marcadores, sin tocar el resto."""
    if START in md_text and END in md_text:
        pre = md_text.split(START)[0].rstrip("\n")
        post = md_text.split(END, 1)[1].lstrip("\n")
        body = pre + "\n\n" + section
        if post:
            body += "\n\n" + post
        return body.rstrip("\n") + "\n"
    return md_text.rstrip("\n") + "\n\n" + section + "\n"


def main():
    ap = argparse.ArgumentParser(description="Sincroniza la seccion de dependencias Nexus en los CLAUDE.md.")
    ap.add_argument("--apply", action="store_true", help="escribe los cambios (sin esto, dry-run)")
    ap.add_argument("--project", default="", help="procesar solo este proyecto")
    args = ap.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"No se encontro la BD del hub: {DB_PATH}")

    data = build_deps()
    if args.project:
        data = {k: v for k, v in data.items() if k == args.project}
    if not data:
        print("No hay proyectos con dependencias declaradas (o el filtro no coincide).")
        return

    changed = 0
    for project, info in sorted(data.items()):
        path = info["path"]
        if not path:
            print(f"[skip]  {project}: sin ruta registrada en el hub.")
            continue
        base = Path(path)
        md = next((base / fn for fn in ("CLAUDE.md", "claude.md", "Claude.md") if (base / fn).exists()), None)
        if md is None:
            print(f"[skip]  {project}: sin CLAUDE.md en {base} (no se crea automaticamente).")
            continue
        section = render_section(project, info["deps"])
        old = md.read_text(encoding="utf-8", errors="replace")
        new = upsert(old, section)
        provs = ", ".join(sorted(info["deps"]))
        if old == new:
            print(f"[ok]    {project}: ya esta al dia ({md.name}).")
            continue
        changed += 1
        if args.apply:
            md.write_text(new, encoding="utf-8")
            print(f"[write] {project}: actualizado {md} -> proveedores: {provs}")
        else:
            print(f"[dry]   {project}: actualizaria {md} -> proveedores: {provs}")

    if not args.apply and changed:
        print(f"\nDRY-RUN ({changed} por actualizar). Para escribir: python tools/sync_nexus_deps.py --apply")
    elif args.apply:
        print(f"\nListo: {changed} CLAUDE.md actualizados.")


if __name__ == "__main__":
    main()
