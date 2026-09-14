<script lang="ts">
  /**
   * The reading pane — the one place this app spends its typography budget.
   *
   * Documents are the product here, so the measure, the leading and the
   * surface are set for reading rather than for filling the column. Internal
   * `viking://` links resolve inside the app instead of dying in the browser,
   * because a memory that references another memory should be one click away.
   */
  import { MAX_EDIT_CHARS, describeLimit } from "../../shared/limits";
  import { isInlineImage } from "../../shared/media";
  import { isMemoryUri } from "../../shared/names";
  import type { FileDetail, Saved, Tree } from "../../shared/schemas";
  import { ApiError, api } from "./api";
  import Download from "./Download.svelte";
  import { drafts } from "./drafts";
  import Icon from "./Icon.svelte";
  import { isMarkdown, renderMarkdown } from "./markdown";
  import { toast } from "./state.svelte";
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
    /**
     * Called once OpenViking has rewritten a description.
     *
     * The pane holds what it was given rather than fetching it, so somebody
     * else has to read it back — this is how they find out there is something
     * new to read.
     */
    onrefresh?: (uri: string) => void;
    /** Called after the open resource is deleted, so the tree drops the row. */
    ondeleted?: (uri: string) => void;
    /**
     * Called when a listing may no longer be true, without saying how.
     *
     * Its one caller is a delete that failed on the way back, where "gone" and
     * "still there" are both live and only a fresh listing can settle it. Kept
     * apart from `onrefresh` so a describe — which rewrites words and moves
     * nothing — does not pay for a whole tree read.
     */
    onlisting?: () => void;
  }

  const {
    detail = null,
    folder = null,
    folderName = "",
    loading = false,
    failure = "",
    onopen,
    onrefresh,
    ondeleted,
    onlisting,
  }: Props = $props();

  const body = $derived(
    detail && !detail.binary && isMarkdown(detail.node.kind)
      ? renderMarkdown(detail.content)
      : "",
  );

  /*
   * The summaries are markdown too, and were being printed as source.
   *
   * OpenViking writes them with a model: the per-file section it lifts out of
   * a folder's overview arrives full of `**bold**`, bullets and `[name](uri)`
   * links, and shown raw it reads as a wall of asterisks. Same renderer as the
   * document below, so the same sanitizing applies.
   */
  const fileSummary = $derived(detail?.abstract ? renderMarkdown(detail.abstract) : "");
  const aboutFolder = $derived(
    detail?.folderSummary ? renderMarkdown(detail.folderSummary) : "",
  );
  const folderSummary = $derived(folder?.summary ? renderMarkdown(folder.summary) : "");

  /*
   * The folder's overview, which is where OpenViking's real work on a folder is.
   *
   * The abstract is one sentence; this describes every entry inside, and its
   * Quick Navigation links are `viking://` uris the click handler already opens
   * in this pane. It matters most for an image: OpenViking stores a large one as
   * a preview, a grid and a set of tiles, and what it made of them is written
   * here and nowhere else — so a folder page showing only the abstract read as
   * one where nothing had been described.
   *
   * Shown instead of the abstract rather than beside it, because the overview
   * opens with that same sentence — the server takes the duplicated title off
   * the front, and printing both would say it twice.
   */
  const folderNotes = $derived(folder?.overview ? renderMarkdown(folder.overview) : "");

  /** What is open in the pane, whichever shape it arrived in. */
  const openUri = $derived(detail?.node.uri ?? folder?.root ?? "");
  const openName = $derived(detail?.node.name ?? folderName);
  const openIsDir = $derived(!detail && !!folder);

  /** How often to ask whether the describe job has finished, in milliseconds. */
  const POLL_EVERY = 2500;

  /**
   * How long to wait on one before giving up, in milliseconds.
   *
   * A model rewriting a long document takes minutes, so this is generous. It is
   * bounded all the same: a page left open on a job that never finishes would
   * otherwise poll for the life of the tab.
   */
  const PATIENCE = 4 * 60_000;

  /*
   * What is in flight, remembered by uri rather than as a flag.
   *
   * A describe runs for minutes and this one component shows every file in
   * turn, so a boolean would leave the next file you clicked with its buttons
   * greyed out and labelled "Describing…" about a job that is not its own.
   * Keyed by uri, the greying follows the file it belongs to.
   *
   * One slot, not a set: two describes at once is not a thing worth building
   * for, and the second would come back 409 anyway — OpenViking refuses a
   * second reindex of a uri while the first is running.
   */
  let describing = $state("");
  let deleting = $state("");
  /** Set by the first click on Delete; the second one does it. */
  let confirming = $state(false);

  /*
   * ── Editing ──────────────────────────────────────────────────
   *
   * The pane could read a memory and not change a word of it, so fixing a line
   * of `soul.md` or a preference meant opening a terminal. Offered for whatever
   * OpenViking handed over as text, which is the same table that decided to
   * inline it — so what can be edited is exactly what can be read.
   *
   * Tracked by uri, like the describe above and for the same reason: one
   * component shows every file in turn, and a boolean would leave the next file
   * you clicked sitting in an editor holding the previous one's words.
   */
  let editing = $state("");
  let draft = $state("");
  let saving = $state("");
  /** Set by the first click on Discard; the second one throws the typing away. */
  let discarding = $state(false);

  /**
   * The stored text the open draft started from.
   *
   * Held rather than read off `detail`, because `detail` can be replaced under an
   * open editor — a reread landing, or somebody else's write arriving — and then
   * "has this changed" would be measured against text the person never saw. With
   * `changed` reading `detail`, an untouched editor could light the Save button
   * and invite a lost update; the pane and the draft book also disagreed about
   * what "changed" meant, so the hint could claim unsaved work that leaving then
   * quietly declined to park.
   *
   * Declared above the deriveds that read it, which is not style: a `$derived`
   * referring to a `let` below it does not compile.
   */
  let loadedFrom = $state("");

  /**
   * What the file looked like when this editor opened.
   *
   * Sent back with the save so the server can refuse one that would replace
   * somebody else's write — an agent, a cron, another tab. OpenViking issues no
   * version tag of its own, so the modified time and the size are the nearest
   * thing a read carries.
   *
   * Not sent at all by "Overwrite theirs", which is how a reported clash is
   * deliberately overridden: an absent stamp means write it regardless. The value
   * here is left alone by that and replaced by whatever the write produced.
   */
  let seenWhenOpened = $state<Saved | null>(null);

  /** What the server said when it refused to let a save overwrite somebody. */
  let clash = $state("");

  /*
   * There is deliberately no "the stamp my last save produced, kept by uri" here.
   *
   * One was added, to cover a window where `detail` still describes the version
   * from before a save. That window does not exist: both parents null `detail`
   * synchronously when they re-read (`Files.svelte` openFile, `File.svelte` load)
   * and the pane renders `{#if loading}` first, so there is no Edit button to
   * press until the fresh read lands.
   *
   * What it did instead was outrank a *newer* `detail` forever, because nothing
   * cleared it: save a file, let an agent write it, re-read — the pane shows their
   * text, and editing that text still sent the stamp from this pane's own older
   * write. Every save 409s, including the one the "Open theirs" button leads you
   * to, so the clash could not be resolved except by overwriting. A stamp that
   * survives the read it describes is a stamp that lies.
   */

  /**
   * How many times the draft book has been written to.
   *
   * A plain class is invisible to the reactive graph, and the Edit button has to
   * re-read `has()` when a draft is put down or picked up.
   */
  let parked = $state(0);

  /**
   * Whether this file can be edited at all.
   *
   * Text, open, and inside the cap the server will accept. The length test is
   * not pedantry: `/api/file` inlines a text file of any size, so a 3 MB log
   * would otherwise get an Edit button, an editor, and a 413 on save with no way
   * back but copying out of the textarea by hand.
   */
  const tooLong = $derived((detail?.content.length ?? 0) > MAX_EDIT_CHARS);
  const editable = $derived(!!detail && !detail.binary && !tooLong);
  /** Whether a save here replaces a memory's body or a resource outright. */
  const isMemory = $derived(isMemoryUri(openUri));
  const onEdit = $derived(editing !== "" && editing === openUri);
  /** True once the draft says something different from the text it opened on. */
  const changed = $derived(onEdit && draft !== loadedFrom);
  /** Whether the file on screen has typing parked from an earlier visit. */
  const hasParked = $derived.by(() => {
    void parked;
    return !onEdit && drafts.has(openUri);
  });

  const busy = $derived(
    describing === openUri || deleting === openUri || saving === openUri,
  );

  /*
   * A different file is a different question, so a half-asked one is dropped,
   * and the editor closes — parking what was typed rather than losing it.
   *
   * One effect, not two. An earlier version split them on the theory that this
   * half ran only when the uri "really changed"; both are `$effect`s over the
   * same dependency and fire together, and the `editing !== openUri` guard was
   * doing all the work either way.
   */
  $effect(() => {
    void openUri;
    confirming = false;
    discarding = false;
    if (editing && editing !== openUri) {
      drafts.keep(editing, draft, loadedFrom);
      parked += 1;
      editing = "";
      draft = "";
    }
  });

  function startEditing(): void {
    if (!detail) return;
    editing = detail.node.uri;
    loadedFrom = detail.content;
    // What the file was when this editor opened, taken from the same `detail` the
    // text came from, so the pair and the words always describe one version.
    seenWhenOpened = { modTime: detail.node.modTime, size: detail.node.size };
    clash = "";
    draft = drafts.open(detail.node.uri, detail.content);
    // Picked up, so it is no longer parked; leaving again parks it afresh.
    drafts.drop(detail.node.uri);
    parked += 1;
  }

  /** Close the editor, keeping the typing for when this file is opened again. */
  function stopEditing(): void {
    if (editing) {
      drafts.keep(editing, draft, loadedFrom);
      parked += 1;
    }
    editing = "";
    draft = "";
    discarding = false;
    clash = "";
  }

  /** Throw the typing away for good. Only reached from the second click. */
  function discard(): void {
    if (editing) {
      drafts.drop(editing);
      parked += 1;
    }
    editing = "";
    draft = "";
    discarding = false;
    clash = "";
  }

  /*
   * Warn before the tab closes on unsaved typing.
   *
   * The parked drafts live for the tab, so this is the one exit that really does
   * lose them. Every route inside the app keeps them instead, and deleting or
   * forgetting a file drops its draft on purpose — `api.ts` does that, since the
   * file it was about no longer exists.
   */
  $effect(() => {
    void parked;
    void draft;
    const unsaved = drafts.pending > 0 || changed;
    if (!unsaved) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      // `preventDefault` alone is the current spec and enough in Chrome,
      // Firefox and Safari 17+. The assignment is what older Safari reads, and
      // it costs a line.
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  });

  /**
   * Save the draft, and ask the parent to read the file back.
   *
   * The re-read matters because what lands is not always what was sent: a memory
   * keeps its `MEMORY_FIELDS` trailer across a write and has its links re-rendered,
   * so the pane should show what is stored rather than what was typed. `onrefresh`
   * is the request — the parent owns the reading.
   *
   * @param force - Send no stamp, so the write happens whatever the file says now.
   *   Only "Overwrite theirs" passes this, after a clash has been reported and
   *   somebody has decided to replace the other version deliberately.
   */
  async function save(force = false): Promise<void> {
    const uri = openUri;
    const name = openName;
    if (!uri || saving) return;
    saving = uri;
    clash = "";
    // Captured, because the box stays typeable while the write is in flight and
    // this is what was actually stored.
    const sent = draft;
    try {
      const stored = await api.save(uri, sent, force ? undefined : (seenWhenOpened ?? undefined));
      /*
       * The file is now this, so later saves compare against it.
       *
       * Without this the second save from one editor is refused as a clash with
       * the first — the guard firing on the one person it exists to protect.
       */
      /*
       * The file is now this, so a later save from this same editor compares
       * against the write that just happened rather than the version it opened on
       * — otherwise the guard fires on the one person it exists to protect.
       *
       * An empty `modTime` means the server's post-write stat failed, so it does
       * not know what the file is now either. Cleared rather than left alone: the
       * old stamp is *knowably* wrong, since this write certainly changed the
       * file, and keeping it would refuse every later save from this editor until
       * somebody pressed Overwrite. No stamp costs one unguarded save; a wrong one
       * costs the editor.
       */
      seenWhenOpened = stored.modTime ? stored : null;
      /*
       * Forget the parked draft for this file, but only if it is what was sent.
       *
       * Two failures either side of this line, and both were real. Reading the
       * live `editing` instead of the captured uri left the *saved* text parked as
       * unsaved work forever — the Edit button read "Resume editing" on text
       * already on disk, and the tab warned on the way out about nothing. And
       * dropping unconditionally destroyed the opposite case: type, press Save,
       * type more, click the next row. Leaving parks the newer text, and the write
       * then resolved and deleted text that was never sent anywhere.
       *
       * So the rule is the same one the editor branch below uses: a save may throw
       * away the text it stored and nothing else.
       */
      drafts.dropSaved(uri, sent);
      parked += 1;
      /*
       * Close the editor only if nothing has been typed since the click.
       *
       * The write is not instant and the box stays typeable throughout, so typing
       * on after pressing Save is ordinary — and clearing `draft` on the way back
       * threw all of it away under a toast saying "Saved". Typing during a save
       * leaves the editor open and marked unsaved instead, which is what it is.
       *
       * The uri is the only other thing worth checking. A session counter is not
       * needed: `busy` covers `saving === openUri` and disables every control
       * including Edit, so this editor cannot be reopened while its own write is in
       * flight — `reader-editing.test.ts` pins that.
       */
      if (editing === uri && draft === sent) {
        editing = "";
        draft = "";
        discarding = false;
      } else if (editing === uri) {
        /*
         * Still open with newer typing in it, so move the baseline forward:
         * undoing back to the version just saved should read as no change.
         *
         * `sent` is the best available guess at what is stored, not a certainty.
         * A memory write re-renders its links and re-appends its metadata trailer,
         * so what landed can differ from what was sent — and only a fresh read
         * would settle it, which would overwrite the editor.
         */
        loadedFrom = sent;
      }
      onrefresh?.(uri);
      toast(`Saved ${name}`);
    } catch (error) {
      // The draft is deliberately kept: a refused save with the editor closed
      // and the text gone is the one failure nobody can recover from.
      const failure = error as ApiError;
      if (failure.code === "STALE_EDIT" && editing === uri) {
        /*
         * Shown in the editor rather than as a toast, because this one needs an
         * answer. A toast is gone in seconds and offers nothing to press; this is
         * the only refusal here where the person has to choose between their
         * version and somebody else's.
         */
        clash = failure.message;
      } else {
        toast(failure.message);
      }
    } finally {
      if (saving === uri) saving = "";
    }
  }

  /*
   * Whether this pane is still on screen.
   *
   * A describe polls for up to four minutes and nothing about leaving the page
   * stops it, so without this the job's toast would land over whatever the
   * person navigated to — a note about a file they are no longer looking at.
   */
  let onScreen = true;
  $effect(() => () => {
    onScreen = false;
    /*
     * Park an open editor's typing on the way out, too.
     *
     * The effect above parks when the *selection* moves within the page. This is
     * the other exit, and it is the more common one: clicking anything in the nav
     * unmounts the page — `App.svelte` swaps the whole view — and an editor open
     * with text in it was not in the book yet, so it died silently. Making the
     * book a module singleton only saved drafts that had already been parked; the
     * state somebody is actually in when they click away is "typed, not parked".
     */
    if (editing) drafts.keep(editing, draft, loadedFrom);
  });

  const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

  /**
   * Ask OpenViking to describe this again, and wait for it.
   *
   * The work happens out of band — see `describeAgain` on the server for why —
   * so there is nothing to await but the job record.
   */
  async function describe(): Promise<void> {
    const uri = openUri;
    const name = openName;
    if (!uri) return;
    describing = uri;
    try {
      const { taskId } = await api.describe(uri);
      if (taskId) await waitFor(taskId, name);
      if (!onScreen) return;
      api.refresh();
      onrefresh?.(uri);
      toast(`OpenViking has described ${name} again`);
    } catch (error) {
      if (onScreen) toast((error as Error).message);
    } finally {
      if (describing === uri) describing = "";
    }
  }

  /** Poll one job to its end, or give up out loud. */
  async function waitFor(taskId: string, name: string): Promise<void> {
    const deadline = Date.now() + PATIENCE;
    while (Date.now() < deadline) {
      await sleep(POLL_EVERY);
      // Leaving the page ends the waiting, not the job. OpenViking finishes
      // either way; there is just nobody here to be told about it, and ninety
      // more polls would be asking on behalf of a pane that is gone.
      if (!onScreen) return;
      const job = await api.job(taskId);
      // `gone` is a record OpenViking has stopped keeping, not a failure. There
      // is nothing left to wait for, so read the file back and let it speak.
      if (job.state === "done" || job.state === "gone") return;
      if (job.state === "failed") {
        throw new Error(job.error || "OpenViking could not describe that");
      }
    }
    throw new Error(`OpenViking is still describing ${name} — open it again in a minute`);
  }

  /** Delete what is open. Only reached from the second click. */
  async function remove(): Promise<void> {
    const uri = openUri;
    const name = openName;
    if (!uri) return;
    deleting = uri;
    try {
      await api.remove(uri);
      confirming = false;
      ondeleted?.(uri);
      toast(`Deleted ${name}`);
    } catch (error) {
      /*
       * A failed delete is not proof the file is still there.
       *
       * A recursive delete of a big folder can outrun the dashboard's own
       * timeout while OpenViking carries on and finishes it. Left alone, the
       * tree would keep drawing a row for something that is gone, and the next
       * click on it would 404. So the listing is read again either way, and it
       * is the listing that decides what is true.
       */
      api.refresh();
      onlisting?.();
      toast((error as Error).message);
    } finally {
      if (deleting === uri) deleting = "";
    }
  }

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

<!--
  The controls that act on whatever is open, drawn the same for a file and for
  a folder. Delete asks twice: the first click turns the button into the
  question, the second answers it. A browser `confirm()` would have done the
  same job and looked like it came from a different program.
-->
{#snippet actions()}
  <div class="racts">
    {#if editable}
      <button
        class="mini"
        class:waiting={hasParked}
        onclick={onEdit ? stopEditing : startEditing}
        disabled={busy}
        title={onEdit
          ? "Close the editor, keeping what you typed for later"
          : hasParked
            ? "Open the editor again on the text you had not saved"
            : "Edit this file's text"}
      >
        <Icon name={onEdit ? "close" : "pencil"} size={13} />
        {onEdit ? "Close editor" : hasParked ? "Resume editing" : "Edit"}
      </button>
    {:else if tooLong}
      <!--
        Said rather than left as a missing button. A file this long is rare
        enough that its absence reads as a bug, and the limit is the server's,
        so the reader is the only place that can explain it.
      -->
      <span class="ask">
        Too long to edit here — over {describeLimit(MAX_EDIT_CHARS)}
      </span>
    {/if}

    <!--
      Both of these replace what the pane is holding, so neither is offered
      while the editor is open: a describe finishing mid-edit used to swap
      `detail` under the textarea, leaving a draft of text nobody could see any
      more, and a delete would leave the editor on a file that is gone.
    -->
    <button
      class="mini"
      onclick={describe}
      disabled={busy || onEdit}
      title={onEdit
        ? "Close the editor first — describing replaces what is on screen"
        : "Have OpenViking read this again and rewrite what it says about it"}
    >
      <Icon name="refresh" size={13} />
      {describing === openUri ? "Describing…" : "Describe again"}
    </button>

    {#if confirming}
      <span class="ask">
        Delete {openName}{openIsDir ? " and everything in it" : ""}?
      </span>
      <button class="mini danger" onclick={remove} disabled={busy}>
        {deleting === openUri ? "Deleting…" : "Yes, delete"}
      </button>
      <button class="mini" onclick={() => (confirming = false)} disabled={busy}>
        Keep it
      </button>
    {:else}
      <button
        class="mini danger"
        onclick={() => (confirming = true)}
        disabled={busy || onEdit}
        title={onEdit ? "Close the editor first" : ""}
      >
        <Icon name="trash" size={13} />
        Delete
      </button>
    {/if}
  </div>
{/snippet}

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

    {@render actions()}

    <!--
      Sanitized in renderMarkdown before it reaches the DOM, and the click
      handler only delegates for the links inside — real anchors, which already
      answer to Enter — so this needs no key handler of its own.
    -->
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    {#if fileSummary}
      <aside class="summary">
        <div class="slabel2 mono">What OpenViking makes of this</div>
        <article class="md stext" onclick={intercept}>{@html fileSummary}</article>
      </aside>
    {:else if aboutFolder}
      <!--
        Labelled as the folder's, because that is what it is. OpenViking has
        no abstract per file and answers with the folder's, so showing it under
        "Summary" claimed a description of this file that nobody wrote.
      -->
      <aside class="summary quiet">
        <div class="slabel2 mono">About this folder</div>
        <article class="md stext" onclick={intercept}>{@html aboutFolder}</article>
      </aside>
    {:else}
      <aside class="summary quiet">
        <div class="slabel2 mono">What OpenViking makes of this</div>
        <p class="none">
          Nothing yet. “Describe again” hands it back to OpenViking to read.
        </p>
      </aside>
    {/if}

    {#if onEdit}
      <!--
        The editor: the file's own text, in the monospace face.

        Plain source rather than a rich editor on purpose. What is stored is
        markdown, and every other writer of these files — the agent, the CLI —
        writes markdown, so showing anything else would mean guessing how to turn
        a formatted document back into the text OpenViking holds.
      -->
      <div class="editor">
        <!--
          Somebody else wrote this file while it was open here.

          Shown above the text and not as a toast, because this is the one refusal
          in the pane that needs an answer rather than an acknowledgement: your
          version and theirs both exist and only one can win. Neither button is
          the safe default, so neither is styled as one — "Open theirs" keeps your
          typing parked, so the choice is reversible either way.
        -->
        {#if clash}
          <div class="callout c-sheet clash">
            <span class="ci"><Icon name="close" /></span>
            <div>
              <p>{clash}</p>
              <div class="cgoes">
                <button
                  class="mini danger"
                  onclick={() => save(true)}
                  disabled={busy || saving !== ""}
                >
                  Overwrite theirs
                </button>
                <button
                  class="mini"
                  onclick={() => {
                    // Their version into the pane, yours parked under this uri —
                    // the Edit button will say "Resume editing".
                    stopEditing();
                    onrefresh?.(openUri);
                  }}
                  disabled={busy}
                >
                  Open theirs, keep mine
                </button>
              </div>
            </div>
          </div>
        {/if}

        <!-- svelte-ignore a11y_autofocus -->
        <textarea
          bind:value={draft}
          spellcheck="false"
          autofocus
          aria-label={`Text of ${openName}`}
          onkeydown={(event) => {
            // Escape closes the editor and keeps the typing, rather than
            // throwing it away — one keystroke should not be able to lose work.
            if (event.key === "Escape") stopEditing();
            // The shortcut people already have in their fingers. Enter alone
            // stays a newline, which is what a document needs it to be.
            if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
              event.preventDefault();
              void save();
            }
          }}
        ></textarea>
        <div class="egoes">
          <!--
            Disabled while *any* save is in flight, not just this file's.

            `saving` is one slot, so `save()` returns early when it is occupied —
            and `busy` only covers `saving === openUri`, which left the button on a
            second file live and dead at once: it looked clickable, the click was
            swallowed, and nothing happened. The describe button gets away with one
            slot because a second describe would 409 upstream anyway; two files can
            be written concurrently without conflict, so that argument does not
            carry here. Saying "Saving…" while another file writes is the honest
            version of a state this pane really is in.
          -->
          <button
            class="tbtn primary"
            onclick={() => save()}
            disabled={busy || saving !== "" || !changed}
            title={saving !== "" && saving !== openUri
              ? `Waiting for ${saving.split("/").pop()} to finish saving`
              : ""}
          >
            {saving === openUri ? "Saving…" : "Save"}
          </button>
          <button class="mini" onclick={stopEditing} disabled={busy}>Close</button>

          <!--
            Throwing typing away asks twice, the same as Delete does. The first
            version let Escape and a stray click on the tree do it in one, with
            no question, while Delete two buttons along asked for confirmation.
          -->
          {#if changed}
            {#if discarding}
              <span class="ask">Throw away what you typed?</span>
              <button class="mini danger" onclick={discard} disabled={busy}>
                Yes, discard
              </button>
              <button class="mini" onclick={() => (discarding = false)} disabled={busy}>
                Keep it
              </button>
            {:else}
              <button
                class="mini danger"
                onclick={() => (discarding = true)}
                disabled={busy}
              >
                Discard
              </button>
            {/if}
          {/if}

          <span class="ehint mono">
            {#if changed}Unsaved changes{:else}No changes yet{/if}
          </span>
        </div>
        <p class="enote">
          {#if isMemory}
            <!--
              Said plainly because it differs from every other file, and the
              earlier wording got it wrong: OpenViking's memory write path opens
              with `del processing_mode` and reports the semantic step skipped,
              so no model runs. It re-embeds the file — the new words are
              searchable — and keeps the memory's own metadata trailer, which is
              why the editor holds less text than the file's size on disk.
            -->
            Saving replaces this memory's body and keeps its stored metadata.
            OpenViking re-indexes the new words, but does not rewrite what it
            says about the file — “Describe again” is what asks for that.
          {:else}
            Saving replaces the file, and OpenViking reads it again afterwards —
            so what it says about this file catches up a little later.
          {/if}
        </p>
      </div>
    {:else if showsImage}
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

    {@render actions()}

    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    {#if folderNotes}
      <!--
        The overview, which is the fullest thing OpenViking writes about a
        folder: a line on every entry inside, and links to each. Shown instead
        of the abstract, not beside it — OpenViking derives the abstract *from*
        this text (`_extract_abstract_from_overview` takes everything down to the
        first `##`), so printing both says the opening sentence twice.

        Labelled, like every other model-written block in this pane. Unlabelled
        it read as a folder's hand-written README, which is a claim about who
        wrote it.
      -->
      <section class="notes">
        <div class="slabel2 mono">What OpenViking makes of this folder</div>
        <article class="md" onclick={intercept}>{@html folderNotes}</article>
      </section>
    {:else if folderSummary}
      <aside class="summary">
        <div class="slabel2 mono">What OpenViking makes of this</div>
        <article class="md stext" onclick={intercept}>{@html folderSummary}</article>
      </aside>
    {:else}
      <aside class="summary quiet">
        <div class="slabel2 mono">Summary</div>
        <p class="none">
          OpenViking has not summarised this folder yet. “Describe again” asks
          it to.
        </p>
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
  .summary p.none {
    margin: 0;
    font-size: 15px;
    line-height: 1.62;
    color: var(--ink-3);
  }

  /* The controls that act on what is open, between the meta line and the prose. */
  .racts {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--s1);
    margin-top: var(--s3);
  }
  .racts .mini {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .racts .danger:hover:not(:disabled) {
    background: color-mix(in srgb, var(--rose) 16%, transparent);
    border-color: color-mix(in srgb, var(--rose) 40%, transparent);
    color: var(--rose);
  }
  .ask {
    font-size: 13px;
    color: var(--ink-2);
    padding-left: var(--s1);
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

  /*
   * The editor, on the same measure as the source view below it.
   *
   * Monospace and 84ch, matching `.raw`: this is the file's text, not a
   * rendering of it, and a serif proportional face over a 68ch measure makes
   * markdown syntax hard to line up.
   */
  .editor {
    margin-top: var(--s5);
    max-width: 84ch;
  }
  .editor textarea {
    display: block;
    width: 100%;
    /* Tall enough to hold a memory without scrolling, short enough that the
       buttons under it stay on screen. */
    min-height: 58vh;
    resize: vertical;
    background: var(--surface);
    border: 1px solid var(--rule);
    border-radius: var(--r2);
    padding: var(--s4);
    font-family: var(--fm);
    font-size: 12.5px;
    line-height: 1.7;
    color: var(--ink);
    tab-size: 2;
  }
  .editor textarea:focus {
    outline: none;
    border-color: var(--accent);
  }
  .egoes {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--s1);
    margin-top: var(--s3);
  }

  /* The clash sits above the text it is about, not below the buttons. */
  .clash {
    margin: 0 0 var(--s3);
    align-items: flex-start;
  }
  .clash p {
    margin: 0;
  }
  .cgoes {
    display: flex;
    flex-wrap: wrap;
    gap: var(--s1);
    margin-top: var(--s2);
  }
  .cgoes .danger:hover:not(:disabled) {
    background: color-mix(in srgb, var(--rose) 16%, transparent);
    border-color: color-mix(in srgb, var(--rose) 40%, transparent);
    color: var(--rose);
  }
  .egoes .danger:hover:not(:disabled) {
    background: color-mix(in srgb, var(--rose) 16%, transparent);
    border-color: color-mix(in srgb, var(--rose) 40%, transparent);
    color: var(--rose);
  }
  /* The Edit button, when the file has typing waiting in it. */
  .racts .mini.waiting {
    border-color: var(--accent-line);
    color: var(--accent-ink);
  }

  /*
   * The folder's overview: its own block, under its own label.
   *
   * The label carries the top margin a `.md` would have taken, so the two do
   * not stack it twice.
   */
  .notes {
    margin-top: var(--s5);
  }
  .notes .md {
    margin-top: var(--s2);
  }
  .ehint {
    font-size: 11px;
    color: var(--ink-3);
    padding-left: var(--s1);
  }
  .enote {
    margin: var(--s2) 0 0;
    font-size: 12.5px;
    color: var(--ink-3);
    line-height: 1.55;
    max-width: 68ch;
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

  /*
   * The summary: the same prose rules as the document, one notch quieter.
   *
   * It carries `.md` for the element styles above — links, lists, code, tables
   * — and overrides the size here, so a heading in a summary is a summary
   * heading rather than a second document title. These rules come last on
   * purpose: they tie with the `.md` ones on specificity, and order is what
   * decides.
   */
  .stext {
    margin-top: 0;
    font-size: 15px;
    line-height: 1.62;
    color: var(--ink-2);
    font-variation-settings: "opsz" 14;
  }
  .summary.quiet .stext {
    color: var(--ink-3);
  }
  .stext :global(h1),
  .stext :global(h2),
  .stext :global(h3),
  .stext :global(h4) {
    font-size: 15px;
    margin: 1em 0 0.35em;
  }
  .stext :global(> :last-child) {
    margin-bottom: 0;
  }
</style>
