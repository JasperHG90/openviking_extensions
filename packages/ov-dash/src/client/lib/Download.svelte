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
  import { app, toast } from "./state.svelte";

  interface Props {
    uri: string;
    name: string;
    isDir: boolean;
    label?: boolean;
  }

  const { uri, name, isDir, label = false }: Props = $props();
  let ticked = $state(false);
  let timer: ReturnType<typeof setTimeout> | undefined;

  async function start(event: MouseEvent): Promise<void> {
    // Rows are links; a download must not also navigate.
    event.preventDefault();
    event.stopPropagation();

    /*
     * Every other call answers a dead session by putting the sign-in page back
     * up. This one cannot: the download streams through a hidden iframe, which
     * reports nothing — a 401 there is a "Downloading…" toast followed by
     * silence. So the session is checked first, which is cheap and, unlike the
     * iframe, has an answer to read.
     */
    const session = await api.session().catch(() => null);
    if (session && !session.signedIn) {
      app.session = session;
      toast("Your session ended — sign in again to download this.");
      return;
    }

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
