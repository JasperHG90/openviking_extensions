# Where this came from

Reflection is ported from [memex](https://github.com/JasperHG90/memex),
`packages/core/src/memex_core/memory/`, at the state of `main` on 2026-09-08.
This file is the itemized accounting: what came across, what was rewritten,
what was deliberately left behind.

## Per file

Counted with docstrings and comments excluded, so the numbers compare code
against code. Two kinds of borrowing are separated, because they are not the
same thing: **code** that came across as code, and **text** — prompt
instructions and Pydantic field descriptions — which is what the model
actually reads and is where memex's accumulated learning about failure modes
lives.

| File | Source | Code lines | Ported code | Ported text |
|---|---|---|---|---|
| `models.py` | `reflect/prompts.py`, `contradiction/signatures.py` | 90 | ~50 (field declarations) | 6-line `EVIDENCE_INDEX_DESCRIPTION`, 15 field descriptions |
| `citations.py` | `reflect/utils.py` | 40 | ~28 (`citation_map`, `parse_timestamp`) | — |
| `prompts.py` | `reflect/prompts.py`, `contradiction/signatures.py` | 38 | 0 | 36 lines of instruction, transcribed from the signature docstrings |
| `verify.py` | `reflect/trends.py` (`verify_evidence_quotes`) | 67 | 0 — the idea, reimplemented | — |
| `engine.py` | `reflect/reflection.py` | 199 | 0 — phase *sequence* followed, no code lifted | — |
| `viking.py` | — | 208 | 0 | — |
| `config.py` | — | 109 | 0 | — |
| `register.py`, `ports.py`, `watermark.py`, `__init__.py` | — | 127 | 0 | — |
| **Total** | | **878** | **~78** | **~57** |

So: **about 78 of 878 code lines are ported**, plus roughly 57 lines of prompt
and field text carried across near-verbatim. The rest is new, and almost all
of it is the part that touches OpenViking — which had to be written either way,
because memex's equivalent is bound to a schema this package does not have.

The low ratio is the expected result rather than a disappointment. memex's
value here is concentrated in its declarative layer; its orchestration layer
exists to make row updates safe under concurrency, which is a problem
OpenViking's file-behind-a-URI model does not pose.

## Deliberate divergences

1. **No DSPy.** memex drives every call through `dspy.Predict`. Nothing else —
   no `ChainOfThought`, no optimizers, no compiled prompts — so the signatures
   carry only their text, and that text ports without the dependency.
   OpenViking's `StructuredVLM.complete_model` does the same job, which keeps
   one model config, one set of credentials and one trace in a process that is
   already OpenViking's.

2. **No `MentalModel` table, no CAS.** memex stores observations as rows and
   guards concurrent updates with a version column. An observation here is a
   memory file behind a URI, so most of `reflection.py` — phases 0, 4 and 5,
   `AdvisoryLockTakenError`, `RefreshStaleReadError`, `RefreshCASAbandonedError`
   — has nothing to guard and is not ported.

3. **No Beta posterior.** `confidence.py` maintains a `Beta(1,1)` mean and
   variance per unit, and contradiction applies signed deltas to it. There is
   no confidence column on an OpenViking memory and adding one would cost a
   document-store write per delta. `StoredLink.weight` already carries how
   strongly two memories disagree, so contestedness is read off the graph
   instead of stored. The cost: no variance, so memex's
   "reflect on what you are least certain about" prioritisation is not
   available.

4. **No queue.** `queue_service.py` is a Postgres work queue with
   `SELECT … FOR UPDATE SKIP LOCKED`, retry counts and dead-lettering. A
   watermark file replaces it. `calculate_priority` — the salience formula —
   is not ported either: two of its three terms need counters OpenViking does
   not keep, so it would weight on permanent zeros.

5. **No triage stage.** memex flags which new units look like corrections
   before classifying them, to avoid classifying hundreds per batch. A sweep
   here sees a handful, so it asks directly.

6. **No authority resolution.** `ContradictionRelationship.authoritative` and
   the later-date-wins default are dropped. Reflection records that two
   memories are in tension and leaves the resolution to a person, so a field
   naming a winner would be one nothing reads.

7. **Tail sampling by age, not randomness.** `_sample_tail_memories` uses
   `ORDER BY random()`. OpenViking's filter API has no random ordering, so
   `tail_sample` takes the oldest rows — a different mechanism for the same
   purpose, which is that the model must see memories nothing selected for
   resembling its own candidates.

8. **Quote verification is a gate, not a report.** memex's
   `verify_evidence_quotes` returns errors that a caller may act on. Here a
   failed quote drops its evidence, and an observation left under the floor is
   discarded before anything is written.

9. **Cross-peer evidence is new.** No memex equivalent. An observation records
   which areas of the store its evidence spans, and a pass can require more
   than one.

10. **Reinforcement is not recorded.** memex applies a positive confidence
    delta for `reinforce`. Agreement is the normal state of a memory store, so
    linking it would add an edge to most pairs and bury the disagreements.

## Not ported at all

`reflect/reflection.py` (2090 lines), `reflect/queue_service.py` (706),
`contradiction/engine.py` (458), `confidence.py` (256), `reflect/trends.py`
(119 — the trend computation itself is queued, not dropped),
`reflect/entity_locks.py` (54 — needed only for parallel workers),
`reflect/exceptions.py` (56 — every one of them describes a CAS failure mode
that does not exist here).
