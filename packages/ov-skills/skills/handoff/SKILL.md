---
name: handoff
description: "Write a technical handoff of the current work into OpenViking — what the work is, the approach and the decisions behind it, where it stands, and what comes next — so you or another agent can pick the thread up later. Use at the end of a workday, when wrapping up an involved conversation, or when the user says 'hand this off', 'write up where we are', 'summarize this so I can continue later', or 'I'll come back to this'."
argument-hint: "[optional: what to focus the handoff on]"
---

# /handoff — write a technical handoff

The user has been working through something — an implementation, an
investigation, a long conversation — and wants to hand it to their future self.
Write a technical summary good enough that someone picking it up cold can get
back up to speed and keep going. Think end-of-day brief, not a status form.

Its companion is `/continue`, which reads these back in a later session.

## Tool names

OpenViking's tools appear under a prefix that varies by harness —
`openviking_write`, `mcp__openviking__write`, `mcp__plugin_openviking-memory_openviking__write`.
They are the same tools. This skill names them bare: `write`, `read`, `glob`.

## 1. Work out where it goes

Pick a **slug** — two to four words naming the work — then ask the script that
ships with this skill for the URI, from inside the repo:

```bash
bash "<this skill's directory>/scripts/ov-handoff-path.sh" new "vault routing"
# viking://~/resources/handoffs/github.com/acme/api/2026-09-07T1432--vault-routing.md
```

Run it; do not reconstruct the path by hand. `/continue` derives the same path
with the same script, and the two must agree exactly or resume finds nothing.
The project directory, the UTC stamp and the filename shape are all settled
there — the slug is the only judgment call.

If you already wrote a handoff in *this* conversation, skip the script and
reuse that exact URI, so the latest version replaces it instead of piling up a
second near-duplicate file. `/continue` shows only the three newest, and three
snapshots of one afternoon crowd out three different threads.

## 2. Understand the work

Read back over the conversation, and over `$ARGUMENTS` if the user pointed at a
focus. Reconstruct the substance: what the work is about, what got figured out,
what was decided and why, what is still open. Capture the technical reality — a
handoff earns its keep by saving the next session from re-deriving everything.

## 3. Write it

Lead with prose. Brief a capable colleague taking over, don't fill in boxes.
These headings are a spine to adapt, not a template to complete:

```markdown
# Handoff: <short descriptor>

> One sentence naming the work and where it stands.

## Summary
What this work is, the approach, and the decisions — with the *why* behind
them. This is the bulk of the handoff.

## Where it stands
Done and working; in progress; untested or uncertain.

## Next steps
The concrete threads to pick up, specific enough to act on.

## Key references
Files, branches, PRs, commands, links.
```

Keep the blockquote gist on line 3. `/continue` shows it in the picker, so it
has to stand on its own — "Vault routing is wired through the CLI and green;
the migration for the new column is next" tells the reader something, "worked on
routing" does not.

## 4. Save it

Call `write` with the URI from step 1 and `mode="replace"`. Missing parent
directories are created for you, so the first handoff in a new project needs no
setup.

Use `replace` even for a brand-new handoff. The stamp only resolves to the
minute, so a second `/handoff` on the same work inside one minute would collide
— under `create` that is an error, under `replace` it is simply the newer
version winning, which is what you want.

Indexing runs in the background, so the file is also reachable through ordinary
recall later. That is a feature here — do not pass `wait=true` for it, since
`/continue` finds handoffs by path and never waits on the index.

## 5. Stay on one plane

A handoff records *where the work is*. It is not a durable fact and not a
reusable how-to, so do not also call `remember` with the same content. If the
session produced a genuinely durable learning, say the user can run
`/learnings` for it.

## 6. Confirm

State the file's URI and its one-line gist, and that `/continue` will offer it
next session. One or two lines — the handoff is the artifact, not your report
about it.
