#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trazabilidad por requerimiento para el dashboard de Nexus.

Agrupa lo que ya vive en hub.db (handoffs, state, interactions, observations)
por el ticket RC-xxxx que aparece en el texto, y arma por cada requerimiento su
cronologia, su flujo entre proyectos y sus pendientes.
"""
import html
import json
import re

TICKET = re.compile(r"RC[-_ ]?(\d{3,5})", re.I)
TAG = re.compile(r"^\s*\[(PEND|PENDIENTE|LISTO|OK|DONE|COMPLETADO|ANALISIS|INFO)\]\s*", re.I)
PEND_TAGS = {"PEND", "PENDIENTE"}


def find_ticket(*texts):
    """Primer RC encontrado, priorizando los textos en el orden dado."""
    for t in texts:
        if not t:
            continue
        m = TICKET.search(t)
        if m:
            return "RC-" + m.group(1)
    return ""


def split_tag(value):
    m = TAG.match(value or "")
    if not m:
        return "", value or ""
    return m.group(1).upper(), TAG.sub("", value, count=1)


def _objetivo(payload):
    try:
        d = json.loads(payload)
    except Exception:
        return (payload or "")[:120]
    for k in ("objetivo", "origen_requerimiento", "alcance"):
        if d.get(k):
            return str(d[k])[:200]
    return (payload or "")[:120]


def collect(con):
    """Devuelve (tickets, pendientes_sin_ticket)."""
    def rows(sql):
        return [dict(r) for r in con.execute(sql).fetchall()]

    events = []

    for h in rows("SELECT id, from_project, to_project, stage, payload, status, created_at "
                  "FROM handoffs ORDER BY id"):
        events.append({
            "ticket": find_ticket(h["stage"], h["payload"]),
            "when": h["created_at"] or "",
            "kind": "handoff",
            "project": h["from_project"],
            "to": h["to_project"],
            "title": h["stage"] or _objetivo(h["payload"]),
            "detail": _objetivo(h["payload"]),
            "open": h["status"] == "pending",
            "status": h["status"],
            "ref": "handoff#%s" % h["id"],
        })

    for s in rows("SELECT project, key, value, updated_at FROM state"):
        tag, body = split_tag(s["value"])
        events.append({
            "ticket": find_ticket(s["key"], s["value"]),
            "when": s["updated_at"] or "",
            "kind": "estado",
            "project": s["project"],
            "to": "",
            "title": s["key"],
            "detail": body[:300],
            "open": tag in PEND_TAGS,
            "status": tag.lower() or "nota",
            "ref": "state#%s/%s" % (s["project"], s["key"]),
        })

    for i in rows("SELECT id, from_project, to_project, intent, outcome, created_at FROM interactions"):
        events.append({
            "ticket": find_ticket(i["intent"]),
            "when": i["created_at"] or "",
            "kind": "consulta",
            "project": i["from_project"],
            "to": i["to_project"],
            "title": (i["intent"] or "")[:120],
            "detail": "",
            "open": False,
            "status": i["outcome"] or "",
            "ref": "interaction#%s" % i["id"],
        })

    for o in rows("SELECT id, project, branch, first_prompt, created_at FROM observations"):
        events.append({
            "ticket": find_ticket(o["branch"], o["first_prompt"]),
            "when": o["created_at"] or "",
            "kind": "sesion",
            "project": o["project"],
            "to": "",
            "title": o["branch"] or "(sin rama)",
            "detail": (o["first_prompt"] or "")[:200],
            "open": False,
            "status": "",
            "ref": "observation#%s" % o["id"],
        })

    tickets = {}
    huerfanos = []
    for e in events:
        if e["ticket"]:
            tickets.setdefault(e["ticket"], []).append(e)
        elif e["open"]:
            huerfanos.append(e)

    out = []
    for name, evs in tickets.items():
        evs.sort(key=lambda e: e["when"])
        pendientes = [e for e in evs if e["open"]]
        projects = []
        for e in evs:
            for p in (e["project"], e["to"]):
                if p and p not in projects:
                    projects.append(p)
        out.append({
            "ticket": name,
            "events": evs,
            "pendientes": pendientes,
            "projects": projects,
            "flow": _flow(evs),
            "status": "abierto" if pendientes else "listo",
            "last": evs[-1]["when"] if evs else "",
        })
    out.sort(key=lambda t: t["last"], reverse=True)
    huerfanos.sort(key=lambda e: e["when"], reverse=True)
    return out, huerfanos


def _flow(events):
    """Secuencia de saltos entre proyectos segun los handoffs del ticket."""
    hops = []
    for e in events:
        if e["kind"] != "handoff":
            continue
        if not hops:
            hops.append({"project": e["project"], "open": False})
        hops.append({"project": e["to"], "open": e["open"]})
    return hops


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------
def esc(s):
    return html.escape(str(s if s is not None else ""))


def _when(s):
    return (s or "")[:16].replace("T", " ")


def _flow_html(flow):
    if not flow:
        return "<div class='flow'><span class='empty'>sin handoffs</span></div>"
    bits = []
    for i, h in enumerate(flow):
        if i:
            bits.append("<span class='hoparrow'>&rarr;</span>")
        cls = "hop open" if h["open"] else "hop"
        bits.append("<span class='%s'>%s</span>" % (cls, esc(h["project"])))
    return "<div class='flow'>" + "".join(bits) + "</div>"


def _event_row(e):
    dest = " &rarr; " + esc(e["to"]) if e["to"] else ""
    chip = ("<span class='chip s-%s'>%s</span>" % (esc(e["status"] or "nota"), esc(e["status"]))
            if e["status"] else "")
    return ("<tr>"
            "<td class='ndate'>%s</td>"
            "<td><span class='kind k-%s'>%s</span></td>"
            "<td>%s%s</td>"
            "<td>%s<div class='evdetail'>%s</div></td>"
            "<td>%s</td></tr>"
            % (esc(_when(e["when"])), esc(e["kind"]), esc(e["kind"]),
               esc(e["project"]), dest, esc(e["title"]), esc(e["detail"]), chip))


def render_tickets(tickets):
    if not tickets:
        return ('<p class="empty">Sin requerimientos detectados. Se agrupan por el RC-xxxx que '
                'aparezca en el <code>stage</code>/payload del handoff, en la clave de '
                '<code>set_state</code> o en la rama de la sesion.</p>')
    out = []
    for t in tickets:
        rows = "".join(_event_row(e) for e in t["events"])
        pend = ("<span class='chip s-in-progress'>%d pendiente(s)</span>" % len(t["pendientes"])
                if t["pendientes"] else "<span class='chip s-merged'>sin pendientes</span>")
        out.append(
            "<details class='feat tk' data-projects='%s' data-status='%s'%s>"
            "<summary class='feathead'><span class='chip s-%s'>%s</span>"
            "<span class='featslug'>%s</span>%s"
            "<span class='ndate'>%s</span></summary>"
            "%s"
            "<table class='tbl'><thead><tr><th>Fecha</th><th>Tipo</th><th>Proyecto</th>"
            "<th>Que paso</th><th>Estado</th></tr></thead><tbody>%s</tbody></table>"
            "</details>"
            % (esc(",".join(t["projects"])), t["status"],
               " open" if t["status"] == "abierto" else "",
               t["status"], t["status"], esc(t["ticket"]), pend, esc(_when(t["last"])),
               _flow_html(t["flow"]), rows))
    return "".join(out)


def render_todo(tickets, huerfanos):
    items = []
    for t in tickets:
        for e in t["pendientes"]:
            items.append((t["ticket"], e))
    for e in huerfanos:
        items.append(("", e))
    if not items:
        return '<p class="empty">No hay pendientes: ningun handoff en pending ni estado [PEND].</p>'
    items.sort(key=lambda p: p[1]["when"], reverse=True)
    trs = []
    for tk, e in items:
        owner = e["to"] or e["project"]
        trs.append("<tr class='todo' data-projects='%s'>"
                   "<td>%s</td><td><strong>%s</strong></td><td><span class='kind k-%s'>%s</span></td>"
                   "<td>%s<div class='evdetail'>%s</div></td><td class='ndate'>%s</td></tr>"
                   % (esc(owner), esc(tk), esc(owner), esc(e["kind"]), esc(e["kind"]),
                      esc(e["title"]), esc(e["detail"]), esc(_when(e["when"]))))
    return ("<table class='tbl'><thead><tr><th>Req.</th><th>Le toca a</th><th>Tipo</th>"
            "<th>Que falta</th><th>Desde</th></tr></thead><tbody>" + "".join(trs) + "</tbody></table>")


def _selfcheck():
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
      CREATE TABLE handoffs(id INTEGER PRIMARY KEY, from_project TEXT, to_project TEXT,
        stage TEXT, payload TEXT, status TEXT, created_at TEXT);
      CREATE TABLE state(project TEXT, key TEXT, value TEXT, updated_at TEXT);
      CREATE TABLE interactions(id INTEGER PRIMARY KEY, from_project TEXT, to_project TEXT,
        intent TEXT, outcome TEXT, created_at TEXT);
      CREATE TABLE observations(id INTEGER PRIMARY KEY, project TEXT, branch TEXT,
        first_prompt TEXT, created_at TEXT);
      INSERT INTO handoffs VALUES
        (1,'checkempresa','respaldos-scraps','rc-3755-consulta','{"objetivo":"de donde sale X"}','consumed','2026-09-01T10:00'),
        (2,'respaldos-scraps','checkempresa','rc-3755-respuesta','{"objetivo":"sale de Y"}','consumed','2026-09-02T10:00'),
        (3,'checkempresa','agrotop','rc-3755-replicar','{"objetivo":"replicar"}','pending','2026-09-03T10:00'),
        (4,'crm','g-back','otra-cosa','{"objetivo":"sin ticket"}','pending','2026-09-04T10:00');
      INSERT INTO state VALUES ('checkempresa','rc_3755_landing','[LISTO] hecho','2026-09-03T11:00');
      INSERT INTO interactions VALUES (1,'checkempresa','respaldos-scraps','ask: RC-3755 duda','asked','2026-09-01T09:00');
      INSERT INTO observations VALUES (1,'checkempresa','feature/rc-3755-landing','armar la landing','2026-09-01T08:00');
    """)
    tickets, huerfanos = collect(con)
    assert [t["ticket"] for t in tickets] == ["RC-3755"], tickets
    t = tickets[0]
    assert t["status"] == "abierto"
    assert len(t["events"]) == 6, t["events"]
    assert [h["project"] for h in t["flow"]] == \
        ["checkempresa", "respaldos-scraps", "checkempresa", "agrotop"], t["flow"]
    assert t["flow"][-1]["open"] is True
    assert t["projects"] == ["checkempresa", "respaldos-scraps", "agrotop"], t["projects"]
    assert len(t["pendientes"]) == 1
    assert [e["ref"] for e in huerfanos] == ["handoff#4"], huerfanos
    assert split_tag("[PEND] algo") == ("PEND", "algo")
    assert split_tag("sin tag") == ("", "sin tag")
    assert find_ticket("", "rama feature/rc-4097-x") == "RC-4097"
    assert render_todo(tickets, huerfanos).count("<tr class") == 2
    print("trace selfcheck OK")


if __name__ == "__main__":
    _selfcheck()
