from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class HealthServer:
    """Tiny stdlib HTTP server for Railway health checks.

    The bot itself works through Telegram long polling, so Railway still needs a
    small HTTP endpoint to know the container is alive. This avoids pulling in a
    full web framework while keeping /health and / ready for deploy checks.
    """

    def __init__(self, host: str, port: int, app_name: str = "ChannelPilot AI") -> None:
        self.host = host
        self.port = port
        self.app_name = app_name
        self._server: Optional[asyncio.base_events.Server] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sockets = self._server.sockets or []
        bind = ", ".join(str(sock.getsockname()) for sock in sockets)
        logger.info("Health server started on %s", bind)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        logger.info("Health server stopped")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.read(2048), timeout=5)
            request_line = raw.decode("latin-1", errors="ignore").splitlines()[0] if raw else "GET / HTTP/1.1"
            parts = request_line.split()
            method = parts[0].upper() if parts else "GET"
            path = parts[1] if len(parts) > 1 else "/"

            if method not in {"GET", "HEAD"}:
                await self._send(writer, 405, {"ok": False, "error": "method_not_allowed"}, include_body=method != "HEAD")
                return

            if path in {"/", "/health", "/healthz", "/ready"}:
                await self._send(
                    writer,
                    200,
                    {"ok": True, "service": self.app_name, "status": "running"},
                    include_body=method != "HEAD",
                )
                return

            await self._send(writer, 404, {"ok": False, "error": "not_found"}, include_body=method != "HEAD")
        except Exception as exc:  # pragma: no cover - defensive network edge cases
            logger.debug("Health request failed: %s", exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # pragma: no cover
                pass

    async def _send(self, writer: asyncio.StreamWriter, status: int, payload: dict, include_body: bool = True) -> None:
        reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed"}.get(status, "OK")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if include_body else b""
        headers = [
            f"HTTP/1.1 {status} {reason}",
            "Content-Type: application/json; charset=utf-8",
            f"Content-Length: {len(body)}",
            "Connection: close",
            "Cache-Control: no-store",
            "",
            "",
        ]
        writer.write("\r\n".join(headers).encode("latin-1") + body)
        await writer.drain()
