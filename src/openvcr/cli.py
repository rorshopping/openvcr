"""The openvcr command line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

from . import __version__
from .cassette import Cassette
from .diff import DEFAULT_IGNORE_FIELDS, diff_cassettes, render_report
from .proxy import DEFAULT_PORT, ProxyError, RecordServer, ReplayServer

BANNER = r"""
   ____                   __     ____ ____
  / __ \____  ___  ____  / /__  /  // __ \  the flight recorder
 / / / / __ \/ _ \/ __ \/ / _ \/  // /_/ /  for AI agents
/ /_/ / /_/ /  __/ / / / /  __/  // ____/
\____/ .___/\___/_/ /_/_/\___/_//_/_/      https://github.com/rorshopping/openvcr
    /_/
"""


def _supports_color() -> bool:
    return sys.stdout.isatty() and not __import__("os").environ.get("NO_COLOR")


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _supports_color() else text


def _green(text):  return _c(text, "32")
def _red(text):    return _c(text, "31")
def _yellow(text): return _c(text, "33")
def _dim(text):    return _c(text, "2")
def _bold(text):   return _c(text, "1")


def _compile_extra_patterns(patterns: list[str]) -> list:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            raise SystemExit(f"invalid --scrub-pattern regex {pattern!r}: {exc}")
    return compiled


def _add_ignore_fields(args) -> frozenset:
    return frozenset(getattr(args, "ignore_field", None) or []) | DEFAULT_IGNORE_FIELDS


# --------------------------------------------------------------------------- #
#  record
# --------------------------------------------------------------------------- #

def cmd_record(args) -> int:
    cassette = Cassette(args.out)
    if cassette.path.exists() and not args.append:
        raise SystemExit(f"refusing to overwrite {cassette.path} (use --append, or remove it)")

    extra = _compile_extra_patterns(args.scrub_pattern or [])
    server = RecordServer(args.upstream, cassette,
                          scrub_body=not args.no_scrub_body, extra_scrub_patterns=extra)

    print(_bold(f"\n  OpenVCR record"), _dim(f"v{__version__}"))
    print(f"  upstream : {args.upstream}")
    print(f"  cassette : {cassette.path}")
    print(_dim("  proxy forwards auth headers upstream but never records them."))

    async def run():
        runner, port = await server.start(args.port, args.host)
        try:
            print(f"""
  {_green("● recording on port " + str(port))}

  Point your agent at the proxy:

    export OPENAI_BASE_URL=http://{args.host}:{port}/v1        {_dim("# OpenAI SDK")}
    export ANTHROPIC_BASE_URL=http://{args.host}:{port}        {_dim("# Anthropic SDK")}
    export ANTHROPIC_API_KEY=...                               {_dim("# keys pass through, never recorded")}

  Run your agent, then press Ctrl-C to stop.
""")
            while True:
                await asyncio.sleep(3600)
        finally:
            await server.stop(runner)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    print(f"\n  {_green('✓')} saved {server.record_count} exchange(s) → {cassette.path}")
    print(_dim(f"  replay with: openvcr replay {cassette.path}\n"))
    return 0


# --------------------------------------------------------------------------- #
#  replay
# --------------------------------------------------------------------------- #

def cmd_replay(args) -> int:
    cassette = Cassette(args.cassette)
    n = len(cassette)
    if n == 0:
        raise SystemExit(f"cassette {cassette.path} is empty or missing")

    server = ReplayServer(cassette, mode=args.match, latency=args.latency,
                          ignore_fields=_add_ignore_fields(args))
    print(_bold(f"\n  OpenVCR replay"), _dim(f"v{__version__}"))
    print(f"  cassette : {cassette.path}  ({n} exchange(s))")
    print(f"  matching : {args.match}   latency: {args.latency}")

    async def run():
        runner, port = await server.start(args.port, args.host)
        try:
            print(f"""
  {_green("● replaying on port " + str(port))}  {_dim("— no network calls will be made")}

  Point your agent (or tests) at the recorded traffic:

    export OPENAI_BASE_URL=http://{args.host}:{port}/v1
    export ANTHROPIC_BASE_URL=http://{args.host}:{port}

  Press Ctrl-C to stop.
""")
            while True:
                await asyncio.sleep(3600)
        finally:
            await ReplayServer.stop(runner)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    print(f"\n  {_green('✓')} served {server.hits} response(s) from {cassette.path}\n")
    return 0


# --------------------------------------------------------------------------- #
#  diff
# --------------------------------------------------------------------------- #

def cmd_diff(args) -> int:
    results, summary = diff_cassettes(args.baseline, args.candidate,
                                      ignore_fields=_add_ignore_fields(args))
    report = render_report(args.baseline, args.candidate, results, summary, fmt=args.format)
    if args.output:
        Path(args.output).write_text(report + "\n", encoding="utf-8")
        print(f"wrote report → {args.output}")
    else:
        print(report)
    if summary["drift"] == 0 and summary["missing_in_b"] == 0 and summary["missing_in_a"] == 0:
        print(_green("\n✓ no drift detected"))
        return 0
    if args.exit_zero:
        return 0
    return 1


# --------------------------------------------------------------------------- #
#  info
# --------------------------------------------------------------------------- #

def cmd_info(args) -> int:
    cassette = Cassette(args.cassette)
    records = cassette.load()
    if not records:
        raise SystemExit(f"cassette {cassette.path} is empty or missing")
    methods: dict[str, int] = {}
    paths: dict[str, int] = {}
    total_ms = stream_events = 0
    first = last = None
    for rec in records:
        m, p = rec.request.get("method", "?"), rec.request.get("path", "?")
        methods[m] = methods.get(m, 0) + 1
        paths[p] = paths.get(p, 0) + 1
        total_ms += rec.duration_ms
        if rec.response.get("stream"):
            stream_events += len(rec.response["stream"])
        ts = rec.recorded_at
        first = ts if first is None or (ts and ts < first) else first
        last = ts if last is None or (ts and ts > last) else last

    print(_bold(f"\n  {cassette.path}"))
    print(f"  exchanges: {len(records)}")
    print(f"  recorded : {first or '?'} → {last or '?'}")
    print(f"  time seen: {total_ms / 1000:.1f}s upstream")
    if stream_events:
        print(f"  SSE events: {stream_events}")
    print("  by method:")
    for m, n in sorted(methods.items(), key=lambda kv: -kv[1]):
        print(f"    {m:<7} {n}")
    print("  endpoints:")
    for p, n in sorted(paths.items(), key=lambda kv: -kv[1]):
        print(f"    {n:>4}× {p}")
    print()
    return 0


# --------------------------------------------------------------------------- #
#  demo
# --------------------------------------------------------------------------- #

def _demo_cassette_path() -> Path:
    from importlib.resources import as_file, files
    with as_file(files("openvcr").joinpath("example_cassettes", "openai-chat-demo.cassette.jsonl")) as p:
        return Path(p)


def cmd_demo_fixed(args) -> int:
    cassette_path = _demo_cassette_path()
    cassette = Cassette(cassette_path)
    n = len(cassette)
    server = ReplayServer(cassette, latency=args.latency)
    print(_bold(f"\n  OpenVCR demo"), _dim(f"v{__version__}"))
    print(f"  cassette : {cassette_path.name}  ({n} exchange(s), bundled)")

    async def run():
        runner, port = await server.start(args.port, args.host)
        try:
            print(f"""
  {_green("● replaying on port " + str(port))}

  Try it right now:

    curl http://{args.host}:{port}/v1/chat/completions \\
      -H 'Content-Type: application/json' \\
      -d '{{"model":"gpt-4o","messages":[{{"role":"user","content":"Hello!"}}]}}'

  Or point a real client at it:

    export OPENAI_BASE_URL=http://{args.host}:{port}/v1
    export OPENAI_API_KEY=demo   {_dim("# any value works — nothing leaves your machine")}

  Press Ctrl-C to stop.
""")
            while True:
                await asyncio.sleep(3600)
        finally:
            await ReplayServer.stop(runner)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    print(f"\n  {_green('✓')} served {server.hits} response(s)\n")
    return 0


# --------------------------------------------------------------------------- #
#  parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openvcr",
        description="OpenVCR — record, replay, and diff LLM API traffic.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_dim("docs: https://github.com/rorshopping/openvcr"))
    parser.add_argument("--version", action="version", version=f"openvcr {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("record", help="run a recording proxy against a real API")
    p.add_argument("--upstream", required=True,
                   help="API origin, e.g. https://api.openai.com")
    p.add_argument("--out", default="openvcr.cassette.jsonl", help="cassette file (default: %(default)s)")
    p.add_argument("--append", action="store_true", help="append to an existing cassette")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-scrub-body", action="store_true",
                   help="disable best-effort secret redaction inside bodies")
    p.add_argument("--scrub-pattern", action="append", metavar="REGEX",
                   help="extra regex to redact in bodies (repeatable)")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("replay", help="serve a cassette as a deterministic API")
    p.add_argument("cassette", help="cassette file to replay")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--match", choices=["hash", "sequence", "any"], default="hash",
                   help="request matching strategy (default: %(default)s)")
    p.add_argument("--latency", choices=["fast", "real"], default="fast",
                   help="replay at recorded pace or instantly (default: %(default)s)")
    p.add_argument("--ignore-field", action="append", metavar="FIELD",
                   help="request body field to ignore when matching (repeatable)")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("diff", help="compare two cassettes and report drift")
    p.add_argument("baseline")
    p.add_argument("candidate")
    p.add_argument("--ignore-field", action="append", metavar="FIELD",
                   help="extra response field to ignore (repeatable)")
    p.add_argument("--format", choices=["text", "markdown"], default="text")
    p.add_argument("--output", metavar="FILE", help="write report to file instead of stdout")
    p.add_argument("--exit-zero", action="store_true", help="always exit 0 (drift is informational)")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("info", help="summarize a cassette")
    p.add_argument("cassette")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("demo", help="replay the bundled demo cassette — no API key needed")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--latency", choices=["fast", "real"], default="fast")
    p.set_defaults(func=cmd_demo_fixed)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ProxyError as exc:
        print(_red(f"error: {exc}"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
