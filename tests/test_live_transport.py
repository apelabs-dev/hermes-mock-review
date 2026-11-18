"""Hold actual wfile methods after Output's lock/closed check, with a real socket."""
from concurrent.futures import ThreadPoolExecutor
import socket
import threading
import time
from types import SimpleNamespace

import pytest
from mockreview import sse


@pytest.mark.parametrize('phase', ['hello','reset','presence','thread.created','thread.replied'])
@pytest.mark.parametrize('boundary', ['write','flush'])
def test_actual_wfile_boundary_is_fenced(phase, boundary):
    left,right = socket.socketpair()
    right.settimeout(1)
    entered, release = threading.Event(), threading.Event()
    output = sse.Output()
    class Writer:
        def write(self,data):
            if boundary == 'write':
                entered.set()
                assert release.wait(5)
            left.sendall(data)
        def flush(self):
            if boundary == 'flush':
                entered.set()
                assert release.wait(5)
    handler = SimpleNamespace(connection=left,wfile=Writer())
    output.bind(handler)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(sse.write,handler,'PRIVATE ALREADY ASSEMBLED',event=phase)
        try:
            assert entered.wait(2)
            # For flush the previous write was already accepted before restriction. It cannot be un-sent.
            before=right.recv(4096) if boundary=='flush' else b''
            start=time.monotonic()
            output.close()
            elapsed=time.monotonic()-start
            assert elapsed < sse.CLOSE_WAIT + .5
            release.set()
            result=pending.result(timeout=1)
            after=right.recv(4096)
            assert after == b'', 'new bytes after close at the actual wfile boundary'
            assert sse.write(handler,'NEW AFTER CLOSE',event=phase) is False
            if boundary=='write':
                assert result is False
            else:
                assert b'PRIVATE ALREADY ASSEMBLED' in before
            print('WFILE_FENCE',boundary,phase,'seconds',round(elapsed,3),'new_bytes',len(after))
        finally:
            release.set()
            left.close()
            right.close()
