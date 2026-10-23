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
    assert out["pins"] == [{"block": "d1", "option": "a", "w": "phone", "x": 0.5, "y": 1.0, "text": "here"}]
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
