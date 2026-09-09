<script lang="ts">
  /**
   * Search, by meaning.
   *
   * One face, the vector one — the same search `ov search` runs. The page used
   * to offer grep alongside it and a hybrid of the two, which meant every
   * query carried a mode decision nobody wanted to make, and choosing wrong
   * cost twenty seconds: measured against the lab cluster, the vector face
   * answers in about two seconds and grep takes twenty. Asking in your own
   * words is the thing this box is for, so it is the only thing it does.
   */
  import { api } from "../lib/api";
  import Icon from "../lib/Icon.svelte";
  import PageHead from "../lib/PageHead.svelte";
  import { go } from "../lib/state.svelte";
  import type { SearchHit } from "../../shared/schemas";

  let query = $state("");
  let hits = $state<SearchHit[]>([]);
  let found = $state(0);
  let failure = $state("");
  let ran = $state(false);
  let searching = $state(false);

  let token = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;

  function schedule(): void {
    clearTimeout(timer);
    timer = setTimeout(run, 300);
  }

  async function run(): Promise<void> {
    const q = query.trim();
    const mine = ++token;
    hits = [];
    found = 0;
    failure = "";
    if (!q) {
      ran = false;
      searching = false;
      return;
    }
    ran = true;
    searching = true;

    try {
      const result = await api.search(q, "meaning");
      // A slow answer landing after the query moved on must not overwrite the
      // newer results.
      if (token !== mine) return;
      hits = result.hits;
      found = result.counts.meaning;
    } catch (error) {
      if (token === mine) failure = (error as Error).message;
    } finally {
      if (token === mine) searching = false;
    }
  }

</script>

<PageHead eyebrow="Search" title="Search" desc="Ask in your own words." />

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

{#if ran && !searching}
  <div class="views" style="border-bottom:none">
    <span class="sp"></span>
    <span class="mono tally">{found} by meaning</span>
  </div>
{/if}

{#if searching}
  <div class="working mono">
    <span class="spin" aria-hidden="true"></span>
    Searching by meaning…
  </div>
{/if}

{#if failure}
  <div class="callout c-sheet"><span class="ci"><Icon name="close" /></span><p>{failure}</p></div>
{/if}

{#each hits as hit (hit.uri)}
  <button class="hit" onclick={() => go({ page: "file", uri: hit.uri })}>
    <span class="hh">
      <span class="hn">{hit.name}</span>
      {#if hit.match.meaning !== null}
        <span class="chip mean">meaning {hit.match.meaning.toFixed(2)}</span>
      {/if}
    </span>
    <div class="hp">{hit.relPath}</div>
    <div class="hs">{hit.snippet}</div>
  </button>
{:else}
  {#if ran && !searching}
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
  .working {
    display: flex;
    align-items: center;
    gap: 9px;
    font-size: 12px;
    color: var(--ink-2);
    margin: 16px 0 4px;
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
