<script lang="ts">
  /**
   * Memories, grouped by category.
   *
   * A flat list stops working somewhere around fifty entries, and this grows
   * with every agent run. So: a text filter over name and body, category
   * headers that collapse, and long entries clipped until asked to expand.
   * Forgetting one deletes it upstream, so the row goes only after the server
   * confirms.
   */
  import { api, type MemoryGroup } from "../lib/api";
  import Icon from "../lib/Icon.svelte";
  import PageHead from "../lib/PageHead.svelte";
  import { renderMarkdown } from "../lib/markdown";
  import { go } from "../lib/state.svelte";
  import { toast } from "../lib/state.svelte";
  import { formatWhen } from "../lib/tree";

  let groups = $state<MemoryGroup[] | null>(null);
  let failure = $state("");
  let busy = $state("");
  let filter = $state("");

  /**
   * How the shelves are ordered.
   *
   * "Newest" flattens the grouping on purpose: a date order that restarts
   * inside every category answers a different question from the one being
   * asked. Grouping stays available under "Category".
   */
  type Sort = "category" | "newest" | "oldest";
  let sort = $state<Sort>("category");
  const SORTS: { key: Sort; label: string }[] = [
    { key: "category", label: "Category" },
    { key: "newest", label: "Newest" },
    { key: "oldest", label: "Oldest" },
  ];
  let collapsed = $state<Set<string>>(new Set());
  let expanded = $state<Set<string>>(new Set());

  function load(): void {
    api
      .memories()
      .then((result) => {
        groups = result;
      })
      .catch((error: Error) => {
        failure = error.message;
      });
  }

  $effect(load);

  const matching = $derived.by(() => {
    if (!groups) return [];
    const query = filter.trim().toLowerCase();
    if (!query) return groups;
    return groups
      .map((group) => ({
        ...group,
        memories: group.memories.filter(
          (memory) =>
            memory.name.toLowerCase().includes(query) ||
            memory.text.toLowerCase().includes(query) ||
            group.category.toLowerCase().includes(query),
        ),
      }))
      .filter((group) => group.memories.length > 0);
  });

  /**
   * The groups to draw.
   *
   * Sorting by date collapses everything into one list under a single heading,
   * because the point of that order is to see what changed most recently
   * across the whole workspace.
   */
  const shown = $derived.by(() => {
    if (sort === "category") {
      return matching.map((group) => ({
        ...group,
        memories: [...group.memories].sort((a, b) => a.name.localeCompare(b.name)),
      }));
    }
    const flat = matching.flatMap((group) =>
      group.memories.map((memory) => ({ ...memory, category: group.category })),
    );
    flat.sort((a, b) =>
      sort === "newest"
        ? b.modTime.localeCompare(a.modTime)
        : a.modTime.localeCompare(b.modTime),
    );
    return flat.length ? [{ category: sort === "newest" ? "Newest first" : "Oldest first", memories: flat }] : [];
  });

  const total = $derived(
    groups?.reduce((sum, group) => sum + group.memories.length, 0) ?? 0,
  );
  const showing = $derived(shown.reduce((sum, group) => sum + group.memories.length, 0));

  /** Grouping controls only make sense when the grouping is showing. */
  const grouped = $derived(sort === "category");

  function toggleGroup(category: string): void {
    const next = new Set(collapsed);
    if (next.has(category)) next.delete(category);
    else next.add(category);
    collapsed = next;
  }

  function toggleOne(uri: string): void {
    const next = new Set(expanded);
    if (next.has(uri)) next.delete(uri);
    else next.add(uri);
    expanded = next;
  }

  async function forget(uri: string, name: string): Promise<void> {
    busy = uri;
    try {
      await api.forget(uri);
      groups =
        groups
          ?.map((group) => ({
            ...group,
            memories: group.memories.filter((memory) => memory.uri !== uri),
          }))
          .filter((group) => group.memories.length > 0) ?? null;
      toast(`Forgot ${name}`);
    } catch (error) {
      toast((error as Error).message);
    } finally {
      busy = "";
    }
  }

  /** How much of a memory to show before it needs expanding. */
  const CLIP = 240;

  /**
   * A colour per category, so a shelf is recognisable before it is read.
   *
   * Assigned by hashing the name rather than by a fixed table: OpenViking's
   * built-in categories are known, but a deployment can add its own and an
   * unlisted one should still get a stable colour instead of no colour.
   */
  const HUES = ["--sage", "--clay", "--slate", "--plum", "--gold", "--rose"];

  /**
   * OpenViking's built-in memory types, each given its own colour.
   *
   * A fixed table rather than a hash: these eight are documented and stable, and
   * every hash tried put half of them on the same colour, which defeats the
   * point of colouring them at all. Anything a deployment adds falls through to
   * the hash below, so a custom type still gets a stable colour.
   */
  const KNOWN: Record<string, string> = {
    profile: "--slate",
    preferences: "--sage",
    entities: "--plum",
    events: "--clay",
    identity: "--gold",
    soul: "--rose",
    cases: "--clay",
    trajectories: "--slate",
    experiences: "--sage",
    tools: "--gold",
    skills: "--plum",
    patterns: "--rose",
  };

  function hue(category: string): string {
    const known = KNOWN[category.toLowerCase()];
    if (known) return `var(${known})`;
    let h = 5381;
    for (const ch of category) h = ((h << 5) + h + ch.charCodeAt(0)) >>> 0;
    return `var(${HUES[h % HUES.length]})`;
  }
</script>

<PageHead
  eyebrow="Memories"
  title="Memories"
  desc="What OpenViking has learned from your agent runs."
/>

{#if failure}
  <div class="callout c-sheet"><span class="ci"><Icon name="close" /></span><p>{failure}</p></div>
{:else if !groups}
  <p class="pdesc">Loading…</p>
{:else if total === 0}
  <div class="callout c-sheet">
    <span class="ci"><Icon name="memory" /></span>
    <p>No memories yet. They appear once an agent run is committed.</p>
  </div>
{:else}
  <div class="bar">
    <div class="sbox">
      <Icon name="search" size={13} />
      <input placeholder="Filter memories" bind:value={filter} />
      {#if filter}
        <button class="clr" onclick={() => (filter = "")} aria-label="Clear filter">
          <Icon name="close" size={12} />
        </button>
      {/if}
    </div>
    <span class="mono tally">
      {filter ? `${showing} of ${total}` : `${total}`} memories
    </span>
    <span class="sortlabel mono">Sort</span>
    <div class="seg sorts">
      {#each SORTS as option (option.key)}
        <button aria-pressed={sort === option.key} onclick={() => (sort = option.key)}>
          {option.label}
        </button>
      {/each}
    </div>
    <!--
      Always rendered, disabled when there is nothing to collapse. Hiding them
      in date order made the whole toolbar jump sideways as you switched.
    -->
    <button
      class="mini"
      disabled={!grouped}
      title={grouped ? "Collapse every category" : "Only applies when grouped by category"}
      onclick={() => (collapsed = new Set(shown.map((g) => g.category)))}
    >
      Collapse all
    </button>
    <button
      class="mini"
      disabled={!grouped}
      title={grouped ? "Expand every category" : "Only applies when grouped by category"}
      onclick={() => (collapsed = new Set())}
    >
      Expand all
    </button>
  </div>

  {#each shown as group (group.category)}
    <button class="ghead" onclick={() => toggleGroup(group.category)}>
      <span class="gchev" class:open={!collapsed.has(group.category)}>
        <Icon name="chevron" size={9} />
      </span>
      <!-- A date group is not a category, so it gets no category colour. -->
      {#if grouped}
        <span class="dot" style={`background:${hue(group.category)}`}></span>
      {/if}
      <span class="gname">{group.category}</span>
      <span class="mono gcount">{group.memories.length}</span>
    </button>

    {#if !collapsed.has(group.category)}
      {#each group.memories as memory (memory.uri)}
        {@const long = memory.text.length > CLIP}
        {@const open = expanded.has(memory.uri)}
        <article class="mi">
          <!-- Sanitized in renderMarkdown before it reaches the DOM. -->
          <div class="mtext" class:clip={long && !open}>{@html renderMarkdown(memory.text)}</div>

          <footer class="mfoot">
            <span class="dot" style={`background:${hue(memory.category)}`}></span>
            {#if !grouped}<span class="mcat">{memory.category}</span>{/if}
            <span class="mname mono">{memory.name}</span>
            <span class="mwhen mono">{formatWhen(memory.modTime)}</span>
            <span class="grow"></span>
            {#if long}
              <button class="act" onclick={() => toggleOne(memory.uri)}>
                {open ? "Show less" : "Show more"}
              </button>
            {/if}
            <button class="act" onclick={() => go({ page: "file", uri: memory.uri })}>
              Open
            </button>
            <button
              class="act danger"
              disabled={busy === memory.uri}
              onclick={() => forget(memory.uri, memory.name)}
            >
              {busy === memory.uri ? "Forgetting\u2026" : "Forget"}
            </button>
          </footer>
        </article>
      {/each}
    {/if}
  {:else}
    <p class="pdesc">Nothing matches “{filter}”.</p>
  {/each}
{/if}

<style>
  .bar {
    display: flex;
    align-items: center;
    gap: var(--s2);
    margin: var(--s5) 0 var(--s2);
  }
  .sbox {
    flex: 1;
    max-width: 320px;
    display: flex;
    align-items: center;
    gap: var(--s2);
    background: var(--surface);
    border-radius: var(--r2);
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
  .sbox input::placeholder {
    color: var(--ink-3);
  }
  .clr {
    display: grid;
    place-items: center;
    color: var(--ink-3);
  }
  .clr:hover {
    color: var(--ink);
  }
  .tally {
    font-size: 12px;
    color: var(--ink-3);
    margin-right: auto;
    padding-left: var(--s1);
  }

  /* Shelf header: chevron, colour, name, count — in that order, left to right. */
  .ghead {
    display: flex;
    align-items: center;
    justify-content: flex-start;
    gap: 9px;
    width: 100%;
    text-align: left;
    padding: 9px 6px;
    margin: var(--s5) 0 var(--s2);
    border-radius: var(--r1);
    border-bottom: 1px solid var(--rule);
  }
  .ghead:hover {
    background: var(--hover);
  }
  .gchev {
    display: grid;
    place-items: center;
    color: var(--ink-3);
    transition: transform 0.16s ease;
    flex: none;
  }
  .gchev.open {
    transform: rotate(90deg);
  }
  .gname {
    font-family: var(--fd);
    font-size: 18px;
    font-weight: 600;
    letter-spacing: -0.008em;
  }
  .sorts button {
    padding: 5px 11px;
  }
  .sortlabel {
    font-size: 10px;
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--ink-3);
  }
  .mcat {
    font-size: 11.5px;
    font-weight: 600;
    color: var(--ink-2);
  }
  .gcount {
    font-size: 11px;
    color: var(--ink-3);
    margin-left: auto;
  }

  /* One card per memory, held to a readable measure. */
  .mi {
    display: block;
    max-width: 78ch;
    padding: var(--s4);
    margin: var(--s2) 0;
    border-radius: var(--r3);
    background: var(--surface);
    border: 1px solid var(--rule);
  }
  .mi:hover {
    border-color: color-mix(in srgb, var(--accent) 30%, var(--rule));
  }

  .mtext {
    font-family: var(--fd);
    font-size: 15.5px;
    line-height: 1.62;
    color: var(--ink);
  }
  /*
   * Clipped by height, not by line count.
   *
   * -webkit-line-clamp only counts lines inside one block, so a memory made of
   * several paragraphs and a heading ignored it and ran full length. A height
   * cap with a fade works whatever the shape of the content.
   */
  .mtext.clip {
    max-height: 7.2em;
    overflow: hidden;
    /*
     * The fade holds full strength most of the way down, so a clip that lands
     * on a heading reads as "there is more" rather than as a half-drawn line.
     */
    mask-image: linear-gradient(to bottom, #000 78%, transparent 100%);
  }
  .mtext :global(p) {
    margin: 0 0 0.55em;
  }
  .mtext :global(p:last-child) {
    margin-bottom: 0;
  }
  .mtext :global(h1),
  .mtext :global(h2),
  .mtext :global(h3),
  .mtext :global(h4) {
    font-family: var(--fd);
    font-size: 16px;
    font-weight: 600;
    margin: 0.9em 0 0.3em;
    line-height: 1.3;
  }
  .mtext :global(h1:first-child),
  .mtext :global(h2:first-child),
  .mtext :global(h3:first-child) {
    margin-top: 0;
  }
  .mtext :global(ul),
  .mtext :global(ol) {
    margin: 0 0 0.55em;
    padding-left: 1.3em;
  }
  .mtext :global(li) {
    margin: 0.2em 0;
  }
  .mtext :global(li::marker) {
    color: var(--ink-3);
  }
  .mtext :global(code) {
    font-family: var(--fm);
    font-size: 0.8em;
    background: var(--surface-2);
    padding: 1px 5px;
    border-radius: 4px;
  }
  .mtext :global(a) {
    color: var(--accent-ink);
    text-decoration-thickness: 1px;
    text-underline-offset: 2px;
  }
  .mtext :global(pre) {
    background: var(--surface-2);
    border-radius: var(--r1);
    padding: 10px 12px;
    overflow-x: auto;
    font-size: 12px;
  }

  /* Provenance and actions on one line, so nothing floats loose. */
  .mfoot {
    display: flex;
    align-items: center;
    gap: var(--s2);
    margin-top: var(--s3);
    padding-top: var(--s2);
    border-top: 1px solid var(--rule-2);
  }
  .mname {
    font-size: 11px;
    color: var(--ink-2);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 34ch;
  }
  .mwhen {
    font-size: 11px;
    color: var(--ink-3);
  }
  .grow {
    flex: 1;
  }
  .act {
    font-size: 12px;
    font-weight: 600;
    color: var(--ink-3);
    padding: 3px 9px;
    border-radius: 20px;
    white-space: nowrap;
  }
  .act:hover {
    background: var(--hover);
    color: var(--accent);
  }
  .act.danger:hover {
    background: color-mix(in srgb, var(--rose) 16%, transparent);
    color: var(--rose);
  }
  .act:disabled {
    opacity: 0.5;
  }
</style>
