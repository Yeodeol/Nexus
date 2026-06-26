#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Widget del orquestador en vivo (Nexus).

Sirve SOLO el grafo de orquestacion en vivo, leyendo hub.db en modo lectura. No
tiene chat ni agente: el "cerebro" es Claude Code (donde haces las peticiones,
das permisos y consultas los repos). Mientras orquestas alli y se registran
interacciones (log_interaction), este widget las muestra encendiendose en vivo.

Sin dependencias externas (solo biblioteca estandar). Sin costo de API.

Uso:
    python widget_server.py            # http://localhost:8780 (abre el navegador)
    python widget_server.py 9000       # otro puerto
"""
import http.server
import socketserver
import sys
import threading
import webbrowser

import graph  # mismo directorio

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8780


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/interactions"):
            import json
            import urllib.parse as up
            try:
                since = int(up.parse_qs(up.urlparse(self.path).query).get("since", ["0"])[0])
            except (ValueError, TypeError):
                since = 0
            try:
                payload = json.dumps(graph.interactions_since(since), ensure_ascii=False)
            except Exception:
                payload = "[]"
            self._send(payload.encode("utf-8"), "application/json; charset=utf-8")
            return
        if self.path in ("/", "/index.html"):
            try:
                self._send(graph.render_graph_page().encode("utf-8"), "text/html; charset=utf-8")
            except Exception as exc:  # pragma: no cover
                self._send(("Error generando el widget: " + str(exc)).encode("utf-8"),
                           "text/plain; charset=utf-8")
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    if not graph.DB_PATH.exists():
        sys.exit(f"No se encontro la BD del hub: {graph.DB_PATH}")
    url = f"http://127.0.0.1:{PORT}/"
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    print(f"Widget del orquestador en {url}")
    print("El cerebro es Claude Code; orquesta alli y aqui veras las interacciones. Ctrl+C para detener.")
    try:
        with Server(("127.0.0.1", PORT), Handler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido.")
