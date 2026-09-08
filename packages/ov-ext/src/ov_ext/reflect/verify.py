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
3. **Enough distinct memories survive.** An observation resting on fewer
   than ``min_evidence`` *different* memories is dropped. The count is over
   source URIs, not quotes: three quotes from one paragraph -- or three nested
   substrings of one sentence -- are one memory restated, which is precisely
   what the floor exists to reject. memex gets this from a separate validate
   model call; a floor on distinct sources does most of the same job for
   nothing.

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

__all__ = ["areas_covered", "normalise", "quote_is_present", "verify_observations"]

# Default floor on distinct cited memories. Two rather than one because an
# observation resting on a single memory is that memory restated, which is a
# copy rather than a synthesis.
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


def areas_covered(uris: Sequence[str], rows: Mapping[str, MemoryRow]) -> frozenset[str]:
    """The distinct directories a set of cited URIs spans.

    More than one means the observation connects memories that were not written
    together, which is what a cross-area pass is looking for.
    """
    return frozenset(rows[uri].area for uri in uris if uri in rows)


def verify_observations(
    candidates: Sequence[CandidateObservation],
    index_to_uri: Mapping[int, str],
    rows: Mapping[str, MemoryRow],
    *,
    min_evidence: int = DEFAULT_MIN_EVIDENCE,
    require_cross_area: bool = False,
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
        How many *distinct memories* an observation must cite to survive.
        Counted over source URIs, so repeating one memory cannot clear it.
    require_cross_area :
        When set, additionally drop observations whose evidence sits inside a
        single directory. Off by default -- ordinary same-area observations are
        wanted too -- and turned on by a pass that exists specifically to find
        connections between things written apart.

    Returns
    -------
    tuple[list[Observation], dict[str, int]]
        The survivors, and counts of why the rest were dropped: keys
        ``bad_index``, ``quote_not_found``, ``too_little_evidence``,
        ``single_area``. The counts are what tells you whether a prompt change
        made the model worse, so they are returned rather than logged.
    """
    kept: list[Observation] = []
    dropped = {
        "bad_index": 0,
        "quote_not_found": 0,
        "too_little_evidence": 0,
        "single_area": 0,
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

        # Distinct *memories*, not distinct quotes. Counting quotes would let
        # three nested substrings of one sentence clear a floor of three, which
        # is the restatement the floor exists to reject.
        cited = {uri for uri, _ in verified}
        if len(cited) < min_evidence:
            dropped["too_little_evidence"] += 1
            continue

        areas = areas_covered(sorted(cited), rows)
        if require_cross_area and len(areas) < 2:
            dropped["single_area"] += 1
            continue

        kept.append(
            Observation(
                title=candidate.title.strip(),
                content=candidate.content.strip(),
                evidence=tuple(verified),
                areas=areas,
            )
        )

    return kept, dropped
