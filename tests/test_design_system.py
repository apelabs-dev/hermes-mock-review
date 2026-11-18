"""The rules measured on the approved pack, pinned in code: text scale, token values, the brand pin."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOARD_CSS = (ROOT / "mockreview" / "board" / "board.css").read_text(encoding="utf-8")
SETTINGS_CSS = (ROOT / "mockreview" / "board" / "settings.css").read_text(encoding="utf-8")
BOARD_JS = (ROOT / "mockreview" / "board" / "board.js").read_text(encoding="utf-8")
PROBE_JS = (ROOT / "mockreview" / "board" / "probe.js").read_text(encoding="utf-8")
PAGES = (ROOT / "mockreview" / "pages.py").read_text(encoding="utf-8")
EXPORT = (ROOT / "mockreview" / "export.py").read_text(encoding="utf-8")

ALLOWED = {"12px", "13px", "14px", "16px", "20px"}


def _sizes(css: str):
    return set(re.findall(r"font-size:\s*([\d.]+px|clamp\([^)]*\))", css))


def test_the_text_scale_is_exactly_12_13_14_16_20():
    for name, css in (("board.css", BOARD_CSS), ("settings.css", SETTINGS_CSS),
                      ("pages.py", PAGES), ("export.py", EXPORT)):
        bad = {s for s in _sizes(css) if s not in ALLOWED}
        assert not bad, f"{name} uses font sizes outside the approved scale: {sorted(bad)}"
    for name, css in (("board.css", BOARD_CSS), ("settings.css", SETTINGS_CSS),
                      ("pages.py", PAGES), ("export.py", EXPORT)):
        assert not re.search(r"font-size:\s*(?:clamp|calc|\d+\.\d+px)", css), f"{name} has an off-scale heading"


def test_the_radii_are_ten_for_controls_and_twelve_for_cards():
    assert re.search(r"--r:\s*10px", BOARD_CSS)
    assert re.search(r"--rc:\s*12px", BOARD_CSS)
    assert re.search(r"--hit:\s*36px", BOARD_CSS)
    assert "@media (max-width: 600px)" in BOARD_CSS and "min-height: 44px" in BOARD_CSS


def test_the_pin_is_always_the_brand_indigo():
    assert re.search(r"--pin:\s*#5b5bf0", BOARD_CSS)
    assert "#ff5b3a" not in BOARD_CSS and "#d97706" not in BOARD_CSS
    assert "#ff5b3a" not in PROBE_JS and "#d97706" not in PROBE_JS
    assert "#5b5bf0" in PROBE_JS, "prototype pins are the same brand indigo"
    design_vars = re.search(r"var DESIGN_VARS = \[(.*?)\];", BOARD_JS, re.S).group(1)
    assert "--pin" not in design_vars, "--pin must not be a design-profile variable"
    assert "--pin-ring" not in design_vars
    assert "removeProperty('--pin')" in BOARD_JS and "removeProperty('--pin-ring')" in BOARD_JS
