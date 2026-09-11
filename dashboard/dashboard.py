#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dashboard de monitoreo de Nexus.

Lee la base compartida del hub (~/.claude-projects-hub/hub.db) en SOLO LECTURA y
sirve un panel HTML en http://localhost:8788 con:

  - metricas globales (proyectos, capacidades, interacciones, features),
  - el grafo de dependencias e interacciones entre proyectos,
  - el ruteo resuelto (quien consume que y quien lo provee),
  - el mapa de capacidades por proyecto (provee / consume),
  - el estado de las features coordinadas (rama por repo),
  - las interacciones recientes.

No tiene dependencias externas: solo la biblioteca estandar de Python (3.10+).
El HTML se regenera leyendo la BD en cada request, asi siempre esta fresco; el
front hace polling a /version y recarga solo cuando la BD cambia.

Uso:
    python dashboard.py                  # sirve en http://localhost:8788
    python dashboard.py --port 9000      # usa otro puerto
    python dashboard.py --once           # imprime el HTML una vez y sale
"""
import argparse
import html
import json
import math
import sqlite3
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tickets as nxtickets

DB_PATH = Path.home() / ".claude-projects-hub" / "hub.db"
DEFAULT_PORT = 8788


# --------------------------------------------------------------------------
# Capa de datos (solo lectura sobre hub.db)
# --------------------------------------------------------------------------
def _con():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _rows(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def db_version():
    """Marca de version para el auto-reload: mtime de la BD."""
    try:
        return str(DB_PATH.stat().st_mtime)
    except OSError:
        return "0"


def build_data():
    """Lee el hub y arma el modelo que consume la plantilla."""
    con = _con()
    projects = _rows(con, "SELECT name, path, description, status FROM projects ORDER BY name")
    caps = _rows(con, "SELECT project, kind, name, category, contract, notes "
                      "FROM capabilities ORDER BY project, kind, name")
    interactions = _rows(con, "SELECT from_project, to_project, intent, capability, outcome, "
                              "feature, created_at FROM interactions ORDER BY id DESC")
    features = _rows(con, "SELECT id, slug, branch, type, description, status, updated_at "
                          "FROM coordinated_features ORDER BY COALESCE(updated_at, created_at) DESC")
    branches = _rows(con, "SELECT feature_id, project, branch, state, pr_url "
                          "FROM feature_branches ORDER BY feature_id, project")
    tickets, huerfanos = nxtickets.collect(con)
    con.close()

    # Ruteo: por cada 'consumes', que proyecto(s) lo 'provides'.
    providers = {}
    for c in caps:
        if c["kind"] == "provides":
            providers.setdefault(c["name"], []).append(c["project"])
    routes = []
    for c in caps:
        if c["kind"] == "consumes":
            for prov in providers.get(c["name"], []):
                routes.append({
                    "consumer": c["project"],
                    "provider": prov,
                    "capability": c["name"],
                    "category": c["category"] or "",
                })

    # Aristas del grafo, agrupadas por (origen, destino). Peso = capacidades + interacciones.
    edge_map = {}
    for r in routes:
        e = edge_map.setdefault((r["consumer"], r["provider"]),
                                {"src": r["consumer"], "dst": r["provider"], "caps": 0, "ints": 0})
        e["caps"] += 1
    for it in interactions:
        e = edge_map.setdefault((it["from_project"], it["to_project"]),
                                {"src": it["from_project"], "dst": it["to_project"], "caps": 0, "ints": 0})
        e["ints"] += 1
    edges = list(edge_map.values())

    nodes = set()
    for e in edges:
        nodes.add(e["src"])
        nodes.add(e["dst"])

    # Capacidades agrupadas por proyecto.
    by_project = {}
    for c in caps:
        bp = by_project.setdefault(c["project"], {"provides": [], "consumes": []})
        bp[c["kind"]].append(c)

    # Features coordinadas con sus ramas por repo.
    branch_by_feat = {}
    for b in branches:
        branch_by_feat.setdefault(b["feature_id"], []).append(b)
    for f in features:
        f["branches"] = branch_by_feat.get(f["id"], [])

    abiertos = [t for t in tickets if t["status"] == "abierto"]
    pendientes = sum(len(t["pendientes"]) for t in tickets) + len(huerfanos)

    return {
        "projects": projects,
        "by_project": by_project,
        "routes": routes,
        "edges": edges,
        "nodes": sorted(nodes),
        "interactions": interactions,
        "features": features,
        "tickets": tickets,
        "huerfanos": huerfanos,
        "filter_projects": sorted({p["name"] for p in projects} |
                                  {p for t in tickets for p in t["projects"]}),
        "metrics": {
            "projects": len(projects),
            "capabilities": len(caps),
            "interactions": len(interactions),
            "abiertos": len(abiertos),
            "pendientes": pendientes,
        },
    }


# --------------------------------------------------------------------------
# Render de secciones
# --------------------------------------------------------------------------
def esc(s):
    return html.escape(str(s if s is not None else ""))


def render_metrics(m):
    cells = [
        ("Proyectos", m["projects"], ""),
        ("Capacidades", m["capabilities"], ""),
        ("Req. abiertos", m["abiertos"], "warn"),
        ("Pendientes", m["pendientes"], "warn"),
    ]
    out = []
    for label, val, cls in cells:
        out.append(f'<div class="metric {cls}"><div class="lbl">{esc(label)}</div>'
                   f'<div class="val">{val}</div></div>')
    return "".join(out)


def render_graph(nodes, edges):
    """SVG con layout circular: nodos = proyectos, aristas curvas = dependencias/interacciones."""
    if not nodes:
        return ('<p class="empty">Aun no hay relaciones entre proyectos. Declara capacidades '
                '(provides/consumes) o registra interacciones para ver el grafo.</p>')
    W, H = 880, 620
    cx, cy = W / 2, H / 2
    R = min(W, H) / 2 - 110
    NR = 27
    n = len(nodes)
    pos = {}
    for i, name in enumerate(nodes):
        if n == 1:
            pos[name] = (cx, cy)
        else:
            ang = (2 * math.pi * i / n) - math.pi / 2
            pos[name] = (cx + R * math.cos(ang), cy + R * math.sin(ang))

    grados = {name: {"n": 0, "caps": 0, "ints": 0} for name in nodes}
    for e in edges:
        for side in ("src", "dst"):
            g = grados[e[side]]
            g["n"] += 1
            g["caps"] += e["caps"]
            g["ints"] += e["ints"]

    parts = [f'<svg viewBox="0 0 {W} {H}" class="graph" xmlns="http://www.w3.org/2000/svg" '
             f'role="img" aria-label="Grafo de dependencias entre proyectos">']
    # markerUnits fijo: si no, la punta escala con el grosor y tapa el grafo.
    parts.append('<defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" '
                 'markerUnits="userSpaceOnUse" markerWidth="9" markerHeight="9" '
                 'orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" fill="currentColor"/>'
                 '</marker></defs>')

    for e in edges:
        x1, y1 = pos[e["src"]]
        x2, y2 = pos[e["dst"]]
        dx, dy = x2 - x1, y2 - y1
        dist = math.hypot(dx, dy) or 1.0
        ux, uy = dx / dist, dy / dist
        # Arco siempre curvado hacia el mismo lado: separa A->B de B->A.
        bow = dist * 0.13
        qx, qy = (x1 + x2) / 2 - uy * bow, (y1 + y2) / 2 + ux * bow
        s = math.hypot(qx - x1, qy - y1) or 1.0
        sx, sy = x1 + (qx - x1) / s * NR, y1 + (qy - y1) / s * NR
        t = math.hypot(qx - x2, qy - y2) or 1.0
        ex, ey = x2 + (qx - x2) / t * (NR + 4), y2 + (qy - y2) / t * (NR + 4)

        peso = e["caps"] + e["ints"]
        width = min(1.2 + peso * 0.35, 4.5)
        bits = []
        if e["caps"]:
            bits.append(f'{e["caps"]} capacidad(es)')
        if e["ints"]:
            bits.append(f'{e["ints"]} interaccion(es)')
        info = f'{e["src"]} → {e["dst"]} · ' + ", ".join(bits)
        cls = "edge cap" if e["caps"] else "edge int"
        parts.append(f'<path class="{cls}" d="M{sx:.1f},{sy:.1f} Q{qx:.1f},{qy:.1f} {ex:.1f},{ey:.1f}" '
                     f'stroke-width="{width:.1f}" marker-end="url(#arrow)" '
                     f'data-src="{esc(e["src"])}" data-dst="{esc(e["dst"])}" '
                     f'data-info="{esc(info)}"><title>{esc(info)}</title></path>')

    for name, (x, y) in pos.items():
        g = grados[name]
        info = (f'{name} · {g["n"]} conexion(es) · {g["caps"]} capacidad(es) '
                f'· {g["ints"]} interaccion(es)')
        # Etiqueta hacia afuera del circulo para que no se pise con las aristas.
        dirx, diry = x - cx, y - cy
        d = math.hypot(dirx, diry) or 1.0
        lx, ly = x + dirx / d * (NR + 16), y + diry / d * (NR + 16)
        anchor = "middle"
        if dirx / d > 0.35:
            anchor = "start"
        elif dirx / d < -0.35:
            anchor = "end"
        parts.append(f'<g class="gnode-g" data-name="{esc(name)}" data-info="{esc(info)}" '
                     f'tabindex="0" role="button" aria-label="{esc(info)}">'
                     f'<title>{esc(info)}</title>'
                     f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{NR}" class="gnode"/>'
                     f'<text x="{x:.1f}" y="{y + 4:.1f}" class="nodecnt" text-anchor="middle">'
                     f'{g["n"]}</text>'
                     f'<text x="{lx:.1f}" y="{ly + 4:.1f}" class="nodelbl" '
                     f'text-anchor="{anchor}">{esc(name)}</text></g>')
    parts.append("</svg>")

    legend = ('<div class="glegend">'
              '<span><i class="sw cap"></i> dependencia declarada</span>'
              '<span><i class="sw int"></i> interaccion registrada</span>'
              '<span>grosor = volumen &middot; numero en el nodo = conexiones</span>'
              '<span class="ghint">pasa el mouse para aislar, clic en un proyecto para filtrar</span>'
              '</div><div class="gcaption" id="gcap"></div>')
    return '<div class="graphwrap">' + "\n".join(parts) + legend + "</div>"


def render_routes(routes):
    if not routes:
        return '<p class="empty">No hay dependencias declaradas todavia.</p>'
    trs = []
    for r in routes:
        cat = f' <span class="cat">{esc(r["category"])}</span>' if r["category"] else ""
        trs.append(f"<tr data-projects='{esc(r['consumer'])},{esc(r['provider'])}'>"
                   f"<td>{esc(r['consumer'])}</td>"
                   f"<td class='arrow'>&rarr;</td>"
                   f"<td><span class='cap'>{esc(r['capability'])}</span>{cat}</td>"
                   f"<td class='arrow'>&rarr;</td>"
                   f"<td><strong>{esc(r['provider'])}</strong></td>"
                   "</tr>")
    return ("<table class='tbl'><thead><tr><th>Consume</th><th></th><th>Capacidad</th>"
            "<th></th><th>Lo provee</th></tr></thead><tbody>" + "".join(trs) + "</tbody></table>")


def _cap_li(c, with_cat=True):
    cat = f" <span class='cat'>{esc(c['category'])}</span>" if (with_cat and c['category']) else ""
    return f"<li><span class='cap'>{esc(c['name'])}</span>{cat}</li>"


def render_caps(by_project):
    if not by_project:
        return '<p class="empty">Ningun proyecto declaro capacidades.</p>'
    cards = []
    for project in sorted(by_project):
        bp = by_project[project]
        blocks = []
        if bp["provides"]:
            items = "".join(_cap_li(c) for c in bp["provides"])
            blocks.append(f"<div class='caplane'><div class='lanehead prov'>Provee "
                          f"<span class='cnt'>{len(bp['provides'])}</span></div><ul>{items}</ul></div>")
        if bp["consumes"]:
            items = "".join(_cap_li(c, with_cat=False) for c in bp["consumes"])
            blocks.append(f"<div class='caplane'><div class='lanehead cons'>Consume "
                          f"<span class='cnt'>{len(bp['consumes'])}</span></div><ul>{items}</ul></div>")
        body = "".join(blocks)
        cards.append(f"<div class='capcard' data-projects='{esc(project)}'>"
                     f"<div class='capproj'>{esc(project)}</div>"
                     f"<div class='lanes'>{body}</div></div>")
    grid = "".join(cards)
    return f"<div class='capgrid'>{grid}</div>"


def _pr_cell(pr_url):
    if not pr_url:
        return ""
    return f"<a href='{esc(pr_url)}' target='_blank' rel='noopener'>PR &nearr;</a>"


def _branch_row(b):
    return ("<tr>"
            f"<td>{esc(b['project'])}</td>"
            f"<td><code>{esc(b['branch'])}</code></td>"
            f"<td><span class='chip s-{esc(b['state'])}'>{esc(b['state'])}</span></td>"
            f"<td>{_pr_cell(b['pr_url'])}</td>"
            "</tr>")


def render_features(features):
    if not features:
        return ('<p class="empty">No hay features coordinadas. Crea una con '
                '<code>create_coordinated_feature</code> para coordinar la misma rama en varios repos.</p>')
    out = []
    for f in features:
        if f["branches"]:
            brs = "".join(_branch_row(b) for b in f["branches"])
        else:
            brs = "<tr><td colspan='4' class='empty'>Sin ramas sembradas.</td></tr>"
        out.append(
            f"<div class='feat' data-projects='{esc(','.join(b['project'] for b in f['branches']))}'>"
            f"<div class='feathead'><span class='chip s-{esc(f['status'])}'>{esc(f['status'])}</span>"
            f"<span class='featslug'>{esc(f['type'])}/{esc(f['slug'])}</span></div>"
            f"<div class='featdesc'>{esc(f['description'])}</div>"
            "<table class='tbl'><thead><tr><th>Repo</th><th>Rama</th><th>Estado</th><th>PR</th></tr></thead>"
            f"<tbody>{brs}</tbody></table></div>"
        )
    return "".join(out)


def render_interactions(interactions):
    if not interactions:
        return ('<p class="empty">Sin interacciones registradas. El orquestador las anota con '
                '<code>log_interaction</code> cada vez que un proyecto consulta a otro.</p>')
    trs = []
    for it in interactions[:50]:
        when = (it["created_at"] or "")[:16].replace("T", " ")
        trs.append(f"<tr data-projects='{esc(it['from_project'])},{esc(it['to_project'])}'>"
                   f"<td>{esc(it['from_project'])} &rarr; {esc(it['to_project'])}</td>"
                   f"<td>{esc(it['capability'])}</td>"
                   f"<td>{esc(it['intent'])}</td>"
                   f"<td>{esc(it['outcome'])}</td>"
                   f"<td class='ndate'>{esc(when)}</td></tr>")
    return ("<table class='tbl'><thead><tr><th>Interaccion</th><th>Capacidad</th><th>Intencion</th>"
            "<th>Resultado</th><th>Fecha</th></tr></thead><tbody>" + "".join(trs) + "</tbody></table>")


# --------------------------------------------------------------------------
# Plantilla HTML
# --------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nexus - Trazabilidad</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.11.0/dist/tabler-icons.min.css">
<style>
  :root{
    --navy:#1D2233; --navy2:#2A3147; --orange:#F5821F; --orange2:#E06A1B;
    --bg:#F6F7F9; --surface:#FFFFFF; --surface2:#FAFBFC; --stripe:#FAFBFC;
    --text:#1D2233; --text2:#4A5160; --text3:#9AA1AD;
    --border:#E6E8EC; --border2:#D7DCE6;
    --ok:#1F7A5A; --info:#1F3A5F; --thead-bg:#1D2233; --thead-fg:#FFFFFF;
    --font:"Segoe UI",Lato,-apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,sans-serif;
    --mono:Consolas,ui-monospace,SFMono-Regular,Menlo,"Courier New",monospace;
    --radius:10px;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --navy:#161A24; --navy2:#2A3147;
      --bg:#161A24; --surface:#1E2330; --surface2:#252B3A; --stripe:#212734;
      --text:#E6E8EC; --text2:#B6BCC8; --text3:#8C93A1;
      --border:rgba(255,255,255,.10); --border2:rgba(255,255,255,.20);
      --ok:#4ECCA8; --info:#7FB2EC; --thead-bg:#2A3147; --thead-fg:#E6E8EC;
    }
  }
  *{box-sizing:border-box;}
  body{margin:0;background:var(--bg);color:var(--text);font-family:var(--font);
       font-size:14px;line-height:1.55;}

  /* ---------- Header institucional ---------- */
  header.topbar{display:flex;justify-content:space-between;align-items:center;gap:1rem;
       flex-wrap:wrap;background:var(--surface);border-bottom:3px solid var(--orange);
       padding:14px 1.5rem 12px;}
  .logo{display:flex;align-items:center;gap:9px;}
  .logo-text{font-size:19px;font-weight:800;color:var(--orange);letter-spacing:-.5px;}
  .logo .sep{width:1px;height:20px;background:var(--border2);}
  .logo .appname{font-size:16px;font-weight:700;color:var(--text);letter-spacing:-.2px;}
  .topright{text-align:right;color:var(--text3);font-size:11.5px;line-height:1.45;}

  .wrap{max-width:1040px;margin:0 auto;padding:1.25rem 1.5rem 3rem;}

  /* ---------- Metricas ---------- */
  .metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;}
  .metric{background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--orange);
       border-radius:var(--radius);padding:.7rem .9rem;}
  .metric .lbl{font-size:11.5px;color:var(--text3);text-transform:uppercase;letter-spacing:.4px;}
  .metric .val{font-size:27px;font-weight:800;line-height:1.15;}
  .metric.warn{border-left-color:var(--orange);} .metric.warn .val{color:var(--orange);}

  /* ---------- Pestanas ---------- */
  nav.tabs{display:flex;gap:2px;margin:1.4rem 0 0;border-bottom:1px solid var(--border);
       flex-wrap:wrap;}
  nav.tabs button{font:inherit;font-size:13.5px;font-weight:600;color:var(--text2);
       background:none;border:none;border-bottom:3px solid transparent;cursor:pointer;
       padding:.55rem .9rem;display:flex;align-items:center;gap:6px;margin-bottom:-1px;}
  nav.tabs button:hover{color:var(--text);}
  nav.tabs button[aria-selected="true"]{color:var(--orange);border-bottom-color:var(--orange);}
  nav.tabs i{font-size:16px;}
  .badge{background:var(--orange);color:#fff;font-size:11px;font-weight:700;
       border-radius:9px;padding:0 6px;min-width:18px;text-align:center;}
  nav.tabs button[aria-selected="false"] .badge{background:var(--text3);}

  /* ---------- Filtros ---------- */
  .filters{display:flex;gap:.75rem;align-items:center;flex-wrap:wrap;
       background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
       padding:.55rem .8rem;margin:.9rem 0 1rem;font-size:13px;color:var(--text2);}
  .filters input[type=search],.filters select{font:inherit;background:var(--surface2);
       color:var(--text);border:1px solid var(--border2);border-radius:7px;padding:4px 8px;}
  .filters input[type=search]{flex:1;min-width:180px;}
  .filters input[type=search]:focus,.filters select:focus{outline:2px solid var(--orange);
       outline-offset:-1px;}
  .filters label{display:flex;align-items:center;gap:5px;white-space:nowrap;}
  .fcount{margin-left:auto;color:var(--text3);font-size:12px;font-variant-numeric:tabular-nums;}

  /* ---------- Paneles y secciones ---------- */
  .pane[hidden]{display:none;}
  h2{font-size:15px;font-weight:800;color:var(--text);border-left:4px solid var(--orange);
       padding-left:10px;margin:1.5rem 0 .7rem;}
  .pane > h2:first-child{margin-top:0;}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
       padding:.9rem 1.1rem;margin-bottom:.4rem;overflow-x:auto;}
  .empty{font-size:13px;color:var(--text2);margin:.25rem 0;padding:.5rem 0;}
  .empty code,h2 code,.featdesc code,.hint code{font-family:var(--mono);font-size:12px;
       background:var(--surface2);border:1px solid var(--border);padding:0 4px;border-radius:4px;}
  .hint{font-size:12px;color:var(--text3);margin:.1rem 0 .9rem;}

  /* ---------- Tablas ---------- */
  table.tbl{width:100%;border-collapse:collapse;font-size:13px;}
  table.tbl thead th{background:var(--thead-bg);color:var(--thead-fg);font-weight:700;
       text-align:left;padding:7px 10px;font-size:11.5px;text-transform:uppercase;
       letter-spacing:.3px;white-space:nowrap;}
  table.tbl thead th:first-child{border-radius:6px 0 0 6px;}
  table.tbl thead th:last-child{border-radius:0 6px 6px 0;}
  table.tbl td{padding:7px 10px;border-bottom:1px solid var(--border);vertical-align:top;}
  table.tbl tbody tr:nth-child(even){background:var(--stripe);}
  table.tbl tbody tr:hover{background:var(--surface2);}
  table.tbl tr:last-child td{border-bottom:none;}
  td.arrow{color:var(--text3);width:1%;white-space:nowrap;}
  .ndate{color:var(--text3);font-family:var(--mono);font-size:11.5px;white-space:nowrap;}
  a{color:var(--orange);}

  /* ---------- Chips y estados ---------- */
  .chip{font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px;
       border:1px solid var(--border2);white-space:nowrap;color:var(--text2);}
  .s-abierto,.s-pending,.s-pend,.s-pushed,.s-pr-open,.s-in-progress{
       color:var(--orange);border-color:var(--orange);background:rgba(245,130,31,.09);}
  .s-listo,.s-consumed,.s-merged{color:var(--ok);border-color:var(--ok);
       background:rgba(31,122,90,.09);}
  .s-created,.s-committed,.s-open{color:var(--info);border-color:var(--info);}
  .s-planned,.s-closed,.s-nota,.s-analisis,.s-asked,.s-consulted{color:var(--text3);}

  /* ---------- Flujo de un requerimiento ---------- */
  details.tk{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
       padding:.7rem .9rem;margin-bottom:10px;}
  details.tk[open]{border-left:3px solid var(--orange);}
  details.tk summary{cursor:pointer;list-style:none;}
  details.tk summary::-webkit-details-marker{display:none;}
  details.tk .feathead{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
  details.tk .featslug{font-family:var(--mono);font-size:14px;font-weight:700;margin-right:auto;}
  .flow{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:.7rem 0 .8rem;}
  .hop{font-size:12px;font-weight:600;padding:3px 11px;border-radius:20px;
       background:var(--surface2);border:1px solid var(--border2);color:var(--text2);}
  .hop.open{color:#fff;background:var(--orange);border-color:var(--orange);}
  .hoparrow{color:var(--text3);}
  .kind{font-size:10.5px;font-family:var(--mono);font-weight:600;text-transform:uppercase;
       letter-spacing:.3px;color:var(--text3);}
  .k-handoff{color:var(--orange);} .k-estado{color:var(--info);}
  .k-consulta{color:var(--ok);} .k-sesion{color:var(--text3);}
  .evdetail{font-size:12px;color:var(--text3);}

  /* ---------- Grafo ---------- */
  .graphwrap{position:relative;}
  svg.graph{width:100%;height:auto;display:block;}
  .gnode{fill:var(--surface2);stroke:var(--orange);stroke-width:2;}
  .nodelbl{fill:var(--text);font:600 12.5px var(--font);}
  .nodecnt{fill:var(--text3);font:700 12px var(--mono);}
  .edge{fill:none;stroke:currentColor;opacity:.55;transition:opacity .12s;}
  .edge.cap{color:var(--orange);}
  .edge.int{color:var(--text3);}
  .gnode-g{cursor:pointer;transition:opacity .12s;}
  .gnode-g:focus{outline:none;}
  .gnode-g:focus .gnode,.gnode-g:hover .gnode{fill:var(--orange);stroke:var(--orange);}
  .gnode-g:focus .nodecnt,.gnode-g:hover .nodecnt{fill:#fff;}
  .graph.focused .edge{opacity:.06;}
  .graph.focused .edge.on{opacity:1;}
  .graph.focused .gnode-g{opacity:.25;}
  .graph.focused .gnode-g.on{opacity:1;}
  .glegend{display:flex;gap:1rem;flex-wrap:wrap;align-items:center;font-size:11.5px;
       color:var(--text3);border-top:1px solid var(--border);margin-top:.5rem;padding-top:.6rem;}
  .glegend .sw{display:inline-block;width:18px;height:3px;border-radius:2px;
       vertical-align:middle;margin-right:4px;}
  .glegend .sw.cap{background:var(--orange);} .glegend .sw.int{background:var(--text3);}
  .glegend .ghint{margin-left:auto;font-style:italic;}
  .gcaption{min-height:1.3em;font-size:12.5px;font-weight:600;color:var(--orange);
       font-family:var(--mono);margin-top:.3rem;}

  /* ---------- Capacidades ---------- */
  .cap{font-family:var(--mono);font-size:12px;color:var(--info);}
  .cat{font-size:11px;color:var(--text3);}
  .capgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px;}
  .capcard{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
       padding:.7rem .9rem;}
  .capproj{font-size:14px;font-weight:700;margin-bottom:.45rem;
       border-left:3px solid var(--orange);padding-left:8px;}
  .caplane{margin-bottom:.45rem;}
  .lanehead{font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.3px;}
  .lanehead.prov{color:var(--ok);} .lanehead.cons{color:var(--orange);}
  .lanehead .cnt{color:var(--text3);font-weight:400;}
  .caplane ul{margin:.1rem 0 0;padding-left:1rem;}
  .caplane li{margin:1px 0;}

  /* ---------- Features coordinadas ---------- */
  .feat{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
       padding:.7rem .9rem;margin-bottom:10px;}
  .feathead{display:flex;align-items:center;gap:8px;margin-bottom:4px;}
  .featslug{font-family:var(--mono);font-size:13px;font-weight:600;}
  .featdesc{font-size:13px;color:var(--text2);margin-bottom:8px;}
  .noresult{font-size:13px;color:var(--text3);padding:.6rem 0;}
</style>
</head>
<body>
<header class="topbar">
  <div class="logo">
    <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true">
      <path d="M22 6.5 A11 11 0 1 0 22 19.5 L17.5 16.8 A6 6 0 1 1 17.5 9.2 Z" fill="#F5821F"/>
      <circle cx="20.5" cy="13" r="2.6" fill="#F5821F"/>
    </svg>
    <span class="logo-text">RedCapital</span>
    <span class="sep"></span>
    <span class="appname">Nexus</span>
  </div>
  <div class="topright">Trazabilidad multi-proyecto<br>__SNAPSHOT__ &middot; hub.db (solo lectura)</div>
</header>

<div class="wrap">
  <div class="metrics">__METRICS__</div>

  <nav class="tabs" role="tablist">
    <button role="tab" data-tab="todo" aria-selected="true">
      <i class="ti ti-checkbox" aria-hidden="true"></i> Pendientes
      <span class="badge">__PENDCOUNT__</span></button>
    <button role="tab" data-tab="req" aria-selected="false">
      <i class="ti ti-timeline" aria-hidden="true"></i> Requerimientos
      <span class="badge">__REQCOUNT__</span></button>
    <button role="tab" data-tab="mapa" aria-selected="false">
      <i class="ti ti-share" aria-hidden="true"></i> Mapa de proyectos</button>
    <button role="tab" data-tab="act" aria-selected="false">
      <i class="ti ti-arrows-exchange" aria-hidden="true"></i> Actividad</button>
  </nav>

  <div class="filters">
    <input type="search" id="fq" placeholder="Buscar requerimiento, proyecto, texto...">
    <label>Proyecto
      <select id="fproj"><option value="">todos</option>__PROJOPTS__</select>
    </label>
    <label><input type="checkbox" id="fopen"> solo abiertos</label>
    <span class="fcount" id="fcount"></span>
  </div>

  <section class="pane" id="p-todo">
    <h2>Pendientes</h2>
    <p class="hint">Handoffs en <code>pending</code> y estados <code>[PEND]</code>: el trabajo
      que quedo esperando a que alguien lo tome.</p>
    <div class="card">__TODO__</div>
  </section>

  <section class="pane" id="p-req" hidden>
    <h2>Requerimientos y su recorrido</h2>
    <p class="hint">Agrupados por el <code>RC-xxxx</code> que aparece en el handoff, el
      <code>set_state</code>, la consulta o la rama. El chip naranjo del flujo es el salto que
      nadie tomo todavia.</p>
    __TICKETS__
  </section>

  <section class="pane" id="p-mapa" hidden>
    <h2>Grafo de dependencias e interacciones</h2>
    <div class="card">__GRAPH__</div>
    <h2>Ruteo resuelto</h2>
    <div class="card">__ROUTES__</div>
    <h2>Capacidades por proyecto</h2>
    __CAPS__
  </section>

  <section class="pane" id="p-act" hidden>
    <h2>Interacciones recientes</h2>
    <div class="card">__INTERACTIONS__</div>
    <h2>Features coordinadas</h2>
    <div class="card">__FEATURES__</div>
  </section>
</div>

<script>
(function(){
  var tabs=[].slice.call(document.querySelectorAll('nav.tabs button'));
  var q=document.getElementById('fq'), proj=document.getElementById('fproj'),
      open=document.getElementById('fopen'), count=document.getElementById('fcount');
  var current='todo';

  function pane(){ return document.getElementById('p-'+current); }

  function show(tab){
    current=tab;
    tabs.forEach(function(b){ b.setAttribute('aria-selected', b.dataset.tab===tab); });
    document.querySelectorAll('.pane').forEach(function(p){ p.hidden = p.id!=='p-'+tab; });
    open.parentNode.hidden = (tab!=='req');
    location.hash = tab;
    apply();
  }

  function apply(){
    var text=q.value.trim().toLowerCase(), p=proj.value, o=open.checked && current==='req';
    var items=pane().querySelectorAll('[data-projects]');
    var shown=0;
    items.forEach(function(el){
      var ps=(el.dataset.projects||'').split(',');
      var hide = (p && ps.indexOf(p)<0)
              || (o && el.dataset.status!=='abierto')
              || (text && el.textContent.toLowerCase().indexOf(text)<0);
      el.hidden = hide;
      if(!hide){ shown++; }
    });
    count.textContent = items.length ? shown+' de '+items.length : '';
    pane().querySelectorAll('.noresult').forEach(function(n){ n.remove(); });
    if(items.length && !shown){
      var d=document.createElement('p');
      d.className='noresult'; d.textContent='Nada coincide con el filtro.';
      pane().appendChild(d);
    }
    try{ localStorage.setItem('nexusFilter',JSON.stringify({q:q.value,p:p,o:open.checked})); }catch(e){}
  }

  tabs.forEach(function(b){ b.addEventListener('click',function(){ show(b.dataset.tab); }); });
  [q,proj,open].forEach(function(el){ el.addEventListener('input',apply); });

  try{
    var s=JSON.parse(localStorage.getItem('nexusFilter')||'{}');
    if(s.q){ q.value=s.q; } if(s.p){ proj.value=s.p; } if(s.o){ open.checked=true; }
  }catch(e){}
  show((location.hash||'#todo').slice(1));
})();
(function(){
  var svg=document.querySelector('svg.graph'); if(!svg){ return; }
  var cap=document.getElementById('gcap');
  var edges=[].slice.call(svg.querySelectorAll('.edge'));
  var nodes=[].slice.call(svg.querySelectorAll('.gnode-g'));
  function focus(name){
    svg.classList.toggle('focused', !!name);
    edges.forEach(function(e){
      e.classList.toggle('on', !!name && (e.dataset.src===name || e.dataset.dst===name));
    });
    nodes.forEach(function(nd){
      var me=nd.dataset.name;
      nd.classList.toggle('on', me===name || edges.some(function(e){
        return e.classList.contains('on') && (e.dataset.src===me || e.dataset.dst===me);
      }));
    });
  }
  function bind(el, name){
    el.addEventListener('mouseenter', function(){ focus(name); cap.textContent=el.dataset.info; });
    el.addEventListener('focus', function(){ focus(name); cap.textContent=el.dataset.info; });
    ['mouseleave','blur'].forEach(function(ev){
      el.addEventListener(ev, function(){ focus(''); cap.textContent=''; });
    });
  }
  nodes.forEach(function(nd){
    bind(nd, nd.dataset.name);
    nd.addEventListener('click', function(){
      var p=document.getElementById('fproj');
      p.value=nd.dataset.name; p.dispatchEvent(new Event('input'));
    });
  });
  edges.forEach(function(e){ bind(e, e.dataset.src); });
})();
(function(){
  var v=null;
  setInterval(function(){
    fetch('/version',{cache:'no-store'}).then(function(r){return r.text();}).then(function(t){
      if(v===null){v=t;} else if(t!==v){location.reload();}
    }).catch(function(){});
  },3000);
})();
</script>
</body>
</html>
"""


def render_html():
    data = build_data()
    opts = "".join(f'<option value="{esc(p)}">{esc(p)}</option>' for p in data["filter_projects"])
    return (HTML_TEMPLATE
            .replace("__SNAPSHOT__", datetime.now().strftime("%Y-%m-%d %H:%M"))
            .replace("__METRICS__", render_metrics(data["metrics"]))
            .replace("__PROJOPTS__", opts)
            .replace("__PENDCOUNT__", str(data["metrics"]["pendientes"]))
            .replace("__REQCOUNT__", str(len(data["tickets"])))
            .replace("__TODO__", nxtickets.render_todo(data["tickets"], data["huerfanos"]))
            .replace("__TICKETS__", nxtickets.render_tickets(data["tickets"]))
            .replace("__GRAPH__", render_graph(data["nodes"], data["edges"]))
            .replace("__ROUTES__", render_routes(data["routes"]))
            .replace("__CAPS__", render_caps(data["by_project"]))
            .replace("__FEATURES__", render_features(data["features"]))
            .replace("__INTERACTIONS__", render_interactions(data["interactions"])))


# --------------------------------------------------------------------------
# Servidor
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype="text/html; charset=utf-8", status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.path = self.path.split("?", 1)[0]
        if self.path.startswith("/version"):
            self._send(db_version(), "text/plain; charset=utf-8")
        elif self.path in ("/", "/index.html"):
            try:
                self._send(render_html())
            except Exception as exc:  # pragma: no cover - defensivo
                self._send(f"<pre>Error generando el panel:\n{esc(exc)}</pre>", status=500)
        else:
            self._send("<h1>404</h1>", status=404)

    def log_message(self, *args):
        pass  # silencioso


def serve(port):
    if not DB_PATH.exists():
        sys.exit(f"No se encontro la BD del hub: {DB_PATH}")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"Nexus - Monitoreo sirviendo en {url}  (Ctrl+C para detener)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido.")
        httpd.shutdown()


def main():
    ap = argparse.ArgumentParser(description="Dashboard de monitoreo de Nexus.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"puerto (def. {DEFAULT_PORT})")
    ap.add_argument("--once", action="store_true", help="imprime el HTML una vez y sale")
    args = ap.parse_args()
    if args.once:
        if not DB_PATH.exists():
            sys.exit(f"No se encontro la BD del hub: {DB_PATH}")
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(render_html())
    else:
        serve(args.port)


if __name__ == "__main__":
    main()
