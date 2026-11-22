"""The wake prompt: one text for both ways a board wakes a chat.

A webhook route (a messaging chat) has Hermes render `PROMPT_TEMPLATE` from the signed payload; `setup.py` subscribes
the route with it. A Desktop chat is woken inside its own session (sessionwake.py), and `render` fills the same
template from the same payload with Hermes's own rule, so both sessions read the same words. Reviewer text reaches the
prompt only through the payload fields Hermes would interpolate too (`summary`, already cut to one line).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

# The one line a woken session without the tools to build posts instead (Hermes runs webhook sessions with its safe
# webhook tools unless wake_toolsets grants more). The origin chat's own session builds the round.
HANDOFF_LINE = "Round {round} feedback is saved; the build continues in this thread's chat session."

# The wake prompt Hermes renders for an inbound round or an @Hermes thread mention. `{...}` placeholders are filled
# from the payload; `{summary}` says what a mention wake is about and which thread it names.
PROMPT_TEMPLATE = (
    "Mock review wake: {event_type} for {review_id} round {round} — {title}. "
    "If event_type is review.thread_mention, a reviewer mentioned Hermes in a comment thread: {summary} "
    "Read it with mock_review_list_comments(review_id='{review_id}'). When the comment asks for a change, patch "
    "one block in place FIRST with mock_review_patch_block (pass the round and the rev you read, and the wake "
    "payload's request_id as request_ids=[request_id]); then answer in that thread with "
    "mock_review_reply(thread_id=..., text=...). A question that needs no patch goes straight to the answer. "
    "Mark it resolved with mock_review_resolve(thread_id=...) when you are done. "
    "If event_type is review.submitted, read the feedback with "
    "mock_review_feedback(review_id='{review_id}', round={round}), then act on the notes, or build if Build was "
    "pressed. "
    "A next round asks only what is still open: approved blocks and answered questions go in a `settled` block, "
    "never back as questions. "
    "If this session has no mock_review tools or cannot write files, do not explain what is missing: answer "
    f"with exactly this one line and stop: {HANDOFF_LINE} "
    "Board: {review_url}"
)

# Hermes's placeholder rule (gateway/platforms/webhook.py `_TEMPLATE_KEY_RE`).
_KEY_RE = re.compile(r"\{([a-zA-Z0-9_.]+)\}")


def render(template: str, payload: Dict[str, Any]) -> str:
    """Fill `template` from `payload` the way Hermes's webhook adapter renders a route prompt (`_render_prompt`):
    `{a.b}` walks nested mappings, an unknown key stays as written, a mapping or list becomes indented JSON (at most
    2000 characters), `{__raw__}` is the whole payload as JSON (at most 4000) and `{event_type}` is the payload's
    event."""
    event_type = str(payload.get("event_type") or "")

    def resolve(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key == "__raw__":
            return json.dumps(payload, indent=2)[:4000]
        if key == "event_type":
            return event_type
        value: Any = payload
        for part in key.split("."):
            if not isinstance(value, dict):
                return "{" + key + "}"
            value = value.get(part, "{" + key + "}")
        return json.dumps(value, indent=2)[:2000] if isinstance(value, (dict, list)) else str(value)

    return _KEY_RE.sub(resolve, template)
