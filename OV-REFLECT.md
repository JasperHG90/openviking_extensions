# ov-reflect

Reflection for OpenViking: periodically re-read a slice of memory, synthesize
observations with cited evidence, detect contradictions, write back. Ported
selectively from [memex](https://github.com/JasperHG90/memex)
(`packages/core/src/memex_core/memory/{reflect,contradiction}`), adapted to
OpenViking's primitives.

## Why

OV has no reflection. `session/memory/core.py:8` names a
`ConsolidationExtractContextProvider` ("定时整理已有记忆") but ships only the
abstract base — the seam was designed and never filled. What OV has instead is
within-session working-memory compaction, trajectory-triggered experience
consolidation, and resource→memory traceability. None of them revisit stored
memory.

## What v1 does

```
changed = query_L2(updated_at > watermark, select=uri)   # cheap, exact, no lag
for dir, uris in group_by_parent(changed):
    scope = read_L1(dir)                                 # overview, context only
    mems  = abstracts(uris) + neighbors() + tail_sample()
    obs   = llm_propose(scope, mems)                     # call 1
    con   = llm_contradict(mems)                         # call 2
    obs   = verify_quotes(obs, mems)                     # code, no LLM
    write(obs, con)
```

One search, two LLM calls, one write pass. Not seven phases — a function.

## Load-bearing findings

| # | Finding | Where |
|---|---|---|
| 1 | Link vocabulary already exists: `belongs_to`, `related_to`, `derived_from`, `caused_by`, `contradicts`, `evolved_from`, plus `weight` (0–1) and `match_text` ("must exist verbatim") | `session/memory/dataclass.py:51`, `:133` |
| 2 | Memory text is **already in Postgres**: the L2 vector row's `content` is `strip_all_links(mf.content)` truncated at 50 KB. Files live on MinIO via AGFS | storage-model note |
| 3 | `set_tags` writes the **vector store only** — never the document store. Tag enrichment is the cheapest write, not the most expensive | `content_write.py:1468` |
| 4 | L0/L1 are **per-directory** (`{dir}/.abstract.md`, `{dir}/.overview.md`); memories are L2 files. There is no L1 for an individual memory | `abstract_overview.py:454`, `collection_schemas.py:111` |
| 5 | L1 overviews **lag deliberately** — `decide_parent_refresh` marks pending until the change ratio crosses `refresh_ratio`. Never use L1 timestamps as a change signal | `freshness_policy.py:56` |
| 6 | OV's DSPy equivalent is `StructuredVLM.complete_model(prompt, model_class)`, plus `get_json_schema_prompt`, `parse_json_from_response`, and `FaultTolerantBaseModel` | `models/vlm/llm.py`, `dataclass.py:363` |
| 7 | Custom memory types load from YAML via `config.memory.custom_templates_dir` — a real documented extension point | `memory_type_registry.py:47`, `memory_config.py:31` |
| 8 | OV's link repair on delete is **better than memex's**: `_inherit_deleted_link_relations` walks links *and* backlinks, remaps endpoints, and fixes the file at the far end of each edge. `_resolve_replacement_uri` follows chains with a cycle guard | `memory_updater.py:1266`, `:168` |
| 9 | But it fires on the **delete path only**, and `delete_replacements` lives on the transient `ResolvedOperations` — nothing persists A→B→C | `dataclass.py:352` |
| 10 | OV tracks **no usage statistics**. No `retrieval_count`, `mention_count`, `last_retrieved_at`. The `last_accessed_at` in `privacy/models.py` is consent metadata | `service/search_service.py` |
| 11 | `HybridRetriever` is already in every search's hot path and already maps level-suffixed URIs back — the counting seam | `ov_ext/retrieval/retriever.py:150` |

## Decisions and the forks behind them

1. **Observations are a custom memory type**, not lines appended to
   `memories/entities/`. Extracted facts and synthesized claims need different
   trust levels; mixing them means you can't tell which to distrust when
   reflection is wrong.
2. **Evidence is a `StoredLink`**, not a new structure: `link_type:
   derived_from`, `match_text` = the verbatim quote, `weight` = support
   strength. Memex's quote-verification gate isn't a port — it's the field's
   existing contract.
3. **Evidence must cite L2, never L1.** L1 overviews are LLM-generated, so a
   quote can be verbatim-present in a summary that was itself wrong; the gate
   would verify the wrong thing. L1 is for scoping only.
4. **Change detection on L2, scope from L1** — because of finding 5.
5. **Cross-peer linking is an observation, not a link.** The ask was "repo A
   and repo B are solving the same problem", which is a candidate whose
   evidence spans two peers. The engine counts distinct top-level paths across
   an observation's evidence and tags the ones spanning several; a dedicated
   cross-peer pass sets `require_cross_peer` and drops the rest. A blanket
   filter would reject ordinary single-project observations, which are also
   wanted. *(Rejected: generic memory↔memory association, which degenerates.)*
6. **Port contradiction detection; skip the Beta posterior.** No confidence
   column on OV memories, and adding one costs a MinIO write per delta.
   `StoredLink.weight` already carries contestedness — compute it from the
   graph at query time. Memex needs the Beta machinery because it has a row to
   update; OV has a graph.
7. **Durable merge log** at `viking://user/jasper/reflect/merges/<date>-<slug>.md`
   — frontmatter `{merged: [uri…], into: uri, at: ts, pass: …}`, loser's full
   content in the body. Needed because of finding 9. *(Replaced an earlier
   staging-tree proposal: merges land and stay reversible, rather than sitting
   pending.)*
8. **Preference conflicts get flagged, never auto-resolved.** A contradiction
   between preferences usually means the context differs, not that one is
   stale. Policy belongs per memory type in the YAML, beside the merge ops:
   `flag` for preferences, `latest_wins` for entity facts.
9. **Use OV's `StructuredVLM`, not DSPy.** *(Reversed twice. Final reasoning:
   memex only ever calls `dspy.Predict` — no ChainOfThought, no optimizers, no
   compiled prompts — so the signatures carry only their text. Going OV-native
   costs restructuring ~134 lines of signature into prompt templates and buys
   one model config, one set of credentials, one telemetry stream, and no
   second LLM stack in a process already patching OV.)*
10. **One package.** The retrieval counter must live in the retriever, and
    reflection's tags feed MMR's tag-similarity leg — already mutually
    dependent. **Done** — `ov-retrieval` is now `ov-ext`, with `retrieval/` and
    `reflect/` as subsystems under one `install()`. `git describe` matches both
    tag prefixes so the version line continues from v0.3.0. Nothing here goes
    to PyPI, so the name change costs nothing downstream.
11. **Salience proxy** for memex's formula: urgency = changed-since-watermark;
    importance = inbound link count (free, already stored); resonance = a
    counter in `HybridRetriever`, kept in the ov-postgres schema. *(Deferred
    past v1 — sort by changed-since-watermark and cap the batch. Two of three
    terms would be permanently zero until the counters exist.)*
12. **Deletion is a trigger, at top priority.** A memory that lost its evidence
    is *wrong*, not merely stale, so it jumps the queue. In OV terms: when a
    resource is deleted, or a watch re-ingests a doc that lost a section, the
    memories citing it need a look. `resource_memory_link_service` already
    records those references (and has `unlink_resource_references_from_memory`),
    so "what rests on this resource" is answerable today — no new bookkeeping.
13. **The model proposes deletion; the code decides.** Memex's retention
    guardrail (`reflection.py:1018`) lets the LLM vote `should_drop` and then
    overrides it when enough live evidence remains. That inversion is the rule
    for every destructive path here, not just refresh — it is what makes
    unattended running acceptable, alongside the merge log in decision 7.
14. **Extract the pass protocol on the second pass, not the first.** The reuse
    boundary is real — `select` / `gather` / `synthesize` / `apply`, with the
    engine owning watermarks, citation mapping, quote verification, the merge
    log and the retention guardrail. But generalizing from one caller invents
    hooks nobody calls. Write the sweep concretely, write contradiction second,
    then lift what both share. **Never make the storage layer pluggable** — OV
    is the only backend, and the abstraction buys optionality that will never
    be spent.

## Cut from memex, deliberately

Triage (an optimization for batch volume), authority resolution (we flag, not
resolve), confidence deltas, source-diverse round-robin, validate as its own
LLM call (the quote check plus an evidence floor recovers most of it), the
seed/hunt split (gather first, propose over the gathered set), phase 4
compare/merge, phase 0 update-existing, phase 6 enrich, trends,
`entity_locks.py`, the Postgres queue and dead-lettering.

Phases 0 and 4 are also memex's most complex — CAS abandonment, stale-read
detection, the provenance index mapping — so cutting them drops the *least
portable* code, not merely the most.

Kept because they're cheap and load-bearing: **tail sampling** (~10 lines;
without it the loop only ever confirms itself), **quote verification** (~15
lines), **index-based citations** (~50 lines; stops the model inventing URIs).

## Port accounting

Measured, not estimated. Ports from memex:

| From | Lines |
|---|---|
| `reflect/prompts.py` — `ReflectMemoryContext`, `HasEvidenceIndices`, `CandidateObservation`, `NewEvidenceItem` | ~55 |
| `contradiction/signatures.py` — `CandidateUnit`, `ContradictionRelationship` (minus `authoritative`) | ~15 |
| `reflect/utils.py` — `create_citation_map`, `parse_timestamp` | ~50 |
| Signature docstrings and field descriptions, transcribed into prompt templates | ~40 |

**~160 ported, ~300 new.** Everything else in memex's reflect —
`reflection.py` (2090), `queue_service.py` (706), `contradiction/engine.py`
(458), `confidence.py` (256), `trends.py` (119), `entity_locks.py` (54) — is
not in v1.

Provenance goes in `src/ov_ext/reflect/PROVENANCE.md`: a file → source
path → verbatim/adapted/new → line-count table, plus the divergences above.
Per-file headers on the ported files.

## Todos

**v1 — build now**

- [ ] `reflect/models.py` — ported Pydantic models
- [ ] `reflect/citations.py` — `create_citation_map`, `parse_timestamp`
- [ ] `reflect/verify.py` — quote verification, evidence floor, cross-peer filter
- [ ] `reflect/prompts.py` — two templates transcribed from memex signatures
- [ ] `reflect/ports.py` — `MemoryStore` / `StructuredLLM` Protocols
- [ ] `reflect/viking.py` — adapters over OV (`VikingFS`, `StructuredVLM`)
- [ ] `reflect/engine.py` — the sweep loop
- [ ] `reflect/watermark.py` — watermark persistence
- [ ] `reflect/config.py` — `ReflectSettings`, `OV_REFLECT_` prefix
- [ ] `reflect/templates/observations.yaml` — the custom memory type
- [ ] `reflect/PROVENANCE.md`
- [ ] Tests mirroring each, against fakes; gates green (`prek run --all-files`)
- [ ] Wire into `install()` so one call patches retrieval and registers reflect
- [ ] README section

**Next, in order**

- [ ] Phase 6 enrich — cheap (Postgres-only writes), feeds MMR's tag leg
- [ ] Entity + memory consolidation, all memory types, with the merge log
- [ ] Deletion trigger — watch resource deletes and re-ingests, re-reflect the
      memories citing them at top priority (decision 12)
- [ ] Trends — lazy, from Postgres timestamps, stored as a search tag
- [ ] Compare/merge with provenance, once observation churn is visible
- [ ] Phase 0 incremental update, with the retention guardrail (decision 13)
- [ ] Retrieval counter in `HybridRetriever` → salience formula

**Uses this engine unlocks** (same machinery, different `select`)

- Cross-trajectory distillation — OV consolidates one trajectory at a time.
  "Across 40 sessions, adversarial review caught this same class of bug three
  times" is an observation no single trajectory can produce.
- Stale-doc detection — trends over ingested resources. `ls(sort_by=updated_at)`
  plus row timestamps answer "what's old" without reading a file; contradiction
  links answer "what's superseded". Only open a doc when acting on it.
- Preference drift — a contradiction pass scoped to
  `memories/preferences/jasper/**`, flagging rather than resolving (decision 8).

**Open questions**

- [ ] In-place edit staleness: OV repairs links on delete but not on rewrite.
      Cheap fix — after a reflection edit, re-verify each inbound link's
      `match_text` still appears; drop or flag the rest. Not yet specced.
- [ ] `merge_links` takes max weight on conflict and never decays, so a
      `contradicts` link sticks at its high-water mark. May not matter.

## References

- OV source: `~/.openviking/openviking-repo/openviking/`
- memex: `github.com/JasperHG90/memex`, `packages/core/src/memex_core/memory/`
- Handoff: `viking://~/resources/handoffs/github.com/jasperhg90/openviking_extensions/2026-09-08T1055--ov-reflect-design.md`
