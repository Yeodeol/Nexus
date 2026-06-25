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

    return {
        "projects": projects,
        "by_project": by_project,
        "routes": routes,
        "edges": edges,
        "nodes": sorted(nodes),
        "interactions": interactions,
        "features": features,
        "metrics": {
            "projects": len(projects),
            "capabilities": len(caps),
            "interactions": len(interactions),
            "features": len(features),
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
        ("Interacciones", m["interactions"], ""),
        ("Features coord.", m["features"], ""),
    ]
    out = []
    for label, val, color in cells:
        style = f' style="color:{color}"' if color else ""
        out.append(f'<div class="metric"><div class="lbl">{esc(label)}</div>'
                   f'<div class="val"{style}>{val}</div></div>')
    return "".join(out)


def render_graph(nodes, edges):
    """SVG con layout circular: nodos = proyectos, aristas = dependencias/interacciones."""
    if not nodes:
        return ('<p class="empty">Aun no hay relaciones entre proyectos. Declara capacidades '
                '(provides/consumes) o registra interacciones para ver el grafo.</p>')
    W, H = 720, 440
    cx, cy = W / 2, H / 2
    R = min(W, H) / 2 - 90
    n = len(nodes)
    pos = {}
    for i, name in enumerate(nodes):
        if n == 1:
            pos[name] = (cx, cy)
        else:
            ang = (2 * math.pi * i / n) - math.pi / 2
            pos[name] = (cx + R * math.cos(ang), cy + R * math.sin(ang))

    parts = [f'<svg viewBox="0 0 {W} {H}" class="graph" xmlns="http://www.w3.org/2000/svg" '
             f'role="img" aria-label="Grafo de dependencias entre proyectos">']
    parts.append('<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
                 'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                 '<path d="M0,0 L10,5 L0,10 z" fill="var(--text2)"/></marker></defs>')

    for e in edges:
        x1, y1 = pos[e["src"]]
        x2, y2 = pos[e["dst"]]
        dx, dy = x2 - x1, y2 - y1
        dist = math.hypot(dx, dy) or 1.0
        ux, uy = dx / dist, dy / dist
        r = 24
        sx, sy = x1 + ux * r, y1 + uy * r
        ex, ey = x2 - ux * r, y2 - uy * r
        mx, my = (sx + ex) / 2, (sy + ey) / 2
        weight = 1.2 + (e["caps"] + e["ints"]) * 0.6
        bits = []
        if e["caps"]:
            bits.append(f'{e["caps"]} cap.')
        if e["ints"]:
            bits.append(f'{e["ints"]} int.')
        label = " - ".join(bits)
        parts.append(f'<line x1="{sx:.1f}" y1="{sy:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" '
                     f'stroke="var(--border2)" stroke-width="{weight:.1f}" marker-end="url(#arrow)"/>')
        if label:
            parts.append(f'<text x="{mx:.1f}" y="{my - 5:.1f}" class="edgelbl" '
                         f'text-anchor="middle">{esc(label)}</text>')

    for name, (x, y) in pos.items():
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="22" class="gnode"/>')
        parts.append(f'<text x="{x:.1f}" y="{y + 38:.1f}" class="nodelbl" '
                     f'text-anchor="middle">{esc(name)}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def render_routes(routes):
    if not routes:
        return '<p class="empty">No hay dependencias declaradas todavia.</p>'
    trs = []
    for r in routes:
        cat = f' <span class="cat">{esc(r["category"])}</span>' if r["category"] else ""
        trs.append("<tr>"
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
        cards.append(f"<div class='capcard'><div class='capproj'>{esc(project)}</div>"
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
            "<div class='feat'>"
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
        trs.append("<tr>"
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
<title>Nexus - Monitoreo</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.11.0/dist/tabler-icons.min.css">
<style>
  :root{
    --bg:#FAF9F5; --surface:#FFFFFF; --surface2:#F1EFE8;
    --text:#2C2C2A; --text2:#5F5E5A; --text3:#888780;
    --border:rgba(0,0,0,.12); --border2:rgba(0,0,0,.22);
    --prov:#0F6E56; --cons:#854F0B; --accent:#0C447C;
    --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    --radius-md:8px; --radius-lg:12px;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#262624; --surface:#30302E; --surface2:#3A3A38;
      --text:#F1EFE8; --text2:#B4B2A9; --text3:#888780;
      --border:rgba(255,255,255,.12); --border2:rgba(255,255,255,.28);
      --prov:#4ECCA8; --cons:#FAC775; --accent:#7FB2EC;
    }
  }
  *{box-sizing:border-box;}
  body{margin:0;background:var(--bg);color:var(--text);font-family:var(--font);line-height:1.6;}
  .wrap{max-width:880px;margin:0 auto;padding:2rem 1.25rem 3rem;}
  h1{font-size:22px;font-weight:500;margin:0 0 .15rem;display:flex;align-items:center;gap:.5rem;}
  .sub{font-size:13px;color:var(--text3);margin:0 0 1.5rem;}
  h2{font-size:15px;font-weight:600;margin:1.75rem 0 .75rem;display:flex;align-items:center;gap:.5rem;}
  h2 i{color:var(--text3);font-size:18px;}
  .metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:.5rem;}
  .metric{background:var(--surface2);border-radius:var(--radius-md);padding:.8rem 1rem;}
  .metric .lbl{font-size:12px;color:var(--text2);}
  .metric .val{font-size:26px;font-weight:500;}
  .card{background:var(--surface);border:.5px solid var(--border);border-radius:var(--radius-lg);
        padding:1rem 1.25rem;margin-bottom:.25rem;}
  .empty{font-size:13px;color:var(--text2);margin:.25rem 0;padding:.5rem 0;}
  .empty code,h2 code,.featdesc code{font-family:var(--mono);font-size:12px;background:var(--surface2);
        padding:1px 5px;border-radius:4px;}
  svg.graph{width:100%;height:auto;display:block;}
  .gnode{fill:var(--surface2);stroke:var(--border2);stroke-width:1;}
  .nodelbl{fill:var(--text);font:500 12px var(--font);}
  .edgelbl{fill:var(--text3);font:11px var(--mono);}
  table.tbl{width:100%;border-collapse:collapse;font-size:13px;}
  table.tbl th{text-align:left;font-weight:500;color:var(--text3);font-size:12px;
        border-bottom:.5px solid var(--border);padding:6px 8px;}
  table.tbl td{padding:6px 8px;border-bottom:.5px solid var(--border);vertical-align:top;}
  table.tbl tr:last-child td{border-bottom:none;}
  td.arrow{color:var(--text3);width:1%;white-space:nowrap;}
  .cap{font-family:var(--mono);font-size:12px;color:var(--accent);}
  .cat{font-size:11px;color:var(--text3);}
  .capgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px;}
  .capcard{background:var(--surface);border:.5px solid var(--border);border-radius:var(--radius-md);padding:.75rem .9rem;}
  .capproj{font-size:14px;font-weight:600;margin-bottom:.5rem;}
  .caplane{margin-bottom:.5rem;}
  .lanehead{font-size:12px;font-weight:500;margin-bottom:2px;}
  .lanehead.prov{color:var(--prov);} .lanehead.cons{color:var(--cons);}
  .lanehead .cnt{color:var(--text3);font-weight:400;}
  .caplane ul{margin:.1rem 0 0;padding-left:1rem;}
  .caplane li{margin:1px 0;}
  .feat{border:.5px solid var(--border);border-radius:var(--radius-md);padding:.75rem .9rem;margin-bottom:10px;}
  .feathead{display:flex;align-items:center;gap:8px;margin-bottom:4px;}
  .featslug{font-family:var(--mono);font-size:13px;font-weight:500;}
  .featdesc{font-size:13px;color:var(--text2);margin-bottom:8px;}
  .chip{font-size:11px;padding:2px 8px;border-radius:6px;border:.5px solid var(--border2);white-space:nowrap;}
  .s-planned{color:var(--text3);}
  .s-created,.s-committed,.s-open{color:var(--accent);border-color:var(--accent);}
  .s-pushed,.s-pr-open,.s-in-progress{color:var(--cons);border-color:var(--cons);}
  .s-merged{color:var(--prov);border-color:var(--prov);}
  .s-closed{color:var(--text3);}
  .ndate{color:var(--text3);font-family:var(--mono);font-size:12px;white-space:nowrap;}
  a{color:var(--accent);}
</style>
</head>
<body>
<div class="wrap">
  <h1><i class="ti ti-brain" aria-hidden="true"></i> Nexus &middot; Monitoreo</h1>
  <p class="sub">Actualizado: __SNAPSHOT__ &middot; fuente: hub.db (solo lectura)</p>

  <div class="metrics">__METRICS__</div>

  <h2><i class="ti ti-share" aria-hidden="true"></i> Grafo de dependencias e interacciones</h2>
  <div class="card">__GRAPH__</div>

  <h2><i class="ti ti-route" aria-hidden="true"></i> Ruteo resuelto</h2>
  <div class="card">__ROUTES__</div>

  <h2><i class="ti ti-plug-connected" aria-hidden="true"></i> Capacidades por proyecto</h2>
  __CAPS__

  <h2><i class="ti ti-git-branch" aria-hidden="true"></i> Features coordinadas</h2>
  <div class="card">__FEATURES__</div>

  <h2><i class="ti ti-arrows-exchange" aria-hidden="true"></i> Interacciones recientes</h2>
  <div class="card">__INTERACTIONS__</div>
</div>
<script>
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
    return (HTML_TEMPLATE
            .replace("__SNAPSHOT__", datetime.now().strftime("%Y-%m-%d %H:%M"))
            .replace("__METRICS__", render_metrics(data["metrics"]))
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
        sys.stdout.write(render_html())
    else:
        serve(args.port)


if __name__ == "__main__":
    main()
