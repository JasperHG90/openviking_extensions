"""How far the last sweep got.

A single timestamp. The sweep asks for memories updated after it, and moves it
forward only when a sweep finishes -- so a run that dies halfway re-reads its
batch next time rather than skipping it. Re-reading is cheap and idempotent:
the same memories produce the same observations, and writing an observation
that already exists is a merge rather than a duplicate.

The overlap is deliberate too. The watermark advances to the newest row the
sweep actually *read*, not to the moment it ran, so a memory written while the
sweep was in flight is picked up next time instead of falling in the gap
between "when I started" and "what I saw".

A failed batch holds the mark back, which is what stops a provider outage
dropping memories permanently -- but held back forever is its own failure. A
batch that fails every time would block everything behind it, so the mark
counts consecutive sweeps that made no progress and, past a limit, steps over
the blockage and says so loudly. memex solves this with a work queue and a
dead-letter table; this is the same idea at the scale of one counter.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from pydantic import BaseModel, Field

__all__ = ["Watermark"]


class Watermark(BaseModel):
    """The high-water mark of a reflection sweep.

    A model rather than a bare timestamp because it is written to and read
    from the store as JSON, and a hand-parsed dict would defer a malformed
    file to whoever indexes into it.
    """

    last_seen: datetime = Field(
        description=(
            "Newest `updated_at` the previous sweep actually read. Memories "
            "at or before this are considered done."
        )
    )
    swept_at: datetime = Field(
        description="When that sweep finished, for operators reading the file."
    )
    stalls: int = Field(
        default=0,
        ge=0,
        description=(
            "Consecutive sweeps that read something but could not advance, "
            "because a batch failed. Reset the moment the mark moves. Past "
            "`max_stalls` the sweep steps over the blockage rather than "
            "retrying it forever."
        ),
    )

    @classmethod
    def beginning(cls) -> Watermark:
        """Return a watermark that admits everything, for a store never swept.

        The epoch in UTC rather than "now": a first run should reflect on the
        memories that already exist, not wait for the next one to be written.
        """
        epoch = datetime.fromtimestamp(0, tz=timezone.utc)
        return cls(last_seen=epoch, swept_at=epoch, stalls=0)

    @classmethod
    def loads(cls, raw: str | None) -> Watermark:
        """Parse a stored watermark, falling back to :meth:`beginning`.

        A missing file means a store that has never been swept. A corrupt one
        is treated the same way rather than raising: re-reading everything
        costs a sweep, while refusing to start costs every sweep after it.

        Parameters
        ----------
        raw :
            The file's contents, or ``None`` when it does not exist.

        Returns
        -------
        Watermark
            The stored mark, or one that admits everything.
        """
        if not raw or not raw.strip():
            return cls.beginning()
        try:
            return cls.model_validate_json(raw)
        except ValueError:
            return cls.beginning()

    def dumps(self) -> str:
        """Serialize for storage, formatted for a human who opens the file."""
        return json.dumps(json.loads(self.model_dump_json()), indent=2)

    def advanced_to(self, seen: datetime, *, now: datetime) -> Watermark:
        """Return a watermark moved forward to ``seen``, clearing any stall.

        Never moves backwards: a sweep that read only older rows -- because
        nothing new arrived, or because a batch limit truncated it -- must not
        cause the next one to re-read ground already covered.

        Parameters
        ----------
        seen :
            Newest ``updated_at`` this sweep read and finished with.
        now :
            When the sweep finished. Passed in rather than read from the clock
            so the caller controls it and tests stay deterministic.

        Returns
        -------
        Watermark
            A new mark; this one is unchanged.
        """
        advanced = max(seen, self.last_seen)
        moved = advanced > self.last_seen
        return Watermark(
            last_seen=advanced,
            swept_at=now,
            stalls=0 if moved else self.stalls,
        )

    def stalled(self, *, now: datetime) -> Watermark:
        """Return a watermark that read something but could not advance.

        Counts the stall so a batch that fails every time cannot block the
        memories behind it indefinitely.
        """
        return Watermark(last_seen=self.last_seen, swept_at=now, stalls=self.stalls + 1)

    def stepped_over(self, seen: datetime, *, now: datetime) -> Watermark:
        """Return a watermark forced past a blockage, stall count cleared.

        Used only when :attr:`stalls` has passed its limit. The memories being
        stepped over are not reflected on, which is a loss -- but a smaller one
        than every memory behind them never being reflected on either.
        """
        return Watermark(last_seen=max(seen, self.last_seen), swept_at=now, stalls=0)
