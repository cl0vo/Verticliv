from __future__ import annotations

import html
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable


class OAuthCallback:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, path: str = "/callback/") -> None:
        self.host = host
        self.port = port
        self.path = path
        self.params: dict[str, str] | None = None
        self._event = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path.rstrip("/") != owner.path.rstrip("/"):
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = urllib.parse.parse_qs(parsed.query)
                owner.params = {
                    key: values[0] if values else ""
                    for key, values in raw.items()
                }
                owner._event.set()
                error = owner.params.get("error")
                message = (
                    f"Ошибка авторизации: {error}"
                    if error
                    else "Аккаунт подключён. Это окно можно закрыть и вернуться в Verticliv."
                )
                body = (
                    "<!doctype html><meta charset='utf-8'>"
                    "<title>Verticliv</title>"
                    "<body style='background:#0b0a0d;color:#f1c36d;font:20px Segoe UI;"
                    "display:grid;place-items:center;height:100vh;margin:0'>"
                    f"<div>{html.escape(message)}</div></body>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args) -> None:
                return

        self.server = HTTPServer((self.host, self.port), Handler)
        self.port = int(self.server.server_address[1])
        self.server.timeout = 0.5

    @property
    def redirect_uri(self) -> str:
        return f"http://{self.host}:{self.port}{self.path}"

    def authorize(
        self,
        build_url: Callable[[str], str],
        *,
        timeout: int = 240,
    ) -> dict[str, str]:
        url = build_url(self.redirect_uri)
        webbrowser.open(url, new=1, autoraise=True)
        deadline = time.time() + timeout
        try:
            while not self._event.is_set() and time.time() < deadline:
                self.server.handle_request()
            if not self._event.is_set() or self.params is None:
                raise RuntimeError("Время ожидания входа истекло.")
            if self.params.get("error"):
                description = self.params.get("error_description") or self.params["error"]
                raise RuntimeError(f"Авторизация отклонена: {description}")
            return self.params
        finally:
            self.server.server_close()
