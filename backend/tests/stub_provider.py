"""
Copyright 2024-2026 ChatterMate

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

"""Stub hosted-provider server for streaming verification (TEST ONLY).

Mimics the OpenAI-compatible chat-completions streaming API — the same
endpoint the dashboard-configured hosted providers use (see
app/utils/agno_utils.create_model, model types OPENAI / OPENAI_COMPATIBLE):
``POST {base_url}/chat/completions`` with ``{"stream": true}`` returns
``text/event-stream`` SSE chunks (``data: {chunk}`` + ``data: [DONE]``).

It also mimics the OpenAI embeddings API the hosted embedder uses (see
app/knowledge/embedder.py): ``POST {base_url}/embeddings`` returns
deterministic 384-dim vectors in the real OpenAI response shape, so KB
vector search works against the stub instead of 404ing.

No real model, no paid calls, no network beyond localhost: the server
replays a scripted list of content deltas with a small delay between them,
so tests can assert token-by-token streaming, time-to-first-token and the
exact request shape (``stream: true``, the configured model id) the lean
streaming agent sends.

Usage:
    server = StubProviderServer(deltas=["Hello ", "world"])
    server.start()  # -> server.base_url, e.g. http://127.0.0.1:54321/v1
    ... build the model with base_url=server.base_url ...
    server.stop()
"""

import hashlib
import json
import math
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def deterministic_embedding(text: str, dimensions: int = 384) -> list:
    """Deterministic bag-of-words embedding for one text (TEST ONLY).

    Each word hashes to one of ``dimensions`` slots; the vector is
    L2-normalized. Same text always yields the same vector, and texts that
    share words yield similar vectors — so a question about resetting a
    password ranks the password article above unrelated articles in vector
    search, the same way a real embedding would. No model is involved: this
    is a hash, not a learned representation, and it only exists so KB
    search has something to rank against when the tests point the org at
    this stub instead of a real hosted provider.
    """
    vec = [0.0] * dimensions
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    if not words:
        # Empty input: a fixed non-zero vector (a zero vector has no
        # defined cosine distance in pgvector).
        vec[0] = 1.0
        return vec
    for word in words:
        digest = hashlib.md5(word.encode("utf-8")).digest()
        vec[int.from_bytes(digest[:4], "little") % dimensions] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


class StubProviderServer:
    """An OpenAI-compatible chat-completions server replaying scripted deltas."""

    def __init__(self, deltas=None, chunk_delay: float = 0.05,
                 model: str = "stub-model", port: int = 0):
        self.deltas = list(deltas or ["Hello ", "streamed ", "world"])
        self.chunk_delay = chunk_delay
        self.model = model
        # 0 = ephemeral (default, as before). A fixed port lets whoever owns
        # the AI config point an org at this stub in advance — the live
        # streaming tests use STUB_PORT for exactly that (they never write
        # ai_configs themselves; see tests_live/test_streaming_live.py).
        self.port = port
        self.requests = []  # parsed chat-completions JSON bodies, in order
        # Embeddings requests are tracked SEPARATELY on purpose: the live
        # KB-hit tests assert no chat-completions call happened
        # (len(stub.requests) unchanged), and a KB search legitimately
        # embeds the question — counting it there would fail those tests.
        self.embedding_requests = []
        self._httpd = None
        self._thread = None

    @property
    def base_url(self) -> str:
        port = self._httpd.server_address[1]
        return f"http://127.0.0.1:{port}/v1"

    def start(self) -> "StubProviderServer":
        server = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep test output clean
                pass

            def do_POST(self):
                if self.path not in ("/v1/chat/completions", "/v1/embeddings"):
                    return self._send_json(404, {"error": "not found"})
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/v1/embeddings":
                    return self._send_embeddings(body)
                server.requests.append(body)
                if body.get("stream"):
                    return self._send_stream(body)
                return self._send_json(200, {
                    "id": "stub-chatcmpl", "object": "chat.completion",
                    "created": 0, "model": server.model,
                    "choices": [{"index": 0, "message": {
                        "role": "assistant",
                        "content": "".join(server.deltas)},
                        "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                              "total_tokens": 2},
                })

            def _send_json(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_embeddings(self, body):
                # Real OpenAI embeddings shape: {"object": "list", "data":
                # [{"object": "embedding", "index": i, "embedding": [...]}],
                # "model": ..., "usage": {...}}. `input` is one string or a
                # list of strings; `dimensions` is honoured when given.
                server.embedding_requests.append(body)
                raw = body.get("input", "")
                inputs = raw if isinstance(raw, list) else [raw]
                try:
                    dims = int(body.get("dimensions") or 384)
                except (TypeError, ValueError):
                    dims = 384
                model = body.get("model") or "text-embedding-3-small"
                vectors = [deterministic_embedding(str(t), dims) for t in inputs]
                prompt_tokens = sum(len(str(t).split()) for t in inputs)
                return self._send_json(200, {
                    "object": "list",
                    "data": [{"object": "embedding", "index": i,
                              "embedding": v}
                             for i, v in enumerate(vectors)],
                    "model": model,
                    "usage": {"prompt_tokens": prompt_tokens,
                              "total_tokens": prompt_tokens},
                })

            def _send_stream(self, body):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def _chunk(payload: str) -> None:
                    raw = f"data: {payload}\n\n".encode()
                    self.wfile.write(f"{len(raw):X}\r\n".encode() + raw + b"\r\n")
                    self.wfile.flush()

                def _event(delta, role=None, finish=None):
                    event = {"id": "stub-chatcmpl",
                             "object": "chat.completion.chunk", "created": 0,
                             "model": server.model,
                             "choices": [{"index": 0, "delta": delta,
                                          "finish_reason": finish}]}
                    return json.dumps(event)

                _chunk(_event({"role": "assistant", "content": ""}))
                for text in server.deltas:
                    time.sleep(server.chunk_delay)
                    _chunk(_event({"content": text}))
                time.sleep(server.chunk_delay)
                _chunk(_event({}, finish="stop"))
                _chunk("[DONE]")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
