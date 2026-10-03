"""End-to-end: record through the proxy from the fake upstream, then replay
and verify the client sees byte-identical behavior — including SSE streams —
and that no credentials leak into the cassette."""

import json

import aiohttp

from openvcr.cassette import Cassette, parse_sse_events
from openvcr.proxy import RecordServer, ReplayServer, ServerHandle
from fake_upstream import CHAT_PATH, REPLIES


def _chat_body(prompt: str, stream: bool = False) -> bytes:
    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
    }
    if stream:
        body["stream"] = True
    return json.dumps(body).encode()


async def test_record_then_replay_json(upstream_server, tmp_path):
    cassette_path = tmp_path / "flow.cassette.jsonl"
    body = _chat_body("What is the capital of France?")

    # -- record -------------------------------------------------------------
    recorder = RecordServer(f"http://{upstream_server.host}:{upstream_server.port}",
                            Cassette(cassette_path))
    with ServerHandle(recorder) as proxy:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"{proxy.base_url}{CHAT_PATH}",
                data=body,
                headers={"Authorization": "Bearer sk-proj-abcdefghijklmnopqrs123",
                         "Content-Type": "application/json"},
            ) as resp:
                recorded_response = await resp.json()
                assert resp.status == 200

    assert recorder.record_count == 1

    # cassette must contain the exchange but NOT the credentials
    records = Cassette(cassette_path).load()
    assert len(records) == 1
    rec = records[0]
    assert rec.request["headers"].get("Authorization") is None
    assert "Bearer" not in json.dumps(rec.request)
    assert rec.response["body"]["choices"][0]["message"]["content"] == \
        "The capital of France is Paris."
    assert rec.duration_ms >= 0

    # -- replay -------------------------------------------------------------
    player = ReplayServer(Cassette(cassette_path))
    with ServerHandle(player) as proxy:
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{proxy.base_url}{CHAT_PATH}", data=body) as resp:
                replayed = await resp.json()
                assert resp.status == 200
                assert resp.content_type == "application/json"

    assert replayed == recorded_response


async def test_record_then_replay_streaming(upstream_server, tmp_path):
    cassette_path = tmp_path / "stream.cassette.jsonl"
    body = _chat_body("Say hello.", stream=True)
    expected_chunks = REPLIES["Say hello."][1]

    recorder = RecordServer(f"http://{upstream_server.host}:{upstream_server.port}",
                            Cassette(cassette_path))
    with ServerHandle(recorder) as proxy:
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{proxy.base_url}{CHAT_PATH}", data=body) as resp:
                assert resp.status == 200
                assert resp.content_type == "text/event-stream"
                live_events = parse_sse_events(await resp.read())

    records = Cassette(cassette_path).load()
    assert len(records) == 1
    stored = records[0].response["stream"]
    # recorded stream == what the client saw (minus the terminal [DONE] marker
    # from the fake upstream, which is included too)
    assert stored == live_events
    data_payloads = [e[5:].strip() for e in stored if e.startswith("data: ")]
    assert data_payloads[:-1] == [json.dumps(c) for c in expected_chunks]
    assert data_payloads[-1] == "[DONE]"

    player = ReplayServer(Cassette(cassette_path))
    with ServerHandle(player) as proxy:
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{proxy.base_url}{CHAT_PATH}", data=body) as resp:
                assert resp.content_type == "text/event-stream"
                replayed_events = parse_sse_events(await resp.read())

    assert replayed_events == live_events


async def test_replay_no_match_returns_helpful_409(upstream_server, tmp_path):
    cassette_path = tmp_path / "solo.cassette.jsonl"
    recorder = RecordServer(
        f"http://{upstream_server.host}:{upstream_server.port}",
        Cassette(cassette_path))
    with ServerHandle(recorder) as proxy:
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{proxy.base_url}{CHAT_PATH}",
                                 data=_chat_body("What is the capital of France?")) as resp:
                assert resp.status == 200

    player = ReplayServer(Cassette(cassette_path))
    with ServerHandle(player) as proxy:
        async with aiohttp.ClientSession() as http:
            different = _chat_body("Unheard question")
            async with http.post(f"{proxy.base_url}{CHAT_PATH}", data=different) as resp:
                assert resp.status == 409
                payload = await resp.json()
                assert payload["error"]["type"] == "openvcr_no_match"
                assert "Re-record" in payload["error"]["message"]


async def test_replay_ignore_field_loosens_matching(upstream_server, tmp_path):
    cassette_path = tmp_path / "ignore.cassette.jsonl"
    recorder = RecordServer(f"http://{upstream_server.host}:{upstream_server.port}",
                            Cassette(cassette_path))
    body_a = json.dumps({"model": "gpt-4o", "seed": 1,
                         "messages": [{"role": "user", "content": "Say hello."}]}).encode()
    body_b = json.dumps({"model": "gpt-4o", "seed": 99,
                         "messages": [{"role": "user", "content": "Say hello."}]}).encode()
    with ServerHandle(recorder) as proxy:
        async with aiohttp.ClientSession() as http:
            await http.post(f"{proxy.base_url}{CHAT_PATH}", data=body_a)

    strict = ReplayServer(Cassette(cassette_path))
    with ServerHandle(strict) as proxy:
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{proxy.base_url}{CHAT_PATH}", data=body_b) as resp:
                assert resp.status == 409  # seed differs -> no match

    loose = ReplayServer(Cassette(cassette_path), ignore_fields=frozenset({"seed"}))
    with ServerHandle(loose) as proxy:
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{proxy.base_url}{CHAT_PATH}", data=body_b) as resp:
                assert resp.status == 200  # seed ignored -> match
