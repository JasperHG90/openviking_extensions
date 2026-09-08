# ov-ext

Extensions to OpenViking, installed into its server process in one call.

OpenViking has no plugin mechanism, so every extension here reaches into the
running server the same way: `ov_ext.install()` runs once at startup and each
subsystem patches or registers what it needs. One package rather than several
because the subsystems already depend on each other.

| Subsystem | What it adds |
|---|---|
| [`retrieval`](#retrieval) | A lexical leg fused into vector search, an MMR diversity pass, and a pooled reranker |
| [`reflect`](#reflect) | A sweep that re-reads recent memory, writes observations with cited evidence, and flags contradictions |

Settings are per subsystem and read from the environment — `OV_RETRIEVAL_` for
retrieval, `OV_REFLECT_` for reflection. The prefixes name the subsystem rather
than the package, so they survive this package being renamed again.

> Renamed from `ov-retrieval` at v0.3.0. The version line continues unbroken:
> `git describe` matches both tag prefixes until an `ov-ext-v*` tag overtakes
> the old ones. Imports moved from `ov_retrieval.x` to `ov_ext.retrieval.x`,
> and span names from `ov_retrieval.*` to `ov_ext.retrieval.*`.

## Retrieval

OpenViking retrieves by vector similarity, then optionally re-scores with a
cross-encoder. That leaves two gaps. An exact term the embedding misses — an
identifier, an acronym, a rare proper noun — stays missed, because nothing
searches lexically. And five near-identical documents fill five of your ten
slots, because nothing measures how much a result repeats the one above it.

This subsystem adds a keyword leg fused by **reciprocal rank fusion**, and a
**maximal marginal relevance** pass that trades a little relevance for less
redundancy. Both algorithms are ported from
[memex](https://github.com/JasperHG90/memex), whose retrieval engine already
runs them.

## Why RRF rather than a weighted sum

A cosine distance and a `ts_rank_cd` value are not comparable quantities, and
no fixed weighting makes them so. `ts_rank_cd` has no upper bound and no
inverse document frequency; cosine similarity is bounded and means something
entirely different. Adding them requires a calibration that drifts the moment
either ranker is retuned.

RRF reads *positions* instead. "Third best by vector" and "first best by
keyword" combine perfectly well without either number being on a common scale,
and any monotonic rescaling of a ranker's scores is invisible to the fusion.

Each ranking contributes `weight / (k + rank)`, so agreement between rankers is
what lifts an item rather than a strong showing in any single one.

```python
from ov_ext.retrieval import rrf_fuse

fused = rrf_fuse(
    {"vector": ["a", "b", "c"], "keyword": ["b", "a", "d"]},
    weights={"keyword": 0.7},
    limit=10,
)
```

An item absent from a ranking scores nothing there, which is deliberate: a
keyword search that cannot match a document should not be made to rank it.

## Diversity without moving embeddings

MMR needs pairwise similarity between *candidates*, which normally means
pulling their embeddings into the application. OpenViking excludes vectors from
retrieval output on purpose — they are large and rarely wanted — so that would
mean a second round trip per query carrying the biggest payload in the system.

Instead the caller supplies the matrix, computed wherever the vectors already
live. For the PostgreSQL backend that is one `pgvector` query
(`ov_postgres`'s `pairwise_similarity`), and the embeddings never leave the
database.

```python
from ov_ext.retrieval import blend_similarity, mmr_select, tag_similarity_matrix

similarity = blend_similarity(
    adapter.pairwise_similarity([d.uri for d in docs]),   # cosine, from the DB
    tag_similarity_matrix({d.uri: d.search_tags for d in docs}),
)
ranked = mmr_select(docs, key=lambda d: d.uri, similarity=similarity, lambda_=0.7)
```

Similarity blends two signals, following memex: 0.6 embedding cosine and 0.4
tag overlap. Two documents can be worded differently and still cover identical
ground — the tag overlap catches that and the cosine does not. memex uses
extracted entities here; OpenViking already carries `search_tags` on every
retrieval result, so the same signal costs no extra query.

`lambda_` weights relevance against novelty. 1.0 disables diversity; 0.0 ranks
on novelty alone and ignores the query.

## Install and run

OpenViking has no plugin mechanism — its server is a console script that builds
its retriever inline — so this package ships its own entry point. Swap one
command for the other:

```bash
openviking-server --config /etc/ov.conf      # stock
ov-ext-server --config /etc/ov.conf          # every subsystem installed
```

Arguments pass through untouched, subcommands included. The wrapper installs
the patch, logs what it enabled, and hands over.

To go back, run `openviking-server` again. Nothing is written to disk and
nothing about your data changes, so switching is reversible either way.

## Configuration

Retrieval's settings are environment variables prefixed `OV_RETRIEVAL_`.
There is no config file: the settings belong to this package, and putting
them in `ov.conf` would mean OpenViking's own schema having to know about
them.

```bash
OV_RETRIEVAL_MMR_LAMBDA=0.5 ov-ext-server --config /etc/ov.conf
```

| Variable | Default | What it does |
|---|---|---|
| `OV_RETRIEVAL_KEYWORD_ENABLED` | `true` | Run the lexical leg and fuse it in |
| `OV_RETRIEVAL_KEYWORD_WEIGHT` | `0.7` | Weight of the keyword ranking, against `1.0` for vectors |
| `OV_RETRIEVAL_RRF_K` | `60` | RRF smoothing; lower sharpens the preference for rank 1 |
| `OV_RETRIEVAL_POOL_FACTOR` | `4` | Candidates gathered per requested result before re-ranking |
| `OV_RETRIEVAL_MMR_ENABLED` | `true` | Apply the diversity pass |
| `OV_RETRIEVAL_MMR_LAMBDA` | `0.7` | Relevance against novelty; `1.0` disables diversity |
| `OV_RETRIEVAL_MMR_EMBEDDING_WEIGHT` | `0.6` | Weight of embedding cosine in the similarity blend |
| `OV_RETRIEVAL_MMR_ENTITY_WEIGHT` | `0.4` | Weight of tag overlap in the same blend |

A misspelled variable is **refused at startup**, naming the settings that do
exist. Silently ignoring it would leave you tuning a knob connected to nothing.
Out-of-range values are refused the same way — `MMR_LAMBDA=5` fails rather
than clamping.

### Tuning MMR

`MMR_LAMBDA` is the one to reach for first. It weights relevance against
novelty:

| Value | Effect |
|---|---|
| `1.0` | Diversity off — pure relevance order |
| `0.7` | Default. Breaks up near-duplicates, relevance still in charge |
| `0.5` | Noticeably more varied; a strong second-best can be displaced |
| `0.0` | Novelty alone, ignoring the query |

Start at the default and lower it only if results repeat themselves. Below
about `0.4` the top result stops reliably being the best match, which is
usually not what a search is for.

The **similarity blend** decides what "repeats itself" means. Embedding cosine
catches documents that read alike; `search_tags` overlap catches documents
about the same things even when worded differently. Raise
`MMR_ENTITY_WEIGHT` if your corpus is well tagged and the wording varies;
raise `MMR_EMBEDDING_WEIGHT` if tags are sparse. When one signal is entirely
absent the other takes full weight, so an untagged corpus needs no change.

### Tuning the keyword leg

`KEYWORD_WEIGHT` is `0.7` rather than `1.0` on purpose: the lexical leg is a
corrective for terms the embedding misses, not an equal partner. Raise it
toward `1.0` if your queries carry identifiers, error codes or proper nouns;
lower it if keyword matches are crowding out semantically better results.

`POOL_FACTOR` sets how much is over-fetched before re-ranking. Re-ranking a
list already cut to `limit` can only reorder the survivors, so the pool has to
be wider than the answer. Raising it gives both passes more to work with and
costs a wider search; `4` is a reasonable balance. Note that while either pass
is active, OpenViking's retrieval stats count the pool rather than the answer,
so `result_count` reads high by up to this factor.

### Turning it off without uninstalling

```bash
OV_RETRIEVAL_KEYWORD_ENABLED=false OV_RETRIEVAL_MMR_ENABLED=false
```

Both off makes the package a genuine no-op: no keyword query is issued, no
similarity matrix is computed, and no over-fetch happens — retrieval behaves
exactly as it would without this installed. Useful while a collection is still
backfilling its bodies, or to A/B a ranking complaint.

## Rerank cost

OpenViking reranks **once per directory** its hierarchical descent visits, not
once per query. The calls run one after another, and nothing caps how many
directories a search explores — only a convergence heuristic. On a wide tree a
single search made hundreds of rerank calls.

Worse, each went out through a module-level `requests.post` with no session, so
every call paid a fresh TCP and TLS handshake. Against a LAN rerank service,
calls it answered in about 5 ms came back in 15–30 ms: the handshake cost more
than the inference.

Two settings address that:

| Setting | Default | Does |
|---|---|---|
| `OV_RETRIEVAL_RERANK_POOLING` | `true` | Routes rerank calls through one pooled connection and adds a `traceparent` header |
| `OV_RETRIEVAL_RERANK_MAX_CALLS` | `0` (no limit) | Caps rerank calls per retrieval |

Past the cap, candidates keep their vector scores — the same degradation
OpenViking already applies when reranking fails, so the worst case is a ranking
it considers acceptable rather than an error. The `ov_ext.retrieval.rerank` span
records `rerank_budget_spent` when it bites, because a search that quietly
stopped reranking half way looks exactly like one that never had a reranker.

Two details of the pooling worth knowing:

- **Cookies are refused, not shared.** A `Session` keeps a cookie jar where the
  per-call `requests.post` had none. A load balancer's stickiness cookie would
  otherwise be replayed on every later call, pinning the whole process to one
  backend node. The jar is disabled outright.
- **`traceparent` goes out on both clients.** Safe even on the VikingDB one,
  which signs its headers first: `SignerV4` covers `Content-Type`,
  `Content-Md5`, `Host` and `X-*` and names exactly those in `SignedHeaders`,
  so a verifier ignores anything else. That client sends its body as a
  pre-encoded string rather than JSON, though, so its span carries no batch
  size.
- **The pool holds 32 connections; exceeding it costs reuse, not requests.**
  Above that, urllib3 logs `Connection pool is full, discarding connection`.
  It is discarding the socket after the response, not the call — those requests
  are sent and answered, they just fall back to a connection each. A test pins
  this with a pool of 2 and four rounds of eight concurrent calls.

What this does **not** fix: the calls are still one per directory and still
serial. Batching a whole round into one call, or issuing them concurrently,
means overriding `_recursive_search` and copying the descent logic this package
exists to avoid reimplementing. Both belong upstream.

## Tracing

Each pass emits an OpenTelemetry span, so a trace shows where retrieval spent
its time and — more usefully — which passes declined to run.

| Span | Says |
|---|---|
| `ov_ext.retrieval.retrieve` | `limit`, `pool`, which passes are enabled, candidates in, results out |
| `ov_ext.retrieval.keyword_search` | the outcome: `ok`, `not_implemented`, `backend_lacks_keyword_search`, `keyword_search_failed` |
| `ov_ext.retrieval.fuse_keywords` | vector candidates, keyword hits, fused count |
| `ov_ext.retrieval.diversify` | candidates, embedding pairs, tag pairs, selected |
| `ov_ext.retrieval.embedding_similarity` | URIs asked about, pairs returned |
| `ov_ext.retrieval.rerank` | documents scored, or `rerank_budget_spent` |
| `ov_ext.retrieval.rerank_call` | one HTTP call to the rerank service; batch size when the body is JSON |

There is nothing to configure. OpenViking's server installs the global tracer
from `server.observability.traces` in `ov.conf`, and these spans join whatever
trace it already has open, nested under the request that caused them. With
tracing off they are non-recording and cost close to nothing.

This matters most for the failure paths. Every pass here degrades to "do
nothing" rather than to an error, so a keyword leg raising on every query used
to look exactly like one that ran and matched nothing. The `keyword_search`
span now records the exception and its own status is set to error, while the
`retrieve` span stays OK — the caller did get an answer, and saying the whole
request failed would be a lie.

## Reflect

Extraction sees one window and writes what that window says. Nothing goes back
over it, so a pattern spread across ten memories written on ten days is never
noticed, and two memories that contradict each other sit side by side
unremarked. OpenViking names the seam for this — `session/memory/core`
documents a `ConsolidationExtractContextProvider` — and ships only the abstract
base.

One sweep:

```
changed = query_L2(updated_at > watermark)     # URIs only
for dir, uris in group_by_parent(changed):
    scope = read_L1(dir)                       # background, never cited
    mems  = rows(uris) + neighbours() + tail_sample()
    obs   = propose(scope, mems)               # model call 1
    con   = contradict(mems)                   # model call 2
    obs   = verify_quotes(obs, mems)           # code, no model
    write(obs, con)
```

Two model calls over one gathered batch, so contradiction detection costs one
extra call rather than a second pipeline.

**Nothing is written on the model's word.** Every quote must appear verbatim in
the memory it cites, checked by substring in code — a second model asked "is
this true?" shares the first one's blind spots, a substring check does not. A
citation outside the range it was shown is a fabrication and is dropped. An
observation citing fewer than `MIN_EVIDENCE` **distinct memories** is
discarded — counted over sources, not quotes, so three quotes from one
paragraph do not stand in for a synthesis.

Observations land as their own memory type, one `derived_from` link per quote
with the quote as `match_text`. That is not a structure invented here:
OpenViking's link vocabulary already defines `derived_from` for summary facts
and already contracts `match_text` to appear verbatim. Verification is what
makes reflection's links legal rather than merely plausible.

Change detection, evidence and verification all run against the vector index:
the L2 rows carry the memory text, so the sweep never opens a file to find or
check anything. It does read one document per batch — the directory overview it
uses as background — and writes only conclusions.

**This requires the backend to store row content.** OpenViking's adapters
default `USE_CONTENT_FIELD` to `False` (ov-postgres derives it from
`store_content`), and with it off the only text on a row is `abstract`, a
generated summary. Reflection refuses to run rather than verify quotes against
a summary: a link whose `match_text` came from a summary would not appear in
the memory it points at, breaking the contract the whole design rests on.

Registering reflection does not start it. `ov_ext.reflect.run_sweep(fs, db,
ctx)` runs one — it loads the watermark from the store, sweeps, and writes the
mark back — so a cron or a script decides when, because something that writes
to memory unattended should run when someone chose that it would.

| Variable | Default | What it does |
|---|---|---|
| `OV_REFLECT_ENABLED` | `false` | Register the memory type and allow sweeps. Off until you turn it on |
| `OV_REFLECT_DRY_RUN` | `false` | Read, prompt and verify; report what it would write |
| `OV_REFLECT_BATCH_LIMIT` | `50` | Most changed memories per sweep |
| `OV_REFLECT_NEIGHBOUR_LIMIT` | `8` | Semantic neighbours per changed memory |
| `OV_REFLECT_TAIL_SAMPLE` | `3` | Memories drawn from the far end of the store |
| `OV_REFLECT_MIN_EVIDENCE` | `2` | Distinct memories an observation must cite to survive |
| `OV_REFLECT_REQUIRE_CROSS_AREA` | `false` | Keep only observations spanning several directories |
| `OV_REFLECT_CONTRADICTIONS` | `true` | Ask which memories are in tension |
| `OV_REFLECT_MAX_STALLS` | `3` | Sweeps that may advance nothing before stepping over a failing batch |
| `OV_REFLECT_OBSERVATIONS_ROOT` | `viking://~/memories/observations` | Where observations are written |
| `OV_REFLECT_STATE_PATH` | `viking://~/resources/reflect/watermark.json` | Where the watermark lives |

Turn `DRY_RUN` on first. It exercises the whole sweep and reports counts —
proposed, written, and why the rest were dropped — without touching the store,
which is how you find out what a prompt change does before it reaches memory.

`TAIL_SAMPLE` is the one not to set to zero. Without it every memory the model
sees was selected for resembling something it already believes, and reflection
converges on confirming itself.

Ported from memex. `src/ov_ext/reflect/PROVENANCE.md` is the itemized
accounting of what came across and what deliberately did not.

### Proving it works

The unit suite runs the whole sweep against in-memory stand-ins. That is not
enough on its own: every adapter defect found in review was an assumption about
OpenViking's API that a fake happily satisfied. So there is a second suite
against the real thing — a real RAGFS filesystem, a real
`VikingVectorIndexBackend`, the real `ov_postgres` adapter, and a real
PostgreSQL with pgvector in a container:

```bash
uv run --directory packages/ov-ext pytest -m integration
```

It needs a container runtime; point `OV_POSTGRES_TEST_DSN` at an existing
server to skip that. Only the model is scripted, which is what lets a test
prove a fabricated quote never reaches the store.

## Layout

| Module | Depends on OpenViking? |
|---|---|
| `install` | Yes — the one entry point, fans out to each subsystem |
| `observability` | No — OpenTelemetry API only, shared by every subsystem |
| `retrieval.fusion` | No — pure functions |
| `retrieval.diversity` | No — pure functions |
| `retrieval.rerank` | Patches OpenViking's rerank client |
| `retrieval.retriever`, `retrieval.patch` | Yes |
| `reflect.models`, `.citations`, `.verify`, `.prompts`, `.watermark` | No — pure |
| `reflect.engine` | No — talks to the protocols in `reflect.ports` |
| `reflect.viking`, `reflect.register` | Yes — the only OpenViking-shaped code |

The algorithms are deliberately free of OpenViking imports, so they are
testable without a server, a database, or a model. Reflection keeps the same
split: the engine runs against `reflect.ports`, so the whole sweep is tested
against in-memory stand-ins, and everything OpenViking-shaped is confined to
`reflect.viking`.

## Testing

```bash
uv run pytest                  # unit tests, no Docker
uv run pytest -m integration   # needs Docker, PostgreSQL and an OpenViking server
```
