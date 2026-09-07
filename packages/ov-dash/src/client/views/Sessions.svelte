<script lang="ts">
  /**
   * Agent runs, and what each one actually did.
   *
   * The list endpoint carries only an id and a mod time, so the counts here
   * come from the per-session detail the server fetches for the newest runs.
   * A row the server did not enrich says so rather than reporting a confident
   * zero — an earlier version defaulted those to 0 and every run looked idle.
   */
  import { api, type AgentSession } from "../lib/api";
  import Icon from "../lib/Icon.svelte";
  import PageHead from "../lib/PageHead.svelte";
  import { formatWhen } from "../lib/tree";

  let sessions = $state<AgentSession[] | null>(null);
  let failure = $state("");
  let filter = $state("");
  let onlyActive = $state(true);

  const COLUMNS = "minmax(0,1fr) 84px 84px 84px 96px 118px";

  $effect(() => {
    api
      .sessions()
      .then((result) => {
        sessions = result;
      })
      .catch((error: Error) => {
        failure = error.message;
      });
  });

  const shown = $derived.by(() => {
    if (!sessions) return [];
    const query = filter.trim().toLowerCase();
    return sessions.filter((session) => {
      if (onlyActive && session.detailed && session.messages === 0) return false;
      return !query || session.id.toLowerCase().includes(query);
    });
  });

  const totals = $derived.by(() => {
    const rows = shown.filter((s) => s.detailed);
    return {
      runs: shown.length,
      messages: rows.reduce((n, s) => n + s.messages, 0),
      memories: rows.reduce((n, s) => n + s.memories, 0),
      tokens: rows.reduce((n, s) => n + s.tokens, 0),
    };
  });

  /** Thousands separators, so a seven-figure token count stays readable. */
  function num(value: number): string {
    return value.toLocaleString();
  }

  function compact(value: number): string {
    if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
    if (value >= 1_000) return `${(value / 1_000).toFixed(0)}k`;
    return String(value);
  }
</script>

<PageHead
  eyebrow="Agent runs"
  title="Sessions"
  desc="Every run that wrote to this workspace, and what it left behind."
/>

{#if failure}
  <div class="callout c-sheet"><span class="ci"><Icon name="close" /></span><p>{failure}</p></div>
{:else if !sessions}
  <p class="pdesc">Loading…</p>
{:else if sessions.length === 0}
  <div class="callout c-sheet">
    <span class="ci"><Icon name="sessions" /></span>
    <p>No agent runs recorded yet.</p>
  </div>
{:else}
  <div class="strip">
    <div><span class="n">{num(totals.runs)}</span><span class="l">Runs</span></div>
    <div><span class="n">{num(totals.messages)}</span><span class="l">Messages</span></div>
    <div><span class="n">{num(totals.memories)}</span><span class="l">Memories kept</span></div>
    <div><span class="n">{compact(totals.tokens)}</span><span class="l">Tokens</span></div>
  </div>

  <div class="bar">
    <div class="sbox">
      <Icon name="search" size={13} />
      <input placeholder="Filter by session id" bind:value={filter} />
    </div>
    <label class="toggle">
      <input type="checkbox" bind:checked={onlyActive} />
      Hide empty runs
    </label>
  </div>

  <div class="dbhead" style={`grid-template-columns:${COLUMNS}`}>
    <span>Session</span>
    <span class="r">Messages</span>
    <span class="r">Commits</span>
    <span class="r">Memories</span>
    <span class="r">Tokens</span>
    <span class="r">Updated</span>
  </div>
  <div class="rows">
    {#each shown as session (session.id)}
      <div class="rw" style={`grid-template-columns:${COLUMNS}`}>
        <span class="cell nm">
          <span class="tg"><Icon name="sessions" /></span>
          <span class="tlb" title={session.id}>{session.id}</span>
        </span>
        {#if session.detailed}
          <span class="cell r">{num(session.messages)}</span>
          <span class="cell r">{num(session.commits)}</span>
          <span class="cell r">
            {#if session.memories > 0}
              <span class="chip hybrid">{session.memories}</span>
            {:else}
              —
            {/if}
          </span>
          <span class="cell r">{session.tokens ? compact(session.tokens) : "—"}</span>
        {:else}
          <span class="cell r dim" title="Detail not loaded for older runs">·</span>
          <span class="cell r dim">·</span>
          <span class="cell r dim">·</span>
          <span class="cell r dim">·</span>
        {/if}
        <span class="cell r">{formatWhen(session.updatedAt)}</span>
      </div>
    {:else}
      <p class="pdesc">No runs match that filter.</p>
    {/each}
  </div>

  {#if sessions.some((s) => !s.detailed)}
    <p class="note mono">
      Older runs show · because their detail was not fetched — the list endpoint
      carries no counts, so only the newest {sessions.filter((s) => s.detailed).length} were read in full.
    </p>
  {/if}
{/if}

<style>
  .bar {
    display: flex;
    align-items: center;
    gap: 14px;
    margin: 20px 0 4px;
  }
  .sbox {
    flex: 1;
    max-width: 300px;
    display: flex;
    align-items: center;
    gap: 8px;
    background: var(--surface);
    border-radius: 9px;
    padding: 8px 11px;
    border: 1px solid var(--rule);
    color: var(--ink-3);
  }
  .sbox:focus-within {
    border-color: var(--accent);
  }
  .sbox input {
    flex: 1;
    min-width: 0;
    border: none;
    background: none;
    font: inherit;
    font-size: 14px;
    color: var(--ink);
  }
  .sbox input:focus {
    outline: none;
  }
  .toggle {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 13.5px;
    color: var(--ink-2);
    cursor: pointer;
  }
  .tlb {
    font-family: var(--fm);
    font-size: 12.5px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    padding-left: 5px;
  }
  .dim {
    color: var(--ink-3);
    opacity: 0.5;
  }
  .note {
    font-size: 11.5px;
    color: var(--ink-3);
    margin-top: 16px;
    line-height: 1.6;
  }
</style>
