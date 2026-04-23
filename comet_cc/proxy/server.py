"""TLS interception server — CC hits us via ANTHROPIC_BASE_URL, we forward
to api.anthropic.com. Every /v1/messages request flows through the rewrite
hook so trim/retrieval injection can run before upstream sees it.

Runs inside the daemon process so the warm BGE-M3 + NodeStore are in reach
without RPC hops.
"""
from __future__ import annotations

import ssl
from typing import Awaitable, Callable

import httpx
from aiohttp import web
from loguru import logger

from comet_cc import config
from comet_cc.proxy import cert


# Hook signature: takes (method, path, body) -> possibly-modified body bytes
# or a BlockedResponse if the proxy should short-circuit the upstream call.
RewriteFn = Callable[[str, str, bytes], Awaitable["bytes | BlockedResponse"]]


class BlockedResponse:
    """Sentinel rewrite hooks can return to replace an upstream call with
    a canned error/body (e.g., to disable CC's native /compact)."""
    def __init__(self, status: int, body: bytes,
                 content_type: str = "application/json") -> None:
        self.status = status
        self.body = body
        self.content_type = content_type


async def _passthrough(method: str, path: str, body: bytes) -> bytes:
    return body


# Headers forwarded verbatim end-to-end would break things:
# - content-length: changes when we rewrite
# - content-encoding: httpx already decompresses responses
# - transfer-encoding/connection: hop-by-hop per RFC
_REQ_STRIP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}
_RESP_STRIP = _REQ_STRIP | {"content-encoding"}


class ProxyServer:
    """aiohttp-based TLS MITM. Single instance per daemon."""

    def __init__(self, rewrite: RewriteFn = _passthrough) -> None:
        self._rewrite = rewrite
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    def set_rewrite(self, fn: RewriteFn) -> None:
        self._rewrite = fn

    async def _handler(self, req: web.Request) -> web.StreamResponse:
        body = await req.read()
        url = f"{config.UPSTREAM_URL}{req.rel_url}"
        try:
            new_body = await self._rewrite(req.method, req.path, body)
        except Exception as e:
            logger.exception(f"rewrite hook crashed: {e}")
            new_body = body
        if isinstance(new_body, BlockedResponse):
            logger.info(
                f"block: {req.method} {req.path} -> "
                f"status={new_body.status} len={len(new_body.body)}"
            )
            return web.Response(
                status=new_body.status, body=new_body.body,
                headers={"content-type": new_body.content_type},
            )
        mutated = new_body is not body and new_body != body
        if mutated:
            logger.info(
                f"rewrite: {req.method} {req.path} {len(body)}B -> {len(new_body)}B"
            )
        fwd_headers = {
            k: v for k, v in req.headers.items() if k.lower() not in _REQ_STRIP
        }
        async with httpx.AsyncClient(http2=False, timeout=600.0) as client:
            async with client.stream(
                req.method, url, headers=fwd_headers, content=new_body,
            ) as upstream:
                resp = web.StreamResponse(
                    status=upstream.status_code,
                    headers={
                        k: v for k, v in upstream.headers.items()
                        if k.lower() not in _RESP_STRIP
                    },
                )
                await resp.prepare(req)
                async for chunk in upstream.aiter_bytes():
                    try:
                        await resp.write(chunk)
                    except ConnectionResetError:
                        break
                await resp.write_eof()
        return resp

    def _ssl_context(self) -> ssl.SSLContext:
        paths = cert.ensure_certs()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(paths["server"]))
        return ctx

    async def start(self) -> None:
        app = web.Application(client_max_size=128 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self._handler)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner, host=config.PROXY_HOST, port=config.PROXY_PORT,
            ssl_context=self._ssl_context(),
        )
        await self._site.start()
        logger.info(
            f"proxy listening on https://{config.PROXY_HOST}:{config.PROXY_PORT} "
            f"-> {config.UPSTREAM_URL}"
        )

    async def stop(self) -> None:
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        logger.info("proxy stopped")
