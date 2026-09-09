<script lang="ts">
  /**
   * The reading pane — the one place this app spends its typography budget.
   *
   * Documents are the product here, so the measure, the leading and the
   * surface are set for reading rather than for filling the column. Internal
   * `viking://` links resolve inside the app instead of dying in the browser,
   * because a memory that references another memory should be one click away.
   */
  import { isInlineImage } from "../../shared/media";
  import type { FileDetail, Tree } from "../../shared/schemas";
  import { api } from "./api";
  import Download from "./Download.svelte";
  import Icon from "./Icon.svelte";
  import { isMarkdown, renderMarkdown } from "./markdown";
  import { formatSize, formatWhen } from "./tree";

  interface Props {
    /** The file being read, when one is selected. */
    detail?: FileDetail | null;
    /** The folder being previewed, when one is selected instead. */
    folder?: Tree | null;
    /** Name to show for the folder, since a listing carries no title. */
    folderName?: string;
    loading?: boolean;
    failure?: string;
    /** Called when a link or row inside the pane points at another resource. */
    onopen?: (uri: string) => void;
  }

  const {
    detail = null,
    folder = null,
    folderName = "",
    loading = false,
    failure = "",
    onopen,
  }: Props = $props();

  const body = $derived(
    detail && !detail.binary && isMarkdown(detail.node.kind)
      ? renderMarkdown(detail.content)
      : "",
  );

  /*
   * The picture that would not load, remembered by uri rather than as a flag.
   *
   * A flag would have to be cleared when the selection changes, and a missed
   * reset means the next image never gets its chance. Keyed by uri, opening
   * another file simply stops matching.
   */
  let brokenImage = $state("");
  const showsImage = $derived(
    !!detail &&
      detail.binary &&
      isInlineImage(detail.node.kind) &&
      brokenImage !== detail.node.uri,
  );

  /**
   * Send an internal link back to the app rather than to the browser.
   *
   * Every link OpenViking writes between memories is a `viking://` uri, which
   * no browser can follow. Caught here, they open in this same pane. Anything
   * external is left alone and opens in a new tab.
   */
  function intercept(event: MouseEvent): void {
    const link = (event.target as HTMLElement).closest("a");
    const href = link?.getAttribute("href");
    if (!href) return;

    if (href.startsWith("viking://")) {
      event.preventDefault();
      onopen?.(href);
      return;
    }
    if (/^https?:/i.test(href)) {
      link?.setAttribute("target", "_blank");
      link?.setAttribute("rel", "noopener noreferrer");
    }
  }
</script>

<div class="reader">
  {#if loading}
    <p class="pdesc">Loading…</p>
  {:else if failure}
    <div class="callout c-sheet">
      <span class="ci"><Icon name="close" /></span>
      <p>{failure}</p>
    </div>
  {:else if detail}
    <header class="rhead">
      <div class="rtitlewrap">
        <div class="reyebrow mono">{detail.node.kind}</div>
        <h2 class="rtitle">{detail.node.name}</h2>
        <div class="rpath mono">{detail.node.relPath}</div>
      </div>
      <Download uri={detail.node.uri} name={detail.node.name} isDir={false} />
    </header>

    <div class="rmeta mono">
      {formatSize(detail.node.size, false)}
      {#if detail.node.modTime}<span class="sep">·</span>{formatWhen(detail.node.modTime)}{/if}
    </div>

    {#if detail.abstract}
      <aside class="summary">
        <div class="slabel2 mono">What OpenViking makes of this</div>
        <p>{detail.abstract}</p>
      </aside>
    {:else if detail.folderSummary}
      <!--
        Labelled as the folder's, because that is what it is. OpenViking has
        no abstract per file and answers with the folder's, so showing it under
        "Summary" claimed a description of this file that nobody wrote.
      -->
      <aside class="summary quiet">
        <div class="slabel2 mono">About this folder</div>
        <p>{detail.folderSummary}</p>
      </aside>
    {/if}

    {#if showsImage}
      <figure class="shot">
        <img
          src={api.imageUrl(detail.node.uri)}
          alt={detail.node.name}
          onerror={() => {
            brokenImage = detail.node.uri;
          }}
        />
      </figure>
    {:else if detail.binary}
      <div class="callout c-sheet">
        <span class="ci"><Icon name="doc" /></span>
        {#if brokenImage === detail.node.uri}
          <p>
            This {detail.node.kind} would not open — the stored file may be damaged.
            Download it to check.
          </p>
        {:else}
          <p>This is a {detail.node.kind} file. Download it to open it.</p>
        {/if}
      </div>
    {:else if body}
      <!--
        Sanitized in renderMarkdown before it reaches the DOM. The click
        handler only delegates for the links inside, which are real anchors and
        already answer to Enter, so this needs no key handler of its own.
      -->
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <article class="md" onclick={intercept}>{@html body}</article>
    {:else if detail.content}
      <pre class="raw">{detail.content}</pre>
    {:else}
      <p class="pdesc">This file is empty.</p>
    {/if}
  {:else if folder}
    <header class="rhead">
      <div class="rtitlewrap">
        <div class="reyebrow mono">Folder</div>
        <h2 class="rtitle">{folderName}</h2>
        <div class="rpath mono">{folder.root}</div>
      </div>
      <Download uri={folder.root} name={folderName} isDir={true} />
    </header>

    <div class="rmeta mono">
      {folder.nodes.length}
      {folder.nodes.length === 1 ? "item" : "items"}
    </div>

    {#if folder.summary}
      <aside class="summary">
        <div class="slabel2 mono">What OpenViking makes of this</div>
        <p>{folder.summary}</p>
      </aside>
    {:else}
      <aside class="summary quiet">
        <div class="slabel2 mono">Summary</div>
        <p>OpenViking has not summarised this folder yet.</p>
      </aside>
    {/if}

    <h3 class="contents">Contents</h3>
    <ul class="flist">
      {#each folder.nodes as item (item.uri)}
        <li>
          <button onclick={() => onopen?.(item.uri)}>
            <span class="fi"><Icon name={item.isDir ? "folder" : "doc"} /></span>
            <span class="fn">{item.name}</span>
            <span class="fs2 mono">{formatSize(item.size, item.isDir)}</span>
          </button>
        </li>
      {:else}
        <li class="pdesc">This folder is empty.</li>
      {/each}
    </ul>
  {:else}
    <div class="empty">
      <Icon name="doc" size={24} />
      <p class="pdesc">Pick a file to read it here.</p>
    </div>
  {/if}
</div>

<style>
  .reader {
    min-width: 0;
    padding-bottom: 80px;
  }

  .rhead {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--s4);
  }
  .rtitlewrap {
    min-width: 0;
  }
  .reyebrow {
    font-size: 10px;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--accent);
    margin-bottom: 5px;
  }
  .rtitle {
    font-family: var(--fd);
    font-size: 29px;
    font-weight: 600;
    margin: 0;
    letter-spacing: -0.016em;
    line-height: 1.15;
    hyphens: none;
    word-break: break-word;
    font-variation-settings: "opsz" 32;
  }
  .rpath {
    font-size: 11px;
    color: var(--ink-3);
    margin-top: 5px;
    word-break: break-all;
  }
  .rmeta {
    font-size: 11px;
    color: var(--ink-3);
    margin: var(--s3) 0 0;
  }
  .sep {
    margin: 0 6px;
    opacity: 0.5;
  }

  /* The summary sits apart from the document, marked by a warm rule. */
  .summary {
    margin: var(--s5) 0 0;
    padding-left: var(--s4);
    border-left: 2px solid var(--accent-line);
    max-width: 68ch;
  }
  .summary.quiet {
    border-left-color: var(--rule);
  }
  .slabel2 {
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin-bottom: 5px;
  }
  .summary p {
    margin: 0;
    font-size: 15px;
    line-height: 1.62;
    color: var(--ink-2);
  }
  .summary.quiet p {
    color: var(--ink-3);
  }

  .empty {
    display: grid;
    place-items: center;
    gap: var(--s3);
    padding: 110px 0;
    color: var(--ink-3);
  }

  .contents {
    margin-top: var(--s6);
  }
  .flist {
    list-style: none;
    margin: 0;
    padding: 0;
    max-width: 68ch;
  }
  .flist li button {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    text-align: left;
    padding: 8px 10px;
    border-radius: var(--r1);
    border-bottom: 1px solid var(--rule-2);
    font-size: 14px;
    color: var(--ink-2);
  }
  .flist li button:hover {
    background: var(--hover);
    color: var(--ink);
  }
  .fi {
    color: var(--ink-3);
    display: grid;
    place-items: center;
  }
  .fn {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .fs2 {
    font-size: 11px;
    color: var(--ink-3);
  }

  /*
   * The picture, on the same measure as the prose.
   *
   * Sized to its own pixels up to that width rather than stretched to fill it:
   * a 200px icon blown up to 68ch is worse than a small icon.
   */
  .shot {
    margin: var(--s5) 0 0;
    max-width: 68ch;
  }
  .shot img {
    display: block;
    max-width: 100%;
    height: auto;
    border: 1px solid var(--rule);
    border-radius: var(--r2);
    /* Light artwork on a dark page needs something to sit on. */
    background: var(--surface);
  }

  .raw {
    background: var(--surface);
    border: 1px solid var(--rule);
    border-radius: var(--r2);
    padding: var(--s4);
    font-family: var(--fm);
    font-size: 12.5px;
    line-height: 1.7;
    white-space: pre-wrap;
    word-break: break-word;
    overflow-x: auto;
    margin-top: var(--s5);
    max-width: 84ch;
  }

  /*
   * The document itself.
   *
   * Set in the serif at 17px with 1.72 leading over a 68ch measure — inside
   * the 45–90 character band, and wide enough that a paragraph is not a
   * ladder. Global, because this markup arrives through {@html}.
   */
  .md {
    margin-top: var(--s5);
    max-width: 68ch;
    font-family: var(--fd);
    font-size: 17px;
    line-height: 1.72;
    color: var(--ink);
    font-variation-settings: "opsz" 16;
  }
  .md :global(h1),
  .md :global(h2),
  .md :global(h3),
  .md :global(h4) {
    font-family: var(--fd);
    font-weight: 600;
    letter-spacing: -0.012em;
    line-height: 1.25;
    hyphens: none;
    /* Space above beats space below: a heading belongs to what follows. */
    margin: 1.9em 0 0.5em;
  }
  .md :global(h1) {
    font-size: 25px;
  }
  .md :global(h2) {
    font-size: 21px;
  }
  .md :global(h3) {
    font-size: 18px;
  }
  .md :global(h4) {
    font-size: 16px;
  }
  .md :global(h1:first-child),
  .md :global(h2:first-child),
  .md :global(h3:first-child) {
    margin-top: 0;
  }
  .md :global(p) {
    margin: 0 0 0.85em;
  }
  .md :global(ul),
  .md :global(ol) {
    margin: 0 0 0.9em;
    padding-left: 1.35em;
  }
  .md :global(li) {
    margin: 0.3em 0;
  }
  .md :global(li::marker) {
    color: var(--ink-3);
  }
  .md :global(a) {
    color: var(--accent-ink);
    text-decoration: underline;
    text-decoration-thickness: 1px;
    text-underline-offset: 2px;
    text-decoration-color: var(--accent-line);
  }
  .md :global(a:hover) {
    text-decoration-color: var(--accent-ink);
  }
  .md :global(strong) {
    font-weight: 700;
  }
  .md :global(em) {
    font-style: italic;
  }
  .md :global(code) {
    font-family: var(--fm);
    font-size: 0.82em;
    background: var(--surface-2);
    padding: 1px 5px;
    border-radius: 4px;
    color: var(--ink);
  }
  .md :global(pre) {
    background: var(--surface);
    border: 1px solid var(--rule);
    border-radius: var(--r2);
    padding: var(--s3) var(--s4);
    overflow-x: auto;
    margin: 0 0 1em;
  }
  .md :global(pre code) {
    background: none;
    padding: 0;
    font-size: 12.5px;
    line-height: 1.7;
  }
  .md :global(blockquote) {
    margin: 0 0 1em;
    padding-left: var(--s4);
    border-left: 2px solid var(--rule);
    color: var(--ink-2);
  }
  /* No borders on data: the grid is implied. Keep one rule under the header. */
  .md :global(table) {
    border-collapse: collapse;
    margin: 0 0 1em;
    display: block;
    overflow-x: auto;
    font-family: var(--f);
    font-size: 14px;
  }
  .md :global(th) {
    font-family: var(--fm);
    font-size: 10px;
    letter-spacing: 0.11em;
    text-transform: uppercase;
    color: var(--ink-3);
    font-weight: 500;
    text-align: left;
    padding: 0.5em 1.2em 0.5em 0;
    border-bottom: 1px solid var(--rule);
  }
  .md :global(td) {
    padding: 0.5em 1.2em 0.5em 0;
    border-bottom: 1px solid var(--rule-2);
  }
  .md :global(hr) {
    border: 0;
    border-top: 1px solid var(--rule);
    margin: 2em 0;
  }
  .md :global(img) {
    max-width: 100%;
    border-radius: var(--r2);
  }
</style>
