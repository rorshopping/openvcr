import json

import pytest

from openvcr.cassette import (
    Cassette,
    Exchange,
    canonical_json,
    compute_match_key,
    decode_body,
    encode_body,
    normalize_path,
    parse_sse_events,
)


def test_normalize_path_strips_single_v1():
    assert normalize_path("/v1/chat/completions") == "/chat/completions"
    assert normalize_path("/chat/completions") == "/chat/completions"
    assert normalize_path("/v1/v1/x") == "/v1/x"
    assert normalize_path("chat/completions") == "/chat/completions"


def test_match_key_stable_across_key_order_and_whitespace():
    a = b'{"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}'
    b = b'{"messages":[{"content":"hi","role":"user"}],"model":"gpt-4o"}'
    assert compute_match_key("POST", "/v1/chat/completions", a) == \
        compute_match_key("POST", "/chat/completions", b)


def test_match_key_distinguishes_method_path_and_body():
    base = b'{"a": 1}'
    k1 = compute_match_key("POST", "/v1/x", base)
    k2 = compute_match_key("GET", "/v1/x", base)
    k3 = compute_match_key("POST", "/v1/y", base)
    k4 = compute_match_key("POST", "/v1/x", b'{"a": 2}')
    assert len({k1, k2, k3, k4}) == 4


def test_match_key_ignore_fields():
    body_a = json.dumps({"model": "m", "seed": 1}).encode()
    body_b = json.dumps({"model": "m", "seed": 2}).encode()
    assert compute_match_key("POST", "/x", body_a, frozenset({"seed"})) == \
        compute_match_key("POST", "/x", body_b, frozenset({"seed"}))


def test_canonical_json_drops_ignored_fields_recursively():
    obj = {"usage": {"total_tokens": 5, "prompt_tokens": 3}, "id": "x", "keep": 1}
    assert canonical_json(obj, frozenset({"id", "prompt_tokens"})) == '{"keep":1,"usage":{"total_tokens":5}}'


def test_body_encoding_roundtrip_binary():
    raw = bytes([0, 1, 2, 255, 254])
    encoded = encode_body(raw)
    assert "body_b64" in encoded
    assert decode_body(encoded.get("body"), encoded.get("body_b64")) == raw


def test_body_encoding_roundtrip_json():
    encoded = encode_body(b'{"hello": [1, 2]}')
    assert encoded == {"body": {"hello": [1, 2]}}
    assert json.loads(decode_body(encoded["body"], None)) == {"hello": [1, 2]}


def test_sse_parsing():
    raw = (b'data: {"a": 1}\n\ndata: {"b": 2}\n\ndata: [DONE]\n\n')
    events = parse_sse_events(raw)
    assert events == ['data: {"a": 1}', 'data: {"b": 2}', "data: [DONE]"]


def test_cassette_roundtrip_and_torn_line(tmp_path):
    path = tmp_path / "c.cassette.jsonl"
    cassette = Cassette(path)
    ex1 = Exchange(recorded_at="2026-01-01T00:00:00Z", duration_ms=10,
                   key="k1", request={"method": "POST", "path": "/v1/a", "body": {"x": 1}},
                   response={"status": 200, "body": {"ok": True}})
    ex2 = Exchange(recorded_at="2026-01-01T00:00:01Z", duration_ms=20,
                   key="k2", request={"method": "GET", "path": "/v1/b", "body": None},
                   response={"status": 200, "stream": ["data: hi"]})
    cassette.append(ex1)
    cassette.append(ex2)
    with path.open("a") as fh:  # simulate a crash mid-write
        fh.write('{"v": 1, "request": {"met')

    loaded = Cassette(path).load()
    assert len(loaded) == 2
    assert loaded[0].key == "k1"
    assert loaded[1].response["stream"] == ["data: hi"]
