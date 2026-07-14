import os
import tempfile
import unittest

from ast_graph import build_graph
from impact import impact


def _write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


class AstGraphTest(unittest.TestCase):
    def _graph(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        _write(root, "core/util.py", '"""Utilidades."""\ndef foo(x):\n    return x + 1\n')
        _write(root, "app.py",
               "from core.util import foo\n\n"
               "class Handler:\n"
               "    def run(self):\n"
               "        return foo(1)\n")
        return build_graph(root, ignore_tests=True)

    def test_nodes_and_edges(self):
        g = self._graph()
        ids = {n["id"] for n in g["nodes"]}
        self.assertIn("file:core/util.py", ids)
        self.assertIn("function:core/util.py:foo", ids)
        self.assertIn("class:app.py:Handler", ids)
        self.assertIn("function:app.py:Handler.run", ids)

        etypes = {(e["source"], e["target"], e["type"]) for e in g["edges"]}
        self.assertIn(("file:app.py", "file:core/util.py", "imports"), etypes)
        self.assertIn(("function:app.py:Handler.run", "function:core/util.py:foo", "calls"), etypes)
        self.assertIn(("class:app.py:Handler", "function:app.py:Handler.run", "contains"), etypes)
        self.tmp.cleanup()

    def test_impact_reverse_closure(self):
        g = self._graph()
        res = impact(g, {"core/util.py"})
        self.assertIn("app.py", res["affected_files"])
        self.tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
