# ov-retrieval

Hybrid fusion and diversity re-ranking for OpenViking.

OpenViking retrieves by vector similarity, then optionally re-scores with a
cross-encoder. That leaves two gaps. An exact term the embedding misses — an
identifier, an acronym, a rare proper noun — stays missed, because nothing
searches lexically. And five near-identical documents fill five of your ten
slots, because nothing measures how much a result repeats the one above it.

This package adds a keyword leg fused by **reciprocal rank fusion**, and a
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
from ov_retrieval import rrf_fuse

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
from ov_retrieval import blend_similarity, mmr_select, tag_similarity_matrix

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
openviking-server --config /etc/ov.conf      # vector only
ov-retrieval-server --config /etc/ov.conf    # keyword leg + diversity
```

Arguments pass through untouched, subcommands included. The wrapper installs
the patch, logs what it enabled, and hands over.

To go back, run `openviking-server` again. Nothing is written to disk and
nothing about your data changes, so switching is reversible either way.

## Configuration

Every setting is an environment variable prefixed `OV_RETRIEVAL_`. There is no
config file: the settings belong to this package, and putting them in
`ov.conf` would mean OpenViking's own schema having to know about them.

```bash
OV_RETRIEVAL_MMR_LAMBDA=0.5 ov-retrieval-server --config /etc/ov.conf
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

## Layout

| Module | Depends on OpenViking? |
|---|---|
| `fusion` | No — pure functions |
| `diversity` | No — pure functions |
| `retriever`, `install` | Yes |

The algorithms are deliberately free of OpenViking imports, so they are
testable without a server, a database, or a model.

## Testing

```bash
uv run pytest                  # unit tests, no Docker
uv run pytest -m integration   # needs Docker, PostgreSQL and an OpenViking server
```
