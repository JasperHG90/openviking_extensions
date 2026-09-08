<script lang="ts">
  /**
   * The shell: the dark rail, the top bar, and whichever view the route names.
   *
   * Nothing renders until the session is known, because every view needs to
   * know whether there is anybody to render it for.
   */
  import { api, whenSessionEnds } from "./lib/api";
  import Icon from "./lib/Icon.svelte";
  import { app, go, hashFor, toast, toggleTheme, type Route } from "./lib/state.svelte";
  import AccountView from "./views/Account.svelte";
  import AddFile from "./views/AddFile.svelte";
  import FileView from "./views/File.svelte";
  import Files from "./views/Files.svelte";
  import Home from "./views/Home.svelte";
  import Memories from "./views/Memories.svelte";
  import Search from "./views/Search.svelte";
  import Sessions from "./views/Sessions.svelte";
  import SignIn from "./views/SignIn.svelte";

  let loading = $state(true);
  let failure = $state("");

  /**
   * Drop every cached answer and redraw.
   *
   * Reads are cached for a minute, so there has to be a way to say "no, now".
   * Re-navigating to the current route is what makes the views refetch.
   */
  function refresh(): void {
    api.refresh();
    const here = window.location.hash || "#/home";
    window.location.hash = "#/reloading";
    setTimeout(() => {
      window.location.hash = here;
    }, 0);
  }

  $effect(() => {
    api
      .session()
      .then((state) => {
        app.session = state;
      })
      .catch((error: Error) => {
        failure = error.message;
      })
      .finally(() => {
        loading = false;
      });
  });

  /*
   * A session can end while the page is still open — a Vault-minted token
   * expires on its own and nothing renews it. Re-reading the session is what
   * puts the sign-in page back up, instead of a shell that looks signed in and
   * answers every click with a toast.
   *
   * Re-read rather than blanked, because the sign-in page has two shapes and
   * only the server knows which one this deployment wants. Blanking first would
   * flash the wrong one.
   */
  /**
   * One reload per tab, the same guard `reloadIfStale` uses.
   *
   * Without it a flapping server loops: boot succeeds, a view 401s, the
   * re-read fails on the network, reload, boot succeeds, round again — a page
   * reloading too fast to read the error on.
   */
  const RELOAD_GUARD = "ovdash-session-reloaded";

  let rechecking = false;
  whenSessionEnds(() => {
    // A view in flight fires several calls at once, and they all 401 together.
    if (rechecking) return;
    rechecking = true;
    api
      .session()
      .then((state) => {
        app.session = state;
        // The server answered, so the next genuine expiry gets its reload back.
        sessionStorage.removeItem(RELOAD_GUARD);
      })
      .catch(() => {
        // Not blanked: the sign-in page has two shapes and only the server
        // knows which, so `app.session = null` would render the redirect one at
        // a deployment that wants the password form — a sign-in page with
        // nothing to sign in with. A reload re-reads the session from scratch,
        // and if the server really is unreachable it says so instead.
        if (sessionStorage.getItem(RELOAD_GUARD)) {
          // Reloaded once already and still cannot ask. Say so and stop, rather
          // than reload at whatever rate the views retry.
          toast("Your session ended, and the dashboard cannot be reached.");
          return;
        }
        sessionStorage.setItem(RELOAD_GUARD, "1");
        window.location.reload();
      })
      .finally(() => {
        rechecking = false;
      });
  });

  // The rail's collapsed state lives on <body>, which is what the stylesheet
  // keys off, so the transition is one CSS rule rather than per-component logic.
  $effect(() => {
    document.body.classList.toggle("narrow", !app.railOpen);
  });

  const viewer = $derived(
    app.session?.signedIn ? app.session.viewer : null,
  );

  const NAV: { page: Route["page"]; label: string; icon: string }[] = [
    { page: "home", label: "Home", icon: "home" },
    { page: "files", label: "Files", icon: "files" },
    { page: "search", label: "Search", icon: "search" },
    { page: "memories", label: "Memories", icon: "memory" },
    { page: "sessions", label: "Sessions", icon: "sessions" },
    { page: "add", label: "Add a file", icon: "plus" },
  ];

  const crumbs = $derived.by(() => {
    const route = app.route;
    switch (route.page) {
      case "home":
        return [{ label: "Home", href: "#/home" }];
      case "files":
        return [{ label: "Files", href: "#/files" }];
      case "folder":
      case "file": {
        const tail = route.uri.replace(/\/$/, "").split("/").pop() ?? route.uri;
        return [
          { label: "Files", href: "#/files" },
          { label: tail, href: hashFor(route) },
        ];
      }
      default:
        return [
          {
            label: route.page.charAt(0).toUpperCase() + route.page.slice(1),
            href: hashFor(route),
          },
        ];
    }
  });
</script>

<!--
  Nothing renders until the session is known, and the shell does not render at
  all until there is somebody to render it for. A rail whose only news is that
  you are not signed in, and a breadcrumb pointing at a page you cannot open,
  are chrome for a room you have not entered — so the sign-in page gets the
  whole window and draws its own.
-->
{#if loading}
  <div class="boot"><p class="pdesc">Loading…</p></div>
{:else if !viewer}
  <SignIn {failure} />
{:else}
<div class="app">
  <aside class="side">
    <div class="side-in">
      <button class="ws" onclick={() => go({ page: "home" })}>
        <span class="ws-i">{viewer.name.charAt(0).toUpperCase()}</span>
        <span class="ws-n">
          {viewer.name}'s workspace
          <span class="ws-sub">{viewer.account}</span>
        </span>
      </button>

      <div class="slabel">Workspace</div>
      {#each NAV as item (item.page)}
        <button
          class="srow"
          aria-current={app.route.page === item.page}
          onclick={() => go({ page: item.page } as Route)}
        >
          <span class="gi"><Icon name={item.icon} /></span>
          <span class="lb">{item.label}</span>
        </button>
      {/each}

      <div class="sfoot">
        <button class="ws" onclick={() => go({ page: "account" })}>
          <span class="ws-i">{viewer.name.charAt(0).toUpperCase()}</span>
          <span class="ws-n">
            {viewer.name}
            <!-- Falls back to the account: a Vault sign-in carries no address. -->
            <span class="ws-sub">{viewer.email || viewer.account}</span>
          </span>
          <span
            class="ws-x"
            role="button"
            tabindex="0"
            aria-label="Collapse the sidebar"
            onclick={(event) => {
              event.stopPropagation();
              app.railOpen = false;
            }}
            onkeydown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                event.stopPropagation();
                app.railOpen = false;
              }
            }}
          >
            <Icon name="panel" />
          </span>
        </button>
      </div>
    </div>
  </aside>

  <main class="main">
    <div class="topbar">
      <button
        class="icb"
        id="openSide"
        aria-label="Show the sidebar"
        onclick={() => {
          app.railOpen = true;
        }}
      >
        <Icon name="panel" />
      </button>

      <nav class="bc" aria-label="Breadcrumb">
        {#each crumbs as crumb, index (crumb.href)}
          {#if index > 0}<span class="sl">/</span>{/if}
          <button class={index === crumbs.length - 1 ? "cur" : ""} onclick={() => { window.location.hash = crumb.href; }}>
            {crumb.label}
          </button>
        {/each}
      </nav>

      <span class="sp"></span>

      <button
        class="icb"
        onclick={refresh}
        aria-label="Reload from OpenViking"
        title="Reload from OpenViking"
      >
        <Icon name="refresh" />
      </button>

      <button
        class="icb"
        onclick={toggleTheme}
        aria-label={app.theme === "dark" ? "Switch to the light theme" : "Switch to the dark theme"}
        title={app.theme === "dark" ? "Light" : "Dark"}
      >
        <Icon name={app.theme === "dark" ? "sun" : "moon"} />
      </button>

      <button class="tbtn primary" onclick={() => go({ page: "add" })}>
        <Icon name="plus" /> Add a file
      </button>
    </div>

    <div class="scroll">
      <div class="doc">
        {#if app.route.page === "home"}
          <Home />
        {:else if app.route.page === "files"}
          <Files />
        {:else if app.route.page === "file"}
          <FileView uri={app.route.uri} />
        {:else if app.route.page === "search"}
          <Search />
        {:else if app.route.page === "memories"}
          <Memories />
        {:else if app.route.page === "sessions"}
          <Sessions />
        {:else if app.route.page === "add"}
          <AddFile />
        {:else if app.route.page === "account"}
          <AccountView />
        {/if}
      </div>
    </div>
  </main>
</div>
{/if}

{#if app.toast}
  <div class="toast">{app.toast}</div>
{/if}

<style>
  .toast {
    position: fixed;
    left: 24px;
    bottom: 24px;
    z-index: 40;
    background: var(--surface);
    color: var(--ink);
    font-family: var(--fm);
    font-size: 12px;
    padding: 10px 15px;
    border-radius: var(--r2);
    border: 1px solid var(--rule);
    box-shadow: 0 8px 26px rgba(0, 0, 0, 0.4);
  }
</style>
