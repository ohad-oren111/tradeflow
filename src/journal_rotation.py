"""Size-based JSONL journal rotation (W-S14.2 Track 6b).

The orchestrator appends ``decisions.jsonl`` (~884 records/day) and the SeanBot
reconciler appends ``reconciliations.jsonl`` — both unbounded. On a long-lived
container (no recreate) these grow without limit. This rotates a journal in-place
when it exceeds a size threshold, keeping a single ``.1`` backup (the prior
backup is replaced), so worst-case disk per journal is ~2x the threshold.

Deliberately the lower-blast-radius option vs a separate daily cleanup job: the
rotation rides the existing best-effort write path, needs no cron/volume, and a
failure degrades to "keep appending" rather than dropping data.
"""

from __future__ import annotations

import logging
import pathlib

LOGGER = logging.getLogger(__name__)

# Roll a journal once it reaches this size. 5 MB ~ a few days of decisions.jsonl.
JOURNAL_MAX_BYTES = 5 * 1024 * 1024


def rotate_jsonl_if_large(path: str | pathlib.Path, max_bytes: int = JOURNAL_MAX_BYTES) -> bool:
    """If the journal at ``path`` is >= ``max_bytes``, roll it to ``<path>.1``.

    Replaces any existing ``.1`` backup (keeps exactly one). Returns True if a
    rotation happened. Best-effort: any IO error is swallowed and logged at
    debug — the caller's append must never break because rotation failed.
    """
    try:
        p = pathlib.Path(path)
        if not p.exists() or p.stat().st_size < max_bytes:
            return False
        backup = p.parent / (p.name + ".1")
        p.replace(backup)  # atomic rename; overwrites a prior .1
        LOGGER.info("[ROTATE] %s rolled to %s (>= %d bytes)", p.name, backup.name, max_bytes)
        return True
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("[ROTATE] %s rotation skipped — %s", path, exc)
        return False
