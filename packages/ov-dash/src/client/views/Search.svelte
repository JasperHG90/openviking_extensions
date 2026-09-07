<script lang="ts">
  /**
   * Search across the two faces OpenViking offers.
   *
   * Hybrid runs them as two requests rather than one, because they are not
   * remotely the same speed: measured against the lab cluster, the vector face
   * answers in about two seconds and grep takes twenty. Waiting for both meant
   * twenty seconds of blank page, which reads as broken. So the meaning results
   * are shown as soon as they land and the exact ones merge in underneath.
   */
  import { api, type SearchResponse } from "../lib/api";
  import Icon from "../lib/Icon.svelte";
  import PageHead from "../lib/PageHead.svelte";
  import { go } from "../lib/state.svelte";
  import type { SearchHit, SearchMode } from "../../shared/schemas";

  let query = $state("");
  let mode = $state<SearchMode>("hybrid");
  let hits = $state<SearchHit[]>([]);
  let counts = $state({ meaning: 0, exact: 0 });
  let failure = $state("");
  let ran = $state(false);

  /** Which face is still outstanding, so the UI can say so rather than stall. */
  let pending = $state<Set<SearchMode>>(new Set());

  const MODES: { key: SearchMode; label: string; hint: string }[] = [
    { key: "hybrid", label: "Hybrid", hint: "both, fastest first" },
    { key: "meaning", label: "By meaning", hint: "vector search, ~2s" },
    { key: "exact", label: "Exact words", hint: "grep, slower" },
  ];

  let token = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;

  function schedule(): void {
    clearTimeout(timer);
    timer = setTimeout(run, 300);
  }

  /** Merge a face's results into what is already on screen, by uri. */
  function merge(incoming: SearchResponse): void {
    const byUri = new Map(hits.map((hit) => [hit.uri, hit]));
    for (const hit of incoming.hits) {
      const existing = byUri.get(hit.uri);
      if (!existing) {
        byUri.set(hit.uri, hit);
        continue;
      }
      byUri.set(hit.uri, {
        ...existing,
        score: existing.score + hit.score,
        snippet: existing.snippet || hit.snippet,
        match: {
          meaning: existing.match.meaning ?? hit.match.meaning,
          exact: [...new Set([...existing.match.exact, ...hit.match.exact])],
        },
      });
    }
    hits = [...byUri.values()].sort((a, b) => b.score - a.score);
    counts = {
      meaning: Math.max(counts.meaning, incoming.counts.meaning),
      exact: Math.max(counts.exact, incoming.counts.exact),
    };
  }

  async function run(): Promise<void> {
    const q = query.trim();
    const mine = ++token;
    hits = [];
    counts = { meaning: 0, exact: 0 };
    failure = "";
    if (!q) {
      ran = false;
      pending = new Set();
      return;
    }
    ran = true;

    const faces: SearchMode[] = mode === "hybrid" ? ["meaning", "exact"] : [mode];
    pending = new Set(faces);

    await Promise.all(
      faces.map(async (face) => {
        try {
          const result = await api.search(q, face);
          // A slower face landing after the query moved on must not overwrite
          // the newer results.
          if (token !== mine) return;
          merge(result);
        } catch (error) {
          if (token === mine) failure = (error as Error).message;
        } finally {
          if (token === mine) {
            const next = new Set(pending);
            next.delete(face);
            pending = next;
          }
        }
      }),
    );
  }

  function pick(next: SearchMode): void {
    mode = next;
    void run();
  }

  function escapeHtml(value: string): string {
    return value.replace(
      /[&<>"']/g,
      (ch) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch] ??
        ch,
    );
  }

  /**
   * Wrap matched words so the reader can see why a result is here.
   *
   * The split runs on the raw snippet and each piece is escaped after, so a
   * search for `amp` cannot land inside an entity and cut it in half.
   */
  function highlight(snippet: string, terms: string[]): string {
    if (terms.length === 0) return escapeHtml(snippet);
    const pattern = new RegExp(
      `(${terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`,
      "gi",
    );
    return snippet
      .split(pattern)
      .map((piece, index) =>
        index % 2 === 1 ? `<mark>${escapeHtml(piece)}</mark>` : escapeHtml(piece),
      )
      .join("");
  }

  const waiting = $derived([...pending]);
</script>

<PageHead
  eyebrow="Search"
  title="Search"
  desc="Ask in your own words, or spell the words exactly. Hybrid does both."
/>

<div class="sfield">
  <Icon name="search" />
  <input
    type="search"
    placeholder="Search your files and memories"
    bind:value={query}
    oninput={schedule}
    onkeydown={(event) => {
      if (event.key === "Enter") void run();
    }}
  />
</div>

<div class="views" style="border-bottom:none">
  <div class="seg">
    {#each MODES as option (option.key)}
      <button
        aria-pressed={mode === option.key}
        title={option.hint}
        onclick={() => pick(option.key)}
      >
        {option.label}
      </button>
    {/each}
  </div>
  <span class="sp"></span>
  {#if ran}
    <span class="mono tally">
      {counts.meaning} by meaning · {counts.exact} exact
    </span>
  {/if}
</div>

{#if waiting.length > 0}
  <div class="working mono">
    <span class="spin" aria-hidden="true"></span>
    Searching {waiting.map((f) => (f === "meaning" ? "by meaning" : "for exact words")).join(" and ")}…
    {#if waiting.includes("exact")}<span class="dim">grep takes about 20 seconds</span>{/if}
  </div>
{/if}

{#if failure}
  <div class="callout c-sheet"><span class="ci"><Icon name="close" /></span><p>{failure}</p></div>
{/if}

{#each hits as hit (hit.uri)}
  <button class="hit" onclick={() => go({ page: "file", uri: hit.uri })}>
    <span class="hh">
      <span class="hn">{hit.name}</span>
      {#if hit.match.meaning !== null && hit.match.exact.length > 0}
        <span class="chip hybrid">both</span>
      {:else if hit.match.meaning !== null}
        <span class="chip mean">meaning {hit.match.meaning.toFixed(2)}</span>
      {:else}
        <span class="chip word">exact</span>
      {/if}
      {#if hit.match.exact.length > 0}
        <span class="mono matched">matched {hit.match.exact.join(", ")}</span>
      {/if}
    </span>
    <div class="hp">{hit.relPath}</div>
    <div class="hs">{@html highlight(hit.snippet, hit.match.exact)}</div>
  </button>
{:else}
  {#if ran && waiting.length === 0}
    <p class="pdesc">Nothing matched “{query}”.</p>
  {:else if !ran}
    <p class="pdesc">Type to search.</p>
  {/if}
{/each}

<style>
  .tally {
    font-size: 12px;
    color: var(--ink-3);
  }
  .matched {
    font-size: 11.5px;
    color: var(--ink-3);
  }
  .working {
    display: flex;
    align-items: center;
    gap: 9px;
    font-size: 12px;
    color: var(--ink-2);
    margin: 16px 0 4px;
  }
  .dim {
    color: var(--ink-3);
  }
  .spin {
    width: 11px;
    height: 11px;
    border-radius: 50%;
    border: 1.6px solid var(--rule);
    border-top-color: var(--accent);
    animation: spin 0.7s linear infinite;
  }
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .spin {
      animation: none;
    }
  }
</style>
