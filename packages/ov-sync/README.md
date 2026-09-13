# ov-sync

Sync a local folder into OpenViking. One way, incrementally, with the record of
what was sent kept in a SQLite file beside the notes.

`ov` can import a folder once (`ov add-resource ./docs`), and it can re-fetch a
URL or a git repo on a timer. It cannot keep a folder on your disk in step with
a `viking://` directory: a local import is a one-shot upload, and the server
never sees that path again. `ovsync` fills that gap.

```console
$ ovsync init ~/notes                      # writes ov-sync.toml
$ ovsync run ~/notes --create-root         # sends everything
$ ovsync run ~/notes                       # sends nothing; it already matches
$ ovsync watch ~/notes                     # keeps sending as you edit
```

## Install

```bash
uv tool install ov-sync --from "git+https://github.com/JasperHG90/openviking_extensions@ov-sync-v0.2.0#subdirectory=packages/ov-sync"
```

Or from a checkout:

```bash
uv tool install ./packages/ov-sync
```

That puts `ovsync` on your path. It needs Python 3.11+ (for `tomllib`) and an
OpenViking server to talk to. Credentials come from the `ov` CLI's config file
or from two environment variables — see [Credentials](#credentials) — so there
is nothing else to set up. To upgrade, run the same command with a newer tag
and `--force`.

## How it maps

A file's path under the folder is its path under the root:

```
~/notes/projects/acme/spec.md  ->  viking://resources/notes/projects/acme/spec.md
```

That mapping is the whole idempotency story. The target URI **is** the key, so
re-sending a file replaces it instead of duplicating it, and a run can be
repeated, interrupted, or resumed with no cleanup.

### Names a URI cannot hold

Disk allows names a `viking://` URI does not. A `#`, a `?` or a `\` is refused
by the server outright — and refused for the whole batch the file travelled in,
so one badly named PDF used to take 255 innocent files down with it. Trailing
whitespace is worse: the server trims it silently, and the file lands somewhere
the local state does not point.

`ovsync` rewrites those on the way through, replacing each with `_`, and says
so:

```console
$ ovsync run ~/notes
Created: 1
Renamed [#TDAI-709] hub and spoke.pdf -> [_TDAI-709] hub and spoke.pdf
```

Nothing the server would have accepted is touched. Spaces, brackets, colons,
accents, emoji, and CJK all survive verbatim, so a folder that syncs today
keeps syncing to the same URIs — that rule is what keeps the rewrite from
re-uploading your library under new names.

When the rewrite makes two files want one URI, neither is guessed at — sending
both would have the second quietly overwrite the first:

```console
Skipped a#b.md: its name becomes 'a_b.md' in OpenViking, which 'a?b.md' also claims. Rename one of them, or exclude it.
Skipped a?b.md: its name becomes 'a_b.md' in OpenViking, which 'a#b.md' also claims. Rename one of them, or exclude it.
```

Both wait, because neither has a better claim than the other. Add a real
`a_b.md` and it wins outright — it is the file whose own name that is — while
the two rewritten rivals keep waiting.

A rewrite can also land a file on a *folder's* name. OpenViking stores that
pair without complaint, and that is the trap: the file wins the node, so `ls`
on it returns nothing and every file underneath is invisible to listing, though
`stat` still finds them. It does not heal on its own — syncing more files into
that folder changes nothing. Only removing the file and then writing under the
node again brings the listing back. So the rewritten side waits instead.

The same pair can arrive without any rewrite — sync `notes.md`, delete it, then
make a folder called `notes.md`. A deletion is only *reported* until `--delete`
acts on it, so the old file is still sitting at that URI. `ovsync` holds the
folder back rather than burying it:

```console
Gone from the folder: 1 still in OpenViking. Re-run with --delete to remove them.
Skipped notes.md/inner.md: 'notes.md' in OpenViking is the file synced from 'notes.md'. Writing underneath it would hide this file from listing. Re-run with --delete to remove it first.
```

Renaming the loser is the fix, and renaming it *to* the name it was landing on
is safe: `ovsync` knows the old URI has a new owner, so it updates the file
rather than deleting it. When a name is taken over that way, the run says so:

```console
Taken over viking://resources/notes/a_b.md: a_b.md replaces the copy sent from a#b.md
```

## What makes a second run cheap

Three filters, cheapest first:

1. **Size and mtime.** A file matching the state is never opened.
2. **Content hash.** A file whose timestamp moved gets hashed. If the bytes
   match what was last sent, it is *not* re-sent — a `git checkout`, a Dropbox
   round-trip, or an editor rewriting a file byte-for-byte costs nothing. This
   matters more here than it would elsewhere: every write makes the server
   re-embed the file.
3. **Batching.** What is left goes through `content/batch-write`, up to 256
   files per request, and the server reindexes **once per batch** rather than
   once per file. Batches are filled by the bytes actually read, not by the
   sizes the scan recorded, so a file that grows mid-run cannot take the rest
   of its batch down with it.

The state lives in `.ov-sync.db` in the folder root. Delete it and the next run
re-sends everything, which is harmless.

## Deletes

When a local file disappears, the copy in OpenViking does not — not by default.
A run reports it:

```console
$ ovsync run ~/notes
Gone from the folder: 3 still in OpenViking. Re-run with --delete to remove them.
```

`--delete` acts on it. OpenViking has no archived state, so "remove" means
remove; the undo path is a snapshot, which `ovsync` commits before the first
deletion:

```console
$ ovsync run ~/notes --delete
Deleted: 3
  Undo with: ov snapshot restore <commit>
```

If the snapshot fails, nothing is deleted. `--no-snapshot` overrides that when
snapshots are unavailable on your server.

## Commands

| Command | What it does |
|---|---|
| `ovsync init <folder>` | Write a starter `ov-sync.toml`. |
| `ovsync status <folder>` | Show what the next run would do. Touches only the local state, never the server. |
| `ovsync run <folder>` | Send what changed. `--dry-run`, `--full`, `--delete`, `--no-snapshot`, `--create-root`. |
| `ovsync watch <folder>` | Keep syncing as the folder changes. |

`watch` does not trust events on their own. It syncs the whole folder when it
starts — anything edited while it was stopped produced no event at all — and
rescans every `reconcile_interval_seconds`, because watchdog drops events when
its queue overflows. Between those, it reacts to what it sees, waiting for the
folder to go quiet first so a `git checkout` becomes one sync rather than
fifty.

## Configuration

`ov-sync.toml` in the folder root, or `~/.config/ov-sync/ov-sync.toml` for
folders that carry none. Every setting can be overridden by an environment
variable: `OV_SYNC_` plus the path, with `__` between levels
(`OV_SYNC_SYNC__ROOT_URI`, `OV_SYNC_WATCH__MODE`).

```toml
[sync]
root_uri = "viking://resources/notes"
include_extensions = [".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".toml", ".html", ".pdf"]
max_operations_per_batch = 256
wait_for_indexing = true

[sync.exclude]
base = [".git", ".obsidian", ".trash", ".venv", "__pycache__", "node_modules"]
extends_exclude = []          # globs, e.g. "templates/**", "*.draft.md"
ignore_folders = []           # exact folder names, at any depth
frontmatter_skip_key = "ov"
frontmatter_skip_value = "skip"

[watch]
mode = "events"                   # or "poll"
debounce_seconds = 5              # quiet period before syncing a burst of edits
poll_interval_seconds = 300       # poll mode: seconds between scans
reconcile_interval_seconds = 900  # event mode: seconds between full rescans
```

A Markdown file can keep itself out of the sync:

```markdown
---
ov: skip
---
```

## Credentials

`ovsync` reads the OpenViking CLI's own config — `~/.openviking/ovcli.conf`, or
whatever `$OPENVIKING_CLI_CONFIG_FILE` points at — so there is no second copy of
your API key on disk, and `ovx run <profile> -- ovsync run ~/notes` reaches the
same server as `ovx run <profile> -- ov ...`.

`OV_SYNC_URL` and `OV_SYNC_API_KEY` override the file, for CI that keeps the key
out of a file entirely.

## Limits

Server-side, and enforced before a request goes out rather than after it is
refused:

- **8 MB per file.** Anything larger is skipped and reported.
- **16 MB per batch**, and 256 files per batch. Batches are split to fit.
- **The root must already exist** and be a directory inside a resource or
  memory namespace. `ovsync` checks, and `--create-root` makes it. Directories
  *below* the root need no such care: batch-write creates them itself.
- **Binary files land as bytes.** They are stored verbatim, not put through
  OpenViking's parse pipeline, so a PDF synced this way is not converted to
  text for retrieval. Use `ov add-resource` for documents that need parsing.
- **Memory namespaces take text only.** A binary file targeted at
  `viking://user/<you>/memories/...` is refused by the server.

## Concurrency

OpenViking locks a tree while it writes and indexes it, so two syncs against
the same root at once — a `run` while a `watch` is going, or two `run`s — make
each other wait. `ovsync` retries a busy lock, because the server marks that
error retryable and it clears once indexing finishes. If it is still busy after
that, the batch is reported as failed and picked up by the next run: every
write is an upsert, so nothing is lost and nothing is duplicated.

## What it does not do

It does not pull. OpenViking is the copy; the folder is the original. Nothing
here reads the server's version of a file, so an edit made through `ov write`
or the web UI is overwritten by the next sync of that file. If you want the
server to be authoritative for a tree, do not point `ovsync` at it.

## Development

```console
$ uv sync
$ uv run pytest
$ uv run ruff check src tests
$ uv run mypy src tests
```

Or run the repo's gates the way CI does: `prek run --all-files`.

Integration tests hit a real server, so they stay out of the default run. Point
them at a `viking://` parent they may create and remove directories under:

```console
$ OV_SYNC_TEST_ROOT=viking://resources uv run pytest -m integration
```

They skip without that variable. They are worth running before a release: both
bugs that reached the first working version were disagreements between the
mocks and the real API, which only these catch.
