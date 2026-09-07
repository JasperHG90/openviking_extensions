<script lang="ts">
  /**
   * Files: a tree on the left, the file you picked on the right.
   *
   * One recursive listing builds the whole hierarchy, so a folder opens in
   * place at any depth and never navigates. Picking a file loads it into the
   * pane beside the tree, so the tree keeps its shape and its scroll position
   * and there is nothing to go "back" from.
   */
  import { api, type FileDetail, type Tree } from "../lib/api";
  import Download from "../lib/Download.svelte";
  import Icon from "../lib/Icon.svelte";
  import PageHead from "../lib/PageHead.svelte";
  import Reader from "../lib/Reader.svelte";
  import { formatSize } from "../lib/tree";
  import { allFolderUris, buildTree, visibleRows, type TreeNode } from "../lib/tree";

  let tree = $state<Tree | null>(null);
  let failure = $state("");
  // Replaced rather than mutated on every change, which is what makes the rows
  // re-render; a Set mutated in place would not.
  let open = $state<Set<string>>(new Set());
  let filter = $state("");

  /**
   * Children fetched on demand, keyed by folder uri.
   *
   * OpenViking's recursive listing stops at depth 2 even when deeper content
   * exists — measured against the cluster — so one call cannot fill the tree.
   * Expanding a folder we have no children for fetches them and merges them in.
   */
  let extra = $state<Map<string, import("../../shared/schemas").Node[]>>(new Map());
  let loadingDirs = $state<Set<string>>(new Set());

  let selected = $state<string>("");
  let detail = $state<FileDetail | null>(null);
  let folderView = $state<Tree | null>(null);
  let folderName = $state("");
  let detailFailure = $state("");
  let loadingFile = $state(false);

  const allNodes = $derived.by(() => {
    if (!tree) return [];
    const seen = new Set(tree.nodes.map((node) => node.uri));
    const merged = [...tree.nodes];
    for (const children of extra.values()) {
      for (const child of children) {
        if (!seen.has(child.uri)) {
          seen.add(child.uri);
          merged.push(child);
        }
      }
    }
    return merged;
  });

  const roots = $derived<TreeNode[]>(buildTree(allNodes));

  /**
   * Rows to draw.
   *
   * With a filter, matching files and every ancestor holding them, all forced
   * open — a filter that only dimmed rows would leave you opening folders to
   * find out whether anything matched.
   */
  const rows = $derived.by(() => {
    const query = filter.trim().toLowerCase();
    if (!query) return visibleRows(roots, open);

    const keep = new Set<string>();
    const walk = (node: TreeNode): boolean => {
      const hit = node.name.toLowerCase().includes(query);
      let any = false;
      for (const child of node.children) any = walk(child) || any;
      if (hit || any) keep.add(node.uri);
      return hit || any;
    };
    for (const root of roots) walk(root);

    const out: TreeNode[] = [];
    const collect = (list: TreeNode[]): void => {
      for (const node of list) {
        if (!keep.has(node.uri)) continue;
        out.push(node);
        collect(node.children);
      }
    };
    collect(roots);
    return out;
  });

  const COLUMNS = "minmax(0,1fr) 78px 40px";

  $effect(() => {
    api
      .tree()
      .then((result) => {
        tree = result;
        // Open the first two levels: enough to see the shape of the workspace
        // without burying you in every leaf at once.
        const next = new Set<string>();
        const seed = (list: TreeNode[], depth: number): void => {
          for (const node of list) {
            if (!node.isDir) continue;
            if (depth < 2) next.add(node.uri);
            seed(node.children, depth + 1);
          }
        };
        seed(buildTree(result.nodes), 0);
        open = next;
      })
      .catch((error: Error) => {
        failure = error.message;
      });
  });

  function toggle(node: TreeNode): void {
    const next = new Set(open);
    if (next.has(node.uri)) next.delete(node.uri);
    else {
      next.add(node.uri);
      void fill(node);
    }
    open = next;
  }

  /**
   * Fetch a folder's children the first time it is opened.
   *
   * Skipped when the initial listing already brought them, so the common case
   * costs nothing. An empty folder is remembered as empty so it is not
   * re-fetched on every open.
   */
  async function fill(node: TreeNode): Promise<void> {
    if (!node.isDir || extra.has(node.uri) || node.children.length > 0) return;
    if (loadingDirs.has(node.uri)) return;

    loadingDirs = new Set(loadingDirs).add(node.uri);
    try {
      const listing = await api.folder(node.uri);
      extra = new Map(extra).set(node.uri, listing.nodes);
    } catch {
      // Remember the attempt, so a folder that cannot be read does not retry
      // on every click.
      extra = new Map(extra).set(node.uri, []);
    } finally {
      const next = new Set(loadingDirs);
      next.delete(node.uri);
      loadingDirs = next;
    }
  }

  function pick(node: TreeNode): void {
    if (node.isDir) {
      // A folder does both: unfolds where it sits, and shows what OpenViking
      // makes of it in the pane. Neither replaces the other.
      toggle(node);
      openFolder(node.uri, node.name);
      return;
    }
    openFile(node.uri);
  }

  /** Load a file into the reading pane. */
  function openFile(uri: string): void {
    selected = uri;
    detail = null;
    folderView = null;
    detailFailure = "";
    loadingFile = true;
    api
      .file(uri)
      .then((result) => {
        if (selected === uri) detail = result;
      })
      .catch((error: Error) => {
        if (selected === uri) detailFailure = error.message;
      })
      .finally(() => {
        if (selected === uri) loadingFile = false;
      });
  }

  /** Load a folder's summary and contents into the pane. */
  function openFolder(uri: string, name: string): void {
    selected = uri;
    detail = null;
    folderView = null;
    folderName = name;
    detailFailure = "";
    loadingFile = true;
    api
      .folder(uri)
      .then((result) => {
        if (selected === uri) folderView = result;
      })
      .catch((error: Error) => {
        if (selected === uri) detailFailure = error.message;
      })
      .finally(() => {
        if (selected === uri) loadingFile = false;
      });
  }

  /**
   * Follow a link out of the reader, or a row inside a folder listing.
   *
   * The server decides whether the target is a file or a folder: an internal
   * link looks the same either way, and the tree may not have loaded the
   * target at all.
   */
  function openUri(uri: string): void {
    selected = uri;
    detail = null;
    folderView = null;
    detailFailure = "";
    loadingFile = true;
    api
      .open(uri)
      .then((result) => {
        if (selected !== uri) return;
        if (result.kind === "folder") {
          folderView = result.folder;
          folderName = result.name;
        } else {
          detail = result.file;
        }
      })
      .catch((error: Error) => {
        if (selected === uri) detailFailure = error.message;
      })
      .finally(() => {
        if (selected === uri) loadingFile = false;
      });
  }

  let expanding = $state(false);

  /**
   * Open everything, fetching levels the recursive listing did not reach.
   *
   * Loops because each round can reveal folders that were not known before.
   * Bounded so a deep or cyclic tree cannot spin here forever.
   */
  async function expandAll(): Promise<void> {
    expanding = true;
    try {
      for (let round = 0; round < 6; round++) {
        const folders = allFolderUris(roots);
        open = new Set(folders);
        const missing = roots.length ? findUnfetched(roots) : [];
        if (missing.length === 0) break;
        await Promise.all(missing.slice(0, 40).map((node) => fill(node)));
      }
    } finally {
      expanding = false;
    }
  }

  /** Folders whose children have never been fetched. */
  function findUnfetched(list: TreeNode[]): TreeNode[] {
    const out: TreeNode[] = [];
    const walk = (nodes: TreeNode[]): void => {
      for (const node of nodes) {
        if (node.isDir && node.children.length === 0 && !extra.has(node.uri)) {
          out.push(node);
        }
        walk(node.children);
      }
    };
    walk(list);
    return out;
  }

</script>

<PageHead eyebrow="Files" title="Files" desc="Everything OpenViking holds for you." />

{#if failure}
  <div class="callout c-sheet"><span class="ci"><Icon name="close" /></span><p>{failure}</p></div>
{:else if !tree}
  <p class="pdesc">Loading…</p>
{:else}
  <div class="split">
    <div class="pane">
      <div class="toolbar">
        <div class="sbox">
          <Icon name="search" size={13} />
          <input placeholder="Filter by name" bind:value={filter} />
          {#if filter}
            <button class="clr" onclick={() => (filter = "")} aria-label="Clear filter">
              <Icon name="close" size={12} />
            </button>
          {/if}
        </div>
        <button class="mini" onclick={expandAll} disabled={expanding}>
          {expanding ? "Expanding…" : "Expand all"}
        </button>
        <button class="mini" onclick={() => (open = new Set())}>Collapse</button>
      </div>

      {#if tree.truncated}
        <div class="callout c-sheet" style="margin:10px 0">
          <span class="ci"><Icon name="files" /></span>
          <p>This listing hit OpenViking's limit, so it may be incomplete.</p>
        </div>
      {/if}

      <div class="fs" role="tree" aria-label="Files">
        {#each rows as node (node.uri)}
          <div
            class="tnode {node.isDir && open.has(node.uri) ? 'open' : ''}"
            class:sel={node.uri === selected}
            role="treeitem"
            aria-expanded={node.isDir ? open.has(node.uri) : undefined}
            aria-selected={node.uri === selected}
            tabindex="-1"
            style={`grid-template-columns:${COLUMNS}`}
          >
            <span class="cell nm">
              {#each { length: node.depth } as _, level (level)}
                <span class="ind" aria-hidden="true"></span>
              {/each}

              {#if node.isDir}
                <button
                  class="tw"
                  onclick={(event) => {
                    event.stopPropagation();
                    toggle(node);
                  }}
                  aria-label={open.has(node.uri) ? `Collapse ${node.name}` : `Expand ${node.name}`}
                >
                  <Icon name="chevron" size={9} />
                </button>
              {:else}
                <span class="tw leaf"></span>
              {/if}

              <span class="tg">
                {#if loadingDirs.has(node.uri)}
                  <span class="dspin" aria-label="Loading"></span>
                {:else}
                  <Icon name={node.isDir ? "folder" : "doc"} />
                {/if}
              </span>
              <button class="tlb" onclick={() => pick(node)} title={node.name}>
                {node.name}
              </button>
            </span>

            <span class="cell r">{formatSize(node.size, node.isDir)}</span>
            <span class="cell r">
              <Download uri={node.uri} name={node.name} isDir={node.isDir} />
            </span>
          </div>
        {:else}
          <p class="pdesc">{filter ? `Nothing matches “${filter}”.` : "No files yet."}</p>
        {/each}
      </div>
    </div>

    <Reader
      {detail}
      folder={folderView}
      {folderName}
      loading={loadingFile}
      failure={detailFailure}
      onopen={openUri}
    />
  </div>
{/if}

<style>
  .split {
    display: grid;
    grid-template-columns: minmax(280px, 360px) minmax(0, 1fr);
    gap: var(--s6);
    align-items: start;
    margin-top: var(--s4);
  }

  /* The tree scrolls under its own steam so the document keeps its place. */
  .pane {
    position: sticky;
    top: 0;
    max-height: calc(100vh - 140px);
    overflow: auto;
    padding-right: var(--s1);
  }

  .toolbar {
    display: flex;
    align-items: center;
    gap: var(--s1);
    margin-bottom: var(--s2);
  }
  .sbox {
    flex: 1;
    display: flex;
    align-items: center;
    gap: 7px;
    background: var(--surface);
    border-radius: var(--r2);
    padding: 6px 10px;
    border: 1px solid var(--rule);
    color: var(--ink-3);
    min-width: 0;
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
    font-size: 13px;
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

  .tnode.sel {
    background: var(--accent-quiet);
  }
  .tnode.sel .tlb {
    color: var(--accent-ink);
    font-weight: 600;
  }
  .tlb {
    background: none;
    border: none;
    padding: 0 0 0 5px;
    font: inherit;
    color: inherit;
    text-align: left;
    cursor: pointer;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
    min-width: 0;
  }
  .tlb:hover {
    color: var(--accent);
  }

  .dspin {
    width: 11px;
    height: 11px;
    border-radius: 50%;
    border: 1.5px solid var(--rule);
    border-top-color: var(--accent);
    animation: dspin 0.7s linear infinite;
  }
  @keyframes dspin {
    to {
      transform: rotate(360deg);
    }
  }

  @media (max-width: 980px) {
    .split {
      grid-template-columns: 1fr;
    }
    .pane {
      position: static;
      max-height: 380px;
    }
  }
</style>
