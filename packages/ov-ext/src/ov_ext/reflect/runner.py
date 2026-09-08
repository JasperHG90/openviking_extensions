"""Run one sweep against a live OpenViking, and remember how far it got.

The engine takes a watermark and returns a new one; something has to load the
old one and store the new one, or every sweep starts from the beginning of time
and re-reflects the whole store. That is this module.

The watermark lives in the store rather than on local disk, so a sweep run from
a cron on one box and by hand on another resume from the same place.

Ordering matters on the write: the mark is stored only after the sweep
returns. A crash mid-sweep therefore re-reads its batch next time, which is
cheap and idempotent -- the same memories yield the same observation, and
writing an observation that already exists merges into it rather than
duplicating it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .config import ReflectSettings
from .engine import ReflectionEngine, SweepReport
from .ports import StructuredLLM
from .viking import VikingLLM, VikingStore
from .watermark import Watermark

__all__ = ["run_sweep"]

logger = logging.getLogger(__name__)


def _expand(path: str, user_id: str) -> str:
    """Expand the ``viking://~`` home alias to this user's root."""
    return path.replace("viking://~", f"viking://user/{user_id}")


async def run_sweep(
    viking_fs: Any,
    vikingdb: Any,
    ctx: Any,
    settings: ReflectSettings | None = None,
    *,
    llm: StructuredLLM | None = None,
    now: datetime | None = None,
) -> SweepReport:
    """Load the watermark, sweep, and store the mark the sweep reached.

    Parameters
    ----------
    viking_fs :
        OpenViking's filesystem.
    vikingdb :
        The vector store manager.
    ctx :
        Request context carrying the user and their permissions.
    settings :
        Behaviour toggles. Read from the environment when omitted.
    llm :
        The model. Defaults to OpenViking's configured one; injectable so a
        test can run the whole path without a provider, and so a caller can
        point one sweep at a different model without changing the server's.
    now :
        Treated as the current time when stamping the mark. Passed in so tests
        stay deterministic.

    Returns
    -------
    SweepReport
        What the sweep did. A disabled or dry-run sweep returns a report and
        stores nothing.
    """
    resolved = settings or ReflectSettings()
    store = VikingStore(viking_fs, vikingdb, ctx, resolved)
    engine = ReflectionEngine(store, llm or VikingLLM(), resolved)
    state_uri = _expand(resolved.state_path, ctx.user.user_id)

    try:
        raw = await viking_fs.read_file(state_uri, ctx=ctx)
    except Exception:
        # No mark yet is the normal first run, not a failure.
        raw = None

    before = Watermark.loads(raw)
    report, after = await engine.sweep(before, now=now or datetime.now(timezone.utc))

    if resolved.dry_run:
        logger.info("ov-ext reflect (dry run): watermark left at %s", before.last_seen)
    elif after.last_seen != before.last_seen:
        await viking_fs.write_file(state_uri, after.dumps(), ctx=ctx)

    logger.info(
        "ov-ext reflect: batches=%d proposed=%d written=%d contradictions=%d "
        "failures=%d dropped=%s",
        report.batches,
        report.proposed,
        report.written,
        report.contradictions,
        report.failures,
        report.dropped,
    )
    return report
