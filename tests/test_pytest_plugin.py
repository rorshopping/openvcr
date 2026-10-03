"""The pytest plugin, tested through a real nested pytest run (pytester)."""

pytest_plugins = ["pytester"]


def test_replay_fixture_serves_recorded_traffic(pytester):
    # a cassette recorded against the fake upstream shape, stored next to the test
    import json
    from openvcr.cassette import Cassette, Exchange, compute_match_key
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    exchange = Exchange(
        recorded_at="2026-01-01T00:00:00Z", duration_ms=1,
        key=compute_match_key("POST", "/v1/chat/completions", json.dumps(body).encode()),
        request={"method": "POST", "path": "/v1/chat/completions", "headers": {}, "body": body},
        response={"status": 200, "headers": {"Content-Type": "application/json"},
                  "body": {"choices": [{"message": {"role": "assistant", "content": "recorded reply"}}]}})

    cassette = pytester.path / "cassettes" / "agent.cassette.jsonl"
    cassette.parent.mkdir()
    Cassette(cassette).append(exchange)

    pytester.makepyfile("""
        import json, urllib.request

        def test_agent_replies(openvcr):
            with openvcr.replay("cassettes/agent.cassette.jsonl") as base_url:
                assert "OPENAI_BASE_URL" in __import__("os").environ
                req = urllib.request.Request(
                    base_url + "/v1/chat/completions",
                    data=json.dumps({"model": "gpt-4o",
                                     "messages": [{"role": "user", "content": "hi"}]}).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req) as resp:
                    payload = json.loads(resp.read())
                assert payload["choices"][0]["message"]["content"] == "recorded reply"
    """)
    result = pytester.runpytest("-p", "openvcr")
    result.assert_outcomes(passed=1)


def test_replay_fixture_fails_on_no_match(pytester):
    import json
    from openvcr.cassette import Cassette, Exchange, compute_match_key
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    exchange = Exchange(
        recorded_at="2026-01-01T00:00:00Z", duration_ms=1,
        key=compute_match_key("POST", "/v1/chat/completions", json.dumps(body).encode()),
        request={"method": "POST", "path": "/v1/chat/completions", "headers": {}, "body": body},
        response={"status": 200, "headers": {}, "body": {}})
    cassette = pytester.path / "cassette.jsonl"
    Cassette(cassette).append(exchange)

    pytester.makepyfile("""
        import json, urllib.error, urllib.request

        def test_unmatched_request_raises(openvcr):
            with openvcr.replay("cassette.jsonl") as base_url:
                req = urllib.request.Request(
                    base_url + "/v1/chat/completions",
                    data=json.dumps({"model": "gpt-4o",
                                     "messages": [{"role": "user", "content": "different"}]}).encode(),
                    headers={"Content-Type": "application/json"})
                try:
                    urllib.request.urlopen(req)
                    raise AssertionError("expected HTTP 409")
                except urllib.error.HTTPError as exc:
                    assert exc.code == 409
    """)
    result = pytester.runpytest("-p", "openvcr")
    result.assert_outcomes(passed=1)


def test_record_fixture_refuses_overwrite(pytester):
    pytester.makepyfile("""
        import pytest

        def test_guard(openvcr, tmp_path):
            target = tmp_path / "c.cassette.jsonl"
            target.write_text("")
            with pytest.raises(AssertionError, match="already exists"):
                with openvcr.record("https://api.openai.com", target):
                    pass
    """)
    result = pytester.runpytest("-p", "openvcr")
    result.assert_outcomes(passed=1)
