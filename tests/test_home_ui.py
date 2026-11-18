"""Home: reviews grouped by what needs the reader, with search, status badge, round and age."""

import json

from conftest import http
from mockreview import store

ORIGIN = {"platform": "discord", "chat_id": "test-chat"}


def make_review(tmp_path, variants, title, status="open", submission=None):
    spec = store.parse_variant_spec(variants[0])
    data = store.create_review(title, "", ORIGIN, "tailscale", [spec], wake=False)
    if submission is not None or status != "open":
        stored = store.load(data["id"])
        if submission is not None:
            stored["rounds"][0]["submission"] = submission
        stored["status"] = status
        store.save(data["id"], stored)
    return data


def test_reviews_are_grouped_by_what_needs_you(env, server, variants, tmp_path):
    make_review(tmp_path, variants, "Needs attention alpha")
    make_review(tmp_path, variants, "Waiting beta", submission={"status": "notified", "submitted_at": "2026-11-15T02:00:00Z"})
    make_review(tmp_path, variants, "Done gamma", status="closed")
    code, _, body = http("GET", server + "/")
    page = body.decode("utf-8")
    assert code == 200
    needs = page.index("Needs you")
    waiting = page.index("Waiting on Hermes")
    done = page.index("Done")
    assert needs < waiting < done, "home groups run needs you -> waiting on Hermes -> done"
    assert page.index("Needs attention alpha") < waiting
    assert waiting < page.index("Waiting beta") < done
    assert done < page.index("Done gamma")
    assert "Round 1 · updated" in page
    assert "Needs you" in page and "Hermes is building" in page and "Closed" in page


def test_search_filters_by_title_and_keeps_the_value_escaped(env, server, variants, tmp_path):
    make_review(tmp_path, variants, "Needs attention alpha")
    make_review(tmp_path, variants, "Waiting beta", submission={"status": "notified", "submitted_at": "2026-11-15T02:00:00Z"})
    code, _, body = http("GET", server + "/?q=beta")
    page = body.decode("utf-8")
    assert code == 200 and "Waiting beta" in page and "Needs attention alpha" not in page
    assert 'value="beta"' in page
    code, _, body = http("GET", server + "/?q=%3Cscript%3Ealert(1)%3C%2Fscript%3E")
    page = body.decode("utf-8")
    assert code == 200 and "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "No reviews match that search." in page
