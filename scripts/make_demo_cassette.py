#!/usr/bin/env python3
"""Regenerate the bundled demo cassette (src/openvcr/example_cassettes/).

Records against the tests' fake OpenAI upstream so the demo works with zero
API keys, then normalizes timestamps so the artifact is reproducible.
"""

import asyncio
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

from aiohttp import ClientSession

from fake_upstream import CHAT_PATH, make_app
from openvcr.cassette import Cassette, parse_sse_events
from openvcr.proxy import RecordServer, ServerHandle

OUT = ROOT / "src" / "openvcr" / "example_cassettes" / "openai-chat-demo.cassette.jsonl"
FIXED_TS = "2026-10-03T09:14:22Z"

PROMPTS = [
    ("Say hello.", False),
    ("What is the capital of France?", False),
    ("Say hello.", True),  # streaming
]


async def main() -> None:
    from aiohttp.test_utils import TestServer
    upstream = TestServer(make_app())
    await upstream.start_server()
    OUT.unlink(missing_ok=True)
    try:
        recorder = RecordServer(
            f"http://{upstream.host}:{upstream.port}", Cassette(OUT))
        with ServerHandle(recorder) as proxy:
            async with ClientSession() as http:
                for prompt, stream in PROMPTS:
                    body = {"model": "gpt-4o",
                            "messages": [{"role": "user", "content": prompt}]}
                    if stream:
                        body["stream"] = True
                    async with http.post(f"{proxy.base_url}{CHAT_PATH}", json=body) as resp:
                        assert resp.status == 200, resp.status
                        if stream:
                            assert len(parse_sse_events(await resp.read())) > 2
                        else:
                            await resp.json()
    finally:
        await upstream.close()

    # Normalize for reproducibility: fixed timestamps, stable duration.
    records = Cassette(OUT).load()
    lines = []
    for rec in records:
        rec.recorded_at = FIXED_TS
        rec.duration_ms = 412 if not rec.response.get("stream") else 1287
        lines.append(rec.to_json())
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(records)} exchanges -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
