# Contributing to OpenVCR

Thanks for helping build the flight recorder for AI agents. The codebase is
small on purpose; this guide gets you productive in five minutes.

## Setup

```console
$ git clone https://github.com/rorshopping/openvcr
$ cd openvcr
$ python3 -m venv .venv && source .venv/bin/activate
$ pip install -e ".[dev]"
$ pytest
```

## Layout

```
src/openvcr/
  cassette.py       cassette format (JSONL), matching keys, path normalization
  scrub.py          credential scrubbing (headers + token-shaped strings)
  proxy.py          RecordServer + ReplayServer (SSE-aware) + threaded runner
  diff.py           semantic drift engine (stream reassembly + field diff)
  cli.py            argparse CLI: record / replay / diff / info / demo
  pytest_plugin.py  the `openvcr` fixture
landing/            openvcr.vercel.app — waitlist site (static + 1 function)
tests/              pytest suite; fake_upstream.py emulates the OpenAI API
```

## Ground rules

- **Zero runtime dependencies beyond aiohttp.** If a feature needs more,
  justify it in the PR.
- **Cassettes are sacred**: never record credentials. New record paths must
  extend `scrub.py` tests, not bypass them.
- **Replay must be byte-identical.** Any change to replay behavior needs a
  roundtrip test (`tests/test_proxy_roundtrip.py`).
- Python ≥ 3.10, stdlib style, type hints on public functions.

## Workflow

1. Open an issue before non-trivial changes — include your recording/replay
   use case so we can design for it.
2. Branch from `main`, add tests, keep `pytest` green.
3. Regenerate the demo cassette only via `scripts/make_demo_cassette.py`
   (its content is part of the out-of-the-box experience).

## Reporting security issues

Found a way cassette recording can leak credentials, or an injection through
recorded traffic? Email via [GitHub security advisories](https://github.com/rorshopping/openvcr/security/advisories/new)
rather than a public issue. We credit reporters in the release notes.
