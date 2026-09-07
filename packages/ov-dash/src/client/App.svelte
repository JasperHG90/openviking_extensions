<script lang="ts">
  /**
   * The shell: the dark rail, the top bar, and whichever view the route names.
   *
   * Nothing renders until the session is known, because every view needs to
   * know whether there is anybody to render it for.
   */
  import { api } from "./lib/api";
  import Icon from "./lib/Icon.svelte";
  import { app, go, hashFor, toggleTheme, type Route } from "./lib/state.svelte";
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

<div class="app">
  <aside class="side">
    <div class="side-in">
      <button class="ws" onclick={() => go({ page: "home" })}>
        <span class="ws-i">{viewer ? viewer.name.charAt(0).toUpperCase() : "O"}</span>
        <span class="ws-n">
          {viewer ? `${viewer.name}'s workspace` : "OpenViking"}
          <span class="ws-sub">{viewer ? viewer.account : "not signed in"}</span>
        </span>
      </button>

      {#if viewer}
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
              <span class="ws-sub">{viewer.email}</span>
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
      {/if}
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

      {#if viewer}
        <button class="tbtn primary" onclick={() => go({ page: "add" })}>
          <Icon name="plus" /> Add a file
        </button>
      {/if}
    </div>

    <div class="scroll">
      <div class="doc">
        {#if loading}
          <p class="pdesc" style="margin-top:60px">Loading…</p>
        {:else if failure}
          <div class="callout c-sheet" style="margin-top:60px">
            <span class="ci"><Icon name="close" /></span>
            <p>{failure}</p>
          </div>
        {:else if !viewer}
          <SignIn />
        {:else if app.route.page === "home"}
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
