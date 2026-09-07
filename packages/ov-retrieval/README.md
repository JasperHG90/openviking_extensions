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
