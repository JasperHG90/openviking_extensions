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
  import { MAX_REASON_CHARS } from "../../shared/limits";
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

  /**
   * Why these are worth keeping, in your own words.
   *
   * Not a note filed beside the bytes. OpenViking takes it as the parsing
   * instruction when nothing else gives one, so it steers the abstract and
   * overview the model writes — and those are what the file is later found by.
   * Typing "for the Q3 pricing argument" is the difference between a file
   * described as what it is and one described as what it is for.
   */
  let why = $state("");

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

  /** True while the queue is being sent, so the fields it reads cannot move. */
  let sending = $state(false);

  async function send(): Promise<void> {
    if (sending) return;
    sending = true;
    /*
     * Both fields read once, before the first upload.
     *
     * A batch is several awaits long and the boxes stay on screen throughout, so
     * reading them per file meant a keystroke half-way through gave the rest of
     * the batch a different reason and a different folder — after the page had
     * said the reason applies to every file in it. The inputs are disabled while
     * this runs as well; the snapshot is what makes that a promise rather than a
     * hope.
     */
    const reason = why.trim();
    const into = destination;
    try {
      for (const item of queue) {
        if (item.status !== "waiting") continue;
        item.status = "sending";
        try {
          await api.upload(item.file, into, reason);
          item.status = "done";
          item.detail = "";
        } catch (error) {
          item.status = "failed";
          item.detail = (error as Error).message;
        }
      }
    } finally {
      sending = false;
    }
    const done = queue.filter((item) => item.status === "done").length;
    if (done > 0) {
      toast(`Added ${done} ${done === 1 ? "file" : "files"}`);
      // Cleared once something landed, so a second batch does not silently
      // inherit the first one's reason. Left alone on a total failure, since
      // that is the case where somebody is about to press the button again.
      why = "";
    }
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
        <select bind:value={scope} disabled={sending}>
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
          disabled={sending}
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

    <!--
      Why you are keeping it.

      Worth its own block rather than a third box in the row above, because it
      is the one field here that changes what OpenViking writes rather than
      where it writes it — and it needs room to say a sentence.
    -->
    <!--
      `reason`, not `why`: `.why` is already the class on the red line under a
      failed row further down this page, and a label carrying both picked up its
      30px indent and sat out of line with the fields above.
    -->
    <label class="field reason">
      <span class="lbl">Why you are keeping it <span class="opt">optional</span></span>
      <textarea
        rows="2"
        maxlength={MAX_REASON_CHARS}
        placeholder="e.g. the pricing numbers for the Q3 argument — I will want these again"
        bind:value={why}
        disabled={sending}
      ></textarea>
    </label>
    <p class="note">
      This is not a label. OpenViking reads it as the instruction for how to
      describe what you are adding, so it shapes the summary written about the
      file — and therefore what finds it later. It applies to every file in this
      batch.
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
    <button class="tbtn primary" disabled={pending === 0 || sending} onclick={send}>
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
  .field input,
  .field textarea {
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
  .field input:focus,
  .field textarea:focus {
    outline: none;
    border-color: var(--accent);
  }
  .field input::placeholder,
  .field textarea::placeholder {
    color: var(--ink-3);
  }

  .field.reason {
    margin-top: var(--s4);
  }
  .field.reason textarea {
    resize: vertical;
    line-height: 1.55;
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
