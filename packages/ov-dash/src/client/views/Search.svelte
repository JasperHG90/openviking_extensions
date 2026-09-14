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
  let searching = $state(false);

  /**
   * The query the results on screen belong to, or "" before the first search.
   *
   * Held rather than read off `query`, because the box keeps changing while an
   * answer is on screen: "Nothing matched X" has to name what was actually
   * asked, not whatever has been typed since.
   */
  let asked = $state("");

  let token = 0;

  /**
   * Run the search, once, when it is asked for.
   *
   * Typing used to start one 300ms after the last keystroke, which meant a
   * query was sent on the way to being finished — "kube" searched before
   * "kubernetes" was typed. Each one is a real call to OpenViking's vector face,
   * about two seconds against the lab cluster, so a half-typed question cost a
   * round trip and put results on screen for something nobody asked. Now the
   * button, or Enter, is what asks.
   */
  async function run(): Promise<void> {
    const q = query.trim();
    const mine = ++token;
    hits = [];
    found = 0;
    failure = "";
    // Unreachable through the form — the submit button is disabled on an empty
    // box, and implicit submission does nothing when the default button is
    // disabled. Here so `run()` is safe to call from anywhere, not because an
    // empty submit is expected.
    if (!q) {
      asked = "";
      searching = false;
      return;
    }
    asked = q;
    searching = true;

    try {
      const result = await api.search(q, "meaning");
      // Belt and braces. Two searches cannot currently overlap — the submit
      // button is the only way in and it is disabled while one is running — so
      // this never fires today. Kept because the guard is one line and a second
      // entry point added later would otherwise let a slow answer overwrite a
      // newer one, which is a bug nobody would look for here.
      if (token !== mine) return;
      hits = result.hits;
      found = result.counts.meaning;
    } catch (error) {
      if (token === mine) failure = (error as Error).message;
    } finally {
      if (token === mine) searching = false;
    }
  }

  /** Whether there is anything to search for. */
  const ready = $derived(query.trim() !== "");
  /**
   * True once a search has run and come back with an answer.
   *
   * A failure is not an answer: without that clause the page drew the error
   * callout, "0 by meaning", and "Nothing matched" all at once — telling somebody
   * their query found nothing when the truth is that nothing was searched.
   */
  const answered = $derived(asked !== "" && !searching && !failure);
</script>

<PageHead
  eyebrow="Search"
  title="Search"
  desc="Ask in your own words, then press Search."
/>

<!--
  A form, so Enter submits it the way Enter submits any search box, and the
  button is a real submit button rather than a click handler pretending to be
  one. Nothing runs until one of those two happens.
-->
<form
  class="sbar"
  onsubmit={(event) => {
    event.preventDefault();
    void run();
  }}
>
  <div class="sfield">
    <Icon name="search" />
    <input
      type="search"
      placeholder="Search your files and memories"
      bind:value={query}
    />
  </div>
  <button class="tbtn primary" type="submit" disabled={!ready || searching}>
    {searching ? "Searching…" : "Search"}
  </button>
</form>

{#if answered}
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
  {#if answered}
    <!-- Names the query the results belong to, not what the box says now. -->
    <p class="pdesc">Nothing matched “{asked}”.</p>
  {:else if !searching && !failure}
    <p class="pdesc">Type a question, then press Search.</p>
  {/if}
{/each}

<style>
  /*
   * The box and its button on one line, the box taking what is left.
   *
   * The measure and the top margin move from the box to the bar: `.sfield`
   * carries both globally, and left there the button would sit adrift past the
   * end of a 74ch box rather than against it.
   */
  .sbar {
    display: flex;
    align-items: center;
    gap: var(--s2);
    max-width: 74ch;
    margin-top: var(--s5);
  }
  .sbar .sfield {
    flex: 1;
    min-width: 0;
    max-width: none;
    margin-top: 0;
  }
  /* Tall enough to match the box beside it, which is padded for reading. */
  .sbar .tbtn {
    padding: 13px 20px;
    flex: none;
  }

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
