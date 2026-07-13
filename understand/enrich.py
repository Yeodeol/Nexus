"""Rellena el campo `summary` de los nodos file de un mapa nexus-understand.

Paso 1 (gratis): summary <- primera frase del docstring, cuando existe.
Paso 2 (opcional): mergea resumenes LLM desde un JSONL de {"file","summary"}.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

SUM_MAX = 300


def first_sentence(doc: str, maxlen: int = 200) -> str:
    parts = re.split(r"(?<=[.!?])\s", doc.strip())
    return parts[0][:maxlen] if parts else doc[:maxlen]


def enrich(map_path: str, summaries_file: str = "") -> tuple[int, int, int]:
    with open(map_path, "r", encoding="utf-8") as fh:
        g = json.load(fh)
    filenodes = {n["file"]: n for n in g["nodes"] if n["type"] == "file"}

    from_doc = 0
    for n in filenodes.values():
        if not n["summary"] and n["doc"]:
            n["summary"] = first_sentence(n["doc"])
            from_doc += 1

    from_llm = 0
    if summaries_file:
        with open(summaries_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rel = rec.get("file", "").replace("\\", "/")
                summ = (rec.get("summary") or "").strip()
                if rel in filenodes and summ:
                    filenodes[rel]["summary"] = summ[:SUM_MAX]
                    from_llm += 1

    with open(map_path, "w", encoding="utf-8") as fh:
        json.dump(g, fh, ensure_ascii=False, indent=2)
    return from_doc, from_llm, len(filenodes)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rellena summaries del mapa nexus-understand.")
    ap.add_argument("map", help="Ruta al understand.json")
    ap.add_argument("--summaries-file", default="", help="JSONL {file, summary} del pase LLM")
    args = ap.parse_args(argv)
    doc, llm, total = enrich(args.map, args.summaries_file)
    print(f"file nodes: {total}  |  summary desde doc: {doc}  |  desde LLM: {llm}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
