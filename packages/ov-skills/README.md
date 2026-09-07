# ov-skills

Session-workflow skills for [OpenViking](https://github.com/volcengine/OpenViking),
for Claude Code, opencode, and Hermes.

## Why this exists

OpenViking's own plugin covers recall and capture well: it injects context at
session start and extracts memories from the conversation afterwards. What it
does not give you is a way to work *across* sessions on purpose — to close a
day's work with a deliberate write-up and open the next one by picking that
thread back up.

These four skills add that. They are ports of the ones
[memex](https://github.com/JasperHG90/memex) shipped, rebuilt on OpenViking's
storage model.

| Skill | What it does |
| --- | --- |
| `/handoff` | Write a technical summary of where the work stands into OpenViking |
| `/continue` | List recent handoffs, pick one or more, get a combined brief |
| `/learnings` | Distill the session's durable learnings and store the keepers |
| `/ingest` | Import a page, site, repo, or file as durable knowledge |

`/handoff` and `/continue` are a pair. The others stand alone.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/JasperHG90/openviking_extensions/main/packages/ov-skills/install.sh | bash
```

That installs the newest [release](https://github.com/JasperHG90/openviking_extensions/releases)
into every harness it finds on the machine. See what it would do first:

```bash
curl -fsSL .../install.sh | bash -s -- --list
```

Pick harnesses explicitly, or a directory:

```bash
bash install.sh --target claude --target hermes
bash install.sh --to ~/somewhere/skills
```

| Target | Directory |
| --- | --- |
| `claude` | `~/.claude/skills` |
| `opencode` | `~/.config/opencode/skills` |
| `hermes` | `~/.hermes/skills/openviking` (set the group with `--category`) |

**One install usually covers two harnesses.** opencode scans `~/.claude/skills`
as well as its own directory, so a `claude` install is already visible there.
The separate `opencode` target is for someone who does not use Claude Code and
would rather not have a `~/.claude` at all.

Restart the agent afterwards; all three read skills at startup.

The installer replaces only the four directories it owns, so a skills directory
you share with hand-written skills survives an install and an upgrade.

### As a Claude Code plugin

The package is also a plugin, if you would rather install it that way. Add a
`git-subdir` entry to a marketplace:

```json
{
  "name": "ov-skills",
  "source": {
    "source": "git-subdir",
    "url": "https://github.com/JasperHG90/openviking_extensions.git",
    "path": "packages/ov-skills"
  }
}
```

Or run against a checkout: `claude --plugin-dir ./packages/ov-skills`.

### Other versions

```bash
bash install.sh --version 0.1.0     # a specific release
bash install.sh --main              # the unreleased tip of main
bash install.sh --local .           # from a checkout
```

## Requirements

An OpenViking MCP connection in the harness — whatever you already use to reach
your server. These skills ship no MCP server of their own and no credentials.
Adding a second connection to the same server is exactly what you do not want,
so the plugin manifest deliberately declares none.

The tools appear under a prefix that differs per harness
(`openviking_write`, `mcp__openviking__write`,
`mcp__plugin_openviking-memory_openviking__write`). The skills name them bare
and say the prefix varies, which is how OpenViking's own skill handles it.

## Where handoffs are stored

```
viking://~/resources/handoffs/<project>/<UTC stamp>--<slug>.md
```

`viking://~` is your user root, so nothing has to look your user id up.
`<project>` is the repo's remote as nested directories — `github.com/acme/api`.
Every spelling of one repo folds onto that one path: HTTPS or SSH, with or
without `.git`, a trailing slash, or a non-default port. An embedded token is
dropped rather than written into the path.

Outside a repository — or when the remote is not a shared identity, such as a
filesystem path or a `../sibling` clone — it falls back to
`local/<repository name>`. That name comes from the repository root, not the
working directory, so `/handoff` from the root and `/continue` from a
subdirectory still agree.

Three things about that choice are worth stating, because each was a real fork:

**Why user-scoped resources.** OpenViking's per-project memory lives under
`viking://~/peers/<peer>/`, which would be the natural home — but that subtree
is managed and read-only. User-scoped resources is the only place that is both
writable and yours.

**Why not OpenViking's peer id.** The server derives a peer id through a rule
with worktree, submodule, userinfo-stripping and length-hashing cases.
Reimplementing it here would be a second copy free to drift from the first.
This directory is ours, so it uses a plain readable path and never has to stay
in step.

**Why a script, not an instruction.** `/handoff` and `/continue` must compute
the same path or resume silently finds nothing. So the derivation, the
timestamp format, and the filename shape live in
`scripts/ov-handoff-path.sh`, shipped byte-identical in both skills, with a
test asserting the copies stay equal. The slug is the only judgment left, and
judgment is the one thing prose is good at.

Handoffs are indexed like anything else, so they also surface through ordinary
recall. That is a feature for this content.

## Cross-harness notes

- **`glob`, not `list`.** In OpenViking, `list` with `recursive=true` returns
  only directories. Listing the handoff tree that way reports the project
  folders and none of the handoffs inside them, which reads exactly like "no
  handoffs found". `/continue` uses `glob`, which returns file URIs.
- **Filenames carry the sort order.** `glob` output has no modification time,
  so recency has to be in the name. That is why the UTC stamp leads it.
- **`glob` truncates to the oldest.** Its `node_limit` defaults to 100 and it
  cuts in lexicographic path order, with nothing to say it did. Since these
  filenames start with a date, that keeps the *oldest* and drops the newest —
  the worst way for it to fail. `/continue` passes `node_limit` explicitly and
  re-globs narrowed by year if the count comes back at the limit.
- **The picker degrades.** `/continue` uses Claude Code's `AskUserQuestion`
  (capped at four options, hence three handoffs plus `more`) where it exists,
  and a numbered list everywhere else.
- **Frontmatter is the intersection of three parsers.** Only `name` and
  `description` are load-bearing; `name` must match the directory and be
  lowercase-kebab, and `description` must stay under 1024 characters. The test
  suite enforces all of it.

## Development

The skills are markdown and bash; the tests are Python. `ov-skills` is not a
member of the root uv workspace — `ov-postgres` still supports 3.10 and these
tests need 3.11+ — so run them scoped to this directory:

```bash
uv run --directory packages/ov-skills pytest                  # offline
uv run --directory packages/ov-skills pytest -m integration   # fetches from GitHub
```

The unit tests validate every skill's frontmatter against all three harnesses'
rules, assert the installer's skill list matches the directories on disk, and
drive `ov-handoff-path.sh` against real temporary git repositories — one per
remote spelling — to prove every spelling of one repo folds onto one path. The
integration test installs from the tip of `main`, which is the only local check
of the `tar` invocation that differs between GNU tar and the bsdtar macOS
ships.

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
