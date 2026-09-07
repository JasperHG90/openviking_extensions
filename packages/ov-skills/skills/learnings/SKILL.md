---
name: learnings
description: "Distill what this session actually taught — durable facts, preferences, decisions, and gotchas — and write the keepers into OpenViking's long-term memory. Use when wrapping up a session worth learning from, or when the user says 'save what we learned', 'remember this for next time', 'capture the takeaways', or 'don't make me explain this again'."
argument-hint: "[optional: what to focus on]"
---

# /learnings — keep what the session taught

OpenViking already captures the conversation and extracts memories from it in
the background. So this skill is not the safety net — it is the **curated**
path. Its value is judgment: naming the few things worth keeping, and writing
them so they survive out of context.

## Tool names

OpenViking's tools appear under a prefix that varies by harness —
`openviking_remember`, `mcp__openviking__remember`,
`mcp__plugin_openviking-memory_openviking__remember`. They are the same tools.
This skill names them bare: `remember`, `search`, `read`, `edit`, `forget`.

## 1. Find the keepers

Read back over the session, narrowed by `$ARGUMENTS` if the user gave a focus.
Keep something only if it is **durable** and **not derivable**:

- A preference or standing instruction: "prefer `uv add` over `uv pip`."
- A decision and its reason: "ovx polls instead of listening, because reading
  the status endpoint deletes the pending row."
- A gotcha that cost real time: "macOS bash 3.2 sometimes skips the EXIT trap
  on Ctrl-C."
- A fact about the user's setup that no repo records.

Leave out anything the code, the git history, or a `CLAUDE.md` already says —
those get re-read anyway, and a memory that duplicates them can go stale while
the file stays right. Leave out what only mattered to this conversation.

Retrieval degrades as the store fills with noise. Three good learnings beat
fifteen mediocre ones. If the session taught nothing durable, say so and stop —
that is a valid outcome, not a failure.

## 2. Check against what is already there

For each candidate, `search` OpenViking for the same ground first. Then:

- **Already there and still right** — drop it. Do not write a near-duplicate.
- **There but now wrong or stale** — `read` the file, then `edit` it in place.
  Correcting the existing memory beats adding a second one that contradicts it;
  a reader who finds both learns nothing.
- **Genuinely new** — keep it for step 3.

## 3. Write them

`remember` takes a **`messages` array**, not a string — it stores a short
exchange and extracts memory from it:

```text
remember(messages=[
  {"role": "user",
   "content": "When working in openviking_extensions, install dependencies with
               `uv add` so they land in pyproject.toml. `uv pip` installs into
               the environment without recording the dependency."},
])
```

Write each learning as a **standalone statement**. It will be read months later
with none of this conversation around it, so no "as we discussed", no "the
fix above", no bare pronouns. Name the project, the tool, the file. State the
reason alongside the rule — a rule without its reason gets misapplied the first
time the situation differs.

Batch related learnings into one call; keep unrelated ones separate so they
extract cleanly.

## 4. Stay on one plane

- Where the *work* stands is a handoff, not a learning — that is `/handoff`.
- A reusable how-to for a multi-step task is an Experience. OpenViking derives
  those itself from the session; do not hand-write one here.

## 5. Confirm

List what you saved, one line each, and say plainly what you considered and
dropped. The second half matters: it shows the filter ran, and it gives the
user the chance to overrule it.
