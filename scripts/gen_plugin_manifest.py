#!/usr/bin/env python3
"""Rewrite plugin.yaml's config_schema block from mockreview/settings.py SPEC (run after editing SPEC)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mockreview.settings import render_config_schema  # noqa: E402

p = ROOT / "plugin.yaml"
text = p.read_text()
head, sep, rest = text.partition("config_schema:\n")
tail = ""
if sep:
    lines = rest.splitlines(keepends=True)
    i = 0
    while i < len(lines) and (lines[i].startswith(" ") or not lines[i].strip()):
        i += 1
    tail = "".join(lines[i:])
p.write_text(head + render_config_schema() + tail)
print(f"wrote {p}")
