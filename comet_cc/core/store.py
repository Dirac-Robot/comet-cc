"""sqlite-backed NodeStore — persistent home for compacted memory nodes."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

from comet_cc.schemas import MemoryNode

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    trigger TEXT NOT NULL,
    recall_mode TEXT NOT NULL,
    importance TEXT NOT NULL,
    topic_tags TEXT NOT NULL,
    session_id TEXT,
    depth_level INTEGER DEFAULT 1,
    compaction_reason TEXT,
    created_at REAL NOT NULL,
    embedding BLOB,
    detailed_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_session ON nodes(session_id);
CREATE INDEX IF NOT EXISTS idx_recall ON nodes(recall_mode);
CREATE INDEX IF NOT EXISTS idx_created ON nodes(created_at);

CREATE TABLE IF NOT EXISTS session_briefs (
    session_id TEXT PRIMARY KEY,
    brief TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS all_tags (
    tag TEXT PRIMARY KEY
);

-- Tier-3 storage: verbatim turns absorbed into each node. Written at
-- compact time, read on `read-node --depth 2`. FK-less cascade because
-- sqlite's FK enforcement is off by default — purge happens via
-- delete_raw_turns() if a node is removed.
CREATE TABLE IF NOT EXISTS raw_turns (
    node_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (node_id, position)
);
CREATE INDEX IF NOT EXISTS idx_rawturns_node ON raw_turns(node_id);
"""

_COLUMNS = (
    "node_id, summary, trigger, recall_mode, importance, topic_tags, "
    "session_id, depth_level, compaction_reason, created_at, embedding, "
    "detailed_summary"
)


class NodeStore:
    """Thread-safe sqlite node persistence + embedding storage.

    Embeddings live alongside nodes (float32 BLOB) — single file, atomic,
    no separate faiss index to keep in sync. Vector search is numpy cosine
    over the full recall_mode='active'/'both' slice; fine up to ~10k nodes.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        # Forward migration for stores created before detailed_summary /
        # raw_turns existed. Idempotent — sqlite raises if column exists.
        try:
            self._conn.execute("ALTER TABLE nodes ADD COLUMN detailed_summary TEXT")
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    def save_node(self, node: MemoryNode, embedding: np.ndarray | None = None) -> None:
        blob = embedding.astype(np.float32).tobytes() if embedding is not None else None
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO nodes ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    node.node_id, node.summary, node.trigger,
                    node.recall_mode, node.importance,
                    json.dumps(node.topic_tags),
                    node.session_id, node.depth_level,
                    node.compaction_reason, node.created_at,
                    blob, node.detailed_summary,
                ),
            )
            for tag in node.topic_tags:
                self._conn.execute(
                    "INSERT OR IGNORE INTO all_tags (tag) VALUES (?)", (tag,),
                )
            self._conn.commit()

    def save_raw_turns(self, node_id: str,
                       turns: list[tuple[str, str]]) -> None:
        """Persist tier-3 raw turn data for a node. `turns` is a list of
        (role, text) pairs in original order."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM raw_turns WHERE node_id = ?", (node_id,),
            )
            self._conn.executemany(
                "INSERT INTO raw_turns (node_id, position, role, text) "
                "VALUES (?, ?, ?, ?)",
                [(node_id, i, role, text) for i, (role, text) in enumerate(turns)],
            )
            self._conn.commit()

    def get_raw_turns(self, node_id: str) -> list[tuple[int, str, str]]:
        """Returns [(position, role, text), ...] in original order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT position, role, text FROM raw_turns "
                "WHERE node_id = ? ORDER BY position",
                (node_id,),
            ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def update_detailed_summary(self, node_id: str, detailed: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE nodes SET detailed_summary = ? WHERE node_id = ?",
                (detailed, node_id),
            )
            self._conn.commit()

    def get_node(self, node_id: str) -> MemoryNode | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM nodes WHERE node_id = ?", (node_id,),
            ).fetchone()
        return _row_to_node(row) if row else None

    def list_passive(
        self, session_id: str | None = None, *, cross_session: bool = False,
    ) -> list[MemoryNode]:
        """Passive + both nodes. When `session_id` is given and
        `cross_session=False`, results are scoped to that session — no
        handoff leakage. Pass `cross_session=True` (or `session_id=None`)
        for global retrieval across all sessions."""
        with self._lock:
            if session_id and not cross_session:
                rows = self._conn.execute(
                    f"SELECT {_COLUMNS} FROM nodes "
                    "WHERE recall_mode IN ('passive', 'both') "
                    "  AND session_id = ? "
                    "ORDER BY created_at DESC",
                    (session_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT {_COLUMNS} FROM nodes "
                    "WHERE recall_mode IN ('passive', 'both') "
                    "ORDER BY created_at DESC"
                ).fetchall()
        return [_row_to_node(r) for r in rows]

    def list_active_with_embeddings(
        self, session_id: str | None = None, *, cross_session: bool = False,
    ) -> list[tuple[MemoryNode, np.ndarray]]:
        """Active + both nodes with embeddings. Scoping matches list_passive."""
        with self._lock:
            if session_id and not cross_session:
                rows = self._conn.execute(
                    f"SELECT {_COLUMNS} FROM nodes "
                    "WHERE recall_mode IN ('active', 'both') "
                    "  AND embedding IS NOT NULL "
                    "  AND session_id = ?",
                    (session_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT {_COLUMNS} FROM nodes "
                    "WHERE recall_mode IN ('active', 'both') "
                    "  AND embedding IS NOT NULL"
                ).fetchall()
        out = []
        for row in rows:
            node = _row_to_node(row)
            emb = np.frombuffer(row[10], dtype=np.float32) if row[10] else None
            if emb is not None:
                out.append((node, emb))
        return out

    def get_all_tags(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT tag FROM all_tags").fetchall()
        return {r[0] for r in rows}

    def save_session_brief(self, session_id: str, brief: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO session_briefs (session_id, brief, updated_at) "
                "VALUES (?, ?, ?)",
                (session_id, brief, time.time()),
            )
            self._conn.commit()

    def load_session_brief(self, session_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT brief FROM session_briefs WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row[0] if row else ""

    def list_session_nodes(self, session_id: str) -> list[MemoryNode]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM nodes WHERE session_id = ? "
                "ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        return [_row_to_node(r) for r in rows]

    def delete(self, node_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM nodes WHERE node_id = ?", (node_id,))
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _row_to_node(row) -> MemoryNode:
    return MemoryNode(
        node_id=row[0], summary=row[1], trigger=row[2],
        recall_mode=row[3], importance=row[4],
        topic_tags=json.loads(row[5]),
        session_id=row[6],
        depth_level=row[7] if row[7] is not None else 1,
        compaction_reason=row[8],
        created_at=row[9],
        # row[10] = embedding (opaque blob, handled elsewhere)
        detailed_summary=row[11] if len(row) > 11 else None,
    )
