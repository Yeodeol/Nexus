#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests del inyector de contexto en SessionStart (fase 4 de memoria pasiva).

Correr:  python -m unittest observer.test_session_context -v
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_context as sc  # noqa: E402
import session_observer as so  # noqa: E402


class SessionContextTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = so.db(self.root / "hub.db")  # crea observations
        self.conn.execute("CREATE TABLE projects (name TEXT PRIMARY KEY, path TEXT, "
                          "description TEXT)")
        self.conn.execute("CREATE TABLE handoffs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                          "from_project TEXT, to_project TEXT, stage TEXT, payload TEXT, "
                          "status TEXT DEFAULT 'pending', created_at TEXT)")
        self.conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                          "from_project TEXT, to_project TEXT, text TEXT, "
                          "status TEXT DEFAULT 'unread', kind TEXT, created_at TEXT)")
        self.conn.commit()
        self.cfg = dict(sc.INJECT_DEFAULTS, projects=[])

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add_obs(self, n, summary="resumen de la sesion", branch="main"):
        self.conn.execute(
            "INSERT INTO observations (project, session_id, branch, first_prompt, summary, "
            "status, created_at) VALUES ('p', ?, ?, 'prompt inicial', ?, 'summarized', ?)",
            (f"s{n}", branch, summary, f"2026-07-0{n}T10:00:00"))
        self.conn.commit()

    def test_contexto_con_sesiones_handoffs_y_mensajes(self):
        self.add_obs(1, "se arreglo el timeout del API")
        self.add_obs(2, "se agrego el endpoint de scoring")
        self.conn.execute("INSERT INTO handoffs (from_project, to_project, stage, status, "
                          "created_at) VALUES ('otro', 'p', 'contrato', 'pending', "
                          "'2026-07-03T10:00:00')")
        self.conn.execute("INSERT INTO messages (from_project, to_project, text, status) "
                          "VALUES ('otro', 'p', 'hola', 'unread')")
        self.conn.commit()
        ctx = sc.build_context(self.conn, self.cfg, "p")
        self.assertIn("[Nexus]", ctx)
        self.assertIn("se agrego el endpoint de scoring", ctx)
        self.assertIn("Handoffs pendientes (1): 'contrato' (de otro).", ctx)
        self.assertIn("Mensajes sin leer en el buzon: 1.", ctx)

    def test_orden_reciente_primero_y_tope_de_observaciones(self):
        for n in range(1, 6):
            self.add_obs(n, f"sesion numero {n}")
        cfg = dict(self.cfg, inject_observations=2)
        ctx = sc.build_context(self.conn, cfg, "p")
        self.assertIn("sesion numero 5", ctx)
        self.assertIn("sesion numero 4", ctx)
        self.assertNotIn("sesion numero 3", ctx)

    def test_usa_first_prompt_si_no_hay_resumen(self):
        self.conn.execute(
            "INSERT INTO observations (project, session_id, branch, first_prompt, summary, "
            "status, created_at) VALUES ('p', 's1', 'main', 'prompt sin resumir', '', "
            "'raw', '2026-07-01T10:00:00')")
        self.conn.commit()
        self.assertIn("prompt sin resumir", sc.build_context(self.conn, self.cfg, "p"))

    def test_respeta_presupuesto_de_chars(self):
        for n in range(1, 4):
            self.add_obs(n, "x" * 300)
        cfg = dict(self.cfg, inject_max_chars=400)
        ctx = sc.build_context(self.conn, cfg, "p")
        self.assertLessEqual(len(ctx), 400)
        self.assertTrue(ctx.endswith("..."))

    def test_sin_datos_devuelve_none(self):
        self.assertIsNone(sc.build_context(self.conn, self.cfg, "p"))

    def test_handoffs_resueltos_no_cuentan(self):
        self.conn.execute("INSERT INTO handoffs (from_project, to_project, stage, status) "
                          "VALUES ('otro', 'p', 'x', 'consumed')")
        self.conn.commit()
        self.assertIsNone(sc.build_context(self.conn, self.cfg, "p"))

    def test_main_salta_con_env_de_listener(self):
        with mock.patch.dict(sc.os.environ, {"NEXUS_LISTENER": "1"}):
            self.assertEqual(sc.main(["--cwd", str(self.root)]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
