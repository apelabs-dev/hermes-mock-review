"""Slow-client limits: per-connection timeout and the concurrent-connection cap."""

import logging
import socket
import threading
import time

import pytest

from mockreview import server as srv


def start(host="127.0.0.1"):
    httpd = srv.make_server(host, 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return httpd


def stop(httpd):
    httpd.shutdown()
    httpd.server_close()


def wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def free_slots(httpd):
    return httpd._slots._value


def healthz(port, timeout=5.0):
    """Status line of GET /healthz on a fresh connection, or '' when the server closed it unanswered."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.settimeout(timeout)
        try:
            s.sendall(f"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
            resp = b""
            while chunk := s.recv(4096):
                resp += chunk
        except ConnectionError:      # reset, aborted (WinError 10053) or broken pipe: closed unanswered
            return ""
    return resp.split(b"\r\n", 1)[0].decode()


def idle(port):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.settimeout(5)
    return s


def test_handler_timeout_is_set():
    assert srv.REQUEST_TIMEOUT_S == 30
    assert srv.Handler.timeout == srv.REQUEST_TIMEOUT_S


def test_both_server_classes_are_capped():
    assert issubclass(srv.BoardHTTPServer6, srv.BoardHTTPServer)
    assert srv.BoardHTTPServer.daemon_threads is True
    try:
        httpd = srv.make_server("::1", 0)
    except OSError:
        pytest.skip("no IPv6 loopback here")
    try:
        assert httpd.address_family == socket.AF_INET6 and httpd.max_connections == srv.MAX_CONNECTIONS == 64
    finally:
        httpd.server_close()


def test_connection_cap(env, monkeypatch, caplog):
    monkeypatch.setattr(srv, "MAX_CONNECTIONS", 2)
    httpd = start()
    port = httpd.server_address[1]
    held = []
    try:
        held = [idle(port), idle(port)]
        assert wait_for(lambda: free_slots(httpd) == 0), "the two idle connections were not accepted"
        with caplog.at_level(logging.WARNING, logger="mockreview.server"):
            t0 = time.monotonic()
            assert healthz(port, timeout=3) == ""        # closed unanswered, not left hanging
            assert healthz(port, timeout=3) == ""
            assert time.monotonic() - t0 < 3
        warnings = [r for r in caplog.records if "connections open" in r.getMessage()]
        assert len(warnings) == 1                         # rate-limited: one a minute
        for s in held:
            s.close()
        held = []
        assert wait_for(lambda: free_slots(httpd) == 2), "slots not released after the clients left"
        assert healthz(port) == "HTTP/1.1 200 OK"
    finally:
        for s in held:
            s.close()
        stop(httpd)


def test_silent_client_does_not_block_others(env, monkeypatch):
    monkeypatch.setattr(srv.Handler, "timeout", 1)
    httpd = start()
    port = httpd.server_address[1]
    silent = idle(port)
    try:
        silent.sendall(b"GET /healthz HTTP/1.1\r\n")     # a request line, then nothing
        t0 = time.monotonic()
        assert healthz(port) == "HTTP/1.1 200 OK"
        assert time.monotonic() - t0 < 1                  # answered while the silent one still holds its thread
        try:
            assert silent.recv(4096) == b""               # the server hung up on it after ~1 s
        except ConnectionError:
            pass
        assert time.monotonic() - t0 < 4
        assert wait_for(lambda: free_slots(httpd) == srv.MAX_CONNECTIONS)
    finally:
        silent.close()
        stop(httpd)
