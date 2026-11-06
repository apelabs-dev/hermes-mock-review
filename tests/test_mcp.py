"""The stdio MCP server: protocol shapes in-process, then the real subprocess and the bin script."""

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT, load_plugin, python_child_env
from mockreview import mcp, service, store
from test_store import _decisions

FB = {"marks": {"recap": {"status": "approve"}, "d1": {"status": "change", "note": "smaller"}},
      "pins": [], "guidelines": "Keep it simple."}


# ---------------------------------------------------------------- helpers

def request(method, params=None, request_id=1):
    req = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        req["params"] = params
    return req


def notification(method, params=None):
    req = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        req["params"] = params
    return req


def call(name, arguments, options=None):
    res = mcp.handle_request(request("tools/call", {"name": name, "arguments": arguments}), options or mcp.Options())
    assert "result" in res, res
    return res["result"]


def payload(result):
    text = result["content"][0]["text"]
    assert result["content"][0]["type"] == "text"
    return json.loads(text)


def run_lines(lines, options=None):
    out = io.StringIO()
    code = mcp.serve(io.StringIO("".join(line if line.endswith("\n") else line + "\n" for line in lines)),
                     out, options or mcp.Options())
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


def plugin_yaml_version():
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    return re.search(r"^version:\s*([0-9][^\s#]*)", text, re.MULTILINE).group(1)


def child_env(tmp_path):
    env = python_child_env(tmp_path)
    env.update(PYTHONPATH=str(ROOT),
               MOCKREVIEW_HOME=str(tmp_path / "data"),
               MOCKREVIEW_OWNER="self",
               MOCKREVIEW_CONFIG=str(tmp_path / "absent-config.yaml"))
    return env


PROTOCOL = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "test", "version": "1"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "mock_review_list", "arguments": {}}},
]


# ---------------------------------------------------------------- initialize and notifications

def test_initialize_echoes_a_supported_version():
    res = mcp.handle_request(request("initialize", {"protocolVersion": "2025-06-18",
                                                    "clientInfo": {"name": "test", "version": "1"}}),
                             mcp.Options())
    assert res["jsonrpc"] == "2.0" and res["id"] == 1
    result = res["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "mock-review"
    assert result["serverInfo"]["version"] == plugin_yaml_version()
    assert "tools" in result["capabilities"]
    assert "mock_review_create" in result["instructions"]


def test_initialize_falls_back_for_an_unsupported_version():
    res = mcp.handle_request(request("initialize", {"protocolVersion": "1999-01-01"}), mcp.Options())
    assert res["result"]["protocolVersion"] == "2025-11-25"


def test_notifications_get_no_response():
    for method in ("notifications/initialized", "notifications/cancelled", "something/else"):
        assert mcp.handle_request(notification(method), mcp.Options()) is None
    code, responses = run_lines([json.dumps(notification("notifications/initialized")),
                                 json.dumps(request("ping", request_id=7))])
    assert code == 0 and [r["id"] for r in responses] == [7]


def test_ping_returns_an_empty_result():
    res = mcp.handle_request(request("ping"), mcp.Options())
    assert res["result"] == {}


# ---------------------------------------------------------------- tools/list

def test_tools_list_has_exactly_the_five_tools_with_object_schemas():
    res = mcp.handle_request(request("tools/list"), mcp.Options())
    listed = res["result"]["tools"]
    assert [t["name"] for t in listed] == list(mcp.TOOL_NAMES)
    assert len(listed) == 5
    for tool in listed:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict) and "required" in schema
        assert tool["description"]


def test_look_keys_come_from_settings_spec():
    listed = {t["name"]: t for t in mcp.handle_request(request("tools/list"), mcp.Options())["result"]["tools"]}
    create = listed["mock_review_create"]["inputSchema"]["properties"]
    for name in ("devices", "default_device", "default_theme", "show_theme_toggle", "pins_enabled",
                 "require_note_on_change", "max_pins", "mobile_width", "tablet_width"):
        assert name in create
    assert create["devices"]["type"] == "array" and create["devices"]["items"] == {"type": "string"}
    assert create["max_pins"]["type"] == "integer"
    assert create["default_device"]["enum"] == ["auto", "desktop", "tablet", "mobile"]
    assert create["mode"]["enum"] == ["tailnet", "tailscale", "local", "lan", "herenow"]


def test_tool_names_match_the_plugin_schemas():
    plugin_mod = load_plugin()
    assert set(plugin_mod.schemas(False, "local", False)) == set(mcp.TOOL_NAMES)


# ---------------------------------------------------------------- JSON-RPC errors

def test_unknown_tool_is_a_tool_error_not_a_protocol_error():
    res = call("mock_review_nope", {})
    assert res["isError"] is True
    assert "unknown tool" in payload(res)["error"]


def test_malformed_json_gets_32700_and_the_next_line_is_answered():
    code, responses = run_lines(["this is not json", json.dumps(request("ping", request_id=2))])
    assert code == 0
    assert responses[0]["error"]["code"] == -32700 and responses[0]["id"] is None
    assert responses[1]["id"] == 2 and responses[1]["result"] == {}


def test_unknown_method_and_bad_requests():
    res = mcp.handle_request(request("server/discover"), mcp.Options())
    assert res["error"]["code"] == -32601
    assert mcp.handle_request([1, 2, 3], mcp.Options())["error"]["code"] == -32600
    res = mcp.handle_request({"jsonrpc": "2.0", "id": 9}, mcp.Options())
    assert res["error"]["code"] == -32600 and res["id"] == 9


def test_bad_params_get_32602():
    res = mcp.handle_request(request("tools/call", []), mcp.Options())
    assert res["error"]["code"] == -32602
    res = mcp.handle_request(request("tools/call", {"arguments": {}}), mcp.Options())
    assert res["error"]["code"] == -32602
    res = mcp.handle_request(request("tools/call", {"name": "mock_review_list", "arguments": []}), mcp.Options())
    assert res["error"]["code"] == -32602


# ---------------------------------------------------------------- the tools against a temp store

def test_create_with_defaults_is_a_wake_off_board(env, tmp_path):
    res = call("mock_review_create", {"title": "MCP board", "decisions": str(_decisions(tmp_path))})
    assert res["isError"] is False
    body = payload(res)
    assert body["review_id"] and body["url"]
    assert body["wake"] is False
    assert store.load(body["review_id"])["origin"] == {}


def test_create_with_wake_and_no_origin_names_origin_platform(env, tmp_path):
    res = call("mock_review_create", {"title": "Wake me", "wake": True,
                                      "decisions": str(_decisions(tmp_path))})
    assert res["isError"] is True
    message = payload(res)["error"]
    assert "origin_platform" in message and "origin_chat_id" in message


def test_create_uses_server_options_for_the_origin(env, tmp_path):
    options = mcp.Options(platform="discord", chat_id="test-chat", wake=True)
    res = call("mock_review_create", {"title": "Option board", "decisions": str(_decisions(tmp_path))}, options)
    assert res["isError"] is False, payload(res)
    review_id = payload(res)["review_id"]
    assert store.load(review_id)["origin"] == {"platform": "discord", "chat_id": "test-chat"}


def test_tool_arguments_override_server_options(env, tmp_path):
    options = mcp.Options(platform="discord", chat_id="test-chat", wake=True)
    res = call("mock_review_create", {"title": "Arg board", "decisions": str(_decisions(tmp_path)),
                                      "origin_platform": "telegram", "origin_chat_id": "c1"}, options)
    review_id = payload(res)["review_id"]
    assert store.load(review_id)["origin"] == {"platform": "telegram", "chat_id": "c1"}


def test_feedback_ack_list_and_add_round_round_trip(env, tmp_path):
    decisions = _decisions(tmp_path)
    created = payload(call("mock_review_create", {"title": "Round trip", "decisions": str(decisions)}))
    review_id = created["review_id"]

    without_feedback = payload(call("mock_review_feedback", {"review_id": review_id}))
    assert without_feedback["round"] == 1 and without_feedback["feedback"] is None
    missing = call("mock_review_ack", {"review_id": review_id})
    assert missing["isError"] is True and "no submitted feedback" in payload(missing)["error"]

    data = store.load(review_id)
    service.submit(review_id, 1, FB, data["token"], do_notify=False)

    feedback = payload(call("mock_review_feedback", {"review_id": review_id}))
    assert feedback["feedback"]["marks"]["d1"]["note"] == "smaller"
    assert feedback["markdown"]
    acked = payload(call("mock_review_ack", {"review_id": review_id, "round": 1}))
    assert acked["round"] == 1 and acked["acked_at"]

    rows = payload(call("mock_review_list", {}))["reviews"]
    assert [r["review_id"] for r in rows] == [review_id]

    added = payload(call("mock_review_add_round", {"review_id": review_id, "decisions": str(decisions)}))
    assert added["round"] == 2 and len(store.load(review_id)["rounds"]) == 2


def test_missing_required_arguments_are_tool_errors(env):
    for name in ("mock_review_feedback", "mock_review_ack", "mock_review_add_round"):
        res = call(name, {})
        assert res["isError"] is True and "required" in payload(res)["error"]


# ---------------------------------------------------------------- real subprocesses

def _speak(argv, tmp_path):
    proc = subprocess.run(argv, input="".join(json.dumps(r) + "\n" for r in PROTOCOL),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          cwd=str(ROOT), env=child_env(tmp_path), timeout=90)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines, proc.stderr
    responses = [json.loads(line) for line in lines]      # every stdout line is JSON, no banner text
    return responses, proc.stderr


def test_module_subprocess_speaks_only_json_on_stdout(tmp_path):
    responses, stderr = _speak([sys.executable, "-m", "mockreview.mcp",
                                "--platform", "discord", "--chat-id", "test-chat"], tmp_path)
    assert [r["id"] for r in responses] == [1, 2, 3]
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    assert [t["name"] for t in responses[1]["result"]["tools"]] == list(mcp.TOOL_NAMES)
    listed = payload(responses[2]["result"])
    assert listed == {"reviews": []}
    assert "initialize: requested protocolVersion" in stderr


@pytest.mark.skipif(os.name == "nt", reason="the checkout script is a POSIX shebang script")
def test_bin_script_speaks_the_same_protocol(tmp_path):
    script = ROOT / "bin" / "mockreview"
    if not os.access(str(script), os.X_OK):
        pytest.skip("bin/mockreview is not executable here")
    responses, _ = _speak([str(script), "mcp", "--platform", "discord", "--chat-id", "test-chat"], tmp_path)
    assert [r["id"] for r in responses] == [1, 2, 3]
    assert payload(responses[2]["result"]) == {"reviews": []}
