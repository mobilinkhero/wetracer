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

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class StubProviderServer:
    """An OpenAI-compatible chat-completions server replaying scripted deltas."""

    def __init__(self, deltas=None, chunk_delay: float = 0.05,
                 model: str = "stub-model"):
        self.deltas = list(deltas or ["Hello ", "streamed ", "world"])
        self.chunk_delay = chunk_delay
        self.model = model
        self.requests = []  # parsed JSON bodies received, in order
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
                if self.path != "/v1/chat/completions":
                    return self._send_json(404, {"error": "not found"})
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
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

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
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
