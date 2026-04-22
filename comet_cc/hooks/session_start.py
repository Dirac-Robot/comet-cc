"""SessionStart hook — opportunistically spawn the daemon.

If the daemon isn't running, try to bring it up so subsequent hooks
(especially UserPromptSubmit, which needs the warm embedder) are fast.
Best-effort: failure never blocks CC.
"""

from __future__ import annotations

import time

from loguru import logger

from comet_cc import config, daemon_mgmt
from comet_cc.core.store import NodeStore
from comet_cc.hooks._common import bail_if_internal,  abort_ok, read_payload, setup_logging


def main() -> None:
    setup_logging("SessionStart"); bail_if_internal()
    payload = read_payload()
    session_id = payload.get("session_id") or ""

    # Touch the store so sqlite file + tables exist before any other hook fires.
    NodeStore(config.store_path()).close()

    t0 = time.perf_counter()
    ok = daemon_mgmt.ensure_running(wait_seconds=15.0)
    dt = time.perf_counter() - t0
    if ok:
        logger.info(f"session_start: session={session_id} daemon ready in {dt:.2f}s")
    else:
        logger.warning(
            f"session_start: session={session_id} daemon NOT ready after {dt:.2f}s — "
            "hooks will fall back to direct mode"
        )

    abort_ok()


if __name__ == "__main__":
    main()
