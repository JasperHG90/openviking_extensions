---
name: continue
description: "Resume work from earlier /handoff summaries stored in OpenViking. Lists the most recent handoffs for this project, lets the user pick one or more, then reads them, summarizes them together, and asks what to do next. Use at the start of a fresh session, or when the user says 'pick up where we left off', 'continue', 'what was I working on', 'resume', or 'catch me up'."
argument-hint: "[optional: project or topic to resume]"
---

# /continue — resume from a handoff

The companion to `/handoff`. It reads back the handoffs that `/handoff` wrote so
the user can pick a thread up across sessions. The shape is **browse, then
load**: show a recency-ordered list, let the user choose, then read only what
they chose.

## Tool names

OpenViking's tools appear under a prefix that varies by harness —
`openviking_glob`, `mcp__openviking__glob`,
`mcp__plugin_openviking-memory_openviking__glob`. They are the same tools. This
skill names them bare: `glob`, `read`.

## 1. Work out where to look

Ask the script that ships with this skill, from inside the repo:

```bash
bash "<this skill's directory>/scripts/ov-handoff-path.sh" dir
# viking://~/resources/handoffs/github.com/acme/api
```

Run it; do not reconstruct the path by hand. `/handoff` writes using the same
script, and a path derived any other way will not match what it wrote.

If `$ARGUMENTS` names a different project, search the whole tree instead —
`viking://~/resources/handoffs` — and filter by what the user named.

## 2. List them

<critical_constraint name="glob_not_list">
Use `glob`, not `list`. `list` with `recursive=true` returns **only
directories** — it will report the project folders and none of the handoffs in
them, which reads exactly like "no handoffs found". `glob` returns full file
URIs.
</critical_constraint>

```text
glob(pattern="**/*.md", uri="viking://~/resources/handoffs/<project>", node_limit=500)
```

Sort the returned URIs by filename, descending. Filenames lead with a UTC
`YYYY-MM-DDTHHMM` stamp, so that is true recency order; there is no mtime in the
output to sort on instead.

<critical_constraint name="glob_truncates_to_the_oldest">
Always pass `node_limit`. It defaults to **100**, and `glob` truncates in
lexicographic path order with no marker to say it did. Because these filenames
lead with a date, lexicographic order is oldest first — so hitting the cap
silently keeps the **oldest** handoffs and drops every recent one, and
`/continue` then offers a months-old handoff as the newest.

If the number of results equals the `node_limit` you passed, assume it
truncated: glob again narrowed to the current and previous year, e.g.
`pattern="**/2026-*.md"`, and say you did.
</critical_constraint>

If it comes back empty, broaden once: `glob(pattern="**/*.md",
uri="viking://~/resources/handoffs", node_limit=500)` picks up every project,
which is what a first `/continue` in a new checkout needs — and is far likelier
to hit the cap, so check for truncation there too. Still empty means there are
none: say so plainly, suggest `/handoff` starts leaving them, and stop.

## 3. Read the candidates

Take the newest **6** URIs and read them in one batched call:

```text
read(uris=["viking://…/2026-09-07T1432--vault-routing.md", …])
```

Handoffs are about a page each, so six is cheap and it means the picker shows
real content rather than filenames. Each file's line-3 blockquote is its gist.

## 4. Offer the choice

**Where an interactive multi-select exists** — Claude Code's
`AskUserQuestion` — use it, with `multiSelect: true`. That tool allows **4
options total**, so show the **3 newest handoffs plus a `more` option**, never
more than three real ones.

- `label` — the handoff's title, so it is readable without selecting it.
- `description` — where it stands, from the gist.
- `preview` — the gist plus the Next-steps section, then the date.

If the user picks `more`, present the next three from the six you already read;
only glob again if they exhaust those.

**Where no such tool exists** — opencode, Hermes, a plain terminal — print a
numbered list, one line each (`1. <title> — <gist>`), and ask the user to reply
with the numbers they want. Do not fabricate a picker UI, and do not read
further until they answer.

Either way: never guess which handoff they meant. If the reply is ambiguous or
empty, ask again.

## 5. Summarize what they picked

You already have the full text from step 3 — do not re-read. Produce one
combined brief:

- What each piece of work was about.
- Where each stands.
- The threads that connect or conflict across them.
- The open items to carry forward.

Name the handoffs you drew from, so the user can tell which fed the brief.

## 6. Ask, don't assume

Close by asking what to do next — continue one of these threads, start
something new, or dig further into one. Wait for the answer before acting.
