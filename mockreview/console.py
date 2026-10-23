"""Keep command output usable on consoles with a legacy encoding."""

import contextlib
import io
import sys


@contextlib.contextmanager
def safe_output():
    """Replace unrepresentable characters for this call; leave embedding/capture streams alone."""
    changed = []
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            previous = stream.errors
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, OSError, ValueError):
                continue
            changed.append((stream, previous))
    try:
        yield
    finally:
        for stream, previous in reversed(changed):
            try:
                stream.reconfigure(errors=previous)
            except (AttributeError, OSError, ValueError):
                pass
