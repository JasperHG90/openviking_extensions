---
name: ingest
description: "Import a document, web page, whole site, or git repository into OpenViking as durable searchable knowledge. Use when the user shares a URL, PDF, or repo worth keeping, or says 'ingest this', 'add this to memory', 'index this doc', 'read this site', or 'keep this for later'."
argument-hint: "[url, file path, or repo to ingest]"
---

# /ingest — import a source into OpenViking

Pull an outside source — a page, a PDF, a site, a repo — into OpenViking so it
becomes searchable alongside everything else. Ingestion is **asynchronous**: the
server accepts the job and processes it in the background.

## Tool names

OpenViking's tools appear under a prefix that varies by harness —
`openviking_add_resource`, `mcp__openviking__add_resource`,
`mcp__plugin_openviking-memory_openviking__add_resource`. They are the same
tools. This skill names them bare: `add_resource`, `list`, `find`.

## 1. Work out what you were handed

Take the source from `$ARGUMENTS`, or from what the user just shared. Four
kinds, and they behave differently:

| Source | What to pass | Notes |
| --- | --- | --- |
| Web page | `path="https://…"` | One page. |
| Sitemap, RSS, or Atom URL | `path="https://…/sitemap.xml"` | Ingests the **whole site** as one resource tree. |
| Bare domain, whole site wanted | `path="https://…"`, `args={"site": true}` | Without `site`, a bare domain is one page. |
| Git repo | `path="https://github.com/…"` or `git@…` | Cloned and indexed. |
| Local file | `path="/abs/path/file.pdf"` | Needs an upload step — see §3. |

If the user's intent is ambiguous between one page and a whole site, ask. A
whole site is a much larger job and re-embeds everything on each refresh.

## 2. Check it is not already there

`find` or `list` under `viking://resources/` first. Re-ingesting a source that
is already indexed spends real work to produce a duplicate, and duplicates make
retrieval worse. If it is there and current, say so and stop.

## 3. Local files need a second step

For a local file, `add_resource` does **not** ingest anything. It returns an
**upload instruction**: a URL to POST the file to. The server ingests it once
the bytes land.

So: call `add_resource(path="/abs/path/file.pdf")`, then POST the file to the
URL it returns — for example `curl -fsS -X POST --data-binary @/abs/path/file.pdf "<returned url>"`,
matching whatever form the response asks for. Do **not** call `add_resource`
again afterwards; that would queue a second copy.

Remote URLs and repos need none of this. They ingest from the one call.

## 4. Place it, and decide about watching

- **`to`** — an exact target under `viking://resources/`, e.g.
  `viking://resources/volcengine/OpenViking`. Leave it empty to let the server
  derive one from the source; that default is usually right.
- **`description`** — one line on why this is worth keeping. Write it. It is
  what tells a future reader why this is in the store.
- **`watch_interval`** — minutes between auto-refreshes; `0` means never, and
  is the right default. Only set it for a source that genuinely changes, and
  keep it at `1440` (daily) or higher: every refresh re-embeds the whole
  resource. Remote sources only.

## 5. Report honestly

Ingestion is queued, not done. Say that: name what was accepted and where it
will land, and that indexing runs in the background. Do not poll for
completion, and do not claim the content is searchable yet — for anything
larger than a page it will not be for a while. The user can search for it
later, or run `/ingest` again to check.
