"""Convert reviews created by the removed here.now hosting to local or tailnet (2.0.0).

A review from 1.x is migrated when its stored mode is `herenow` or when it still carries a `herenow` sub-record. A
legacy `herenow` review takes the target mode (`local` or `tailscale`); any other review keeps its own mode and only
loses the sub-record, and no other field changes except `updated_at`. The command is idempotent: a second run finds
nothing to change.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import config, owner, settings, store
from .store import ReviewError

# Settings an earlier release wrote and 2.0.0 no longer has. `hermes mockreview setup` unsets every one it finds.
OBSOLETE_KEYS = ("herenow_credentials", "subscriptions_file", "herenow_publish_script")

LEGACY_MODE = "herenow"    # the 1.x hosting mode a review's `mode` can still hold
LEGACY_KEY = "herenow"     # the sub-record a 1.x review carries alongside it


def _load(review_id: str) -> Dict[str, Any]:
    try:
        data = store.load(review_id)
    except (ReviewError, ValueError, OSError) as exc:
        raise ReviewError(f"cannot migrate review {review_id}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReviewError(f"cannot migrate review {review_id}: review.json is not a JSON object")
    return data


def _target_from_host(host: str) -> str:
    return "tailscale" if host == "tailnet" else "local"


def target_mode(mode: Optional[str] = None) -> str:
    """`--mode local|tailnet` when given, else the resolved default_host (tailnet -> `tailscale`, else `local`)."""
    if mode is None or mode == "":
        return _target_from_host(settings.get("default_host"))
    value = str(mode).strip().lower()
    if value in ("local", "lan"):
        return "local"
    if value in ("tailnet", "tailscale"):
        return "tailscale"
    raise ReviewError(f"migrate --mode must be local or tailnet, got {mode!r}")


def needs_migration(data: Dict[str, Any]) -> bool:
    return data.get("mode") == LEGACY_MODE or LEGACY_KEY in data


def _migrated(data: Dict[str, Any], target: str) -> Dict[str, Any]:
    out = dict(data)
    mode = out.get("mode")
    out["mode"] = mode if mode in store.MODES else target
    out.pop(LEGACY_KEY, None)
    return out


def run(mode: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
    """Rewrite every here.now review in the watched store. Read-only under `dry_run`."""
    target = target_mode(mode)
    with owner.bound(owner.resolve_soft()):
        root = config.data_root().resolve()
        ids = store.list_ids()
        loaded = {review_id: _load(review_id) for review_id in ids}   # every record read and validated before any write
        changes = []
        for review_id in ids:
            data = loaded[review_id]
            if not needs_migration(data):
                continue
            before = data.get("mode")
            if dry_run:
                after = _migrated(data, target)
            else:
                with store.locked(review_id):
                    data = _load(review_id)
                    if not needs_migration(data):
                        continue
                    before = data.get("mode")
                    after = _migrated(data, target)
                    store.save(review_id, after)
            changes.append({"review_id": review_id, "from": before, "to": after["mode"],
                            "url": store.review_url(after)})
        return {"data_root": str(root), "dry_run": bool(dry_run), "target": target,
                "changes": changes, "changed": len(changes)}
