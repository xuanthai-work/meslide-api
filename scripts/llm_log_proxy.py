"""Log OpenAI-compatible traffic between Presenton and a local vLLM/proxy.

Default: listen :5002, forward to http://127.0.0.1:5000
Log file: presenton-llm.log next to this script (or LLM_LOG_PATH).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LISTEN_HOST = os.environ.get("LLM_PROXY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LLM_PROXY_PORT", "5002"))
UPSTREAM = os.environ.get("LLM_PROXY_UPSTREAM", "http://127.0.0.1:5000").rstrip("/")
LOG_PATH = Path(os.environ.get("LLM_LOG_PATH", Path(__file__).resolve().parent / "presenton-llm.log"))
CONTENT_PREVIEW_CHARS = 20_000
SKIP_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}


def _parse_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _assistant_content(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message") or {}
    content = message.get("content")
    return content if isinstance(content, str) else None


def _append_log(text: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")


class LoggingProxy(BaseHTTPRequestHandler):
    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in SKIP_HEADERS
        }
        request = Request(
            f"{UPSTREAM}{self.path}",
            data=raw or None,
            method=self.command,
            headers=headers,
        )
        status = 502
        body = b""
        response_headers: dict[str, str] = {}
        try:
            with urlopen(request, timeout=300) as response:
                body = response.read()
                status = getattr(response, "status", 200)
                response_headers = dict(response.headers)
        except HTTPError as error:
            status = error.code
            body = error.read() or str(error).encode("utf-8")
            response_headers = dict(error.headers or {})
        except URLError as error:
            body = f"proxy upstream error: {error}".encode("utf-8")

        req_json = _parse_json(raw)
        res_json = _parse_json(body)
        content = _assistant_content(res_json)
        usage = res_json.get("usage") if isinstance(res_json, dict) else None
        finish = None
        if isinstance(res_json, dict):
            choices = res_json.get("choices") or []
            if choices and isinstance(choices[0], dict):
                finish = choices[0].get("finish_reason")

        messages = (req_json or {}).get("messages") if isinstance(req_json, dict) else None
        prompt_chars = 0
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    prompt_chars += len(message["content"])

        _append_log(
            "\n".join(
                [
                    "",
                    f"==== {datetime.now().isoformat()} {self.command} {self.path} ====",
                    f"upstream_status: {status}",
                    f"model: {req_json.get('model') if isinstance(req_json, dict) else None}",
                    f"prompt_chars: {prompt_chars}",
                    f"has_response_format: {isinstance(req_json, dict) and bool(req_json.get('response_format'))}",
                    f"usage: {usage}",
                    f"finish_reason: {finish}",
                    f"content_len: {len(content or '')}",
                    "content:",
                    (content or (body[:CONTENT_PREVIEW_CHARS].decode('utf-8', 'replace')))[
                        :CONTENT_PREVIEW_CHARS
                    ],
                    "",
                ]
            )
        )

        self.send_response(status)
        content_type = response_headers.get("Content-Type") or response_headers.get(
            "content-type"
        ) or "application/json"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._forward()

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), LoggingProxy)
    print(
        f"LLM log proxy {LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM}",
        file=sys.stderr,
    )
    print(f"log file: {LOG_PATH}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
