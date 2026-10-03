"""Cassette diffing: what changed between two recordings of the same agent?

`diff_cassettes` pairs exchanges from cassette A (old) and B (new) — by match
key when possible, otherwise by position — and compares responses field by
field. Known-volatile response fields (ids, timestamps, fingerprints) are
ignored by default. Streaming responses are compared semantically: the
assistant text and tool-call arguments are reconstructed from SSE events, so
chunk-boundary differences don't show up as drift.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from difflib import unified_diff

from .cassette import Cassette, canonical_json, compute_match_key, decode_body

DEFAULT_IGNORE_FIELDS = frozenset({
    "id", "created", "system_fingerprint", "request_id", "service_tier",
    "log_id", "stop_reason_fallback",  # provider-specific volatility
})

# --------------------------------------------------------------------------- #
#  Stream extraction
# --------------------------------------------------------------------------- #

def _sse_data_payloads(events: list[str]) -> list[str]:
    payloads = []
    for event in events:
        for line in event.splitlines():
            if line.startswith("data:"):
                payloads.append(line[5:].strip())
    return payloads


def openai_stream_summary(events: list[str]) -> dict:
    """Reconstruct assistant text + tool calls from OpenAI chunk streams."""
    text_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    raw_parts: list[str] = []
    for payload in _sse_data_payloads(events):
        if payload == "[DONE]":
            continue
        raw_parts.append(payload)
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        if delta.get("content"):
            text_parts.append(delta["content"])
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = tool_calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] += fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]
    summary = {"text": "".join(text_parts)}
    if tool_calls:
        summary["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    summary["_raw"] = raw_parts
    return summary


def anthropic_stream_summary(events: list[str]) -> dict:
    text_parts: list[str] = []
    args_parts: list[str] = []
    for payload in _sse_data_payloads(events):
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            text_parts.append(delta.get("text", ""))
        elif delta.get("type") == "input_json_delta":
            args_parts.append(delta.get("partial_json", ""))
    summary = {"text": "".join(text_parts)}
    if args_parts:
        summary["tool_args"] = "".join(args_parts)
    return summary


def stream_summary(events: list[str]) -> dict:
    summary = openai_stream_summary(events)
    if summary.get("text") or summary.get("tool_calls"):
        return summary
    anthropic = anthropic_stream_summary(events)
    if anthropic.get("text") or anthropic.get("tool_args"):
        return anthropic
    return {"text": "", "_raw": _sse_data_payloads(events)}


# --------------------------------------------------------------------------- #
#  Structural diff
# --------------------------------------------------------------------------- #

@dataclass
class FieldDiff:
    path: str
    old: object
    new: object


def diff_json(old, new, ignore: frozenset[str], path: str = "$",
              out: list[FieldDiff] | None = None, limit: int = 10) -> list[FieldDiff]:
    if out is None:
        out = []
    if len(out) >= limit:
        return out
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            if key in ignore:
                continue
            if len(out) >= limit:
                break
            if key not in old:
                out.append(FieldDiff(f"{path}.{key}", "<missing>", new[key]))
            elif key not in new:
                out.append(FieldDiff(f"{path}.{key}", old[key], "<missing>"))
            else:
                diff_json(old[key], new[key], ignore, f"{path}.{key}", out, limit)
    elif isinstance(old, list) and isinstance(new, list):
        for i in range(max(len(old), len(new))):
            if len(out) >= limit:
                break
            if i >= len(old):
                out.append(FieldDiff(f"{path}[{i}]", "<missing>", new[i]))
            elif i >= len(new):
                out.append(FieldDiff(f"{path}[{i}]", old[i], "<missing>"))
            else:
                diff_json(old[i], new[i], ignore, f"{path}[{i}]", out, limit)
    elif old != new:
        out.append(FieldDiff(path, old, new))
    return out


def _clip(value, width: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= width else text[: width - 1] + "…"


# --------------------------------------------------------------------------- #
#  Pairing + verdicts
# --------------------------------------------------------------------------- #

@dataclass
class PairResult:
    position: int
    method: str
    path: str
    verdict: str  # SAME | DRIFT | MISSING_IN_A | MISSING_IN_B
    fields: list[FieldDiff] = field(default_factory=list)
    note: str = ""


def _response_view(rec) -> dict:
    response = rec.response
    if response.get("stream") is not None:
        summary = stream_summary(response["stream"])
        raw = summary.pop("_raw", None)
        if summary.get("text") or summary.get("tool_calls") or summary.get("tool_args"):
            return {"_stream": summary}
        return {"_stream_raw": raw or []}  # nothing semantic: compare event payloads
    try:
        return {"_json": json.loads(decode_body(response.get("body"), response.get("body_b64")) or b"null")}
    except ValueError:
        return {"_raw": decode_body(response.get("body"), response.get("body_b64")).decode("utf-8", "replace")}


def _compare(old_rec, new_rec, ignore: frozenset[str], limit: int = 10) -> list[FieldDiff]:
    old_view, new_view = _response_view(old_rec), _response_view(new_rec)
    if set(old_view) != set(new_view):
        return [FieldDiff("$._shape", sorted(old_view), sorted(new_view))]
    if "_raw" in old_view:
        return [] if old_view["_raw"] == new_view["_raw"] else [
            FieldDiff("$", _clip(old_view["_raw"]), _clip(new_view["_raw"]))]
    return diff_json(old_view, new_view, ignore, limit=limit)


def diff_cassettes(a_path, b_path, ignore_fields: frozenset[str] = frozenset(),
                   max_fields: int = 10) -> tuple[list[PairResult], dict]:
    ignore = DEFAULT_IGNORE_FIELDS | ignore_fields
    a_recs, b_recs = Cassette(a_path).load(), Cassette(b_path).load()

    def keys_of(recs):
        return [rec.key or compute_match_key(
                    rec.request.get("method", ""), rec.request.get("path", ""),
                    decode_body(rec.request.get("body"), rec.request.get("body_b64")), frozenset())
                for rec in recs]

    a_keys, b_keys = keys_of(a_recs), keys_of(b_recs)
    b_by_key = dict(zip(b_keys, b_recs))
    results: list[PairResult] = []
    used_b: dict[int, int] = {}

    for i, old_rec in enumerate(a_recs):
        new_rec = b_by_key.get(a_keys[i])
        if new_rec is None and not b_keys:  # empty B cassette: fall back to position
            new_rec = None
        if new_rec is None:
            # positional fallback for re-recordings whose keys drifted
            # (e.g. model rename changed request bodies)
            if i < len(b_recs) and b_keys[i] not in a_keys:
                candidate = b_recs[i]
                if i not in used_b:
                    new_rec = candidate
        if new_rec is None:
            results.append(PairResult(i, old_rec.request.get("method", "?"),
                                      old_rec.request.get("path", "?"),
                                      "MISSING_IN_B"))
            continue
        used_b.setdefault(b_recs.index(new_rec), i)
        fields = _compare(old_rec, new_rec, ignore, max_fields)
        results.append(PairResult(i, old_rec.request.get("method", "?"),
                                  old_rec.request.get("path", "?"),
                                  "DRIFT" if fields else "SAME", fields))

    for j, new_rec in enumerate(b_recs):
        if j not in used_b:
            results.append(PairResult(len(results), new_rec.request.get("method", "?"),
                                      new_rec.request.get("path", "?"), "MISSING_IN_A"))

    summary = {
        "total": len(results),
        "same": sum(1 for r in results if r.verdict == "SAME"),
        "drift": sum(1 for r in results if r.verdict == "DRIFT"),
        "missing_in_a": sum(1 for r in results if r.verdict == "MISSING_IN_A"),
        "missing_in_b": sum(1 for r in results if r.verdict == "MISSING_IN_B"),
    }
    return results, summary


def render_report(a_path, b_path, results: list[PairResult], summary: dict,
                  fmt: str = "text", max_fields: int = 6) -> str:
    lines: list[str] = []
    if fmt == "markdown":
        lines += [f"# OpenVCR drift report", "",
                  f"- baseline: `{a_path}`",
                  f"- candidate: `{b_path}`",
                  f"- verdict: **{summary['drift']} drift / {summary['total']} exchanges**",
                  ""]
        bullet = "-"
    else:
        lines += [f"OpenVCR diff", f"  baseline:   {a_path}", f"  candidate:  {b_path}", ""]
        bullet = " "

    for res in results:
        marker = {"SAME": "✓", "DRIFT": "✗", "MISSING_IN_A": "−A",
                  "MISSING_IN_B": "−B"}.get(res.verdict, "?")
        if fmt == "markdown":
            lines.append(f"## {marker} [{res.verdict}] #{res.position} {res.method} {res.path}")
        else:
            lines.append(f"{marker} #{res.position} {res.verdict:<11} {res.method} {res.path}")
        for fd in res.fields[:max_fields]:
            lines.append(f"{bullet}   {fd.path}:")
            lines.append(f"{bullet}     old: {_clip(fd.old)}")
            lines.append(f"{bullet}     new: {_clip(fd.new)}")
        if len(res.fields) > max_fields:
            lines.append(f"{bullet}   … and {len(res.fields) - max_fields} more")
        if fmt != "markdown":
            lines.append("")
    if fmt == "markdown":
        lines += ["", f"_Compared with OpenVCR — ignores provider volatility "
                      f"(ids, timestamps, fingerprints)._"]
    else:
        lines.append(f"Summary: {summary['same']} same, {summary['drift']} drifted, "
                     f"{summary['missing_in_a']} missing in candidate, "
                     f"{summary['missing_in_b']} missing in baseline "
                     f"({summary['total']} total)")
    return "\n".join(lines)
