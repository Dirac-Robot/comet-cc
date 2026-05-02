#!/usr/bin/env python3
"""Demo seeder for the comet-cc graph view.

Streams synthetic memory nodes into the running daemon at a steady cadence
(default 1/sec, 200 nodes total) so the live SSE pipeline at /api/events is
visible end-to-end.  Each node is wired to a small set of pre-existing peers
via the ``links`` field, so the graph grows as a connected web rather than
disconnected islands — which is what the user actually wants to see.

Usage:

    # Make sure the daemon is up and `comet-cc graph` is open in a browser tab.
    comet-cc daemon start
    comet-cc graph &

    python scripts/demo_seed_graph.py            # default 200 / 1.0s
    python scripts/demo_seed_graph.py --count 500 --interval 0.5
    python scripts/demo_seed_graph.py --no-bundle --session demo_alt

The seeder is intentionally lightweight — no `comet -p` calls, no LLM
synthesis, no compaction queue.  It builds a `MemoryNode` directly and
hands it to the daemon's ``save_compacted_node`` RPC, which embeds the
``emb_text`` (BGE-M3 local model, no API key) and writes both row + vector
through the same code path the real proxy uses.

Cross-link similarity threshold (default 0.55) is *not* re-applied here —
the script wires links explicitly when constructing each node, which is
what shows up as `sim` edges on the canvas.  This keeps the visual demo
predictable; if you want auto-similarity linking on top, run the daemon
through a real CC session instead.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

# Make `comet_cc` importable when running from a clean checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comet_cc import client  # noqa: E402
from comet_cc.schemas import MemoryNode  # noqa: E402


# Themed sentence corpus.  Sentences inside a theme share vocabulary so
# even the manual `links` look semantically coherent on the graph; the
# embeddings BGE-M3 produces will also cluster, so retrieval-by-embedding
# would behave the same as a real session.
THEMES: dict[str, list[str]] = {
    "cad_modeling": [
        "Bracket flange diameter set to 120 mm with eight equally spaced bolt holes.",
        "Updated the rib thickness on the support arm from 5 mm to 8 mm.",
        "Loft profile chord length parameterised to drive between 40 mm and 90 mm.",
        "Added a 0.5 mm chamfer along the housing's outer top edge.",
        "Hole pattern on the mounting plate switched from rectangular to circular.",
        "Sweep guide rail respined so the cross-section stays normal to the path.",
        "Bottom face fillet relaxed from R3 to R5 to match the cast tooling radius.",
        "Renamed sketch_42 to plate_outline so the parametric drive reads clearly.",
        "Symmetry constraint added between the left and right boss features.",
        "Extruded boss height tied to a global parameter for batch resizing.",
    ],
    "drawing_engineering": [
        "Drawing sheet annotated with GD&T true position 0.1 mm at MMC.",
        "Section A-A rotated 90° so the bolt circle reads on the right view.",
        "Title block updated: revision C, weight 4.21 kg, scale 1:2.",
        "Reference dimension added between the dowel pins on view 2.",
        "Auto-balloon ran on the assembly view; 14 items numbered.",
        "Surface finish symbol Ra 1.6 placed on the bearing seat face.",
        "Datum feature symbol -A- migrated to the machined face per ASME Y14.5.",
        "Detail view scaled 4:1 to expose the threaded relief groove.",
        "Hidden line layer toggled off on the exploded isometric view.",
        "Note 7 expanded to call out anodise spec MIL-A-8625 Type II Class 2.",
    ],
    "part_search": [
        "Part DB query: hex socket head cap screw M6x20, stainless A4-70.",
        "Search returned three matches for thrust bearing ID 25 mm bore.",
        "Filtered the results to only show suppliers with ≤ 14-day lead time.",
        "Saved the matching part as PartDB_Machine_Arm/CSK_M5x12_SST.",
        "Found a near-duplicate dowel pin record; merging into the canonical entry.",
        "Bushing search restricted to flanged variants with PTFE liner.",
        "Spring catalogue lookup: free length 30 mm, rate 1.4 N/mm, music wire.",
        "Searched compatible o-rings for Bore 32 H8; AS568-027 selected.",
        "Coupling search filtered by 14 mm bore and 15 N·m torque rating.",
        "Located machined washer SKU MW-08-1.0; added to the BOM as item 12.",
    ],
    "manufacturing": [
        "Toolpath simulation flagged a collision at corner 14 of the pocket.",
        "Switched to 3-axis adaptive clearing; cycle time fell from 22 to 13 min.",
        "Generated post-processed G-code for the Haas VF-2; dry-run scheduled.",
        "Sheet metal flat pattern verified; bend allowance Y-factor 0.4.",
        "Selected feed rate 480 mm/min for the 6 mm carbide end mill in 6061.",
        "Inspection plan added: CMM probe touch points on five datum faces.",
        "Casting draft analysis: pull direction +Z, draft angles all ≥ 1°.",
        "Quoted three suppliers for batch of 50 turned shafts in 4140.",
        "Anodise queue lead time bumped to 9 days; flagged on the kanban board.",
        "Heat-treat spec on the gear: case depth 0.6 mm, surface 58–62 HRC.",
    ],
    "reconstruction": [
        "Mesh-to-CAD reconstruction recognised four extrude features and two fillets.",
        "Point-cloud subsampled to 2 M points before the reverse-engineer pass.",
        "Detected revolve axis aligned with the global Y axis; tolerance 0.1°.",
        "Hand-edited the auto-extracted sketch to remove a redundant tangent constraint.",
        "Boundary curve reconstructed from edge-loop projection on the scan.",
        "Symmetry inferred across the YZ plane; mirrored half the body to clean noise.",
        "Hole feature synthesis recovered 12 round holes on the cover plate.",
        "Bracket reconstructed from a single-photo input via the geometry agent.",
        "Smoothed STL deviation < 0.15 mm RMS against the reconstructed solid.",
        "Imported the OBJ scan, decimated to 500 k tris before feature recognition.",
    ],
    "documentation": [
        "Engineering note: assembly torque 14 N·m on the lid bolts, threadlocker blue.",
        "Updated the README of repo Spindle_Housing_v3 with revision history.",
        "ECN 2026-014 raised: bushing material change from bronze to PEEK.",
        "Wrote the inspection report; all features in spec except boss diameter +0.04.",
        "Operator instruction step 5 reworded for clarity on probe alignment.",
        "Drafted the NDA preamble for the supplier kickoff meeting on Tuesday.",
        "Captured the FEA boundary conditions in the Confluence review page.",
        "Logged a regression: feature recognition fails on cylindrical patterns ≥ 12.",
        "BOM exported to CSV; 47 line items, total mass 12.4 kg.",
        "Released drawing pack v4 to the manufacturing partner via SFTP.",
    ],
    "ai_agent": [
        "Sub-agent rebuilt the part search index after the schema migration.",
        "The CAD agent retried the loft on a relaxed tangency tolerance and succeeded.",
        "Compactor folded eight tool calls from this turn into a single bundle node.",
        "Memory retrieval surfaced the prior flange decision before the current edit.",
        "The orchestrator routed the drawing-edit subtask to the GD&T specialist.",
        "Toolbundle synthesis collapsed the 20-step toolpath revision into one summary.",
        "The agent flagged a possible interference and asked for human confirmation.",
        "Vector store cold-cached the BGE-M3 embedder; first-call latency 380 ms.",
        "Cross-session retrieval pulled a related solved bug from a prior session.",
        "The agent logged a retry on the model-fit step, succeeding on attempt two.",
    ],
}


def _build_node(theme: str, peer_ids: list[str], session: str) -> tuple[MemoryNode, str]:
    """Construct a MemoryNode + the text we'll hand to the daemon for embedding.

    Returns (node, emb_text) so the caller can pass both to the RPC."""
    sentence = random.choice(THEMES[theme])
    importance = random.choices(
        ["LOW", "MED", "HIGH"], weights=[2, 6, 2]
    )[0]
    node = MemoryNode(
        node_id=f"demo_{uuid.uuid4().hex[:10]}",
        summary=sentence,
        trigger=f"when {theme.replace('_', ' ')} is the topic",
        session_id=session,
        importance=importance,
        topic_tags=[
            f"DEMO:{theme.upper()}",
            f"IMPORTANCE:{importance}",
        ],
        # The graph endpoint dedupes (sorted-pair sim edges) so unidirectional
        # links from new → existing peers is sufficient for visual rendering.
        links=list(peer_ids),
    )
    return node, f"{node.summary} | {node.trigger}"


def _seed_initial(session: str) -> str | None:
    """Plant a single un-linked seed node so subsequent nodes have something
    to attach to.  Returns the node_id, or None on RPC failure."""
    theme = random.choice(list(THEMES.keys()))
    node, emb_text = _build_node(theme, peer_ids=[], session=session)
    resp = client.save_compacted_node(asdict(node), emb_text)
    if not resp or not resp.get("ok"):
        return None
    return node.node_id


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    p.add_argument("--count", type=int, default=400,
                   help="number of nodes to inject (default: 400)")
    p.add_argument("--interval", type=float, default=1.0,
                   help="seconds between node inserts (default: 1.0)")
    p.add_argument("--links", type=str, default="3",
                   help="links per node — either a fixed integer (e.g. '3') "
                        "or an inclusive range (e.g. '1-3') in which case "
                        "each node samples a random count in that range "
                        "from the running pool (default: 3)")
    p.add_argument("--session", default="demo_seed",
                   help="session_id all generated nodes share (default: demo_seed)")
    p.add_argument("--seed", type=int, default=None,
                   help="RNG seed for reproducible runs (default: time-based)")
    p.add_argument("--orphans", action="store_true",
                   help="emit unlinked nodes (links=[]) so they appear as "
                        "isolated singletons on the canvas. Useful when run "
                        "in parallel with a linked-node pass to demo both.")
    p.add_argument("--orphan-link-prob", type=float, default=0.0,
                   help="(orphan mode only) probability in [0,1] that an "
                        "otherwise-orphan node still gets a single random "
                        "link into the existing pool. 0.0 keeps every "
                        "orphan fully isolated (default); 0.3 bridges "
                        "roughly a third of them back to the main cluster.")
    args = p.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    # `--links` accepts either a single int or `min-max`.  Parse once up
    # front so the per-iteration loop stays a tight `random.randint`.
    raw_links = args.links.strip()
    if "-" in raw_links:
        try:
            lo_str, hi_str = raw_links.split("-", 1)
            links_min, links_max = int(lo_str), int(hi_str)
        except ValueError:
            print(f"invalid --links range: {raw_links!r}", file=sys.stderr)
            return 2
        if links_min < 0 or links_max < links_min:
            print(f"invalid --links range: {raw_links!r}", file=sys.stderr)
            return 2
    else:
        try:
            fixed = int(raw_links)
        except ValueError:
            print(f"invalid --links: {raw_links!r}", file=sys.stderr)
            return 2
        links_min = links_max = fixed

    # Fetch existing nodes — these become valid link targets for the very first
    # demo nodes, which keeps the graph contiguous if the user has been using
    # the daemon for real work alongside the demo.
    resp = client.list_all_nodes(timeout=10.0)
    if not resp or not resp.get("ok"):
        print("daemon not reachable — start it with `comet-cc daemon start`",
              file=sys.stderr)
        return 1
    pool: list[str] = [n["node_id"] for n in (resp.get("nodes") or [])]

    # On a freshly wiped store the pool is empty; plant a single seed first
    # so node #2 has something to link to.  Otherwise the first batch ends up
    # with `links=[]` and you get a sea of orphans on the canvas.
    if not pool:
        seed_id = _seed_initial(args.session)
        if seed_id is None:
            print("seed save failed — daemon may not be ready", file=sys.stderr)
            return 1
        pool.append(seed_id)
        print(f"[seed] planted root node {seed_id}")

    print(
        f"streaming {args.count} demo nodes "
        f"({args.interval}s apart, ~{args.links} links each, session={args.session})…"
    )

    inserted = 0
    started = time.time()
    try:
        for i in range(args.count):
            theme = random.choice(list(THEMES.keys()))
            if args.orphans:
                # By default fully isolated, but with --orphan-link-prob > 0
                # a fraction of orphans bridges back to the existing pool
                # via a single random link.  The pool is the *full* live
                # set (including peer orphans), so bridges can also chain
                # orphans into mini-pairs alongside main-cluster links.
                if (args.orphan_link_prob > 0
                        and pool
                        and random.random() < args.orphan_link_prob):
                    peers: list[str] = [random.choice(pool)]
                else:
                    peers = []
            else:
                target = (
                    random.randint(links_min, links_max)
                    if links_max > links_min
                    else links_max
                )
                link_count = min(target, len(pool))
                peers = random.sample(pool, link_count) if link_count else []
            node, emb_text = _build_node(theme, peers, args.session)
            resp = client.save_compacted_node(asdict(node), emb_text)
            if not resp or not resp.get("ok"):
                print(
                    f"[{i + 1:3d}/{args.count}] save failed — "
                    f"{(resp or {}).get('error', 'rpc unreachable')}",
                    file=sys.stderr,
                )
                # Brief backoff but keep going — the broadcaster will catch up
                # whenever the daemon comes back.
                time.sleep(args.interval)
                continue
            pool.append(node.node_id)
            inserted += 1
            if inserted % 10 == 0 or inserted == args.count:
                elapsed = time.time() - started
                print(
                    f"[{inserted:3d}/{args.count}] {theme:18s} "
                    f"linked={len(peers)} pool={len(pool)} "
                    f"elapsed={elapsed:5.1f}s"
                )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\ninterrupted after {inserted} node(s).")
        return 130
    print(f"done — {inserted} demo node(s) injected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
