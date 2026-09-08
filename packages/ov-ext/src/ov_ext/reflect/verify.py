"""Check the model's work in code, before any of it is written.

Everything here runs without a model call, which is the point: a second model
asked "is this true?" shares the first one's blind spots, while a substring
check does not. Three gates, cheapest first:

1. **The index resolves.** A citation outside the range the model was shown is
   a fabrication, and gets dropped.
2. **The quote is really there.** The quoted span must appear in the cited
   memory word for word. This is memex's ``verify_evidence_quotes``, and it is
   also exactly what OpenViking already promises about a link's ``match_text``
   -- so passing this gate is what makes the evidence link legal, not merely
   plausible.
3. **Enough survives.** An observation left with fewer than
   ``min_evidence`` verified quotes is dropped. memex gets this from a separate
   validate model call; a floor on verified evidence does most of the same job
   for nothing.

Whitespace is normalised on both sides before comparing. Models reflow text --
a newline becomes a space, two spaces become one -- and rejecting a quote for
that would throw away good evidence over formatting. Nothing else is relaxed:
no case folding, no fuzzy matching, no substring-of-a-substring. A quote that
differs in a word is a quote the model wrote rather than read.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .models import CandidateObservation, MemoryRow, Observation

__all__ = ["normalise", "peers_covered", "quote_is_present", "verify_observations"]

# Default floor on verified evidence. Two rather than one because a single
# quote is usually the model restating one memory, which is not an observation
# -- it is a copy.
DEFAULT_MIN_EVIDENCE = 2

_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Collapse runs of whitespace and strip the ends, for comparison only."""
    return _WHITESPACE.sub(" ", text).strip()


def quote_is_present(quote: str, haystack: str) -> bool:
    """Whether ``quote`` appears in ``haystack``, ignoring whitespace shape.

    An empty or whitespace-only quote is never present: it would match
    everything, which would turn the gate into a no-op for the one case it most
    needs to catch.

    Parameters
    ----------
    quote :
        The span the model claims to have read.
    haystack :
        The memory text it claims to have read it from.

    Returns
    -------
    bool
        True when the quote really is in the text.
    """
    needle = normalise(quote)
    if not needle:
        return False
    return needle in normalise(haystack)


def peers_covered(uris: Sequence[str], rows: Mapping[str, MemoryRow]) -> frozenset[str]:
    """The distinct projects a set of cited URIs spans.

    More than one means the observation connects separate bodies of work, which
    is the thing a cross-peer pass is looking for.
    """
    return frozenset(rows[uri].peer for uri in uris if uri in rows)


def verify_observations(
    candidates: Sequence[CandidateObservation],
    index_to_uri: Mapping[int, str],
    rows: Mapping[str, MemoryRow],
    *,
    min_evidence: int = DEFAULT_MIN_EVIDENCE,
    require_cross_peer: bool = False,
) -> tuple[list[Observation], dict[str, int]]:
    """Keep the observations whose evidence holds up.

    Parameters
    ----------
    candidates :
        What the model proposed.
    index_to_uri :
        The citation map it was given, from :func:`ov_ext.reflect.citations.citation_map`.
    rows :
        Every memory shown to the model, keyed by URI, for checking quotes
        against.
    min_evidence :
        How many verified quotes an observation needs to survive.
    require_cross_peer :
        When set, additionally drop observations whose evidence sits inside a
        single project. Off by default -- ordinary single-project observations
        are wanted too -- and turned on by a pass that exists specifically to
        find connections between projects.

    Returns
    -------
    tuple[list[Observation], dict[str, int]]
        The survivors, and counts of why the rest were dropped: keys
        ``bad_index``, ``quote_not_found``, ``too_little_evidence``,
        ``single_peer``. The counts are what tells you whether a prompt change
        made the model worse, so they are returned rather than logged.
    """
    kept: list[Observation] = []
    dropped = {
        "bad_index": 0,
        "quote_not_found": 0,
        "too_little_evidence": 0,
        "single_peer": 0,
    }

    for candidate in candidates:
        verified: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        for item in candidate.evidence:
            if item.memory_index is None:
                dropped["bad_index"] += 1
                continue
            uri = index_to_uri.get(item.memory_index)
            if uri is None:
                dropped["bad_index"] += 1
                continue
            row = rows.get(uri)
            if row is None or not quote_is_present(item.quote, row.text):
                dropped["quote_not_found"] += 1
                continue
            # The same quote cited twice is one piece of evidence, not two, and
            # counting it twice would let a candidate clear the floor alone.
            pair = (uri, normalise(item.quote))
            if pair in seen:
                continue
            seen.add(pair)
            verified.append((uri, item.quote))

        if len(verified) < min_evidence:
            dropped["too_little_evidence"] += 1
            continue

        peers = peers_covered([uri for uri, _ in verified], rows)
        if require_cross_peer and len(peers) < 2:
            dropped["single_peer"] += 1
            continue

        kept.append(
            Observation(
                title=candidate.title.strip(),
                content=candidate.content.strip(),
                evidence=tuple(verified),
                peers=peers,
            )
        )

    return kept, dropped
