import unittest

from sensors.slack_watch import inbound


class TestInbound(unittest.TestCase):
    SELF = "U04SD6XDA72"

    def test_filtra_propios_subtipos_y_viejos(self):
        msgs = [
            {"user": self.SELF, "text": "mio", "ts": "100.5"},
            {"user": "U_OTRO", "text": "editado", "ts": "101.0", "subtype": "message_changed"},
            {"user": "U_OTRO", "text": "viejo", "ts": "99.0"},
            {"bot_id": "B1", "text": "bot sin user", "ts": "102.0"},
            {"user": "U_OTRO", "text": "nuevo 2", "ts": "103.0"},
            {"user": "U_OTRO", "text": "nuevo 1", "ts": "102.5"},
        ]
        out = inbound(msgs, watermark="100.0", self_id=self.SELF)
        self.assertEqual([m["text"] for m in out], ["nuevo 1", "nuevo 2"])

    def test_vacio(self):
        self.assertEqual(inbound([], "100.0", self.SELF), [])


if __name__ == "__main__":
    unittest.main()
