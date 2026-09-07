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

  $effect(() => {
    const target = uri;
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
  />
</div>

<style>
  /* The Reader carries its own header, so the page adds only the top margin a
     PageHead would have given it. */
  .filepage {
    padding-top: var(--s6);
  }
</style>
