"""Pytest integration: the `openvcr` fixture.

    def test_agent_replies(openvcr):
        with openvcr.replay("cassettes/agent.cassette.jsonl") as base_url:
            # OPENAI_BASE_URL / ANTHROPIC_BASE_URL now point at the replay
            # server; run any client code — it gets recorded responses.
            assert run_my_agent("hello").startswith("Hi")

    def test_record_new_flow(openvcr):
        with openvcr.record("https://api.openai.com",
                            "cassettes/agent.cassette.jsonl") as base_url:
            ...  # real calls happen, traffic is captured

Cassette paths are resolved relative to the test file that uses them.
Replay servers run on random free ports on 127.0.0.1 and shut down when the
context exits.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from .proxy import DEFAULT_PORT, RecordServer, ReplayServer, ServerHandle

_DUMMY_KEY = "openvcr-placeholder-key"


class OpenVCRFixture:
    def __init__(self, monkeypatch, base_dir: Path):
        self._monkeypatch = monkeypatch
        self._base_dir = base_dir

    def _resolve(self, cassette: str | Path) -> Path:
        path = Path(cassette)
        return path if path.is_absolute() else (self._base_dir / path).resolve()

    @staticmethod
    def _set_base_url_env(monkeypatch, base_url: str, fake_keys: bool):
        monkeypatch.setenv("OPENAI_BASE_URL", f"{base_url}/v1")
        monkeypatch.setenv("OPENAI_API_BASE", f"{base_url}/v1")  # legacy openai<1.0
        monkeypatch.setenv("ANTHROPIC_BASE_URL", base_url)
        if fake_keys:
            import os
            if not os.environ.get("OPENAI_API_KEY"):
                monkeypatch.setenv("OPENAI_API_KEY", _DUMMY_KEY)
            if not os.environ.get("ANTHROPIC_API_KEY"):
                monkeypatch.setenv("ANTHROPIC_API_KEY", _DUMMY_KEY)

    @contextlib.contextmanager
    def replay(self, cassette: str | Path, *, match: str = "hash",
               latency: str = "fast", ignore_fields: tuple = ()):
        from .cassette import Cassette
        path = self._resolve(cassette)
        server = ReplayServer(Cassette(path), mode=match, latency=latency,
                              ignore_fields=frozenset(ignore_fields))
        with ServerHandle(server) as handle:
            self._set_base_url_env(self._monkeypatch, handle.base_url, fake_keys=True)
            yield handle.base_url

    @contextlib.contextmanager
    def record(self, upstream: str, cassette: str | Path, *,
               scrub_body: bool = True, append: bool = False):
        from .cassette import Cassette
        path = self._resolve(cassette)
        if path.exists() and not append:
            raise AssertionError(
                f"cassette {path} already exists — pass append=True or delete it "
                f"(recordings must never be silently overwritten)")
        server = RecordServer(upstream, Cassette(path), scrub_body=scrub_body)
        with ServerHandle(server) as handle:
            self._set_base_url_env(self._monkeypatch, handle.base_url, fake_keys=False)
            yield handle.base_url


@pytest.fixture
def openvcr(request, monkeypatch, tmp_path):
    """Record/replay context managers bound to this test's directory."""
    test_dir = Path(request.fspath).parent
    return OpenVCRFixture(monkeypatch, Path(test_dir))
