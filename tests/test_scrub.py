from openvcr.scrub import (
    REDACTED,
    redact_json,
    redact_text,
    scrub_request_headers,
    scrub_response_headers,
)


def test_request_header_allowlist():
    headers = {
        "Authorization": "Bearer sk-realtoken1234567890",
        "Content-Type": "application/json",
        "X-Api-Key": "secret",
        "OpenAI-Organization": "org-xyz",
        "Accept": "application/json",
        "User-Agent": "my-agent/1.0",
    }
    kept = scrub_request_headers(headers)
    assert kept == {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "my-agent/1.0",
    }


def test_response_header_allowlist():
    kept = scrub_response_headers({"Content-Type": "application/json",
                                   "X-Request-Id": "abc", "Set-Cookie": "session=1"})
    assert kept == {"Content-Type": "application/json"}


def test_redact_openai_key():
    text = "my key is sk-proj-abcdefghijklmnopqrs1234567890 ok"
    assert "sk-proj-" not in redact_text(text)
    assert REDACTED in redact_text(text)


def test_redact_anthropic_and_github():
    text = "sk-ant-api03-aaaaaaaaaaaaaaaaaaaa and ghp_abcdefghijklmnopqrstuvwx"
    out = redact_text(text)
    assert "sk-ant" not in out and "ghp_" not in out


def test_redact_bearer_and_jwt():
    text = "Authorization: Bearer abcdef1234567890abcdef and eyJhbGciOiJIUzI1NiJ9.e30.abc123signature"
    out = redact_text(text)
    assert "abcdef1234567890abcdef" not in out
    assert "eyJ" not in out


def test_redact_nested_json():
    obj = {"messages": [{"content": "call with sk-proj-abcdefghijklmnopqrs123"}],
           "meta": {"nested": {"deep": ["xoxb-123456789012-abc"]}}}
    out = redact_json(obj)
    assert "sk-proj" not in str(out)
    assert "xoxb" not in str(out)


def test_redact_custom_pattern():
    out = redact_text("internal-secret-xyz-9876", extra_patterns=[__import__("re").compile(r"internal-secret-\w+-\d+")])
    assert REDACTED in out
