"""Graph-view web server.

Serves a single-page knowledge-graph visualization of the store.
Runs as the foreground process of `comet-cc graph` — user closes with
Ctrl+C. All data reads go through the daemon RPC (so the canonical
lock stays with the daemon).

Real-time updates: a single background poll task (`_GraphBroadcaster`)
fetches the node list from the daemon every ``POLL_INTERVAL`` seconds,
diffs against the previously broadcast snapshot, and pushes
``added_nodes / updated_nodes / removed_node_ids / added_edges /
removed_edges`` events to every connected SSE client at /api/events.
The browser applies them to its vis.DataSet incrementally so newly
saved memory nodes show up without a manual refresh."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path

from aiohttp import web

from comet_cc import client

_STATIC = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)


def _edge_key(src: str, dst: str, kind: str) -> tuple[str, str, str]:
    """Canonical key for an edge: similarity edges are bidirectional, so the
    pair is sorted; parent edges keep their direction."""
    if kind == "sim":
        a, b = sorted([src, dst])
        return (a, b, "sim")
    return (src, dst, kind)


def _edge_id(key: tuple[str, str, str]) -> str:
    """Match the id format the existing edgeStyle() helper builds in
    index.html so adds and removes refer to the same DOM identity."""
    src, dst, kind = key
    return f"{src}→{dst}:{kind}"


def _shape_graph(nodes_in: list[dict]) -> tuple[dict[str, dict], dict[tuple[str, str, str], dict]]:
    """Project the daemon's raw node list into the UI shape used by the
    /api/graph endpoint and the /api/events delta stream.

    Returns dicts keyed for diffing — node-id → node payload, edge-key
    tuple → edge payload — instead of plain lists.  Both endpoints flatten
    to ``.values()`` lists when serializing, so the wire format is unchanged
    from the original implementation."""
    known_ids = {n["node_id"] for n in nodes_in}

    out_nodes: dict[str, dict] = {}
    for n in nodes_in:
        summary = (n.get("summary") or "").strip()
        label = summary.split("\n", 1)[0][:80]
        tags = [t for t in (n.get("topic_tags") or [])
                if not t.startswith("IMPORTANCE:")]
        out_nodes[n["node_id"]] = {
            "id": n["node_id"],
            "label": label or "(untitled)",
            "importance": n.get("importance", "MED"),
            "session_id": n.get("session_id"),
            "is_child": bool(n.get("parent_node_id")),
            "tags": tags[:4],
        }

    out_edges: dict[tuple[str, str, str], dict] = {}
    for n in nodes_in:
        nid = n["node_id"]
        for peer in (n.get("links") or []):
            if peer not in known_ids:
                continue
            key = _edge_key(nid, peer, "sim")
            if key in out_edges:
                continue
            out_edges[key] = {
                "id": _edge_id(key),
                "from": key[0],
                "to": key[1],
                "kind": "sim",
            }
        parent = n.get("parent_node_id")
        if parent and parent in known_ids:
            key = _edge_key(parent, nid, "parent")
            out_edges[key] = {
                "id": _edge_id(key),
                "from": key[0],
                "to": key[1],
                "kind": "parent",
            }
    return out_nodes, out_edges


class _GraphBroadcaster:
    """Polls the daemon for graph changes and pushes diffs to SSE clients.

    A single shared poll task drives all subscribers — adding/dropping
    clients is O(1) and the daemon is hit at most once per
    POLL_INTERVAL regardless of how many browser tabs are open."""

    POLL_INTERVAL = 2.0  # seconds — compaction is bursty, sub-second polling
                        # would just thrash the daemon RPC for nothing.
    KEEPALIVE_INTERVAL = 15.0
    QUEUE_MAX = 64

    def __init__(self) -> None:
        self._clients: set[asyncio.Queue[str]] = set()
        self._lock = asyncio.Lock()
        self._last_nodes: dict[str, dict] = {}
        self._last_edges: dict[tuple[str, str, str], dict] = {}
        self._task: asyncio.Task | None = None
        self._bootstrapped = False

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._poll_loop(), name="comet-cc-graph-broadcaster"
            )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def register(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=self.QUEUE_MAX)
        async with self._lock:
            self._clients.add(q)
        return q

    async def unregister(self, q: asyncio.Queue[str]) -> None:
        async with self._lock:
            self._clients.discard(q)

    def seed_state(self, nodes: dict[str, dict],
                   edges: dict[tuple[str, str, str], dict]) -> None:
        """Initial-fetch handlers (`/api/graph`) call this so the first
        broadcasted diff reflects changes since the page load, not since
        the broadcaster started."""
        self._last_nodes = dict(nodes)
        self._last_edges = dict(edges)
        self._bootstrapped = True

    async def _poll_loop(self) -> None:
        backoff = self.POLL_INTERVAL
        while True:
            try:
                await asyncio.sleep(backoff)
                resp = await asyncio.to_thread(client.list_all_nodes, 10.0)
                if not resp or not resp.get("ok"):
                    # Daemon down or slow — back off briefly.  EventSource
                    # auto-reconnects on the browser side anyway, so dropped
                    # events during an outage just mean a state catch-up
                    # when the daemon returns.
                    backoff = min(backoff * 1.5, 10.0)
                    continue
                backoff = self.POLL_INTERVAL
                nodes_in = resp.get("nodes") or []
                new_nodes, new_edges = _shape_graph(nodes_in)
                if not self._bootstrapped:
                    self._last_nodes = new_nodes
                    self._last_edges = new_edges
                    self._bootstrapped = True
                    continue
                diff = self._diff(new_nodes, new_edges)
                if diff is not None:
                    self._last_nodes = new_nodes
                    self._last_edges = new_edges
                    await self._broadcast(diff)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("graph broadcaster poll failed: %s", e)

    def _diff(
        self,
        new_nodes: dict[str, dict],
        new_edges: dict[tuple[str, str, str], dict],
    ) -> dict | None:
        added_nodes: list[dict] = []
        updated_nodes: list[dict] = []
        for nid, n in new_nodes.items():
            old = self._last_nodes.get(nid)
            if old is None:
                added_nodes.append(n)
            elif old != n:
                updated_nodes.append(n)
        removed_node_ids = [
            nid for nid in self._last_nodes if nid not in new_nodes
        ]

        added_edges: list[dict] = []
        for key, e in new_edges.items():
            if key not in self._last_edges:
                added_edges.append(e)
        removed_edges: list[dict] = [
            self._last_edges[key]
            for key in self._last_edges
            if key not in new_edges
        ]

        if not (added_nodes or updated_nodes or removed_node_ids
                or added_edges or removed_edges):
            return None
        return {
            "added_nodes": added_nodes,
            "updated_nodes": updated_nodes,
            "removed_node_ids": removed_node_ids,
            "added_edges": added_edges,
            "removed_edges": removed_edges,
        }

    async def _broadcast(self, diff: dict) -> None:
        payload = json.dumps(diff)
        async with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Slow consumer; drop.  EventSource will reconnect on the
                # next event after the queue drains, and the next /api/graph
                # refresh re-syncs in the worst case.
                pass


_broadcaster = _GraphBroadcaster()


async def _index(_req: web.Request) -> web.FileResponse:
    return web.FileResponse(_STATIC / "index.html")


async def _api_graph(_req: web.Request) -> web.Response:
    resp = await asyncio.to_thread(client.list_all_nodes, 10.0)
    if not resp or not resp.get("ok"):
        return web.json_response(
            {"error": "daemon not reachable — run `comet-cc daemon start`"},
            status=503,
        )
    nodes_in = resp.get("nodes") or []
    out_nodes, out_edges = _shape_graph(nodes_in)
    # Align the broadcaster's last-seen state with what the page just
    # rendered.  Without this, the first SSE diff after a hot reload
    # would either spuriously re-add every node (if broadcaster hadn't
    # bootstrapped yet) or miss adds that landed between bootstrap and
    # this fetch.
    _broadcaster.seed_state(out_nodes, out_edges)
    return web.json_response({
        "nodes": list(out_nodes.values()),
        "edges": list(out_edges.values()),
    })


async def _api_events(req: web.Request) -> web.StreamResponse:
    """Server-Sent Events stream of graph diffs.  Browser opens this with
    EventSource and applies the diffs to its vis.DataSet."""
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        # Disable response buffering for proxies that honor it (nginx).
        "X-Accel-Buffering": "no",
    })
    resp.enable_chunked_encoding()
    await resp.prepare(req)

    queue = await _broadcaster.register()
    try:
        # Friendly "stream open" comment so the browser's EventSource fires
        # `open` immediately even before the first real event lands.
        await resp.write(b": connected\n\n")
        while True:
            try:
                payload = await asyncio.wait_for(
                    queue.get(),
                    timeout=_GraphBroadcaster.KEEPALIVE_INTERVAL,
                )
                await resp.write(f"data: {payload}\n\n".encode("utf-8"))
            except asyncio.TimeoutError:
                # Periodic keepalive comment — load balancers and some
                # browsers idle out long-silent SSE streams otherwise.
                await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    except Exception as e:  # noqa: BLE001
        logger.debug("SSE writer ended: %s", e)
    finally:
        await _broadcaster.unregister(queue)
    return resp


async def _api_node(req: web.Request) -> web.Response:
    nid = req.match_info["node_id"]
    resp = client.get_node(nid)
    if not resp:
        return web.json_response({"error": "daemon unreachable"}, status=503)
    if not resp.get("ok"):
        return web.json_response({"error": resp.get("error", "not found")},
                                 status=404)
    node = resp["node"]
    linked = client.list_linked_nodes(nid, timeout=3.0) or {}
    node["_linked_children"] = [
        {"id": c["node_id"], "label": (c.get("summary") or "")[:80]}
        for c in (linked.get("nodes") or [])
    ]
    # Tier-3 raw turns for the bottom scroll viewer. `read_memory depth=2`
    # returns [[position, role, text], ...]. Daemon RPC is fast (sqlite read).
    raw = client.read_memory(nid, depth=2, timeout=10.0) or {}
    node["_raw_turns"] = raw.get("turns") or []
    return web.json_response({"ok": True, "node": node})


async def _on_startup(_app: web.Application) -> None:
    await _broadcaster.start()


async def _on_cleanup(_app: web.Application) -> None:
    await _broadcaster.stop()


def run(host: str = "127.0.0.1", port: int = 8450) -> None:
    app = web.Application()
    app.router.add_get("/", _index)
    app.router.add_get("/api/graph", _api_graph)
    app.router.add_get("/api/events", _api_events)
    app.router.add_get("/api/node/{node_id}", _api_node)
    app.router.add_static("/static", _STATIC, show_index=False)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=host, port=port, access_log=None, print=None)
