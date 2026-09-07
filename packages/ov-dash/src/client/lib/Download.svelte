<script lang="ts">
  /**
   * The download control that appears on hover on every file and folder row.
   *
   * A file streams straight through; a folder is zipped by the server. The
   * arrow turns into a tick for a moment so a click that starts a long zip
   * still feels like it did something.
   */
  import { api } from "./api";
  import Icon from "./Icon.svelte";
  import { toast } from "./state.svelte";

  interface Props {
    uri: string;
    name: string;
    isDir: boolean;
    label?: boolean;
  }

  const { uri, name, isDir, label = false }: Props = $props();
  let ticked = $state(false);
  let timer: ReturnType<typeof setTimeout> | undefined;

  function start(event: MouseEvent): void {
    // Rows are links; a download must not also navigate.
    event.preventDefault();
    event.stopPropagation();

    const frame = document.createElement("iframe");
    frame.hidden = true;
    frame.src = api.downloadUrl(uri);
    document.body.append(frame);
    setTimeout(() => frame.remove(), 60_000);

    ticked = true;
    clearTimeout(timer);
    timer = setTimeout(() => {
      ticked = false;
    }, 1600);
    toast(isDir ? `Zipping ${name}` : `Downloading ${name}`);
  }
</script>

{#if label}
  <button class="tbtn" onclick={start} title="Download {name}">
    <Icon name={ticked ? "check" : "download"} />
    {isDir ? "Download as zip" : "Download"}
  </button>
{:else}
  <button
    class="dl"
    onclick={start}
    aria-label={isDir ? `Download ${name} as a zip` : `Download ${name}`}
  >
    <Icon name={ticked ? "check" : "download"} />
  </button>
{/if}
