#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests de las tools de busqueda progresiva y timeline (fase 3 de memoria pasiva).

Correr (requiere el paquete `mcp`, usar el venv del MCP desplegado o uno propio):
  C:/Users/Administrador/mcp-servers/nexus-hub/.venv/Scripts/python.exe -m unittest nexus-hub.test_nexus_tools
  (o desde nexus-hub/:  <python-con-mcp> -m unittest test_nexus_tools -v)

Usa una hub.db temporal (parcheando DB_PATH): no toca la base real.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402


def seed(conn):
    """Tablas de projects-hub (en la vida real las crea ese MCP) + datos de prueba."""
    conn.execute("CREATE TABLE IF NOT EXISTS projects (name TEXT PRIMARY KEY, path TEXT, "
                 "description TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS state (project TEXT NOT NULL, key TEXT NOT NULL, "
                 "value TEXT NOT NULL, updated_at TEXT, PRIMARY KEY (project, key))")
    conn.execute("CREATE TABLE IF NOT EXISTS handoffs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "from_project TEXT, to_project TEXT, stage TEXT, payload TEXT, "
                 "status TEXT DEFAULT 'pending', created_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS interactions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "from_project TEXT, to_project TEXT, intent TEXT, capability TEXT, "
                 "outcome TEXT, feature TEXT, created_at TEXT)")

    conn.execute("INSERT INTO projects VALUES ('alpha', 'C:/repos/alpha', "
                 "'API de scoring crediticio')")
    conn.execute("INSERT INTO projects VALUES ('beta', 'C:/repos/beta', 'ETL de respaldos')")
    conn.execute("INSERT INTO state VALUES ('alpha', 'rama', '[PEND] validar scoring en stage', "
                 "'2026-07-01T10:00:00')")
    conn.execute("INSERT INTO handoffs (from_project, to_project, stage, payload, status, "
                 "created_at) VALUES ('alpha', 'beta', 'contrato', "
                 "'endpoint scoring listo para consumir', 'pending', '2026-07-02T10:00:00')")
    conn.execute("INSERT INTO interactions (from_project, to_project, intent, outcome, "
                 "created_at) VALUES ('beta', 'alpha', 'get_project_context', 'consulted', "
                 "'2026-07-03T10:00:00')")
    conn.execute("INSERT INTO knowledge (project, topic, content, updated_at) VALUES "
                 "('alpha', 'endpoints', 'POST /scoring recibe rut y devuelve score entre "
                 "0 y 1000. Implementado en api/scoring.py', '2026-07-01T09:00:00')")
    conn.execute("INSERT INTO checkpoints (project, summary, created_at) VALUES "
                 "('alpha', 'borrador: migrar scoring a lambda', '2026-07-04T10:00:00')")
    conn.execute("INSERT INTO messages (from_project, to_project, text, status, kind, "
                 "created_at) VALUES ('beta', 'alpha', 'que devuelve el scoring?', 'read', "
                 "'question', '2026-07-05T10:00:00')")
    conn.execute("INSERT INTO observations (project, session_id, branch, first_prompt, "
                 "files_touched, stats, summary, status, created_at) VALUES "
                 "('alpha', 'sess-1', 'feature/scoring-v2', 'mejora el scoring', "
                 "'[\"api/scoring.py\"]', '{\"user_msgs\": 3}', "
                 "'Se ajusto el modelo de scoring y quedo pendiente el deploy.', "
                 "'summarized', '2026-07-05T12:00:00')")
    conn.commit()


class NexusToolsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(server, "DB_PATH",
                                        Path(self.tmp.name) / "hub.db")
        self._patch.start()
        with server.db() as conn:  # crea las tablas de nexus-hub
            seed(conn)

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    # -- nexus_search: indice compacto con refs ------------------------------
    def test_search_devuelve_indice_con_refs(self):
        out = json.loads(server.nexus_search("scoring"))
        refs = {h["ref"] for h in out["hits"]}
        self.assertIn("knowledge#1", refs)
        self.assertIn("observation#1", refs)
        self.assertIn("handoff#1", refs)
        self.assertIn("project#alpha", refs)
        self.assertIn("uso", out)  # instruye la capa 2 (nexus_get)
        for h in out["hits"]:      # snippets cortos: es un INDICE, no el contenido
            self.assertLess(len(h["snippet"]), 130, h)

    def test_search_filtro_project(self):
        out = json.loads(server.nexus_search("scoring", project="beta"))
        # el handoff alpha->beta matchea por destino; lo de solo-alpha queda fuera
        refs = {h["ref"] for h in out["hits"]}
        self.assertIn("handoff#1", refs)
        self.assertNotIn("knowledge#1", refs)

    def test_search_filtro_since(self):
        out = json.loads(server.nexus_search("scoring", since="2026-07-05T00:00:00"))
        refs = {h["ref"] for h in out["hits"]}
        self.assertIn("observation#1", refs)     # 05/07
        self.assertNotIn("knowledge#1", refs)    # 01/07
        self.assertNotIn("project#alpha", refs)  # sin fecha: fuera cuando hay since

    def test_search_sin_resultados(self):
        self.assertIn("Sin resultados", server.nexus_search("inexistente-xyz"))

    # -- nexus_get: capa 2 ------------------------------------------------------
    def test_get_trae_contenido_completo_de_varios_refs(self):
        out = json.loads(server.nexus_get("knowledge#1, state#alpha/rama project#alpha"))
        by_ref = {o["ref"]: o for o in out}
        self.assertIn("POST /scoring", by_ref["knowledge#1"]["data"]["content"])
        self.assertIn("[PEND]", by_ref["state#alpha/rama"]["data"]["value"])
        self.assertEqual(by_ref["project#alpha"]["data"]["path"], "C:/repos/alpha")

    def test_get_observation_expande_json(self):
        out = json.loads(server.nexus_get("observation#1"))
        data = out[0]["data"]
        self.assertEqual(data["files_touched"], ["api/scoring.py"])
        self.assertEqual(data["stats"], {"user_msgs": 3})

    def test_get_refs_invalidos_reportan_error(self):
        out = json.loads(server.nexus_get("knowledge#999, rara#1, sinid"))
        self.assertTrue(all("error" in o for o in out))

    def test_get_vacio(self):
        self.assertIn("refs vacio", server.nexus_get("  "))

    # -- nexus_timeline -----------------------------------------------------------
    def test_timeline_ordena_desc_y_trae_refs(self):
        out = json.loads(server.nexus_timeline(days=3650))
        fechas = [e["fecha"] for e in out["eventos"]]
        self.assertEqual(fechas, sorted(fechas, reverse=True))
        tipos = {e["tipo"] for e in out["eventos"]}
        self.assertIn("sesion", tipos)
        self.assertIn("checkpoint", tipos)
        self.assertIn("interaccion", tipos)
        ses = next(e for e in out["eventos"] if e["tipo"] == "sesion")
        self.assertEqual(ses["ref"], "observation#1")
        self.assertIn("feature/scoring-v2", ses["detalle"])

    def test_timeline_filtra_por_proyecto(self):
        out = json.loads(server.nexus_timeline(project="beta", days=3650))
        for e in out["eventos"]:
            self.assertIn("beta", e["project"])

    def test_timeline_respeta_days(self):
        self.assertIn("Sin eventos", server.nexus_timeline(days=1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
