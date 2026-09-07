<script lang="ts">
  import { api, type Home } from "../lib/api";
  import Download from "../lib/Download.svelte";
  import Icon from "../lib/Icon.svelte";
  import PageHead from "../lib/PageHead.svelte";
  import { go } from "../lib/state.svelte";
  import { formatSize, formatWhen } from "../lib/tree";

  let data = $state<Home | null>(null);
  let failure = $state("");

  /** The first line worth showing, skipping a markdown heading marker. */
  function firstLine(text: string): string {
    const line = text.split("\n").find((l) => l.trim()) ?? "";
    return line.replace(/^#+\s*/, "").trim();
  }

  $effect(() => {
    api
      .home()
      .then((home) => {
        data = home;
      })
      .catch((error: Error) => {
        failure = error.message;
      });
  });
</script>

{#if failure}
  <PageHead eyebrow="Your workspace" title="Home" />
  <div class="callout c-sheet"><span class="ci"><Icon name="close" /></span><p>{failure}</p></div>
{:else if !data}
  <PageHead eyebrow="Your workspace" title="Home" />
  <p class="pdesc">Loading…</p>
{:else}
  <PageHead eyebrow="Your workspace" title={`${data.viewer.name}'s workspace`} />

  <div class="strip">
    <div><span class="n">{data.stats.files}</span><span class="l">Files</span></div>
    <div><span class="n">{data.stats.memories}</span><span class="l">Memories</span></div>
    <div><span class="n">{data.stats.sessions}</span><span class="l">Sessions</span></div>
  </div>

  <h2>Recent files</h2>
  <div class="dbhead" style="grid-template-columns:minmax(0,1fr) 104px 96px 120px 46px">
    <span>Name</span><span>Kind</span><span class="r">Size</span><span class="r">Updated</span><span></span>
  </div>
  <div class="rows">
    {#each data.recentFiles as file (file.uri)}
      <div class="rw" style="grid-template-columns:minmax(0,1fr) 104px 96px 120px 46px">
        <button class="cell nm" onclick={() => go({ page: "file", uri: file.uri })}>
          <span class="tg"><Icon name="doc" /></span>
          <span class="tlb">{file.name}</span>
        </button>
        <span class="cell d">{file.kind}</span>
        <span class="cell r">{formatSize(file.size, file.isDir)}</span>
        <span class="cell r">{formatWhen(file.modTime)}</span>
        <span class="cell r"><Download uri={file.uri} name={file.name} isDir={file.isDir} /></span>
      </div>
    {:else}
      <p class="pdesc">Nothing here yet. Add a file to get started.</p>
    {/each}
  </div>
  <button class="newrow" onclick={() => go({ page: "files" })}>
    <Icon name="files" /> Show all files
  </button>

  <h2>Recent memories</h2>
  {#each data.recentMemories as memory (memory.uri)}
    <button class="mi mrow" onclick={() => go({ page: "file", uri: memory.uri })}>
      <div>
        <p>{firstLine(memory.text)}</p>
        <span class="mt"><b>{memory.category}</b> · {formatWhen(memory.modTime)}</span>
      </div>
      <span class="go"><Icon name="chevron" size={10} /></span>
    </button>
  {:else}
    <p class="pdesc">No memories yet. They appear once an agent run is committed.</p>
  {/each}
  <button class="newrow" onclick={() => go({ page: "memories" })}>
    <Icon name="memory" /> Show all memories
  </button>
{/if}

<style>
  .mrow {
    width: 100%;
    text-align: left;
    align-items: center;
  }
  .mrow .go {
    color: var(--ink-3);
    opacity: 0;
  }
  .mrow:hover .go {
    opacity: 1;
    color: var(--accent);
  }
</style>
