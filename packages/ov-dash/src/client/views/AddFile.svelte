<script lang="ts">
  /**
   * Adding files.
   *
   * The browser posts bytes to the dashboard, which writes them to a private
   * temporary file and asks OpenViking to import it. The browser never holds a
   * key, so it cannot upload to OpenViking directly.
   *
   * Where a file goes is chosen here rather than assumed: a scope, then either
   * a folder that already exists or a new one typed in. The final path is shown
   * before anything is sent, because a destination you cannot see is one you
   * only discover by getting it wrong.
   */
  import { api, type Destinations } from "../lib/api";
  import Icon from "../lib/Icon.svelte";
  import PageHead from "../lib/PageHead.svelte";
  import { toast } from "../lib/state.svelte";
  import { formatSize } from "../lib/tree";

  interface Queued {
    file: File;
    status: "waiting" | "sending" | "done" | "failed";
    detail: string;
  }

  let queue = $state<Queued[]>([]);
  let over = $state(false);
  let input: HTMLInputElement | undefined = $state();

  let places = $state<Destinations | null>(null);
  let scope = $state("");
  /** A folder under the scope. Empty means the scope root itself. */
  let folder = $state("");

  $effect(() => {
    api
      .destinations()
      .then((result: Destinations) => {
        places = result;
        scope = result.scopes[0]?.uri ?? "";
      })
      .catch(() => {
        // The picker is a convenience; without it the server's default
        // destination still applies, so a failure here must not block adding.
      });
  });

  /** Folders inside the chosen scope, offered as suggestions. */
  const inScope = $derived(
    places?.folders.filter((f) => f.uri.startsWith(`${scope}/`)) ?? [],
  );

  /** Where files will land, before each gets its own folder. */
  const destination = $derived(
    folder.trim() ? `${scope}/${folder.trim().replace(/^\/+|\/+$/g, "")}` : scope,
  );

  function enqueue(files: FileList | null): void {
    if (!files) return;
    queue = [
      ...queue,
      ...[...files].map((file) => ({
        file,
        status: "waiting" as const,
        detail: "",
      })),
    ];
  }

  async function send(): Promise<void> {
    for (const item of queue) {
      if (item.status !== "waiting") continue;
      item.status = "sending";
      try {
        await api.upload(item.file, destination);
        item.status = "done";
        item.detail = "";
      } catch (error) {
        item.status = "failed";
        item.detail = (error as Error).message;
      }
    }
    const done = queue.filter((item) => item.status === "done").length;
    if (done > 0) toast(`Added ${done} ${done === 1 ? "file" : "files"}`);
  }

  const pending = $derived(queue.filter((item) => item.status === "waiting").length);
  const failed = $derived(queue.filter((item) => item.status === "failed").length);
  const added = $derived(queue.filter((item) => item.status === "done").length);

  /** The folder one file gets, mirroring what the server does with the name. */
  function folderFor(name: string): string {
    const safe = (name.split(/[/\\]/).pop() ?? "file").replace(/[^A-Za-z0-9._-]/g, "_");
    const bare = safe.replace(/^\.+/, "");
    const stem = bare.includes(".") ? bare.replace(/\.[^.]+$/, "") : bare;
    return stem || "file";
  }
</script>

<PageHead
  eyebrow="Add a file"
  title="Add a file"
  desc="Drop it here and OpenViking will read, index and remember it."
/>

<section class="where">
  <h2 class="wh">Where it goes</h2>

  {#if places}
    <div class="fields">
      <label class="field">
        <span class="lbl">Scope</span>
        <select bind:value={scope}>
          {#each places.scopes as s (s.uri)}
            <option value={s.uri}>{s.label}</option>
          {/each}
        </select>
      </label>

      <label class="field grow">
        <span class="lbl">Folder <span class="opt">optional</span></span>
        <input
          list="ovdash-folders"
          placeholder="e.g. notes/meetings — or pick an existing one"
          bind:value={folder}
        />
        <datalist id="ovdash-folders">
          {#each inScope as f (f.uri)}
            <option value={f.label}></option>
          {/each}
        </datalist>
      </label>
    </div>

    <p class="preview mono">
      {destination}<span class="stem"
        >/{queue[0] ? folderFor(queue[0].file.name) : "«file»"}/</span
      >
    </p>
    <p class="note">
      Each file gets a folder of its own, named after it, so adding two files
      never mixes them together.
    </p>
  {:else}
    <p class="pdesc">Loading destinations…</p>
  {/if}
</section>

<div
  class="drop"
  class:over
  role="button"
  tabindex="0"
  ondragover={(event) => {
    event.preventDefault();
    over = true;
  }}
  ondragleave={() => {
    over = false;
  }}
  ondrop={(event) => {
    event.preventDefault();
    over = false;
    enqueue(event.dataTransfer?.files ?? null);
  }}
  onclick={() => input?.click()}
  onkeydown={(event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      input?.click();
    }
  }}
>
  <div class="di"><Icon name="upload" size={26} /></div>
  <p><strong>Drop files here</strong></p>
  <p class="pdesc" style="margin:6px auto 0">or click to choose them</p>
</div>

<input
  bind:this={input}
  type="file"
  multiple
  hidden
  onchange={(event) => enqueue((event.currentTarget as HTMLInputElement).files)}
/>

{#if queue.length > 0}
  <h2>Ready to add</h2>
  <div class="rows">
    {#each queue as item (item.file.name + item.file.size)}
      <div class="rw" style="grid-template-columns:minmax(0,1fr) 190px 84px 96px">
        <span class="cell nm">
          <span class="tg"><Icon name="doc" /></span>
          <span class="tlb">{item.file.name}</span>
        </span>
        <span class="cell r lands mono">/{folderFor(item.file.name)}/</span>
        <span class="cell r">{formatSize(item.file.size, false)}</span>
        <span class="cell d">
          {#if item.status === "done"}
            <span class="chip hybrid">added</span>
          {:else if item.status === "failed"}
            <span class="chip word">failed</span>
          {:else if item.status === "sending"}
            <span class="chip agent">sending</span>
          {:else}
            <span class="chip you">waiting</span>
          {/if}
        </span>
      </div>
      {#if item.status === "failed" && item.detail}
        <p class="why">{item.detail}</p>
      {/if}
    {/each}
  </div>

  {#if added > 0 || failed > 0}
    <div class="callout {failed > 0 ? 'c-sheet' : 'c-teal'}" style="margin-top:14px">
      <span class="ci"><Icon name={failed > 0 ? "close" : "check"} /></span>
      <p>
        {added > 0 ? `Added ${added} ${added === 1 ? "file" : "files"}.` : ""}
        {failed > 0 ? `${failed} failed — the reason is under each row.` : ""}
      </p>
    </div>
  {/if}

  <p style="margin-top:18px">
    <button class="tbtn primary" disabled={pending === 0} onclick={send}>
      <Icon name="upload" /> Add {pending} {pending === 1 ? "file" : "files"}
    </button>
  </p>
{/if}

<style>
  .where {
    margin-top: var(--s5);
    max-width: 74ch;
  }
  .wh {
    margin: 0 0 var(--s3);
    font-size: 18px;
  }
  .fields {
    display: flex;
    gap: var(--s3);
    flex-wrap: wrap;
  }
  .field {
    display: block;
    min-width: 190px;
  }
  .field.grow {
    flex: 1;
    min-width: 260px;
  }
  .lbl {
    display: block;
    font-family: var(--fm);
    font-size: 10px;
    letter-spacing: 0.13em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin-bottom: 5px;
  }
  .opt {
    letter-spacing: 0.06em;
    opacity: 0.7;
  }
  .field select,
  .field input {
    width: 100%;
    font: inherit;
    font-size: 14px;
    color: var(--ink);
    background: var(--surface);
    border: 1px solid var(--rule);
    border-radius: var(--r2);
    padding: 9px 11px;
  }
  .field select:focus,
  .field input:focus {
    outline: none;
    border-color: var(--accent);
  }
  .field input::placeholder {
    color: var(--ink-3);
  }

  .preview {
    margin: var(--s4) 0 var(--s1);
    font-size: 12px;
    color: var(--ink-2);
    word-break: break-all;
  }
  .preview .stem {
    color: var(--accent);
  }
  .note {
    margin: 0;
    font-size: 13px;
    color: var(--ink-3);
    line-height: 1.55;
    max-width: 60ch;
  }

  .lands {
    color: var(--ink-3);
  }
  .why {
    font-size: 12.5px;
    color: var(--rose);
    margin: 2px 0 10px 30px;
    line-height: 1.5;
  }
  .drop {
    cursor: pointer;
  }
  .drop.over {
    border-color: var(--accent);
    background: var(--accent-quiet);
  }
</style>
