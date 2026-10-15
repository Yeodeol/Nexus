import io
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import slack_send


class Respuesta(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestAdjuntos(unittest.TestCase):
    def setUp(self):
        self.llamadas = []
        self.dir = tempfile.TemporaryDirectory()
        self.img = Path(self.dir.name) / "correo.png"
        self.img.write_bytes(b"\x89PNG1234")

    def tearDown(self):
        self.dir.cleanup()

    def falso_urlopen(self, req, timeout=None):
        url = req.full_url
        self.llamadas.append((url, req.data, req.headers.get("Content-type")))
        if url.endswith("files.getUploadURLExternal"):
            return Respuesta(json.dumps({"ok": True, "upload_url": "https://files.slack.test/u1",
                                         "file_id": "F1"}).encode())
        if url.startswith("https://files.slack.test/"):
            return Respuesta(b"OK")
        return Respuesta(json.dumps({"ok": True, "channel": "D1", "ts": "1.1"}).encode())

    def enviar(self, *argv):
        with mock.patch.object(slack_send, "token", return_value="xoxp-test"), \
             mock.patch("urllib.request.urlopen", self.falso_urlopen):
            slack_send.main(list(argv))

    def test_sube_el_archivo_y_lo_publica_con_el_mensaje_en_el_hilo(self):
        self.enviar("D1", "hola", "--attach", str(self.img), "--thread", "10.1")

        metodos = [u.rsplit("/", 1)[-1] for u, _, _ in self.llamadas]
        self.assertEqual(metodos, ["files.getUploadURLExternal", "u1", "files.completeUploadExternal"])

        pedido = urllib.parse.parse_qs(self.llamadas[0][1].decode())
        self.assertEqual(pedido, {"filename": ["correo.png"], "length": ["8"]})
        self.assertEqual(self.llamadas[1][1], b"\x89PNG1234")

        cierre = urllib.parse.parse_qs(self.llamadas[2][1].decode())
        self.assertEqual(cierre["channel_id"], ["D1"])
        self.assertEqual(cierre["initial_comment"], ["hola"])
        self.assertEqual(cierre["thread_ts"], ["10.1"])
        self.assertEqual(json.loads(cierre["files"][0]), [{"id": "F1", "title": "correo.png"}])

    def test_sin_adjuntos_sigue_usando_postMessage(self):
        self.enviar("D1", "hola")
        self.assertEqual([u.rsplit("/", 1)[-1] for u, _, _ in self.llamadas], ["chat.postMessage"])

    def test_un_adjunto_inexistente_corta_antes_de_subir(self):
        with self.assertRaises(SystemExit):
            self.enviar("D1", "hola", "--attach", str(self.img) + ".no")
        self.assertEqual(self.llamadas, [])


if __name__ == "__main__":
    unittest.main()
