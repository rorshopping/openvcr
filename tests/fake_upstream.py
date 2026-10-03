"""A miniature OpenAI-compatible API for tests and demo-cassette generation.

Deterministic: the response (and stream) is keyed by the literal text of the
last user message, so tests can assert exact bytes end to end.
"""

import json

from aiohttp import web

CHAT_PATH = "/v1/chat/completions"

# prompt -> (non-streaming completion body, stream chunk payloads)
REPLIES = {
    "Say hello.": (
        {
            "id": "chatcmpl-fake-001",
            "object": "chat.completion",
            "created": 1759500000,
            "model": "gpt-4o",
            "system_fingerprint": "fp_test",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hello! How can I help you today?"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 9, "completion_tokens": 10, "total_tokens": 19},
        },
        [
            {"id": "chatcmpl-fake-002", "object": "chat.completion.chunk", "created": 1759500001,
             "model": "gpt-4o", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hi"}, "finish_reason": None}]},
            {"id": "chatcmpl-fake-002", "object": "chat.completion.chunk", "created": 1759500001,
             "model": "gpt-4o", "choices": [{"index": 0, "delta": {"content": " there"}, "finish_reason": None}]},
            {"id": "chatcmpl-fake-002", "object": "chat.completion.chunk", "created": 1759500001,
             "model": "gpt-4o", "choices": [{"index": 0, "delta": {"content": "!"}, "finish_reason": None}]},
            {"id": "chatcmpl-fake-002", "object": "chat.completion.chunk", "created": 1759500001,
             "model": "gpt-4o", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ],
    ),
    "What is the capital of France?": (
        {
            "id": "chatcmpl-fake-003",
            "object": "chat.completion",
            "created": 1759500002,
            "model": "gpt-4o",
            "system_fingerprint": "fp_test",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "The capital of France is Paris."},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        },
        [],
    ),
}


def make_app():
    async def chat(request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": {"message": "invalid json"}}, status=400)
        messages = body.get("messages") or []
        last_user = next((m.get("content") for m in reversed(messages)
                          if m.get("role") == "user"), None)
        completion, chunks = REPLIES.get(
            last_user,
            ({"id": "chatcmpl-fake-404", "object": "chat.completion",
              "created": 1759500003, "model": body.get("model", "gpt-4o"),
              "choices": [{"index": 0,
                           "message": {"role": "assistant", "content": "I did not understand that."},
                           "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 1, "completion_tokens": 6, "total_tokens": 7}},
             []),
        )
        if body.get("stream"):
            resp = web.StreamResponse()
            resp.content_type = "text/event-stream"
            await resp.prepare(request)
            for chunk in chunks:
                await resp.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp
        return web.json_response(completion)

    app = web.Application()
    app.router.add_post(CHAT_PATH, chat)
    return app
