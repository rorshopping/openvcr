"""Waitlist API tests — drives the exact dispatch() used in production."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

API = Path(__file__).resolve().parent.parent / "landing" / "api" / "waitlist.py"
spec = importlib.util.spec_from_file_location("waitlist_api", API)
waitlist_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(waitlist_api)


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(waitlist_api, "FALLBACK_PATH", tmp_path / "waitlist.jsonl")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.setattr(waitlist_api, "_rate", {})
    monkeypatch.setattr(waitlist_api, "_store_singleton", None)  # defeat caching
    monkeypatch.setattr(waitlist_api, "_store_kind", "unset")
    # fresh signed-token cache too
    monkeypatch.setattr(waitlist_api, "_signed_token_cache", {})


def signup(email="dev@example.com", **kw):
    body = {"email": email, "role": "builder", "building": "an agent", **kw}
    return waitlist_api.dispatch("POST", json.dumps(body).encode(), "1.2.3.4")


def test_valid_signup_returns_position():
    status, payload = signup()
    assert status == 200 and payload["ok"] is True and payload["position"] == 1
    status, payload = signup("second@example.com")
    assert payload["position"] == 2


def test_duplicate_email_is_idempotent():
    signup("dupe@example.com")
    signup("dupe@example.com")
    status, payload = waitlist_api.dispatch("GET", b"", "1.2.3.4")
    assert payload["count"] == 1


def test_invalid_email_rejected():
    status, payload = signup("not-an-email")
    assert status == 400 and "valid email" in payload["error"]["message"]
    status, _ = signup("missing@tld")
    assert status == 400


def test_honeypot_silently_accepts_without_storing():
    status, payload = signup(email="bot@spam.com", website="http://spam.example")
    assert status == 200
    _, count = waitlist_api.dispatch("GET", b"", "1.2.3.4")
    assert count["count"] == 0


def test_oversized_body_rejected():
    status, _ = waitlist_api.dispatch("POST", json.dumps({"email": "x@y.com", "building": "z" * 5000}).encode(), "1.2.3.4")
    assert status == 413


def test_invalid_json_rejected():
    status, _ = waitlist_api.dispatch("POST", b"{not json", "1.2.3.4")
    assert status == 400


def test_rate_limit_per_ip():
    for i in range(waitlist_api.RATE_LIMIT):
        status, _ = signup(f"user{i}@example.com")
        assert status == 200
    status, payload = signup("overflow@example.com")
    assert status == 429


def test_unknown_role_normalizes():
    _, payload = signup("role@example.com", role="hacker")
    status, payload2 = signup("role2@example.com", role="../../etc")
    assert status == 200  # normalized, not rejected


def test_store_id_derivation_from_token():
    assert waitlist_api._store_id_from_token("vercel_blob_rw_7ABCDEF123_sig") == "7ABCDEF123"
