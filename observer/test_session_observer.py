#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests del observer (fase 1 de memoria pasiva).

Correr:  python -m unittest observer.test_session_observer -v
         (o desde observer/:  python -m unittest test_session_observer -v)

Sin dependencias externas; usa una hub.db temporal y transcripts sinteticos.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_observer as so  # noqa: E402


def make_hub(path):
    """hub.db minima con la tabla projects (la crea projects-hub en la vida real)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE projects (name TEXT PRIMARY KEY, path TEXT, description TEXT)")
    conn.commit()
    conn.close()


def write_transcript(path, entries):
    with open(path, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def user_entry(text, **extra):
    return {"type": "user", "message": {"role": "user", "content": text},
            "timestamp": extra.pop("timestamp", "2026-07-06T10:00:00Z"), **extra}


def tool_entry(name, tool_input, timestamp="2026-07-06T10:05:00Z"):
    return {"type": "assistant", "timestamp": timestamp, "gitBranch": "feature/x",
            "message": {"content": [{"type": "tool_use", "name": name, "input": tool_input}]}}


class ObserverTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "hub.db"
        make_hub(self.db_path)
        self.proj_dir = self.root / "repos" / "miproyecto"
        self.proj_dir.mkdir(parents=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO projects VALUES (?, ?, ?)",
                     ("miproyecto", str(self.proj_dir), "demo"))
        conn.commit()
        conn.close()
        self.conn = so.db(self.db_path)  # crea observations defensivamente
        self.cfg = dict(so.DEFAULTS)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # -- resolucion de proyecto ------------------------------------------------
    def test_resolve_project_exacto_y_subcarpeta(self):
        self.assertEqual(so.resolve_project(self.conn, str(self.proj_dir)), "miproyecto")
        sub = self.proj_dir / "src" / "deep"
        self.assertEqual(so.resolve_project(self.conn, str(sub)), "miproyecto")

    def test_resolve_project_fuera_del_hub(self):
        self.assertIsNone(so.resolve_project(self.conn, str(self.root / "otra")))

    def test_resolve_project_case_insensitive_en_windows(self):
        if os.name != "nt":
            self.skipTest("normcase solo altera mayusculas en Windows")
        self.assertEqual(so.resolve_project(self.conn, str(self.proj_dir).upper()),
                         "miproyecto")

    def test_no_confunde_prefijo_de_nombre(self):
        # 'miproyecto-viejo' comparte prefijo textual pero NO es subcarpeta.
        otro = self.root / "repos" / "miproyecto-viejo"
        self.assertIsNone(so.resolve_project(self.conn, str(otro)))

    # -- opt-in ------------------------------------------------------------------
    def test_opt_in_vacio_captura_todos(self):
        self.assertTrue(so.should_capture({"projects": []}, "miproyecto"))

    def test_opt_in_con_lista_filtra(self):
        self.assertTrue(so.should_capture({"projects": ["miproyecto"]}, "miproyecto"))
        self.assertFalse(so.should_capture({"projects": ["otro"]}, "miproyecto"))
        self.assertFalse(so.should_capture({"projects": []}, None))

    # -- parseo del transcript ---------------------------------------------------
    def test_parse_extrae_prompt_archivos_branch_y_stats(self):
        t = self.root / "t.jsonl"
        write_transcript(t, [
            user_entry("arregla el bug del timeout"),
            tool_entry("Edit", {"file_path": "C:/x/api.py"}),
            tool_entry("Write", {"file_path": "C:/x/test_api.py"},
                       timestamp="2026-07-06T10:15:00Z"),
            # Read NO cuenta como archivo tocado (ultimo timestamp: define la duracion)
            tool_entry("Read", {"file_path": "C:/x/leido.py"},
                       timestamp="2026-07-06T10:30:00Z"),
        ])
        p = so.parse_transcript(t, self.cfg)
        self.assertEqual(p["first_prompt"], "arregla el bug del timeout")
        self.assertEqual(p["files"], ["C:/x/api.py", "C:/x/test_api.py"])
        self.assertEqual(p["branch"], "feature/x")
        self.assertEqual(p["stats"]["tools"], {"Edit": 1, "Write": 1, "Read": 1})
        self.assertEqual(p["stats"]["user_msgs"], 1)
        self.assertEqual(p["stats"]["duration_min"], 30.0)

    def test_parse_salta_wrappers_y_meta(self):
        t = self.root / "t.jsonl"
        write_transcript(t, [
            user_entry("<command-name>/clear</command-name>"),
            user_entry("<system-reminder>ruido</system-reminder>"),
            user_entry("hola, este es el prompt real"),
            user_entry("segundo mensaje"),
        ])
        p = so.parse_transcript(t, self.cfg)
        self.assertEqual(p["first_prompt"], "hola, este es el prompt real")
        self.assertEqual(p["stats"]["user_msgs"], 2)

    def test_parse_lineas_corruptas_no_botan(self):
        t = self.root / "t.jsonl"
        with open(t, "w", encoding="utf-8") as fh:
            fh.write("esto no es json\n")
            fh.write('{"type": "user", "message": {"content": "prompt valido"}}\n')
            fh.write("[1,2,3]\n")  # json valido pero no dict
        p = so.parse_transcript(t, self.cfg)
        self.assertEqual(p["first_prompt"], "prompt valido")

    def test_parse_contenido_en_bloques(self):
        t = self.root / "t.jsonl"
        write_transcript(t, [{
            "type": "user",
            "message": {"content": [{"type": "text", "text": "prompt en bloques"}]},
        }])
        self.assertEqual(so.parse_transcript(t, self.cfg)["first_prompt"],
                         "prompt en bloques")

    def test_transcript_inexistente_devuelve_none(self):
        self.assertIsNone(so.parse_transcript(self.root / "no-existe.jsonl", self.cfg))

    # -- privacidad ----------------------------------------------------------------
    def test_private_se_elimina(self):
        self.assertEqual(so.strip_private("hola <private>token secreto</private> mundo"),
                         "hola  mundo")

    def test_prompt_100_por_ciento_privado_usa_el_siguiente(self):
        t = self.root / "t.jsonl"
        write_transcript(t, [
            user_entry("<private>todo esto es secreto</private>"),
            user_entry("prompt publico"),
        ])
        self.assertEqual(so.parse_transcript(t, self.cfg)["first_prompt"], "prompt publico")

    # -- persistencia -----------------------------------------------------------
    def _payload(self, transcript, session_id="s1"):
        return {"session_id": session_id, "transcript_path": str(transcript),
                "cwd": str(self.proj_dir)}

    def test_process_guarda_observacion(self):
        t = self.root / "t.jsonl"
        write_transcript(t, [user_entry("hazme un feature"),
                             tool_entry("Edit", {"file_path": "a.py"})])
        result = so.process(self._payload(t), self.cfg, self.conn)
        self.assertTrue(result.startswith("ok:"), result)
        row = self.conn.execute("SELECT * FROM observations").fetchone()
        self.assertEqual(row["project"], "miproyecto")
        self.assertEqual(row["status"], "raw")
        self.assertEqual(json.loads(row["files_touched"]), ["a.py"])

    def test_process_es_idempotente_por_session_id(self):
        t = self.root / "t.jsonl"
        write_transcript(t, [user_entry("primera pasada"),
                             tool_entry("Edit", {"file_path": "a.py"})])
        so.process(self._payload(t), self.cfg, self.conn)
        # la sesion se retoma, toca otro archivo y vuelve a terminar
        write_transcript(t, [user_entry("primera pasada"),
                             tool_entry("Edit", {"file_path": "a.py"}),
                             tool_entry("Edit", {"file_path": "b.py"})])
        so.process(self._payload(t), self.cfg, self.conn)
        rows = self.conn.execute("SELECT * FROM observations").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["files_touched"]), ["a.py", "b.py"])

    def test_reprocesar_resetea_a_raw_para_re_resumen(self):
        t = self.root / "t.jsonl"
        write_transcript(t, [user_entry("trabajo"), tool_entry("Edit", {"file_path": "a.py"})])
        so.process(self._payload(t), self.cfg, self.conn)
        self.conn.execute("UPDATE observations SET status='summarized', summary='resumen'")
        self.conn.commit()
        so.process(self._payload(t), self.cfg, self.conn)
        row = self.conn.execute("SELECT status, summary FROM observations").fetchone()
        self.assertEqual(row["status"], "raw")        # el listener debe re-resumir
        self.assertEqual(row["summary"], "resumen")   # sin perder el resumen previo

    def test_process_salta_sesion_trivial(self):
        t = self.root / "t.jsonl"
        write_transcript(t, [user_entry("<command-name>/clear</command-name>")])
        result = so.process(self._payload(t), self.cfg, self.conn)
        self.assertTrue(result.startswith("skip:"), result)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM observations").fetchone()[0], 0)

    def test_process_salta_cwd_fuera_del_hub(self):
        t = self.root / "t.jsonl"
        write_transcript(t, [user_entry("hola"), tool_entry("Edit", {"file_path": "a.py"})])
        payload = self._payload(t)
        payload["cwd"] = str(self.root / "carpeta-ajena")
        result = so.process(payload, self.cfg, self.conn)
        self.assertTrue(result.startswith("skip:"), result)

    def test_prune_borra_raw_viejas_y_conserva_summarized(self):
        self.conn.execute(
            "INSERT INTO observations (project, session_id, status, created_at) VALUES "
            "('miproyecto', 'vieja-raw', 'raw', '2020-01-01T00:00:00'),"
            "('miproyecto', 'vieja-sum', 'summarized', '2020-01-01T00:00:00'),"
            "('miproyecto', 'nueva-raw', 'raw', '2099-01-01T00:00:00')")
        self.conn.commit()
        so.prune(self.conn, {"retention_days": 30})
        ids = {r["session_id"] for r in
               self.conn.execute("SELECT session_id FROM observations")}
        self.assertEqual(ids, {"vieja-sum", "nueva-raw"})

    def test_prune_desactivado_con_cero(self):
        self.conn.execute(
            "INSERT INTO observations (project, session_id, status, created_at) VALUES "
            "('miproyecto', 'vieja-raw', 'raw', '2020-01-01T00:00:00')")
        self.conn.commit()
        so.prune(self.conn, {"retention_days": 0})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM observations").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
