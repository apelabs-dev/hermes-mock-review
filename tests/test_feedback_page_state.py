"""Page state in submitted feedback: the server re-caps what the board's page store accepted, never trusts the board,
and renders it as quoted data under its own markdown section. Page state alone counts as content, so it unlocks a send."""

import json

from mockreview import feedback, store

ORIGIN = {"platform": "discord", "chat_id": "1490000000000000001", "thread_id": "1490000000000000001"}


def _decisions():
    return {"blocks": [
        {"type": "decision", "id": "d1", "title": "Layout", "options": [
            {"id": "a", "label": "One", "html": "<div>a</div>"},
            {"id": "b", "label": "Two", "html": "<div>b</div>"}]},
        {"type": "artifact", "id": "admin", "title": "Admin pages", "html": "<p>page</p>", "markable": True},
        {"type": "artifact", "id": "plain", "title": "Read only", "html": "<p>page</p>"},
        {"type": "text", "text": "context"},
    ]}


def _review(tmp_path):
    f = tmp_path / "dec" / "decisions.json"
    f.parent.mkdir()
    f.write_text(json.dumps(_decisions()), encoding="utf-8")
    data = store.create_review("Page state", "", ORIGIN, "tailscale", store.parse_round_spec(decisions=str(f)))
    return data, data["rounds"][0]


def _submit(round_, page_state, **extra):
    body = {"marks": {"d1": {"status": "approve", "option": "b"}}, "pins": [], "guidelines": ""}
    body.update(extra)
    body["page_state"] = page_state
    return feedback.sanitize_for_round(body, round_)


def test_round_artifacts_lists_every_artifact_and_nothing_else(tmp_path):
    _, r = _review(tmp_path)
    assert store.round_artifacts(r) == ["admin", "plain"]
    assert store.round_artifacts({"kind": "prototype", "decisions": _decisions()}) == []


def test_only_this_rounds_artifact_blocks_keep_state(tmp_path):
    _, r = _review(tmp_path)
    out = _submit(r, {"admin": {"pick": "b"}, "plain": {"x": 1}, "d1": {"nope": 1}, "nope": {"x": 1}})
    assert out["page_state"] == {"admin": {"pick": "b"}, "plain": {"x": 1}}


def test_values_are_capped_and_counted_as_utf8(tmp_path):
    _, r = _review(tmp_path)
    out = _submit(r, {"admin": {
        "ok": "x" * 4000,
        "ascii-too-big": "x" * 9000,
        "wide-too-big": "é" * 4097,          # 4097 characters, 8194 bytes
        "wide-ok": "é" * 4000,               # 8000 bytes
    }})
    assert out["page_state"]["admin"] == {"ok": "x" * 4000, "wide-ok": "é" * 4000}


def test_a_block_keeps_at_most_64_keys(tmp_path):
    _, r = _review(tmp_path)
    keys = {f"k{i:03d}": i for i in range(70)}
    out = _submit(r, {"admin": keys})
    kept = out["page_state"]["admin"]
    assert len(kept) == 64
    assert sorted(kept) == [f"k{i:03d}" for i in range(64)]


def test_the_round_cap_drops_keys_past_256_kb_and_later_blocks(tmp_path):
    _, r = _review(tmp_path)
    big = {"k%02d" % i: "x" * 4096 for i in range(64)}       # far over 256 KB across the block
    out = _submit(r, {"admin": big, "plain": {"last": "y" * 100}})
    kept = out["page_state"]["admin"]
    total = sum(feedback._json_bytes(v) + len(k.encode("utf-8"))
                for block in out["page_state"].values() for k, v in block.items())
    assert total <= feedback.MAX_PAGE_ROUND
    assert len(kept) < 64


def test_non_json_values_are_dropped(tmp_path):
    _, r = _review(tmp_path)
    out = _submit(r, {"admin": {"nan": float("nan"), "ok": [1, {"deep": True}]}})
    assert out["page_state"]["admin"] == {"ok": [1, {"deep": True}]}


def test_page_state_alone_unlocks_a_send(tmp_path):
    _, r = _review(tmp_path)
    out = _submit(r, {"admin": {"pick": "b"}}, marks={})
    assert out["kind"] == "decisions"
    assert out["page_state"] == {"admin": {"pick": "b"}}


def test_page_state_is_kept_when_the_send_has_other_content(tmp_path):
    _, r = _review(tmp_path)
    out = _submit(r, {"admin": {"pick": "b"}})
    assert out["kind"] == "decisions" and out["page_state"] == {"admin": {"pick": "b"}}


def test_markdown_section_sits_between_decisions_and_pins_and_quotes_sorted_values(tmp_path):
    data, r = _review(tmp_path)
    r["feedback"] = _submit(r, {"admin": {"pick": "b", "note": "Keep it"}})
    md = feedback.render_decisions_markdown(data, r)
    assert "## Decisions" in md and "## Pins (0)" in md
    assert md.index("## Decisions") < md.index("## Page state (1 block)") < md.index("## Pins")
    assert '  > note: "Keep it"' in md
    assert '  > pick: "b"' in md
    assert md.index('  > note: "Keep it"') < md.index('  > pick: "b"')
    assert "- **[admin]** — 2 keys" in md


def test_markdown_truncates_a_long_value_and_keeps_the_quote(tmp_path):
    data, r = _review(tmp_path)
    r["feedback"] = _submit(r, {"admin": {"long": "x" * 400}})
    md = feedback.render_decisions_markdown(data, r)
    line = next(ln for ln in md.splitlines() if ln.startswith("  > long:"))
    assert line.endswith("…")
    assert len(line) <= feedback.PAGE_VALUE_RENDER + len("  > long: ")


def test_markdown_says_none_when_no_page_state_was_sent(tmp_path):
    data, r = _review(tmp_path)
    r["feedback"] = _submit(r, {})
    md = feedback.render_decisions_markdown(data, r)
    assert "## Page state (0 blocks)" in md
    assert "_none_" in md.split("## Page state (0 blocks)", 1)[1].split("## Pins", 1)[0]


def test_short_summary_gets_one_page_state_line_and_still_respects_limit(tmp_path):
    data, r = _review(tmp_path)
    r["feedback"] = _submit(r, {"admin": {"pick": "b"}})
    s = feedback.short_decisions_summary(data, r)
    assert "**Page state:** 1 block" in s
    small = feedback.short_decisions_summary(data, r, limit=40)
    assert len(small) <= 40 and small.endswith("…")
