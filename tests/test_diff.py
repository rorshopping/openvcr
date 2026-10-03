import json

import pytest

from openvcr.diff import (
    anthropic_stream_summary,
    diff_cassettes,
    openai_stream_summary,
    render_report,
)
from openvcr.cassette import Cassette, Exchange, compute_match_key, parse_sse_events


def make_exchange(key_seed, content, stream_events=None):
    body = {"messages": [{"role": "user", "content": key_seed}], "model": "m"}
    request = {"method": "POST", "path": "/v1/chat/completions",
               "headers": {"Content-Type": "application/json"}, **{"body": body}}
    if stream_events is not None:
        response = {"status": 200, "headers": {"Content-Type": "text/event-stream"},
                    "stream": stream_events}
    else:
        response = {"status": 200, "headers": {"Content-Type": "application/json"},
                    "body": {"choices": [{"message": {"role": "assistant", "content": content}}],
                             "id": "irrelevant", "created": 1, "usage": {"total_tokens": 10}}}
    return Exchange(recorded_at="2026-01-01T00:00:00Z", duration_ms=5,
                    key=compute_match_key("POST", "/v1/chat/completions",
                                          json.dumps(body).encode()),
                    request=request, response=response)


def write_cassette(path, exchanges):
    cassette = Cassette(path)
    for ex in exchanges:
        cassette.append(ex)


def test_diff_same_despite_volatile_fields(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    write_cassette(a, [make_exchange("q1", "Hello!")])
    write_cassette(b, [make_exchange("q1", "Hello!")])
    # perturb provider-volatile fields in b; diff must stay quiet
    recs = Cassette(b).load()
    recs[0].response["body"]["id"] = "chatcmpl-OTHER"
    recs[0].response["body"]["created"] = 999
    b.write_text("\n".join(r.to_json() for r in recs) + "\n")

    results, summary = diff_cassettes(a, b)
    assert summary["drift"] == 0 and summary["same"] == 1
    assert results[0].verdict == "SAME"


def test_diff_detects_content_drift(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    write_cassette(a, [make_exchange("q1", "Hello!")])
    write_cassette(b, [make_exchange("q1", "Hi there!")])
    results, summary = diff_cassettes(a, b)
    assert summary["drift"] == 1
    paths = [fd.path for fd in results[0].fields]
    assert any("content" in p for p in paths)
    assert "Hello!" in render_report(a, b, results, summary)


def test_diff_ignores_chunk_boundaries_in_streams(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"

    def chunks(*words):
        out = []
        for w in words:
            out.append("data: " + json.dumps(
                {"choices": [{"delta": {"content": w}}]}))
        out.append("data: [DONE]")
        return out

    write_cassette(a, [make_exchange("q1", None, stream_events=chunks("Hel", "lo!"))])
    write_cassette(b, [make_exchange("q1", None, stream_events=chunks("Hello", "!"))])
    results, summary = diff_cassettes(a, b)
    assert summary["drift"] == 0  # same reconstructed text, different chunking


def test_diff_detects_stream_text_drift(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"

    def chunks(word):
        return ["data: " + json.dumps({"choices": [{"delta": {"content": word}}]}),
                "data: [DONE]"]

    write_cassette(a, [make_exchange("q1", None, stream_events=chunks("Paris"))])
    write_cassette(b, [make_exchange("q1", None, stream_events=chunks("Lyon"))])
    results, summary = diff_cassettes(a, b)
    assert summary["drift"] == 1


def test_diff_reports_missing_exchanges(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    write_cassette(a, [make_exchange("q1", "one"), make_exchange("q2", "two")])
    write_cassette(b, [make_exchange("q1", "one")])
    results, summary = diff_cassettes(a, b)
    assert summary["missing_in_b"] == 1
    assert summary["drift"] == 0


def test_openai_stream_summary_reconstructs_text_and_tool_calls():
    events = [
        "data: " + json.dumps({"choices": [{"delta": {"role": "assistant", "content": "Hel"}}]}),
        "data: " + json.dumps({"choices": [{"delta": {"content": "lo"}}]}),
        "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "get_weather", "arguments": "{\"city\""}}]}}]}),
        "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": ":\"Paris\"}"}}]}}]}),
        "data: [DONE]",
    ]
    summary = openai_stream_summary(events)
    assert summary["text"] == "Hello"
    assert summary["tool_calls"] == [
        {"id": "call_1", "name": "get_weather", "arguments": "{\"city\":\"Paris\"}"}]


def test_anthropic_stream_summary():
    events = [
        "data: " + json.dumps({"type": "content_block_delta",
                               "delta": {"type": "text_delta", "text": "Bon"}}),
        "data: " + json.dumps({"type": "content_block_delta",
                               "delta": {"type": "text_delta", "text": "jour"}}),
    ]
    assert anthropic_stream_summary(events)["text"] == "Bonjour"


def test_parse_sse_events_roundtrip():
    raw = b'data: {"x": 1}\n\ndata: [DONE]\n\n'
    assert parse_sse_events(raw) == ['data: {"x": 1}', "data: [DONE]"]
