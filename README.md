# OpenVCR

[![CI](https://github.com/rorshopping/openvcr/actions/workflows/ci.yml/badge.svg)](https://github.com/rorshopping/openvcr/actions/workflows/ci.yml)

**The flight recorder for AI agents.** Record every LLM API call your agent
makes, replay them deterministically in tests, and diff exactly what changed
when you swap models or prompts.

No SDK changes. No code changes. Works with any language that can point an
OpenAI- or Anthropic-compatible client at a URL.

```console
$ openvcr demo                      # see it work in 10 seconds, no API key
$ openvcr record --upstream https://api.openai.com --out agent.cassette.jsonl
$ openvcr replay agent.cassette.jsonl
$ openvcr diff old.cassette.jsonl new.cassette.jsonl
```

## Why

Agents are non-deterministic. When a run fails you cannot reproduce it, when
you change a prompt you cannot regression-test it, and when the provider
updates a model you find out from your users.

OpenVCR fixes all three with one artifact — a **cassette**: a JSONL file that
captures the full request/response traffic of an agent run (including SSE
streams), with credentials scrubbed by default.

| | without OpenVCR | with OpenVCR |
|---|---|---|
| Reproduce a failed run | vibes | `openvcr replay agent.cassette.jsonl` |
| Test your agent in CI | mock everything by hand | replay real recorded traffic, offline, free |
| Swap `gpt-x` → `gpt-y` | hope | `openvcr diff` shows the exact drift per request |

## Quickstart (60 seconds, no API key)

```console
$ pip install git+https://github.com/rorshopping/openvcr
$ openvcr demo
```

> A PyPI release is coming shortly; installing from GitHub gets you the same
> package today.

In another terminal:

```console
$ curl http://127.0.0.1:8817/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Hello!"}]}'
```

You just got a recorded completion served from the bundled cassette — no
network, no key, byte-identical on every call. That's the whole idea.

## Recording your agent

OpenVCR is a **proxy**, so it works with any language or framework:

```console
$ openvcr record --upstream https://api.openai.com --out agent.cassette.jsonl
  ● recording on port 8817

  export OPENAI_BASE_URL=http://127.0.0.1:8817/v1
```

```console
$ export OPENAI_BASE_URL=http://127.0.0.1:8817/v1   # OpenAI SDKs
$ export ANTHROPIC_BASE_URL=http://127.0.0.1:8817   # Anthropic SDKs
$ python my_agent.py                                # LangChain, LlamaIndex,
                                                    # Vercel AI SDK, curl — anything
```

Auth headers pass through to the real API but are **never written to the
cassette**. Bodies are additionally scrubbed for token-shaped strings
(`sk-…`, `ghp_…`, JWTs, …) so cassettes are safe to commit.

## Replaying in tests

The pytest plugin starts a replay server and points the official SDKs at it
via environment variables:

```python
def test_agent_greets_politely(openvcr):
    with openvcr.replay("cassettes/greeting.cassette.jsonl"):
        reply = run_my_agent("hello")      # uses OPENAI_BASE_URL automatically
        assert "hello" in reply.lower()
```

Runs are deterministic, offline, and cost $0 — the whole suite at the speed
of your CPU. Record once with:

```python
def test_manual_record(openvcr):          # run locally, then delete
    with openvcr.record("https://api.openai.com", "cassettes/greeting.cassette.jsonl"):
        run_my_agent("hello")
```

## Diffing model upgrades

The killer workflow — did the new model version break my agent?

```console
$ openvcr record --upstream https://api.openai.com --append --out agent.cassette.jsonl
$ openvcr diff baseline.cassette.jsonl agent.cassette.jsonl
  ✗ #2 DRIFT        POST /v1/chat/completions
       $.choices[0].message.content:
         old: "Your order #4511 ships tomorrow."
         new: "Order #4511 will ship tomorrow."
  Summary: 7 same, 1 drifted, 0 missing (8 total)
```

Diffs are **semantic**: streaming responses are reassembled into assistant
text and tool-call arguments before comparison, so chunk-boundary noise never
shows up as drift. Provider volatility (`id`, `created`, fingerprints,
usage) is ignored by default. Exit code 1 on drift → wire it into CI and
gate model upgrades on evidence.

## What's in a cassette

JSONL, one exchange per line — diffable, greppable, reviewable:

```json
{"v": 1,
 "recorded_at": "2026-10-03T09:14:22Z",
 "duration_ms": 412,
 "key": "sha256:9f2a71…",
 "request":  {"method": "POST", "path": "/v1/chat/completions",
              "headers": {"content-type": "application/json"}, "body": {"…": "…"}},
 "response": {"status": 200, "headers": {"content-type": "application/json"},
              "stream": ["data: {…}", "data: [DONE]"]}}
```

Commit them, code-review them, regenerate them. They're yours.

## How it compares

| | OpenVCR | vcrpy / respx | promptfoo | Langfuse |
|---|---|---|---|---|
| Captures **real** production-shaped runs | ✓ | hand-written mocks | n/a | n/a |
| Language | any (HTTP proxy) | Python only | configs | any |
| SSE streaming replay | ✓ | partial | n/a | observes only |
| Trajectory drift diffing | ✓ | ✗ | evals (LLM-judged) | traces |
| Offline + free in CI | ✓ | ✓ | ✓ | ✗ (SaaS-first) |

These tools are complementary: trace with Langfuse, evaluate with promptfoo,
**regression-test deterministically with OpenVCR**.

## Open core, forever

Everything on this page is MIT-licensed and free, forever, no accounts.
The hosted product — **OpenVCR Cloud** (private beta, joining the waitlist
gets you a founding-member discount) — adds what a single machine can't do:

- **Drift canaries in CI** — we re-run your cassettes against new model
  releases and open a PR with the diff before your users find out.
- **Team cassette library** — shared, access-controlled storage with a
  review UI for drift reports.
- **Flakiness analytics** — which requests come back different across
  providers, regions, and time.

Local-first by design: your cassettes stay on your machine unless *you*
upload them. [Join the waitlist →](https://openvcr.vercel.app)

## Roadmap

- [ ] MCP tool-call recording (beyond LLM APIs)
- [ ] Vitest / Jest helpers for the TypeScript ecosystem
- [ ] `openvcr verify` — re-run a cassette against a live API in one command
- [ ] Cassette anonymization profiles (PII scrubbing beyond secrets)
- [ ] Hosted drift canaries (Cloud beta)

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). The codebase
is small on purpose: `src/openvcr/` is ~1k lines of dependency-light Python
(aiohttp is the only runtime dependency).

## License

[MIT](LICENSE) © OpenVCR contributors
