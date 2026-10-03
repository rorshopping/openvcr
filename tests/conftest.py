"""Shared test fixtures: a miniature OpenAI-shaped upstream we can record from.

aiohttp is imported lazily inside fixtures so that test subsets which don't
need an HTTP stack (e.g. the waitlist API tests) can run with stdlib+pytest.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
async def upstream_server():
    """A fake OpenAI API: deterministic JSON + SSE completions keyed by prompt."""
    from aiohttp.test_utils import TestServer

    from fake_upstream import make_app

    app = make_app()
    server = TestServer(app)
    await server.start_server()
    yield server
    await server.close()
