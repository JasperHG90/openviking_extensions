"""The one place reflection touches OpenViking.

Everything OpenViking-shaped lives behind :class:`VikingStore` and
:class:`VikingLLM`, which satisfy the protocols in :mod:`ov_ext.reflect.ports`
structurally -- OpenViking knows nothing about this package, so nothing is
inherited and the dependency points one way.

Reads run against the vector index, never the document store. The L2 rows
already carry the memory text -- OpenViking writes ``strip_all_links(content)``
into them at index time -- so a sweep can find its changed memories, gather
neighbours and read every quote it verifies without opening a single file.
Only two operations reach the document store, and only for conclusions: writing
an observation, and recording a contradiction on an existing memory.

Reflection writes observations with ``write_file`` rather than through
``remember``. ``remember`` hands text to the extractor, which would rewrite the
observation into whatever it decided the text meant -- the opposite of the
point. A memory file written directly is still a first-class memory: it is
indexed, searchable and linkable, and OpenViking's own ``MemoryFileUtils``
serializes it so the format is theirs rather than ours.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, TypeVar

from pydantic import BaseModel

from .citations import parse_timestamp
from .config import ReflectSettings
from .models import MemoryRow, Observation

__all__ = ["VikingLLM", "VikingStore"]

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Fields the change query asks for. Deliberately not `content`: this query runs
# over every memory in the store, and the text is fetched later for the handful
# that end up in a batch.
_CHANGE_FIELDS = ["uri", "updated_at"]

# Fields a row needs to become a MemoryRow.
_ROW_FIELDS = ["uri", "content", "abstract", "created_at", "updated_at"]


class VikingLLM:
    """OpenViking's structured-output model, behind the reflection protocol.

    Wraps ``StructuredVLM.complete_model``, which appends the JSON schema to
    the prompt, parses the reply and validates it against the class -- returning
    ``None`` when any of that fails. Using OpenViking's own model layer rather
    than a second client means one set of credentials, one timeout policy and
    one trace, in a process that is already OpenViking's.

    Parameters
    ----------
    vlm :
        A ``StructuredVLM``. Built from OpenViking's configured model when
        omitted.
    """

    def __init__(self, vlm: Any | None = None) -> None:
        self._vlm = vlm

    def _get_vlm(self) -> Any:
        """Return the wrapped model, building OpenViking's default on first use."""
        if self._vlm is None:
            from openviking.models.vlm.llm import StructuredVLM

            self._vlm = StructuredVLM()
        return self._vlm

    async def complete(self, prompt: str, model: type[T]) -> T | None:
        """Answer ``prompt`` as an instance of ``model``, or ``None``."""
        schema = model.model_json_schema()
        payload = await self._get_vlm().complete_json_async(prompt, schema=schema)
        if payload is None:
            return None
        try:
            return model.model_validate(payload)
        except ValueError:
            # The model answered in a shape that is not this one. Treated as a
            # non-answer rather than an error: one unparseable reply should
            # cost a batch, not the sweep.
            logger.warning("ov-ext reflect: reply did not validate as %s", model.__name__)
            return None


class VikingStore:
    """Reflection's view of OpenViking, behind the reflection protocol.

    Parameters
    ----------
    viking_fs :
        OpenViking's filesystem, for reads of generated overviews and for
        writes.
    vikingdb :
        The vector store manager, for every indexed read.
    ctx :
        Request context carrying the user and their permissions. Every call
        here is scoped by it.
    settings :
        Behaviour toggles, for the roots reflection reads and writes under.
    """

    def __init__(
        self,
        viking_fs: Any,
        vikingdb: Any,
        ctx: Any,
        settings: ReflectSettings | None = None,
    ) -> None:
        self._fs = viking_fs
        self._db = vikingdb
        self._ctx = ctx
        self._settings = settings or ReflectSettings()

    @property
    def _memory_root(self) -> str:
        """Root under which this user's memories live."""
        return f"viking://user/{self._ctx.user.user_id}/memories"

    def _row_from_record(self, record: dict[str, Any]) -> MemoryRow | None:
        """Build a :class:`MemoryRow` from an index record, or ``None``.

        Prefers ``content`` over ``abstract``: for a memory the two are nearly
        the same text, but ``content`` is the one quotes are verified against,
        and verifying against a shorter field would reject good evidence for
        having been truncated away.
        """
        uri = record.get("uri")
        text = record.get("content") or record.get("abstract")
        if not uri or not text:
            return None
        return MemoryRow(
            uri=str(uri),
            text=str(text),
            created_at=parse_timestamp(record.get("created_at")),
        )

    async def changed_since(self, moment: datetime, *, limit: int) -> list[str]:
        """Return stored URIs of memory rows updated after ``moment``."""
        from openviking.storage.expr import And, Eq, PathScope, TimeRange

        # level=2 is the memory itself. Levels 0 and 1 are the generated
        # directory abstract and overview, whose refresh deliberately lags
        # their contents -- using them as a change signal would miss changes in
        # exactly the large directories that matter most.
        condition = And(
            [
                PathScope("uri", self._memory_root),
                Eq("context_type", "memory"),
                Eq("level", 2),
                TimeRange("updated_at", start=moment),
            ]
        )
        records = await self._db.filter(
            filter=condition,
            limit=limit,
            output_fields=_CHANGE_FIELDS,
            order_by="updated_at",
            order_desc=True,
            ctx=self._ctx,
        )
        return [str(record["uri"]) for record in records if record.get("uri")]

    async def rows(self, uris: Sequence[str]) -> list[MemoryRow]:
        """Fetch the text and timestamps for specific URIs."""
        from openviking.storage.expr import In

        if not uris:
            return []
        records = await self._db.filter(
            filter=In("uri", list(uris)),
            limit=len(uris),
            output_fields=_ROW_FIELDS,
            ctx=self._ctx,
        )
        rows = (self._row_from_record(record) for record in records)
        return [row for row in rows if row is not None]

    async def neighbours(self, row: MemoryRow, *, limit: int) -> list[MemoryRow]:
        """Return memories semantically near ``row``, excluding itself."""
        result = await self._fs.search(
            query=row.text,
            target_uri=self._memory_root,
            limit=limit + 1,
            level=[2],
            ctx=self._ctx,
        )
        found: list[MemoryRow] = []
        for context in getattr(result, "matched_contexts", []) or []:
            candidate = self._row_from_context(context)
            if candidate is not None and candidate.uri != row.uri:
                found.append(candidate)
        return found[:limit]

    def _row_from_context(self, context: Any) -> MemoryRow | None:
        """Build a row from a retrieval result.

        Retrieval returns level-suffixed URIs -- ``/.abstract.md`` for L0,
        ``/.overview.md`` for L1 -- but these are level-2 results, so the URI
        is already the stored one. Guarded anyway: a suffixed URI would not
        match anything the citation map can resolve, so it is dropped rather
        than cited.
        """
        uri = getattr(context, "uri", None)
        text = getattr(context, "content", None) or getattr(context, "abstract", None)
        if not uri or not text:
            return None
        uri = str(uri)
        if uri.endswith("/.abstract.md") or uri.endswith("/.overview.md"):
            return None
        return MemoryRow(
            uri=uri,
            text=str(text),
            created_at=parse_timestamp(getattr(context, "created_at", None)),
        )

    async def tail_sample(self, *, limit: int) -> list[MemoryRow]:
        """Return a few memories from the far end of the store.

        memex samples at random with ``ORDER BY random()``. OpenViking's filter
        API has no random ordering, so this takes the *oldest* rows instead --
        a different mechanism for the same purpose. What matters is that the
        model sees memories nothing selected for resembling its candidates, and
        the oldest are the least likely to have been surfaced by a neighbour
        search over recent changes.
        """
        from openviking.storage.expr import And, Eq, PathScope

        if limit <= 0:
            return []
        records = await self._db.filter(
            filter=And(
                [
                    PathScope("uri", self._memory_root),
                    Eq("context_type", "memory"),
                    Eq("level", 2),
                ]
            ),
            limit=limit,
            output_fields=_ROW_FIELDS,
            order_by="updated_at",
            order_desc=False,
            ctx=self._ctx,
        )
        rows = (self._row_from_record(record) for record in records)
        return [row for row in rows if row is not None]

    async def read_overview(self, directory: str) -> str | None:
        """Return a directory's generated L1 overview, or ``None`` if absent."""
        try:
            content = await self._fs.read_file(
                f"{directory.rstrip('/')}/.overview.md", ctx=self._ctx
            )
        except Exception:
            # A directory with no overview yet is the normal case, not a
            # failure: it is background for the prompt and the sweep runs
            # without it.
            return None
        return str(content) if content else None

    async def write_observation(self, observation: Observation) -> str:
        """Write an observation as a memory file and return its URI."""
        from openviking.session.memory.dataclass import MemoryFile
        from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

        topic = _slug(next(iter(sorted(observation.peers)), "general").split("/")[-1])
        name = _slug(observation.title)
        root = self._settings.observations_root.replace(
            "viking://~", f"viking://user/{self._ctx.user.user_id}"
        )
        uri = f"{root.rstrip('/')}/{topic}/{name}.md"

        links = [
            {
                "from_uri": uri,
                "to_uri": source_uri,
                "link_type": "derived_from",
                "match_text": quote,
                "weight": 1.0 / len(observation.evidence),
                "description": "",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            for source_uri, quote in observation.evidence
        ]
        memory_file = MemoryFile(
            uri=uri,
            content=_render(observation),
            metadata={"topic": topic, "name": name, "links": links},
        )
        await self._fs.write_file(
            uri, MemoryFileUtils.serialize(memory_file), ctx=self._ctx
        )
        return uri

    async def link(
        self,
        from_uri: str,
        to_uri: str,
        *,
        link_type: str,
        match_text: str | None = None,
        weight: float = 0.5,
    ) -> None:
        """Record a typed edge on an existing memory file.

        This is the one write that touches a file reflection did not author, so
        it reads, appends and writes back rather than replacing: everything
        already in the file, links included, stays.
        """
        from openviking.session.memory.merge_op.link_merge import merge_links
        from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

        raw = await self._fs.read_file(from_uri, ctx=self._ctx)
        memory_file = MemoryFileUtils.read(raw, uri=from_uri)
        link = {
            "from_uri": from_uri,
            "to_uri": to_uri,
            "link_type": link_type,
            "weight": weight,
            "description": "",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if match_text is not None:
            link["match_text"] = match_text
        # OpenViking's own merge: dedupes on (from, to, match_text) and keeps
        # the higher weight, so recording the same tension twice is idempotent.
        memory_file.links = merge_links(memory_file.links or [], [link])
        await self._fs.write_file(
            from_uri, MemoryFileUtils.serialize(memory_file), ctx=self._ctx
        )


def _slug(text: str) -> str:
    """Reduce free text to a lowercase, underscore-joined path segment."""
    cleaned = "".join(char if char.isalnum() else " " for char in text.lower())
    words = cleaned.split()
    return "_".join(words[:5]) or "untitled"


def _render(observation: Observation) -> str:
    """Format an observation as the Markdown body of its memory file.

    The shape matches what ``observations.yaml`` tells the extractor to expect:
    a title, the claim, then an Evidence section whose bullets are the verified
    quotes. Every quote here has already been found in the memory it cites.
    """
    lines = [f"# {observation.title}", "", observation.content, "", "## Evidence", ""]
    lines += [f'- [{uri}]({uri}): "{quote}"' for uri, quote in observation.evidence]
    return "\n".join(lines)
