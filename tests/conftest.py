"""Shared test fixtures: a miniature OpenAI-shaped upstream we can record from."""

import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).parent))

from fake_upstream import CHAT_PATH, make_app  # noqa: E402


@pytest.fixture
async def upstream_server():
    """A fake OpenAI API: deterministic JSON + SSE completions keyed by prompt."""
    app = make_app()
    server = TestServer(app)
    await server.start_server()
    yield server
    await server.close()
