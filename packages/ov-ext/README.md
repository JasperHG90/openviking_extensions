# ov-ext

Extensions to OpenViking, installed into its server process in one call.

OpenViking has no plugin mechanism, so every extension here reaches into the
running server the same way: `ov_ext.install()` runs once at startup and each
subsystem patches or registers what it needs. One package rather than several
because the subsystems already depend on each other.

| Subsystem | What it adds |
|---|---|
| [`retrieval`](#retrieval) | A lexical leg fused into vector search, an MMR diversity pass, and a pooled reranker |
| [`reflect`](#reflect) | A sweep that re-reads recent memory and writes observations with cited evidence |

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

There is a third gap, and it is one of cost rather than quality: OpenViking
reranks once per directory it descends into, with no bound on either the calls
or the documents they carry, which is most of what a slow search is spending.
[Rerank cost](#rerank-cost) covers what this package does about that.

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

### More than one worker

`install()` patches the process it runs in, which is the process serving
requests only while `workers = 1`. Past one, OpenViking hands uvicorn an import
string (`bootstrap.py:327`) and uvicorn **spawns** children
(`uvicorn/_subprocess.py:18`) — fresh interpreters with an empty `sys.modules`
that import that one string and never run `ov-ext-server`. Measured: the parent
reports `HybridRetriever`, the child reports `HierarchicalRetriever` and has
not imported `ov_ext` at all. A fork would have inherited the patches; uvicorn
does not fork.

So the parent rewrites the import string it gives uvicorn to name ov-ext's own
factory, which installs and then delegates to OpenViking's. Nothing to
configure — but two consequences worth knowing:

- **Settings must come from the environment.** A spawned child inherits the
  environment and nothing else, so objects passed to `install()` do not reach
  it.
- **`OV_REFLECT_LOCK=process` is refused** when uvicorn is starting more than
  one worker. It asserts that exactly one process sweeps, which is false the
  moment there are several — and a process-local lock cannot detect the others,
  so the failure would be silent duplicate sweeps rather than an error.

If OpenViking ever changes the factory it names, the parent logs an error
saying the workers will be unpatched rather than rewriting a string it does not
recognise.

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
| `OV_RETRIEVAL_KEYWORD_MAX_CHARS` | `1024` (`0` sends all) | Characters of the query the keyword leg sees |
| `OV_RETRIEVAL_RRF_K` | `60` | RRF smoothing; lower sharpens the preference for rank 1 |
| `OV_RETRIEVAL_POOL_FACTOR` | `4` | Candidates gathered per requested result before re-ranking |
| `OV_RETRIEVAL_MMR_ENABLED` | `true` | Apply the diversity pass |
| `OV_RETRIEVAL_MMR_LAMBDA` | `0.7` | Relevance against novelty; `1.0` disables diversity |
| `OV_RETRIEVAL_MMR_EMBEDDING_WEIGHT` | `0.6` | Weight of embedding cosine in the similarity blend |
| `OV_RETRIEVAL_MMR_ENTITY_WEIGHT` | `0.4` | Weight of tag overlap in the same blend |
| `OV_RETRIEVAL_RERANK_POOLING` | `true` | Route rerank calls through one pooled connection |
| `OV_RETRIEVAL_RERANK_MAX_CALLS` | `0` (no limit) | Cap rerank calls during the descent |
| `OV_RETRIEVAL_RERANK_MAX_DOCUMENTS` | `0` (no limit) | Cap documents sent in any one call |
| `OV_RETRIEVAL_RERANK_FINAL` | `true` | Rerank the pool once after fusion, before diversifying |

The four rerank settings are the ones worth understanding before touching, and
[Rerank cost](#rerank-cost) explains what each trades away.

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
OV_RETRIEVAL_KEYWORD_ENABLED=false OV_RETRIEVAL_MMR_ENABLED=false \
OV_RETRIEVAL_RERANK_FINAL=false
```

All three off makes the package a genuine no-op for *results*: no keyword query
is issued, no similarity matrix is computed, no extra rerank call goes out, and
no over-fetch happens — retrieval returns exactly what it would without this
installed. Useful while a collection is still backfilling its bodies, or to A/B
a ranking complaint.

`RERANK_FINAL` has to be in that list. It defaults on and costs one rerank call
per search, so leaving it out gives you a quieter package rather than an absent
one.

Two settings are deliberately not in it. `RERANK_MAX_CALLS` and
`RERANK_MAX_DOCUMENTS` default to no limit, so they already change nothing.
`RERANK_POOLING` stays on: it swaps the transport underneath OpenViking's
rerank client without touching what that client sends or how its answers are
read, so it cannot move a result. Set it to `false` too if you want the process
untouched as well as the ranking.

## Rerank cost

OpenViking reranks **once per directory** its hierarchical descent visits, not
once per query. The calls run one after another, and nothing caps how many
directories a search explores — only a convergence heuristic. On a wide tree a
single search made hundreds of rerank calls.

Worse, each went out through a module-level `requests.post` with no session, so
every call paid a fresh TCP and TLS handshake. Against a LAN rerank service,
calls it answered in about 5 ms came back in 15–30 ms: the handshake cost more
than the inference.

Four settings address that:

| Setting | Default | Does |
|---|---|---|
| `OV_RETRIEVAL_RERANK_POOLING` | `true` | Routes rerank calls through one pooled connection and adds a `traceparent` header |
| `OV_RETRIEVAL_RERANK_MAX_CALLS` | `0` (no limit) | Caps rerank calls during the descent |
| `OV_RETRIEVAL_RERANK_MAX_DOCUMENTS` | `0` (no limit) | Caps documents sent in any one call |
| `OV_RETRIEVAL_RERANK_FINAL` | `true` | Reranks the pool once after fusion, before diversifying |

Past either cap, candidates keep their vector scores — the same degradation
OpenViking already applies when reranking fails, so the worst case is a ranking
it considers acceptable rather than an error. The `ov_ext.retrieval.rerank` span
records `rerank_budget_spent` when the call ceiling bites, because a search that
quietly stopped reranking half way looks exactly like one that never had a
reranker. Two more attributes record the documents rather than the calls:
`reranked_documents` counts those that came back with a score, and
`held_back_documents` those never sent — because a cap excluded them, or
because they carried no text. Neither implies a cap is set.

### Which cap to reach for

Prefer `RERANK_MAX_DOCUMENTS` to a low `RERANK_MAX_CALLS`. A reranker charges
by the document — it scores a few query-document pairs per pass through the
model — so what a search spends is very nearly how many documents it sends, and
the two caps buy that budget differently. A low call ceiling reranks the first
few directories properly and the rest not at all. The same budget spread as a
per-call document cap reranks *at every level of the descent*, which is where
OpenViking intends the cross-encoder to earn its keep: a wrong prune high in the
tree is one nothing downstream can undo.

Candidates that miss the cut keep their vector scores and stay in the running.
The honest caveat is that the result then mixes two scales, and a strong vector
score can outrank a weak rerank score. Upstream already mixes them for documents
it skips, so this widens an existing looseness rather than introducing one.

Blanks are dropped **before** the cap applies. Capping first would let a
backfilling subtree fill the whole allowance with documents the service ignores,
sending an empty request that the call ceiling has already been charged for.

### The final pass

`RERANK_FINAL` adds one call after fusion. It exists because the descent judges
a directory of siblings at a time and never sees the pool it ends up with, so
the candidates the keyword leg promoted, and any left on vector scores by a
spent call ceiling, reach the answer unjudged.

**`RERANK_MAX_DOCUMENTS` applies to this pass too**, and the interaction is the
most important thing to know about either setting. The cap selects by vector
score, so what it holds back is precisely what the keyword leg promoted — its
whole job is lifting documents the embedding ranked low. So the pass reorders
**only the candidates it actually scored, among the positions they already
held**; anything it did not see keeps its place.

That restriction is not fussiness. Sorting the whole pool by the merged scores
compares a rerank score against a cosine, two numbers that share a range and
nothing else. Measured on a four-candidate pool with `MAX_DOCUMENTS=2`, it took
a keyword-promoted document from first to last and lifted a document nothing had
ever judged from last to first — an answer that was neither the fused order nor
the vector order, and strictly worse than leaving the pass off.

The same applies when abstracts are missing. A pool mixing blank and real
abstracts gets only its real ones scored, and the blanks would otherwise be
ranked against them on cosine alone.

It runs before the diversity pass, and that order is load-bearing —
`mmr_select` reads relevance from a candidate's position rather than its score,
so reranking afterwards would undo the diversification without saying so.

It is deliberately **not** charged against `RERANK_MAX_CALLS`: that ceiling
bounds a descent whose length nobody chose, while this is one call the caller
asked for. Charging both to one budget would let a tight ceiling silently drop
the pass that makes the ceiling affordable.

It also never widens the pool on its own. A larger `limit` does not merely
truncate later upstream — it widens every child search and makes the descent's
convergence check harder to satisfy, so the walk visits more directories and
reranks in more of them. A pass added to cut rerank cost must not pay for itself
by lengthening the descent. With the keyword or diversity pass on, the pool is
already wide and the final rerank reads it for free.

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
serial, and nothing here bounds how many directories the descent visits.
Changing either means overriding `_recursive_search` and copying the descent
logic this package exists to avoid reimplementing, so both belong upstream.

Worth knowing before reaching for concurrency there: issuing a round's calls in
parallel is not the win it looks like when the rerank service is one you host.
It re-batches whatever arrives into its own small groups — embark's
`rerank_batch_size` is 4 — so four concurrent calls and four serial ones queue
the same number of passes through the same model, and the concurrent version
adds contention. Merging a round into one request is the better shape, and it
buys round-trips rather than inference. The number that moves is the document
count, which is what `RERANK_MAX_DOCUMENTS` exists to control.

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
noticed. OpenViking names the seam for this — `session/memory/core`
documents a `ConsolidationExtractContextProvider` — and ships only the abstract
base.

One sweep:

```
changed = query_L2(updated_at > watermark)     # URIs only
for dir, uris in group_by_parent(changed):
    scope = read_L1(dir)                       # background, never cited
    mems  = rows(uris) + neighbours() + tail_sample()
    obs   = propose(scope, mems)               # the one model call
    obs   = verify_quotes(obs, mems)           # code, no model
    write(obs)
```

One model call per batch. A second call asked which memories were in tension;
it was removed. A batch is grouped by directory, not by subject, so the model
was asked whether a note about a package rename contradicted a line from a
README — and it answered, because it was asked to. Every pair it returned was
an artifact of the question, and each one cost a call and wrote a `contradicts`
edge into somebody's memory.

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

### Scheduling, and the single-sweeper guarantee

The sweep runs on a timer inside the server process, started when OpenViking's
service finishes booting — that is where the filesystem, the vector store and
an event loop first exist together. memex does the same thing; this is that
shape with the parts that only matter at scale left out.

**Two sweeps at once is a correctness bug, not a performance one.** Both read
the same watermark, both gather the same memories, and both advance the mark —
so each silently consumes work the other is half-way through. The sweep
therefore never runs unlocked, and `OV_REFLECT_LOCK` has **no default**: the
ticker refuses to start until you say which lock, because every default is a
way to end up sweeping unlocked by forgetting a setting rather than by
deciding to.

| `OV_REFLECT_LOCK` | Guarantees | Use when |
|---|---|---|
| `process` | One sweep at a time within this process | Exactly one process ever sweeps — choosing it asserts that |
| `postgres` | One sweep at a time across every process reaching `OV_REFLECT_LOCK_DSN` | Anything else |

`postgres` takes `pg_try_advisory_lock` on a connection opened for the sweep
and closed after it. That lock is **session-scoped**: a crashed process, a
killed container or a severed network drops the connection and the lock goes
with it — no TTL to tune, no clock to trust, no lease to renew.

Session-scoped is not the same as sweep-scoped, though, and the difference bit.
The lock connection sits idle for the whole sweep — everything the sweep does
goes through OpenViking — and an idle connection is what a database reaps.
Measured against a real server with `idle_session_timeout = 1500ms` and a
four-second sweep, a second caller took the lock mid-sweep. So the lock also:

- sets `idle_session_timeout = 0` for its own session, so the server's reaper
  leaves it alone however long the sweep runs; and
- pings the connection every few seconds, cancelling the sweep if the ping
  fails — a lock that is gone means the sweep holding it must stop, not finish
  unprotected. The exposure is bounded by the heartbeat, not by sweep length.

Two constraints follow, and they are load-bearing:

- the DSN must be a **direct** connection, not PgBouncer in transaction mode —
  that hands each statement a different backend, which makes a session lock
  meaningless;
- every sweeper must name the **same database**. Advisory locks are scoped per
  database, so two DSNs differing only in database name both grant the lock.

**Why not Redis.** A `SET NX PX` lock expires on a timer, so a holder that
stalls past its TTL — a GC pause, an IO stall, a frozen VM — loses the lock
while still believing it holds it, and a second sweeper starts. Closing that
needs fencing tokens validated *at the resource*, and OpenViking's `write_file`
validates nothing, so there is no token to fence with. Redlock does not fix it.
Redis would give you a lock that usually works; Postgres gives you one that is
correct.

The remaining cost is liveness: a holder that is alive, connected and wedged
keeps the lock, and nothing sweeps until it is killed. That is the right trade
here — a sweep that does not happen this hour is recoverable; one that happens
twice corrupts the watermark.

`run_sweep(fs, db, ctx, lock=...)` runs exactly one sweep, for a script or a
test. The lock is a **required keyword argument** there too: locking lives on
the one path every sweep goes through, so there is no second, unlocked way in.

| Variable | Default | What it does |
|---|---|---|
| `OV_REFLECT_ENABLED` | `false` | Register the memory type and allow sweeps. Off until you turn it on |
| `OV_REFLECT_DRY_RUN` | `false` | Read, prompt and verify; report what it would write |
| `OV_REFLECT_BATCH_LIMIT` | `50` | Most changed memories per sweep |
| `OV_REFLECT_NEIGHBOUR_LIMIT` | `8` | Semantic neighbours per changed memory |
| `OV_REFLECT_TAIL_SAMPLE` | `3` | Memories drawn from the far end of the store |
| `OV_REFLECT_MIN_EVIDENCE` | `2` | Distinct memories an observation must cite to survive |
| `OV_REFLECT_REQUIRE_CROSS_AREA` | `false` | Keep only observations spanning several directories |
| `OV_REFLECT_MAX_STALLS` | `3` | Sweeps that may advance nothing before stepping over a failing batch |
| `OV_REFLECT_INTERVAL_SECONDS` | `900` | Gap between the end of one sweep and the start of the next |
| `OV_REFLECT_LOCK` | *(none)* | `process` or `postgres`. Required — there is no default |
| `OV_REFLECT_LOCK_DSN` | — | Where to take the advisory lock, required by `postgres` |
| `OV_REFLECT_USER_ID` | *(none)* | Whose memories to reflect on. Required when enabled |
| `OV_REFLECT_HEARTBEAT` | `5` | Seconds between lock-connection liveness checks |
| `OV_REFLECT_ACCOUNT_ID` | `default` | Account the sweep's context belongs to |
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
| `installer` | Yes — the one entry point, fans out to each subsystem |
| `worker` | Yes — carries the patches into uvicorn's spawned workers |
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
