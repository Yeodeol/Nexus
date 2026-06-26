#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Grafo vivo del cockpit (Fase C). Autónomo y genérico: lee hub.db (solo lectura),
arma el grafo de dependencias/interacciones entre proyectos y devuelve una página
HTML que se anima en vivo (los nodos y aristas "se encienden" cuando llega una
interacción nueva, por polling a /api/interactions).

Es independiente del panel personal: el cockpit no depende de panel_proyectos.py.
(En un refactor futuro convendría extraer una librería común con dashboard.py.)
"""
import html
import math
import os
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path.home() / ".claude-projects-hub" / "hub.db"


def _e(s):
    return html.escape(str(s if s is not None else ""))


def _con():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def build_orq():
    """Nodos y aristas del grafo a partir de capabilities + interactions."""
    con = _con()
    caps = [dict(r) for r in con.execute(
        "SELECT project, kind, name FROM capabilities ORDER BY project, kind, name")]
    interactions = [dict(r) for r in con.execute(
        "SELECT id, from_project, to_project FROM interactions ORDER BY id DESC")]
    con.close()

    providers = {}
    for c in caps:
        if c["kind"] == "provides":
            providers.setdefault(c["name"], []).append(c["project"])
    edge_map = {}
    for c in caps:
        if c["kind"] == "consumes":
            for prov in providers.get(c["name"], []):
                key = (c["project"], prov)
                e = edge_map.setdefault(key, {"src": c["project"], "dst": prov, "caps": 0, "ints": 0})
                e["caps"] += 1
    for it in interactions:
        key = (it["from_project"], it["to_project"])
        e = edge_map.setdefault(key, {"src": it["from_project"], "dst": it["to_project"], "caps": 0, "ints": 0})
        e["ints"] += 1
    edges = list(edge_map.values())
    nodes = sorted({n for e in edges for n in (e["src"], e["dst"])})
    last_id = max([i["id"] for i in interactions], default=0)
    return {"nodes": nodes, "edges": edges, "interactions": interactions, "last_id": last_id}


def interactions_since(since_id=0):
    """Interacciones con id > since_id (para el polling en vivo)."""
    con = _con()
    out = con.execute(
        "SELECT id, from_project, to_project, intent, capability, outcome, created_at "
        "FROM interactions WHERE id > ? ORDER BY id ASC", (since_id,)).fetchall()
    con.close()
    return [dict(r) for r in out]


def _render_svg(nodes, edges):
    if not nodes:
        return ('<p class="empty">Aun no hay relaciones entre proyectos. El grafo se '
                'dibuja a partir de las capacidades e interacciones del hub.</p>')
    W, H = 520, 460
    cx, cy = W / 2, H / 2
    R = min(W, H) / 2 - 70
    n = len(nodes)
    pos = {}
    for i, name in enumerate(nodes):
        if n == 1:
            pos[name] = (cx, cy)
        else:
            ang = (2 * math.pi * i / n) - math.pi / 2
            pos[name] = (cx + R * math.cos(ang), cy + R * math.sin(ang))
    parts = [f'<svg viewBox="0 0 {W} {H}" class="graph" xmlns="http://www.w3.org/2000/svg" '
             f'role="img" aria-label="Grafo de dependencias e interacciones">']
    parts.append('<defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
                 'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
                 'fill="var(--text3)"/></marker></defs>')
    for e in edges:
        x1, y1 = pos[e["src"]]
        x2, y2 = pos[e["dst"]]
        dx, dy = x2 - x1, y2 - y1
        d = math.hypot(dx, dy) or 1.0
        ux, uy = dx / d, dy / d
        sx, sy = x1 + ux * 22, y1 + uy * 22
        ex, ey = x2 - ux * 22, y2 - uy * 22
        parts.append(f'<line data-edge="{_e(e["src"])}|{_e(e["dst"])}" x1="{sx:.1f}" y1="{sy:.1f}" '
                     f'x2="{ex:.1f}" y2="{ey:.1f}" class="edge" marker-end="url(#ar)"/>')
    for name, (x, y) in pos.items():
        parts.append(f'<circle data-node="{_e(name)}" cx="{x:.1f}" cy="{y:.1f}" r="20" class="gnode"/>')
        parts.append(f'<text x="{x:.1f}" y="{y + 34:.1f}" text-anchor="middle" class="nlbl">{_e(name)}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


GRAPH_HTML = r"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grafo</title>
<style>
  :root{ --bg:transparent; --text:#2C2C2A; --text2:#5F5E5A; --text3:#888780;
    --border2:rgba(0,0,0,.22); --surface2:#F1EFE8; --on:#1D9E75; --acc:#0C447C;
    --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  @media (prefers-color-scheme: dark){ :root{ --text:#F1EFE8; --text2:#B4B2A9; --text3:#888780;
    --border2:rgba(255,255,255,.28); --surface2:#3A3A38; --on:#4ECCA8; --acc:#7FB2EC; } }
  *{box-sizing:border-box;} body{margin:0;background:var(--bg);color:var(--text);font-family:var(--font);}
  .hd{font-size:12px;color:var(--text3);padding:.5rem .75rem;display:flex;gap:10px;align-items:center;}
  .hd b{color:var(--text);font-weight:500;}
  svg.graph{width:100%;height:auto;display:block;}
  .edge{stroke:var(--border2);stroke-width:1.5;transition:stroke .3s,stroke-width .3s;}
  .gnode{fill:var(--surface2);stroke:var(--border2);stroke-width:1.5;transition:fill .35s,stroke .35s;}
  .nlbl{fill:var(--text2);font:500 11px var(--font);}
  .empty{font-size:13px;color:var(--text2);padding:1rem;}
  .feed{font-size:12px;font-family:var(--mono);color:var(--text3);padding:.25rem .75rem;max-height:96px;overflow:auto;}
  .feed div{padding:2px 0;}
</style></head>
<body>
<div class="hd"><span>interacciones <b id="ic">__LASTCOUNT__</b></span><span id="snap">__SNAP__</span></div>
<div id="wrap">__SVG__</div>
<div class="feed" id="feed"></div>
<script>
(function(){
  var NSV='http://www.w3.org/2000/svg', lastId=__LASTID__;
  function svg(){return document.querySelector('svg.graph');}
  function circ(id){return document.querySelector('[data-node="'+id+'"]');}
  function pos(id){var c=circ(id);return c?[parseFloat(c.getAttribute('cx')),parseFloat(c.getAttribute('cy'))]:null;}
  function esc(s){var d=document.createElement('div');d.textContent=(s==null?'':String(s));return d.innerHTML;}
  function ripple(p){var s=svg();if(!s||!p)return;var r=document.createElementNS(NSV,'circle');
    r.setAttribute('cx',p[0]);r.setAttribute('cy',p[1]);r.setAttribute('r','20');r.style.fill='none';
    r.style.stroke='var(--on)';r.style.strokeWidth='2';s.appendChild(r);var t0=null;
    function st(ts){if(t0===null)t0=ts;var k=Math.min(1,(ts-t0)/700);r.setAttribute('r',20+k*24);
      r.style.opacity=String(0.85*(1-k));if(k<1)requestAnimationFrame(st);else r.remove();}requestAnimationFrame(st);}
  function particle(a,b){var s=svg();if(!s||!a||!b)return;var p=document.createElementNS(NSV,'circle');
    p.setAttribute('r','5');p.style.fill='var(--on)';s.appendChild(p);var t0=null;
    function st(ts){if(t0===null)t0=ts;var k=Math.min(1,(ts-t0)/850);p.setAttribute('cx',a[0]+(b[0]-a[0])*k);
      p.setAttribute('cy',a[1]+(b[1]-a[1])*k);if(k<1)requestAnimationFrame(st);else p.remove();}requestAnimationFrame(st);}
  function lightNode(id){var c=circ(id);if(!c)return;c.style.fill='var(--on)';c.style.stroke='var(--on)';ripple(pos(id));
    setTimeout(function(){c.style.fill='';c.style.stroke='';},1700);}
  function lightEdge(a,b){var e=document.querySelector('[data-edge="'+a+'|'+b+'"]')||document.querySelector('[data-edge="'+b+'|'+a+'"]');
    if(!e)return;e.style.stroke='var(--acc)';e.style.strokeWidth='4';
    setTimeout(function(){e.style.stroke='';e.style.strokeWidth='';},1600);}
  function feedRow(it){var f=document.getElementById('feed');var d=document.createElement('div');
    d.textContent=it.from_project+' → '+it.to_project+(it.capability?(' · '+it.capability):'');
    f.insertBefore(d,f.firstChild);while(f.children.length>6)f.removeChild(f.lastChild);}
  function bump(){var el=document.getElementById('ic');if(el)el.textContent=String((parseInt(el.textContent,10)||0)+1);}
  function fire(it){lightEdge(it.from_project,it.to_project);lightNode(it.from_project);lightNode(it.to_project);
    particle(pos(it.from_project),pos(it.to_project));feedRow(it);bump();}
  function seq(list,i){if(i>=list.length)return;fire(list[i]);setTimeout(function(){seq(list,i+1);},650);}
  function cycle(){fetch('/api/interactions?since='+lastId,{cache:'no-store'}).then(function(r){return r.json();})
    .then(function(rows){if(rows&&rows.length){lastId=rows[rows.length-1].id;seq(rows,0);}}).catch(function(){});}
  setInterval(cycle,2500);
})();
</script>
</body></html>
"""


def render_graph_page():
    data = build_orq()
    return (GRAPH_HTML
            .replace("__SVG__", _render_svg(data["nodes"], data["edges"]))
            .replace("__LASTID__", str(data["last_id"]))
            .replace("__LASTCOUNT__", str(len(data["interactions"])))
            .replace("__SNAP__", datetime.now().strftime("%H:%M")))
