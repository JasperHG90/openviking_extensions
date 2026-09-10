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
| `models.py` | `reflect/prompts.py` | 71 | ~30 (field declarations) | 6-line `EVIDENCE_INDEX_DESCRIPTION`, 9 field descriptions |
| `citations.py` | `reflect/utils.py` | 47 | ~28 (`citation_map`, `parse_timestamp`) | — |
| `prompts.py` | `reflect/prompts.py` | 27 | 0 | 18 lines of instruction, transcribed from the signature docstring |
| `verify.py` | `reflect/trends.py` (`verify_evidence_quotes`) | 68 | 0 — the idea, reimplemented | — |
| `capture.py` | — | 173 | 0 — reads OpenViking's patch blocks, replays them through its own `PatchOp` | — |
| `engine.py` | `reflect/reflection.py` | 232 | 0 — phase *sequence* followed, no code lifted | — |
| `viking.py` | — | see below | 0 | — |
| `config.py`, `register.py`, `runner.py`, `ports.py`, `watermark.py`, `exceptions.py`, `__init__.py` | — | see below | 0 | — |

So: **about 58 code lines are ported** out of 1,554, plus roughly 39 lines of
prompt and field text carried across near-verbatim. Run the snippet at the foot of this file for the current totals; the ratio matters more than
the absolute, and it is low by design. The rest is new, and almost all of it is
the part that touches OpenViking — which had to be written either way, because
memex's equivalent is bound to a schema this package does not have.

The low ratio is the expected result rather than a disappointment. memex's
value here is concentrated in its declarative layer; its orchestration layer
exists to make row updates safe under concurrency, which is a problem
OpenViking's file-behind-a-URI model does not pose.

## Deliberate divergences

1. **No DSPy.** memex drives every call through `dspy.Predict`. Nothing else —
   no `ChainOfThought`, no optimizers, no compiled prompts — so the signatures
   carry only their text, and that text ports without the dependency.
   `openviking_cli.utils.llm.StructuredLLM` does the same job, which keeps one
   model config, one set of credentials and one trace in a process that is
   already OpenViking's. Note it is that class and not
   `openviking.models.vlm.llm.StructuredVLM`: the latter builds a client from a
   config dict and an empty one defaults to OpenAI, which would be a second
   model stack wearing OpenViking's name.

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

6. **No contradiction detection at all.** memex's `contradiction/` package
   was ported and then removed on 2026-09-10, so nothing from it survives here.
   A batch is grouped by directory rather than by subject, so the model was
   being asked whether a note about a package rename contradicted a line from a
   README -- and it answered, because it was asked to. Every pair it returned
   was an artifact of the question. `ContradictionRelationship` (and memex's
   `authoritative` field, which was already dropped) are gone with it.

7. **Tail sampling reads a window, then samples it.** `_sample_tail_memories`
   uses `ORDER BY random()`. OpenViking's filter API has no random ordering, so
   `tail_sample` reads a wider slice of the least recently updated memories and
   picks from it with `random.Random.sample`. Taking the oldest *n* directly
   would return identical rows in every batch of every sweep — a constant, and
   a constant cannot break an echo chamber.

8. **Quote verification is a gate, not a report.** memex's
   `verify_evidence_quotes` returns errors that a caller may act on. Here a
   failed quote drops its evidence, and an observation citing fewer than
   `min_evidence` *distinct memories* is discarded before anything is written.
   The floor counts sources rather than quotes, so nested substrings of one
   sentence cannot stand in for a synthesis.

11. **Reflection refuses to run without stored row content.** memex always has
    the unit text. OpenViking stores a row's `content` only when the backend
    sets `USE_CONTENT_FIELD`, which is `False` by default. Falling back to
    `abstract` would verify quotes against a generated summary and write links
    whose `match_text` is absent from the memory they point at, so
    `ContentUnavailableError` stops the sweep instead.

9. **Cross-area evidence is new.** No memex equivalent. An observation records
   which directories its evidence spans, and a pass can require more than one.
   The area is the parent directory: no fixed prefix depth works across the
   shapes real URIs take, and a shallower rule made the check vacuous for the
   case that motivated it.

10. **Reinforcement is not recorded.** memex applies a positive confidence
    delta for `reinforce`. Agreement is the normal state of a memory store, so
    linking it would add an edge to most pairs and bury the disagreements.

## Not ported at all

`reflect/reflection.py` (2090 lines), `reflect/queue_service.py` (706),
the whole of `contradiction/` (`engine.py` 458, `signatures.py` -- ported, then
removed, see decision 6), `confidence.py` (256), `reflect/trends.py`
(119 — the trend computation itself is queued, not dropped),
`reflect/entity_locks.py` (54 — needed only for parallel workers),
`reflect/exceptions.py` (56 — every one of them describes a CAS failure mode
that does not exist here).


## Counting these numbers

Docstrings and comments excluded, so the figures compare code against code:

```bash
python3 - <<'EOF'
import pathlib, re
def code_lines(p):
    src = re.sub(r'""".*?"""', '', pathlib.Path(p).read_text(), flags=re.S)
    return sum(1 for l in src.splitlines()
               if l.strip() and not l.strip().startswith('#'))
total = 0
for f in sorted(pathlib.Path("src/ov_ext/reflect").glob("*.py")):
    n = code_lines(f); total += n
    print(f"{n:5d}  {f.name}")
print(f"{total:5d}  TOTAL")
EOF
```
