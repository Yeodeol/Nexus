#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests del resumen de observaciones en idle (fase 2 de memoria pasiva).

Correr:  python -m unittest listener.test_observation_summary -v

Usa una hub.db temporal (parcheando DB_PATH) y mockea el subproceso `claude -p`:
no lanza agentes reales ni toca la base real.
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nexus_listener as nl  # noqa: E402


def insert_obs(conn, obs_id, project="miproyecto", status="raw",
               transcript="", created_at="2026-07-06T10:00:00"):
    conn.execute(
        "INSERT INTO observations (id, project, session_id, branch, first_prompt, "
        "files_touched, status, transcript_path, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (obs_id, project, f"sess-{obs_id}", "feature/x", "prompt",
         '["a.py"]', status, transcript, created_at))
    conn.commit()


def fake_claude_result(text):
    """Simula el stdout JSON de `claude -p --output-format json`."""
    proc = mock.Mock()
    proc.returncode = 0
    proc.stdout = json.dumps({"result": text})
    proc.stderr = ""
    return proc


class ObservationSummaryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._patch_db = mock.patch.object(nl, "DB_PATH", self.root / "hub.db")
        self._patch_db.start()
        self.conn = nl.db()  # crea el esquema defensivo en la DB temporal
        self.cfg = dict(nl.DEFAULTS)

    def tearDown(self):
        self.conn.close()
        self._patch_db.stop()
        self.tmp.cleanup()

    def write_transcript(self, entries):
        p = self.root / "t.jsonl"
        with open(p, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
        return str(p)

    # -- extract_dialogue ---------------------------------------------------
    def test_extract_dialogue_usuario_y_asistente_sin_ruido(self):
        p = self.write_transcript([
            {"type": "user", "message": {"content": "arregla el bug"}},
            {"type": "user", "message": {"content": "<system-reminder>ruido</system-reminder>"}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "listo, era un timeout"},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "no debe salir"}]}},
        ])
        d = nl.extract_dialogue(p)
        self.assertEqual(d, "USUARIO: arregla el bug\n\nASISTENTE: listo, era un timeout")

    def test_extract_dialogue_filtra_private(self):
        p = self.write_transcript([
            {"type": "user", "message": {"content": "clave <private>secreta123</private> ok"}},
        ])
        self.assertNotIn("secreta123", nl.extract_dialogue(p))

    def test_extract_dialogue_recorta_inicio_y_final(self):
        p = self.write_transcript(
            [{"type": "user", "message": {"content": f"mensaje numero {i} " + "x" * 200}}
             for i in range(100)])
        d = nl.extract_dialogue(p, max_chars=2000)
        self.assertLessEqual(len(d), 2100)
        self.assertIn("mensaje numero 0", d)     # conserva el inicio
        self.assertIn("mensaje numero 99", d)    # y el final
        self.assertIn("dialogo recortado", d)

    def test_extract_dialogue_transcript_inexistente(self):
        self.assertIsNone(nl.extract_dialogue(str(self.root / "no.jsonl")))

    # -- pending_observations -------------------------------------------------
    def test_pending_solo_raw_y_sin_corridas_previas(self):
        insert_obs(self.conn, 1, status="raw")
        insert_obs(self.conn, 2, status="summarized")
        insert_obs(self.conn, 3, status="raw")
        self.conn.execute(
            "INSERT INTO auto_runs (item_type, item_id, project, status, attempts, "
            "created_at, finished_at) VALUES ('observation', 3, 'miproyecto', 'done', 1, "
            "'2026-07-06T09:00:00', '2026-07-06T09:01:00')")
        self.conn.commit()
        ids = [o["id"] for o in nl.pending_observations(self.conn, self.cfg, "")]
        self.assertEqual(ids, [1])

    def test_pending_filtra_por_observation_projects_y_por_proyecto(self):
        insert_obs(self.conn, 1, project="a")
        insert_obs(self.conn, 2, project="b")
        cfg = dict(self.cfg, observation_projects=["b"])
        self.assertEqual([o["id"] for o in nl.pending_observations(self.conn, cfg, "")], [2])
        self.assertEqual([o["id"] for o in nl.pending_observations(self.conn, self.cfg, "a")], [1])

    def test_pending_reintenta_error_enfriado_pero_no_reciente(self):
        insert_obs(self.conn, 1)
        self.conn.execute(
            "INSERT INTO auto_runs (item_type, item_id, project, status, attempts, "
            "created_at, finished_at) VALUES ('observation', 1, 'miproyecto', 'error', 1, "
            "'2020-01-01T00:00:00', '2020-01-01T00:00:00')")  # error viejo: enfriado
        self.conn.commit()
        cfg = dict(self.cfg, max_retries=1)
        self.assertEqual([o["id"] for o in nl.pending_observations(self.conn, cfg, "")], [1])
        # sin reintentos disponibles no vuelve a salir
        cfg = dict(self.cfg, max_retries=0)
        self.assertEqual(nl.pending_observations(self.conn, cfg, ""), [])

    # -- summarize_observation ----------------------------------------------
    def _obs_row(self, obs_id):
        return dict(self.conn.execute(
            "SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone())

    def _run_row(self, obs_id):
        return dict(self.conn.execute(
            "SELECT * FROM auto_runs WHERE item_type='observation' AND item_id=?",
            (obs_id,)).fetchone())

    def test_summarize_ok_marca_summarized_y_done(self):
        t = self.write_transcript([{"type": "user", "message": {"content": "hola"}}])
        insert_obs(self.conn, 1, transcript=t)
        obs = nl.pending_observations(self.conn, self.cfg, "")[0]
        with mock.patch.object(nl.subprocess, "run",
                               return_value=fake_claude_result("Se arreglo el timeout.")):
            out = nl.summarize_observation(self.cfg, "claude", obs)
        self.assertIn("summarized", out)
        row = self._obs_row(1)
        self.assertEqual(row["status"], "summarized")
        self.assertEqual(row["summary"], "Se arreglo el timeout.")
        self.assertEqual(self._run_row(1)["status"], "done")

    def test_summarize_sin_transcript_cierra_como_skipped(self):
        insert_obs(self.conn, 1, transcript=str(self.root / "no-existe.jsonl"))
        obs = nl.pending_observations(self.conn, self.cfg, "")[0]
        out = nl.summarize_observation(self.cfg, "claude", obs)
        self.assertIn("skipped", out)
        self.assertEqual(self._obs_row(1)["status"], "summarized")
        self.assertEqual(self._run_row(1)["status"], "skipped")
        # y ya no vuelve a aparecer como pendiente
        self.assertEqual(nl.pending_observations(self.conn, self.cfg, ""), [])

    def test_summarize_error_deja_raw_y_error_en_auto_runs(self):
        t = self.write_transcript([{"type": "user", "message": {"content": "hola"}}])
        insert_obs(self.conn, 1, transcript=t)
        obs = nl.pending_observations(self.conn, self.cfg, "")[0]
        proc = mock.Mock(returncode=1, stdout="", stderr="boom")
        with mock.patch.object(nl.subprocess, "run", return_value=proc):
            out = nl.summarize_observation(self.cfg, "claude", obs)
        self.assertIn("error", out)
        self.assertEqual(self._obs_row(1)["status"], "raw")  # queda para reintento
        self.assertEqual(self._run_row(1)["status"], "error")

    def test_summarize_es_idempotente(self):
        t = self.write_transcript([{"type": "user", "message": {"content": "hola"}}])
        insert_obs(self.conn, 1, transcript=t)
        obs = nl.pending_observations(self.conn, self.cfg, "")[0]
        with mock.patch.object(nl.subprocess, "run",
                               return_value=fake_claude_result("resumen")):
            nl.summarize_observation(self.cfg, "claude", obs)
            out2 = nl.summarize_observation(self.cfg, "claude", obs)
        self.assertIn("skip", out2)

    # -- observation_cycle -----------------------------------------------------
    def test_cycle_respeta_tope_y_force_los_toma_todos(self):
        t = self.write_transcript([{"type": "user", "message": {"content": "hola"}}])
        for i in range(1, 6):
            insert_obs(self.conn, i, transcript=t, created_at=f"2026-07-06T10:0{i}:00")
        cfg = dict(self.cfg, observations_per_cycle=2)
        with mock.patch.object(nl.subprocess, "run",
                               return_value=fake_claude_result("resumen")):
            self.assertEqual(nl.observation_cycle(cfg, "claude", "", False), 2)
            self.assertEqual(nl.observation_cycle(cfg, "claude", "", False, force=True), 3)

    def test_cycle_dry_run_no_toca_nada(self):
        t = self.write_transcript([{"type": "user", "message": {"content": "hola"}}])
        insert_obs(self.conn, 1, transcript=t)
        self.assertEqual(nl.observation_cycle(self.cfg, "claude", "", True), 0)
        self.assertEqual(self._obs_row(1)["status"], "raw")


if __name__ == "__main__":
    unittest.main(verbosity=2)
