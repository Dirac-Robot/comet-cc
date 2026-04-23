"""Graph-view web server.

Serves a single-page knowledge-graph visualization of the store.
Runs as the foreground process of `comet-cc graph` — user closes with
Ctrl+C. All data reads go through the daemon RPC (so the canonical
lock stays with the daemon)."""
from __future__ import annotations

from pathlib import Path

from aiohttp import web

from comet_cc import client

_STATIC = Path(__file__).parent / "static"


async def _index(_req: web.Request) -> web.FileResponse:
    return web.FileResponse(_STATIC / "index.html")


async def _api_graph(_req: web.Request) -> web.Response:
    resp = client.list_all_nodes(timeout=10.0)
    if not resp or not resp.get("ok"):
        return web.json_response(
            {"error": "daemon not reachable — run `comet-cc daemon start`"},
            status=503,
        )
    nodes_in = resp.get("nodes") or []

    # Index for quick dedup lookups
    known_ids = {n["node_id"] for n in nodes_in}

    # Nodes: minimal fields the UI needs
    out_nodes = []
    for n in nodes_in:
        summary = (n.get("summary") or "").strip()
        label = summary.split("\n", 1)[0][:80]
        tags = [t for t in (n.get("topic_tags") or [])
                if not t.startswith("IMPORTANCE:")]
        out_nodes.append({
            "id": n["node_id"],
            "label": label or "(untitled)",
            "importance": n.get("importance", "MED"),
            "session_id": n.get("session_id"),
            "is_child": bool(n.get("parent_node_id")),
            "tags": tags[:4],
        })

    # Edges: similarity (from links) + parent-child (from parent_node_id).
    # Similarity is bidirectional, so dedupe pairs by sorted ids.
    seen_sim: set[tuple[str, str]] = set()
    out_edges = []
    for n in nodes_in:
        nid = n["node_id"]
        for peer in (n.get("links") or []):
            if peer not in known_ids:
                continue
            key = tuple(sorted([nid, peer]))
            if key in seen_sim:
                continue
            seen_sim.add(key)
            out_edges.append({"from": key[0], "to": key[1], "kind": "sim"})
        parent = n.get("parent_node_id")
        if parent and parent in known_ids:
            out_edges.append({"from": parent, "to": nid, "kind": "parent"})

    return web.json_response({"nodes": out_nodes, "edges": out_edges})


async def _api_node(req: web.Request) -> web.Response:
    nid = req.match_info["node_id"]
    resp = client.get_node(nid)
    if not resp:
        return web.json_response({"error": "daemon unreachable"}, status=503)
    if not resp.get("ok"):
        return web.json_response({"error": resp.get("error", "not found")},
                                 status=404)
    node = resp["node"]
    # Tier-1 raw-turn count via separate RPC (optional nicety)
    linked = client.list_linked_nodes(nid, timeout=3.0) or {}
    node["_linked_children"] = [
        {"id": c["node_id"], "label": (c.get("summary") or "")[:80]}
        for c in (linked.get("nodes") or [])
    ]
    return web.json_response({"ok": True, "node": node})


def run(host: str = "127.0.0.1", port: int = 8450) -> None:
    app = web.Application()
    app.router.add_get("/", _index)
    app.router.add_get("/api/graph", _api_graph)
    app.router.add_get("/api/node/{node_id}", _api_node)
    app.router.add_static("/static", _STATIC, show_index=False)
    web.run_app(app, host=host, port=port, access_log=None, print=None)
