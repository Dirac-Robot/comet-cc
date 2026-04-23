"""Memory node + sensor signal schemas."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

RecallMode = Literal["passive", "active", "both"]
Importance = Literal["HIGH", "MED", "LOW"]
LogicFlow = Literal["MAINTAIN", "BROKEN"]


@dataclass
class MemoryNode:
    node_id: str
    summary: str = ""
    trigger: str = ""
    session_id: str | None = None
    depth_level: int = 1
    recall_mode: RecallMode = "active"
    topic_tags: list[str] = field(default_factory=list)
    importance: Importance = "MED"
    detailed_summary: str | None = None
    content_key: str = ""
    raw_location: str = ""
    # Hierarchical links. `parent_node_id` means "I'm a child, hide me from
    # default retrieval"; `links` is the outgoing edge list a parent node
    # carries to its children (or any peer cross-reference).
    parent_node_id: str | None = None
    links: list[str] = field(default_factory=list)
    source_links: list[str] = field(default_factory=list)
    capsule: str = ""
    created_at: float = field(default_factory=time.time)
    compaction_reason: str | None = None

    @staticmethod
    def new_id() -> str:
        return f"n_{uuid.uuid4().hex[:12]}"

    def importance_tag(self) -> str:
        return f"IMPORTANCE:{self.importance}"


@dataclass
class CognitiveLoad:
    """Sensor output."""

    logic_flow: LogicFlow = "MAINTAIN"
    load_level: int = 1  # 1..5
    redundancy_detected: bool = False

    @property
    def needs_compacting(self) -> bool:
        return self.logic_flow == "BROKEN" or self.load_level >= 4


@dataclass
class L1Memory:
    """One turn's extracted content held in the pre-compaction buffer."""

    content: str
    raw_content: str = ""
    entities: list[str] = field(default_factory=list)
    intent: str | None = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class CompactedResult:
    """Parsed output of the compacter LLM call."""

    summary: str
    trigger: str
    recall_mode: RecallMode = "active"
    topic_tags: list[str] = field(default_factory=list)
    importance: Importance = "MED"
    session_brief: str = ""
