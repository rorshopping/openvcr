"""CLI smoke tests: exercised through `python -m openvcr` exactly as users run it."""

import json
import subprocess
import sys

import pytest

from openvcr.cassette import Cassette, Exchange, compute_match_key


def run_cli(*argv, expect_code=0):
    proc = subprocess.run(
        [sys.executable, "-m", "openvcr", *argv],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == expect_code, (
        f"openvcr {' '.join(argv)} exited {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return proc


def make_exchange(prompt, content):
    body = {"model": "m", "messages": [{"role": "user", "content": prompt}]}
    return Exchange(recorded_at="2026-01-01T00:00:00Z", duration_ms=5,
                    key=compute_match_key("POST", "/v1/chat/completions",
                                          json.dumps(body).encode()),
                    request={"method": "POST", "path": "/v1/chat/completions",
                             "headers": {}, "body": body},
                    response={"status": 200, "headers": {"Content-Type": "application/json"},
                              "body": {"choices": [{"message": {"content": content}}]}})


def test_info_subcommand(tmp_path):
    path = tmp_path / "info.cassette.jsonl"
    cassette = Cassette(path)
    cassette.append(make_exchange("q1", "one"))
    cassette.append(make_exchange("q2", "two"))
    proc = run_cli("info", str(path))
    assert "exchanges: 2" in proc.stdout
    assert "/v1/chat/completions" in proc.stdout


def test_diff_subcommand_exit_codes(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    Cassette(a).append(make_exchange("q1", "one"))
    Cassette(b).append(make_exchange("q1", "one"))
    run_cli("diff", str(a), str(b), expect_code=0)

    c = tmp_path / "c.jsonl"
    Cassette(c).append(make_exchange("q1", "CHANGED"))
    proc = run_cli("diff", str(a), str(c), expect_code=1)
    assert "DRIFT" in proc.stdout

    run_cli("diff", str(a), str(c), "--exit-zero", expect_code=0)


def test_diff_markdown_report(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    Cassette(a).append(make_exchange("q1", "one"))
    Cassette(b).append(make_exchange("q1", "two"))
    out = tmp_path / "report.md"
    run_cli("diff", str(a), str(b), "--format", "markdown", "--output", str(out),
            expect_code=1)  # drift present → exit 1 even when writing a report
    text = out.read_text()
    assert text.startswith("# OpenVCR drift report")
    assert "drift" in text


def test_record_refuses_overwrite(tmp_path):
    path = tmp_path / "x.cassette.jsonl"
    path.write_text("")
    run_cli("record", "--upstream", "https://api.openai.com",
            "--out", str(path), expect_code=1)


def test_replay_empty_cassette_errors(tmp_path):
    path = tmp_path / "empty.cassette.jsonl"
    path.write_text("")
    run_cli("replay", str(path), expect_code=1)


def test_record_rejects_non_origin_upstream(tmp_path):
    run_cli("record", "--upstream", "api.openai.com/v1", "--out",
            str(tmp_path / "y.jsonl"), expect_code=2)


def test_version():
    proc = run_cli("--version")
    assert proc.stdout.strip().startswith("openvcr 0.")
