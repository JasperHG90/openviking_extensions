<script lang="ts">
  /**
   * Whatever a link points at, on its own page.
   *
   * Reached from a search hit, a memory, or a link inside another document,
   * where there is no tree to sit beside. The server resolves whether the uri
   * is a file or a folder, so following a link never shows an error it then
   * has to take back.
   */
  import { api, type Opened } from "../lib/api";
  import Reader from "../lib/Reader.svelte";
  import { go } from "../lib/state.svelte";

  interface Props {
    uri: string;
  }
  const { uri }: Props = $props();

  let opened = $state<Opened | null>(null);
  let failure = $state("");
  let loading = $state(true);

  /**
   * Load whatever the uri points at. Shared with the reload after a rewrite.
   *
   * The reload arrives minutes late, carrying the uri the describe started on,
   * and by then the page may be showing something else. Without the first
   * line it would blank that, set `loading`, and then never clear it — every
   * later guard compares against the current uri and fails — leaving the pane
   * saying "Loading…" until the next navigation.
   */
  function load(target: string): void {
    if (target !== uri) return;
    opened = null;
    failure = "";
    loading = true;
    api
      .open(target)
      .then((result) => {
        if (target === uri) opened = result;
      })
      .catch((error: Error) => {
        if (target === uri) failure = error.message;
      })
      .finally(() => {
        if (target === uri) loading = false;
      });
  }

  $effect(() => {
    load(uri);
  });
</script>

<div class="filepage">
  <Reader
    detail={opened?.kind === "file" ? opened.file : null}
    folder={opened?.kind === "folder" ? opened.folder : null}
    folderName={opened?.kind === "folder" ? opened.name : ""}
    {loading}
    {failure}
    onopen={(next) => go({ page: "file", uri: next })}
    onrefresh={load}
    ondeleted={() => go({ page: "files" })}
  />
</div>

<style>
  /* The Reader carries its own header, so the page adds only the top margin a
     PageHead would have given it. */
  .filepage {
    padding-top: var(--s6);
  }
</style>
