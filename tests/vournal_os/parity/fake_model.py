"""A stand-in for the Anthropic Messages API, for AI parity without paid calls.

Both backends point ANTHROPIC_BASE_URL here: the NestJS reference with the key "parity-nest"
(isolated_backend.py os-reference), the Python app with "parity-python" (`python_client()`).
A test scripts the model's replies with `script(...)`; each side walks its own copy of the
script, so the two backends get the same answers, and `requests(side)` returns what each one
sent, for comparing prompts, tools and history."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 4199
URL = f"http://127.0.0.1:{PORT}"
SIDES = {"parity-nest": "nest", "parity-python": "python"}

_lock = threading.Lock()
_script: list = []
_cursor: dict[str, int] = {}
_requests: dict[str, list[dict]] = {}


def message(*blocks, stop="end_turn") -> dict:
    """A Messages API reply made of content blocks (see text(), tool_use())."""
    return {"status": 200, "body": {"id": "msg_parity", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
                                    "content": list(blocks), "stop_reason": stop, "stop_sequence": None,
                                    "usage": {"input_tokens": 10, "output_tokens": 5}}}


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def structured(value) -> dict:
    """A structured-output reply: its JSON in one text block."""
    return message(text(json.dumps(value, ensure_ascii=False)))


def tool_use(name: str, tool_input: dict, id: str = "toolu_1") -> dict:
    return {"type": "tool_use", "id": id, "name": name, "input": tool_input}


def tools(*uses) -> dict:
    """One round that calls tools: (name, input) pairs, with a thinking block first."""
    blocks = [{"type": "thinking", "thinking": "Working it out.", "signature": "sig_parity"}]
    blocks += [tool_use(name, tool_input, f"toolu_{n}") for n, (name, tool_input) in enumerate(uses, 1)]
    return message(*blocks, stop="tool_use")


def error(status: int, kind: str = "invalid_request_error", text_: str = "scripted failure") -> dict:
    return {"status": status, "body": {"type": "error", "error": {"type": kind, "message": text_}}}


def script(*replies) -> None:
    """The replies, in order, for each side; past the end every request gets a 500. A reply
    can be a function of the side ("nest" / "python"), for ids that differ between them."""
    with _lock:
        _script[:] = replies
        _cursor.clear()
        _requests.clear()


def requests(side: str) -> list[dict]:
    """What a side sent: [{"path", "beta", "body"}] in order."""
    with _lock:
        return list(_requests.get(side, []))


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 — http.server's name
        side = SIDES.get(self.headers.get("x-api-key", ""), "unknown")
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        with _lock:
            _requests.setdefault(side, []).append({"path": self.path, "beta": self.headers.get("anthropic-beta"), "body": body})
            n = _cursor.get(side, 0)
            _cursor[side] = n + 1
            reply = _script[n] if n < len(_script) else error(500, "api_error", "the script ran out")
            if callable(reply):  # ids differ between the two databases: the reply is made per side
                reply = reply(side)
        payload = json.dumps(reply["body"]).encode()
        self.send_response(reply["status"])
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # quiet
        pass


_server: ThreadingHTTPServer | None = None


def start() -> None:
    """Once per test process; later calls do nothing."""
    global _server
    if _server is None:
        _server = ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
        threading.Thread(target=_server.serve_forever, daemon=True).start()


def python_client():
    """The Python app's client for parity: the reference's settings, pointed here."""
    import anthropic

    return anthropic.AsyncAnthropic(api_key="parity-python", base_url=URL, max_retries=2, timeout=30)
