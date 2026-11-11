import unittest

from slack_watch import inbound, msg_out


class TestMsgOut(unittest.TestCase):
    def test_sin_adjuntos_no_agrega_la_clave(self):
        out = msg_out({"user": "U1", "text": "hola", "ts": "100.1"})
        self.assertEqual(out, {"user": "U1", "text": "hola", "ts": "100.1"})

    def test_mensaje_solo_imagen_conserva_el_id(self):
        out = msg_out({
            "user": "U1", "text": "", "ts": "100.2",
            "files": [{"id": "F123", "name": "image.png", "mimetype": "image/png"},
                      {"name": "sin_id.png"}],
        })
        self.assertEqual(out["text"], "")
        self.assertEqual(out["files"], [{"id": "F123", "name": "image.png",
                                         "mimetype": "image/png"}])


class TestInbound(unittest.TestCase):
    def test_filtra_propios_subtipos_y_viejos(self):
        msgs = [
            {"user": "YO", "ts": "200"},
            {"user": "U1", "ts": "150"},
            {"user": "U1", "ts": "250", "subtype": "channel_join"},
            {"user": "U1", "ts": "300"},
            {"user": "U1", "ts": "260"},
        ]
        self.assertEqual([m["ts"] for m in inbound(msgs, "200", "YO")], ["260", "300"])

    def test_bots_solo_con_include_bots(self):
        msgs = [
            {"bot_id": "B1", "subtype": "bot_message", "text": "alerta", "ts": "300"},
            {"user": "U1", "ts": "310"},
        ]
        self.assertEqual([m["ts"] for m in inbound(msgs, "200", "YO")], ["310"])
        bots = inbound(msgs, "200", "YO", include_bots=True)
        self.assertEqual([m["ts"] for m in bots], ["300", "310"])
        self.assertEqual(msg_out(bots[0])["user"], "B1")


if __name__ == "__main__":
    unittest.main()
