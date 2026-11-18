import base64
import json

import pytest

from mockreview import config, store
from mockreview.store import ReviewError


def test_create_copies_files_and_writes_review_json(review, env):
    d = config.data_root() / review["id"]
    data = json.loads((d / "review.json").read_text(encoding="utf-8"))
    assert store.ID_RE.match(review["id"]) and review["id"].startswith("test-review-")
    assert data["mode"] == "tailscale" and data["status"] == "open"
    assert data["origin"] == {"platform": "discord", "chat_id": "test-chat", "thread_id": "test-topic"}
    assert [v["id"] for v in data["rounds"][0]["variants"]] == ["a", "b", "c"]
    assert data["rounds"][0]["variants"][1] == {**data["rounds"][0]["variants"][1], "label": "Beta",
                                                "stance": "stance beta", "file": "rounds/1/b/index.html"}
    html = (d / "rounds/1/a/index.html").read_text(encoding="utf-8")
    assert "<h1>alpha</h1>" in html and store.PROBE_MARKER in html
    assert html.index(store.PROBE_MARKER) < html.lower().rindex("</body>")
    assert (d / "rounds/1/a/assets/logo.svg").is_file()       # sibling assets dir copied
    assert not (d / "rounds/1/b/assets").exists()
    assert len(data["token"]) >= 30


def test_assets_symlinks_are_skipped(env, tmp_path):
    src = tmp_path / "v"
    (src / "assets").mkdir(parents=True)
    (src / "index.html").write_text("<p>x</p>", encoding="utf-8")
    (src / "assets" / "ok.css").write_text("a{}", encoding="utf-8")
    (src / "assets" / "leak").symlink_to("/etc/hosts")
    data = store.create_review("t", "", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}, "tailscale",
                               [store.parse_variant_spec(f"A|s|{src / 'index.html'}")])
    a = config.data_root() / data["id"] / "rounds/1/a/assets"
    assert (a / "ok.css").is_file() and not (a / "leak").exists()


def _decisions_with_assets(tmp_path):
    src = tmp_path / "round"
    src.mkdir()
    (src / "decisions.json").write_text(json.dumps(
        {"blocks": [{"type": "recap", "id": "recap", "items": ["picked B"]}]}), encoding="utf-8")
    assets = src / "assets"
    (assets / ".git").mkdir(parents=True)
    (assets / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (assets / ".git" / "config").write_text("[core]", encoding="utf-8")
    (assets / "visible.css").write_text("a{}", encoding="utf-8")
    (assets / "leak").symlink_to("/etc/hostname")
    return src / "decisions.json"


def test_dotfiles_and_dot_directories_are_never_copied_from_assets(env, tmp_path):
    data = store.create_review("t", "", {"platform": "discord", "chat_id": "1490000000000000001",
                                         "thread_id": "1490000000000000001"}, "tailscale",
                               store.parse_round_spec(decisions=str(_decisions_with_assets(tmp_path))))
    stored = config.data_root() / data["id"] / "rounds/1/a/assets"
    assert (stored / "visible.css").is_file()
    assert not (stored / ".env").exists(), "the assets dotfile was copied into the served store"
    assert not (stored / ".git").exists(), "the assets dot-directory was copied into the served store"
    assert not (stored / "leak").exists(), "the assets symlink was followed"


def test_a_symlinked_copy_source_is_refused(env, tmp_path):
    outside = _design(tmp_path)
    link = tmp_path / "linked-design"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ReviewError) as e:
        store.save_design("linked", link)
    assert "symlink" in e.value.message and str(link) in e.value.message


@pytest.mark.parametrize("spec,msg", [
    ("nolabel", "label|stance"),
    ("A|s|relative/index.html", "absolute"),
    ("|s|/etc/hosts", "label is empty"),
])
def test_bad_variant_specs(env, spec, msg):
    with pytest.raises(ReviewError, match=msg):
        store.parse_variant_spec(spec)


def test_missing_absolute_variant(env, tmp_path):
    with pytest.raises(ReviewError, match="not found"):
        store.parse_variant_spec(f"A|s|{tmp_path / 'nonexistent' / 'x.html'}")


def test_label_may_contain_pipe_in_stance(env, variants):
    path = variants[0].rsplit("|", 1)[1]
    s = store.parse_variant_spec(f"Label|stance with | pipe|{path}")
    assert s["label"] == "Label" and s["stance"] == "stance with | pipe"


def test_create_validation(env, variants):
    specs = [store.parse_variant_spec(v) for v in variants]
    with pytest.raises(ReviewError, match="origin"):
        store.create_review("t", "", "../../x", "tailscale", specs)
    with pytest.raises(ReviewError, match="mode"):
        store.create_review("t", "", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}, "funnel", specs)
    with pytest.raises(ReviewError, match="1-4"):
        store.create_review("t", "", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}, "tailscale", specs + specs)


def test_add_round_keeps_history(review, variants):
    specs = [store.parse_variant_spec(v) for v in variants[:2]]
    data = store.add_round(review["id"], specs)
    assert [r["n"] for r in data["rounds"]] == [1, 2]
    assert len(data["rounds"][1]["variants"]) == 2
    assert (config.data_root() / review["id"] / "rounds/1/c/index.html").is_file()
    st = store.public_state(data)
    assert st["current_round"] == 2 and len(st["rounds"]) == 2


def test_add_round_on_closed_review_rejected(review, variants):
    with store.locked(review["id"]):
        d = store.load(review["id"])
        d["status"] = "closed"
        store.save(review["id"], d)
    with pytest.raises(ReviewError, match="closed"):
        store.add_round(review["id"], [store.parse_variant_spec(variants[0])])


def test_public_state_hides_thread_and_paths(review):
    st = json.dumps(store.public_state(store.load(review["id"])))
    assert "1490000000000000001" not in st
    assert "/src/" not in st and "source" not in st


def test_public_state_says_whether_the_board_wakes_hermes(env, tmp_path):
    from mockreview import api
    origin = {"platform": "discord", "chat_id": "test-chat", "thread_id": "test-topic"}
    page = tmp_path / "proto" / "index.html"
    page.parent.mkdir()
    page.write_text("<!doctype html><html><body><h1>Proto</h1></body></html>", encoding="utf-8")
    awake = api.create("Awake", origin, mode="local", prototype=str(page), allow_live_demo=True)
    quiet = api.create("Quiet", origin, mode="local", prototype=str(page), wake=False, allow_live_demo=True)
    assert store.public_state(store.load(awake["review_id"]))["wake"] is True
    assert store.public_state(store.load(quiet["review_id"]))["wake"] is False


def test_review_url(review, monkeypatch):
    monkeypatch.setenv("MOCKREVIEW_PUBLIC_BASE", "https://board.example.test:8790")
    assert store.review_url(review) == f"https://board.example.test:8790/r/{review['id']}"


def _decisions(tmp_path):
    dec = {"intro": "hi", "blocks": [
        {"type": "recap", "id": "recap", "items": ["picked B"]},
        {"type": "decision", "id": "d1", "title": "List?", "layout": "side", "options": [
            {"id": "a", "label": "Today", "html": "<div class='x'>A</div>"},
            {"id": "b", "label": "All", "recommended": True, "html": "<div>B</div>"}]},
        {"type": "text", "title": "Note", "text": "context"}]}
    f = tmp_path / "dec" / "decisions.json"
    f.parent.mkdir()
    f.write_text(json.dumps(dec), encoding="utf-8")
    return f


def _design(tmp_path):
    d = tmp_path / "design"
    d.mkdir()
    (d / "design.json").write_text(json.dumps({"name": "proj", "label": "Proj", "default_theme": "dark", "icon": "icon.svg",
        "mock_css": "mock.css", "themes": {"dark": {"bg": "#000", "surface": "#111", "text": "#eee", "accent": "#89f"},
                                            "light": {"bg": "#fff", "surface": "#fff", "text": "#111", "accent": "#35d"}}}), encoding="utf-8")
    (d / "icon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    (d / "mock.css").write_text(".app{color:red}", encoding="utf-8")
    return d


def test_decision_round_is_rendered_by_the_board_not_a_page(env, tmp_path):
    spec = store.parse_round_spec(decisions=str(_decisions(tmp_path)), design=str(_design(tmp_path)))
    data = store.create_review("t", "", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}, "tailscale", spec)
    r = data["rounds"][0]
    assert r["kind"] == "decisions"
    assert [d["id"] for d in r["decisions"]] == ["recap", "d1"]          # text blocks aren't markable
    base = store.review_dir(data["id"]) / "rounds" / "1" / "a"
    assert (base / "decisions.json").is_file() and not (base / "index.html").exists()   # no page, no iframe
    assert (base / "design" / "icon.svg").is_file() and r["design"]["icon"] == "icon.svg"
    st = store.public_state(data)
    assert st["rounds"][0]["kind"] == "decisions" and st["rounds"][0]["design"]["themes"]["light"]["accent"] == "#35d"


def test_add_round_inherits_design_and_prototype_round(env, tmp_path, variants):
    spec = store.parse_round_spec(decisions=str(_decisions(tmp_path)), design=str(_design(tmp_path)))
    data = store.create_review("t", "", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}, "tailscale", spec)
    page = variants[0].split("|")[2]
    data = store.add_round(data["id"], store.parse_round_spec(prototype=page))
    r2 = data["rounds"][1]
    assert r2["kind"] == "prototype" and r2["design"]["name"] == "proj"
    assert "mockreview-probe" in (store.review_dir(data["id"]) / "rounds/2/a/index.html").read_text(encoding="utf-8")


def test_the_emitted_probe_is_one_classic_inline_script(env):
    probe = store._probe()
    assert probe.startswith('<script>/*' + store.PROBE_MARKER + '*/')        # the marker leads, as inject_probe expects
    assert store.PROBE_MARKER in probe
    assert '<script type="module"' not in probe                              # script-src 'unsafe-inline' allows this shape
    assert 'import ' not in probe and 'export ' not in probe
    assert 'data:text/javascript' not in probe
    assert "Object.defineProperty(window, 'mockreview'" in probe             # the probe hook is built synchronously
    assert 'installAPI(' in probe


def test_decision_feedback_is_validated_against_the_round(env, tmp_path):
    from mockreview import feedback
    spec = store.parse_round_spec(decisions=str(_decisions(tmp_path)))
    r = store.create_review("t", "", {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}, "tailscale", spec)["rounds"][0]
    out = feedback.sanitize_for_round({"marks": {"d1": {"status": "approve", "option": "b", "note": "ok"},
                                                 "bogus": {"status": "approve"}, "recap": {"status": "change", "option": "z"}},
                                       "pins": [{"block": "d1", "option": "a", "w": "phone", "x": 0.5, "y": 2, "text": "here"},
                                                {"block": "nope", "x": 0, "y": 0, "text": "x"}], "guidelines": ""}, r)
    assert out["marks"] == {"d1": {"status": "approve", "option": "b", "note": "ok"},
                            "recap": {"status": "change", "option": None, "note": ""}}
    assert [{k: v for k, v in p.items() if k != "id"} for p in out["pins"]] == [
        {"block": "d1", "option": "a", "w": "phone", "x": 0.5, "y": 1.0, "text": "here"}]
    assert out["pins"][0]["id"].startswith("p")
    with pytest.raises(ReviewError):
        feedback.sanitize_for_round({"marks": {}, "pins": []}, r)


def test_bad_decisions_and_designs_are_refused(env, tmp_path):
    with pytest.raises(ReviewError):
        store.validate_decisions({"blocks": [{"type": "decision", "id": "D 1", "options": [{"html": "x"}]}]})
    with pytest.raises(ReviewError):
        store.validate_decisions({"blocks": [{"type": "decision", "id": "d1", "layout": "carousel", "options": [{"html": "x"}]}]})
    d = _design(tmp_path)
    (d / "design.json").write_text(json.dumps({"themes": {"dark": {"bg": "red; } body{", "surface": "#1", "text": "#e", "accent": "#8"}}}), encoding="utf-8")
    with pytest.raises(ReviewError):
        store.validate_design(d)
    with pytest.raises(ReviewError):
        store.parse_round_spec(decisions=str(_decisions(tmp_path)), prototype="/x.html")


# ---------------------------------------------------------------- artifact blocks

ORIGIN = {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}
PAGE = "<!doctype html><html><head><title>t</title></head><body><p id=x>hi</p><script>document.getElementById('x').textContent='ran'</script></body></html>"


def _artifact_round(tmp_path, **art):
    block = {"type": "artifact", "id": "film", "title": "Film", "html": PAGE, **art}
    f = tmp_path / "dec" / "decisions.json"
    f.parent.mkdir(exist_ok=True)
    f.write_text(json.dumps({"blocks": [block]}), encoding="utf-8")
    return store.create_review("t", "", ORIGIN, "tailscale", store.parse_round_spec(decisions=str(f)))


def test_artifact_block_is_validated_with_defaults():
    out = store.validate_decisions({"blocks": [{"type": "artifact", "id": "film", "title": "Film", "html": PAGE}]})
    b = out["blocks"][0]
    assert b["type"] == "artifact" and b["html"] == PAGE and b["height"] == "auto" and b["markable"] is False
    assert b["desktop_width"] is None and b["text"] == "" and b["caption"] == ""
    b = store.validate_decisions({"blocks": [{"type": "artifact", "id": "film", "html": PAGE, "height": 600, "markable": True,
                                              "desktop_width": 900, "caption": "c"}]})["blocks"][0]
    assert b["height"] == 600 and b["markable"] is True and b["desktop_width"] == 900 and b["caption"] == "c"


def test_artifact_display_is_frame_by_default_and_page_opt_in():
    frame = store.validate_decisions({"blocks": [{"type": "artifact", "id": "film", "html": PAGE}]})["blocks"][0]
    page = store.validate_decisions({"blocks": [{"type": "artifact", "id": "film", "html": PAGE,
                                                 "display": "page"}]})["blocks"][0]
    assert frame["display"] == "frame" and page["display"] == "page"


@pytest.mark.parametrize("over,msg", [
    ({"html": "x" * (store.MAX_ARTIFACT_HTML + 1)}, "larger than 2 MB"),
    ({"html": "é" * (store.MAX_ARTIFACT_HTML // 2 + 1)}, "larger than 2 MB"),         # bytes, not characters
    ({"html": "  "}, "non-empty"),
    ({"html": 5}, "non-empty"),
    ({"html": None}, "exactly one of html"),
    ({"src": "/etc/hosts"}, "exactly one of html"),                                    # html and src together
    ({"id": "Film"}, "id"),
    ({"id": "a_b"}, "id"),
    ({"id": "../x"}, "id"),
    ({"id": "a" * 25}, "id"),
    ({"height": 10}, "height"),
    ({"height": 99999}, "height"),
    ({"height": "tall"}, "height"),
    ({"height": True}, "height"),
    ({"markable": "yes"}, "markable"),
    ({"display": "tall"}, "display"),
    ({"display": None}, "display"),
])
def test_bad_artifacts_are_refused(over, msg):
    block = {"type": "artifact", "id": "film", "html": PAGE, **over}
    with pytest.raises(ReviewError, match=msg):
        store.validate_decisions({"blocks": [block]})


def test_artifact_needs_a_page():
    with pytest.raises(ReviewError, match="exactly one of html"):
        store.validate_decisions({"blocks": [{"type": "artifact", "id": "film"}]})


def test_artifact_src_must_be_an_absolute_existing_small_html_file(tmp_path, monkeypatch):
    page = tmp_path / "p.html"
    page.write_text(PAGE, encoding="utf-8")
    ok = store.validate_decisions({"blocks": [{"type": "artifact", "id": "film", "src": str(page)}]})["blocks"][0]
    assert ok["src"] == str(page) and "html" not in ok
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    for bad, msg in (("p.html", "absolute"), (str(tmp_path / "missing.html"), "not found|exist"), (str(tmp_path), "html"),
                     (str(tmp_path / "x.txt"), "html")):
        with pytest.raises(ReviewError, match=msg):
            store.validate_decisions({"blocks": [{"type": "artifact", "id": "film", "src": bad}]})
    big = tmp_path / "big.html"
    big.write_bytes(b"x" * (store.MAX_ARTIFACT_HTML + 1))
    with pytest.raises(ReviewError, match="larger than 2 MB"):
        store.validate_decisions({"blocks": [{"type": "artifact", "id": "film", "src": str(big)}]})


def test_artifact_ids_share_the_block_id_namespace():
    with pytest.raises(ReviewError, match="duplicate|already"):
        store.validate_decisions({"blocks": [{"type": "artifact", "id": "x", "html": PAGE},
                                             {"type": "mock", "id": "x", "html": "<p>m</p>"}]})


def test_markable_artifact_is_a_decision_and_a_plain_one_is_a_mock_target(env, tmp_path):
    dec = store.validate_decisions({"blocks": [
        {"type": "artifact", "id": "plain", "html": PAGE},
        {"type": "artifact", "id": "marked", "html": PAGE, "markable": True}]})
    assert [e["id"] for e in store.decision_index(dec)] == ["marked"]
    assert store.decision_index(dec)[0]["kind"] == "artifact" and store.decision_index(dec)[0]["pinnable"] is True
    assert [e["id"] for e in store.mock_index(dec)] == ["plain"]


def test_stored_artifact_is_a_served_file_and_decisions_json_keeps_neither_html_nor_src(env, tmp_path):
    data = _artifact_round(tmp_path)
    base = store.review_dir(data["id"]) / "rounds" / "1" / "a"
    stored = json.loads((base / "decisions.json").read_text(encoding="utf-8"))["blocks"][0]
    assert stored["file"] == "blocks/film.html" and "html" not in stored and "src" not in stored
    served = (base / "blocks" / "film.html").read_text(encoding="utf-8")
    assert served.startswith('<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy"')
    assert "script-src 'unsafe-inline'" in served and "connect-src 'none'" in served and "form-action 'none'" in served
    assert "base-uri 'none'" in served and "frame-src 'none'" in served and "img-src data:" in served
    assert store.PROBE_MARKER in served and served.index(store.PROBE_MARKER) < served.index("<p id=x>")
    assert served.count("<!doctype") + served.count("<!DOCTYPE") == 1       # the page's own doctype is dropped
    assert "ran" in served                                                  # the page itself is kept, script and all


def test_artifact_src_is_bundled_and_the_path_is_not_stored(env, tmp_path):
    d = tmp_path / "art"
    (d / "assets").mkdir(parents=True)
    (d / "assets" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nxx")
    (d / "page.html").write_text('<img src="assets/a.png"><script>1</script>', encoding="utf-8")
    f = tmp_path / "dec" / "decisions.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"blocks": [{"type": "artifact", "id": "film", "src": str(d / "page.html")}]}), encoding="utf-8")
    from mockreview import bundle
    data = store.create_review("t", "", ORIGIN, "tailscale", bundle.bundle_artifacts(store.parse_round_spec(decisions=str(f))))
    base = store.review_dir(data["id"]) / "rounds" / "1" / "a"
    served = (base / "blocks" / "film.html").read_text(encoding="utf-8")
    assert "data:image/png;base64," in served and "assets/a.png" not in served
    assert str(d) not in (base / "decisions.json").read_text(encoding="utf-8")


def test_artifact_pins_and_marks_are_accepted_by_its_id(env, tmp_path):
    from mockreview import feedback
    data = _artifact_round(tmp_path, markable=True)
    r = data["rounds"][0]
    out = feedback.sanitize_for_round({"marks": {"film": {"status": "change", "note": "tighten"}},
                                       "pins": [{"block": "film", "option": None, "w": "desktop", "x": 0.25, "y": 0.5, "text": "here"},
                                                {"block": "ghost", "x": 0, "y": 0, "text": "x"}], "guidelines": ""}, r)
    assert out["marks"]["film"] == {"status": "change", "option": None, "note": "tighten"}
    assert [{k: v for k, v in p.items() if k != "id"} for p in out["pins"]] == [
        {"block": "film", "option": None, "w": "desktop", "x": 0.25, "y": 0.5, "text": "here"}]
    assert out["pins"][0]["id"].startswith("p")
    (tmp_path / "two").mkdir()
    r = _artifact_round(tmp_path / "two", markable=False)["rounds"][0]
    out = feedback.sanitize_for_round({"marks": {"film": {"status": "approve"}},
                                       "pins": [{"block": "film", "option": None, "w": "desktop", "x": 0.5, "y": 0.5, "text": "pin"}],
                                       "guidelines": ""}, r)
    assert "film" not in out["marks"] and [p["block"] for p in out["pins"]] == ["film"]


# ---------------------------------------------------------------- one asset budget per round

def _media_page(d, name, size=5000):
    (d / "assets").mkdir(parents=True, exist_ok=True)
    (d / "assets" / "m.mp4").write_bytes(b"M" * size)
    (d / f"{name}.html").write_text('<video src="assets/m.mp4"></video>', encoding="utf-8")
    return str(d / f"{name}.html")


def _small_cap(monkeypatch, n=8000):
    from mockreview import bundle
    monkeypatch.setattr(store, "MAX_ASSETS_BYTES", n)      # bundle.py imported its own name for the cap
    monkeypatch.setattr(bundle, "MAX_ASSETS_BYTES", n)


def test_store_counts_every_src_page_against_one_round_budget(env, tmp_path, monkeypatch):
    """A spec that never went through api._spec (so bundle_artifacts never ran) still cannot exceed the round cap."""
    _small_cap(monkeypatch)
    blocks = [{"type": "artifact", "id": n, "src": _media_page(tmp_path / n, "p")} for n in ("one", "two")]
    f = tmp_path / "dec" / "decisions.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"blocks": blocks[:1]}), encoding="utf-8")       # each fits alone
    store.create_review("t", "", ORIGIN, "tailscale", store.parse_round_spec(decisions=str(f)))
    f.write_text(json.dumps({"blocks": blocks}), encoding="utf-8")           # both together do not
    with pytest.raises(ReviewError, match=r"artifact two: the artifact's assets are larger than 8000 bytes once inlined"):
        store.create_review("t", "", ORIGIN, "tailscale", store.parse_round_spec(decisions=str(f)))


def test_inlined_artifact_media_and_the_copied_assets_folder_share_one_round_budget(env, tmp_path, monkeypatch):
    """Chosen fix: the sibling assets/ copy is counted against the same cap as the artifacts' inlined media (not skipped),
    so a mixed round (artifact plus a mock that needs assets/) is bounded too."""
    from mockreview import bundle
    _small_cap(monkeypatch)
    d = tmp_path / "dec"
    (d / "assets").mkdir(parents=True)
    (d / "assets" / "click.wav").write_bytes(b"W" * 5000)                    # the round's own copied asset
    media = tmp_path / "art"
    page = _media_page(media, "p")                                            # 5,000 B of artifact media, inlined
    blocks = [{"type": "artifact", "id": "film", "src": page}, {"type": "mock", "id": "m", "html": "<p>m</p>"}]
    (d / "decisions.json").write_text(json.dumps({"blocks": blocks}), encoding="utf-8")
    spec = bundle.bundle_artifacts(store.parse_round_spec(decisions=str(d / "decisions.json")))   # what api._spec hands the store
    with pytest.raises(ReviewError, match=r"assets dir .*assets is larger than 8000 bytes together with the round's artifact media"):
        store.create_review("t", "", ORIGIN, "tailscale", spec)
    assert spec["inlined"] == len(b"data:video/mp4;base64," + base64.b64encode(b"M" * 5000))
    (d / "assets" / "click.wav").write_bytes(b"W" * 1000)                    # the emitted data: URI plus 1,000 fits in 8,000
    data = store.create_review("t", "", ORIGIN, "tailscale", bundle.bundle_artifacts(store.parse_round_spec(decisions=str(d / "decisions.json"))))
    assert (store.review_dir(data["id"]) / "rounds/1/a/assets/click.wav").stat().st_size == 1000
    # without any artifact the copy keeps its own cap alone
    (d / "decisions.json").write_text(json.dumps({"blocks": blocks[1:]}), encoding="utf-8")
    (d / "assets" / "click.wav").write_bytes(b"W" * 7000)
    store.create_review("t", "", ORIGIN, "tailscale", store.parse_round_spec(decisions=str(d / "decisions.json")))


def test_section_index_keeps_ids_generates_refs_skips_collisions_and_reserves_page():
    dec = store.validate_decisions({"blocks": [
        {"type": "text", "title": "Intro", "text": "x"},
        {"type": "callout", "tone": "ok", "text": "y"},
        {"type": "question", "id": "s1", "title": "Ask", "recommendation": {"answer": "a"}},
        {"type": "text", "text": "z"},
        {"type": "mock", "id": "s4", "html": "<p/>"},
        {"type": "text", "text": "w"}]})
    assert store.section_index(dec) == [
        {"ref": "s2", "type": "text", "title": "Intro"},
        {"ref": "s3", "type": "callout", "title": "Callout"},
        {"ref": "s1", "type": "question", "title": "Ask"},
        {"ref": "s5", "type": "text", "title": "Text"},
        {"ref": "s4", "type": "mock", "title": "Mock"},
        {"ref": "s6", "type": "text", "title": "Text"}]
    assert store.section_index(dec) == store.section_index(dec)
    with pytest.raises(ReviewError, match="block id 'page' is reserved"):
        store.validate_decisions({"blocks": [{"type": "question", "id": "page", "title": "Q",
                                              "recommendation": {"answer": "a"}}]})
    with pytest.raises(ReviewError, match="block id 'page' is reserved"):
        store.validate_decisions({"blocks": [{"type": "recap", "id": "page", "items": ["x"]}]})
    for ref in ("else", "doc"):
        with pytest.raises(ReviewError, match=f"block id '{ref}' is reserved"):
            store.validate_decisions({"blocks": [{"type": "question", "id": ref, "title": "Q",
                                                  "recommendation": {"answer": "a"}}]})
        with pytest.raises(ReviewError, match=f"block id '{ref}' is reserved"):
            store.validate_decisions({"blocks": [{"type": "recap", "id": ref, "items": ["x"]}]})
