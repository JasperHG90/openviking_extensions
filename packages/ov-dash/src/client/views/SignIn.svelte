<script lang="ts">
  /**
   * The signed-out page: the door to the reading room.
   *
   * It draws none of the shell. A rail whose only news is that you are not
   * signed in, and a breadcrumb pointing at a page you cannot open, are chrome
   * for a room you have not entered yet — so the shell stays away until there
   * is a viewer, and this fills the window on its own.
   *
   * Two shapes, decided by the server. Normally there is one button and the
   * sign-in happens at the provider's own page — Vault's, where the password
   * and whatever else it asks for stay. The password form is the older shape,
   * for a deployment with no OIDC client registered; it is the only one that
   * puts a password through this dashboard.
   */
  import Icon from "../lib/Icon.svelte";
  import Seal from "../lib/Seal.svelte";
  import { app, toggleTheme } from "../lib/state.svelte";

  interface Props {
    /** A boot failure worth showing instead of pretending the form will work. */
    failure?: string;
  }

  const { failure = "" }: Props = $props();

  const signedOut = $derived(app.session && !app.session.signedIn ? app.session : null);
  const mode = $derived(signedOut?.mode ?? "redirect");
  const loginUrl = $derived(signedOut?.loginUrl ?? "/auth/login");
  // Naming where you are about to be sent is the whole courtesy of this button:
  // a page that says "Sign in" and then swaps the address bar for Vault's login
  // looks like something went wrong.
  const goLabel = $derived(mode === "vault-oidc" ? "Sign in with Vault" : "Sign in");

  let username = $state("");
  let password = $state("");
  let busy = $state(false);
  let refused = $state("");

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    busy = true;
    refused = "";

    const form = new FormData();
    form.set("username", username);
    form.set("password", password);

    try {
      const response = await fetch("/auth/vault-login", {
        method: "POST",
        body: form,
        credentials: "same-origin",
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as {
          error?: { message?: string };
        } | null;
        refused = body?.error?.message ?? `Sign-in failed (HTTP ${response.status})`;
        return;
      }
      // The password never survives the request that used it.
      password = "";
      window.location.reload();
    } catch (error) {
      refused = (error as Error).message;
    } finally {
      busy = false;
    }
  }
</script>

<div class="room">
  <button
    class="lamp-switch"
    onclick={toggleTheme}
    aria-label={app.theme === "dark" ? "Switch to the light theme" : "Switch to the dark theme"}
    title={app.theme === "dark" ? "Light" : "Dark"}
  >
    <Icon name={app.theme === "dark" ? "sun" : "moon"} />
  </button>

  <main class="door">
    <Seal />

    <p class="mark">OpenViking</p>

    <h1>Your context, in one place</h1>

    <p class="sub">Files, memories and agent runs, kept where you left them.</p>

    {#if failure}
      <!--
        The person gets a sentence and something to press. The machine's own
        words go underneath in small type, where they help whoever is asked to
        look into it and do not have to mean anything to anyone else.
      -->
      <div class="alert">
        <!--
          "Not answering" covers both paths. The other candidate, "could not be
          reached", is false when the dashboard answered — with a 500.
        -->
        <p class="alert-say">The dashboard is not answering right now.</p>
        <p class="alert-why">{failure}</p>
        <button class="go" onclick={() => window.location.reload()}>
          <Icon name="refresh" /> Try again
        </button>
      </div>
    {:else if mode === "vault-userpass"}
      <form onsubmit={submit}>
        <label class="field">
          <span class="lbl">Username</span>
          <input
            name="username"
            autocomplete="username"
            bind:value={username}
            required
            disabled={busy}
          />
        </label>

        <label class="field">
          <span class="lbl">Password</span>
          <input
            name="password"
            type="password"
            autocomplete="current-password"
            bind:value={password}
            required
            disabled={busy}
          />
        </label>

        {#if refused}
          <p class="refused" role="alert">{refused}</p>
        {/if}

        <button class="go" type="submit" disabled={busy}>
          <Icon name="exit" />
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    {:else}
      <p class="redirect">
        <a
          class="go"
          href={`${loginUrl}?returnTo=${encodeURIComponent(`/${window.location.hash || "#/home"}`)}`}
        >
          <Icon name="exit" /> {goLabel}
        </a>
      </p>
      {#if mode === "vault-oidc"}
        <p class="aside">You sign in at Vault. This dashboard never sees your password.</p>
      {/if}
    {/if}
  </main>
</div>

<style>
  /*
   * The lamp is off-centre on purpose.
   *
   * The accent in app.css is described there as "the colour of a desk lamp
   * rather than a status light", so the page is lit by one: the pool falls from
   * over your left shoulder. A symmetrical halo behind a centred logo is the
   * house style of every sign-in page ever made, and light comes from somewhere.
   */
  .room {
    position: relative;
    min-height: 100vh;
    display: grid;
    place-items: center;
    padding: var(--s7) var(--s5);
    isolation: isolate;
    /*
     * `clip`, not `hidden`: the lamp is deliberately wider than the window and
     * hangs off the right edge below about 990px, which used to be swallowed by
     * the shell's own overflow. Out here on its own the page scrolled sideways
     * on every phone. `hidden` would fix that and also trap the form whenever it
     * outgrows the viewport height; `clip` only cuts, and scrolls nothing.
     */
    overflow-x: clip;
  }
  .room::before {
    content: "";
    position: absolute;
    z-index: -1;
    top: -18%;
    left: 50%;
    width: min(1180px, 150vw);
    aspect-ratio: 1.45;
    transform: translateX(-58%);
    /*
     * The stops ease the falloff's shape; they cannot add bit depth. A wide
     * radial over a flat ground steps through whole 8-bit levels, and on the
     * light face the whole pool lives inside about sixteen of them, so faint
     * rings are visible if you look for them — roughly one every fifteen
     * pixels. Killing them needs a dither overlay, which is a texture asset
     * and a compositing cost for something nobody notices while typing a
     * password. Left as it is, deliberately.
     */
    background: radial-gradient(
      ellipse at 42% 32%,
      var(--lamp-1) 0%,
      var(--lamp-1) 8%,
      var(--lamp-2) 30%,
      color-mix(in srgb, var(--lamp-2) 55%, transparent) 44%,
      color-mix(in srgb, var(--lamp-2) 25%, transparent) 56%,
      transparent 72%
    );
    pointer-events: none;
  }

  .door {
    width: 100%;
    max-width: 380px;
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
  }

  .mark {
    margin: var(--s4) 0 0;
    font-family: var(--fm);
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.34em;
    text-indent: 0.34em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  h1 {
    margin: var(--s5) 0 0;
    font-family: var(--fd);
    font-size: 40px;
    line-height: 1.12;
    font-weight: 600;
    letter-spacing: -0.015em;
    text-wrap: balance;
  }

  .sub {
    margin: var(--s3) 0 0;
    max-width: 34ch;
    font-size: 15px;
    line-height: 1.55;
    color: var(--ink-2);
  }

  form {
    width: 100%;
    margin-top: var(--s6);
    display: flex;
    flex-direction: column;
    gap: var(--s4);
    text-align: left;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--s2);
  }

  .lbl {
    font-family: var(--fm);
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  .field input {
    width: 100%;
    font: inherit;
    font-size: 15px;
    color: var(--ink);
    background: var(--surface);
    border: 1px solid var(--rule);
    border-radius: var(--r2);
    padding: 12px 14px;
    transition: border-color 0.15s;
  }
  /*
   * No `outline: none` here. It was overriding the app's own focus ring with a
   * 4%-alpha shadow, so the two controls a keyboard user has to land in were
   * the only ones on the page with no visible focus at all.
   */
  .field input:focus-visible {
    border-color: var(--accent);
  }
  .field input:disabled {
    opacity: 0.6;
  }

  /* A refusal, not a crash: one line under the fields it is about. */
  .refused {
    margin: calc(var(--s2) * -1) 0 0;
    font-size: 13.5px;
    line-height: 1.5;
    color: var(--rose);
  }

  .go {
    width: 100%;
    font: inherit;
    font-size: 15px;
    font-weight: 600;
    color: #17140e;
    background: var(--accent);
    border: none;
    border-radius: var(--r2);
    padding: 12px 16px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: var(--s2);
    text-decoration: none;
    transition:
      background 0.15s,
      transform 0.06s;
  }
  .go:hover {
    background: var(--accent-ink);
  }
  .go:active {
    transform: translateY(1px);
  }
  .go:disabled {
    opacity: 0.45;
    cursor: default;
  }

  .redirect {
    width: 100%;
    margin: var(--s6) 0 0;
  }

  /* Where the password goes instead. Said once, quietly, under the button. */
  .aside {
    margin: var(--s3) 0 0;
    max-width: 34ch;
    font-size: 13px;
    line-height: 1.5;
    color: var(--ink-3);
  }

  .alert {
    width: 100%;
    margin-top: var(--s6);
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--s3);
  }
  .alert p {
    margin: 0;
  }
  .alert-say {
    font-size: 15px;
    line-height: 1.55;
    color: var(--ink-2);
  }
  .alert-why {
    font-family: var(--fm);
    font-size: 12px;
    line-height: 1.5;
    color: var(--ink-3);
    word-break: break-word;
  }

  .lamp-switch {
    position: fixed;
    top: var(--s4);
    right: var(--s4);
    width: 34px;
    height: 34px;
    display: grid;
    place-items: center;
    padding: 0;
    color: var(--ink-3);
    background: transparent;
    border: none;
    border-radius: var(--r1);
    cursor: pointer;
  }
  .lamp-switch:hover {
    background: var(--hover);
    color: var(--ink-2);
  }

  /* One orchestrated arrival, not a scatter of effects. */
  @keyframes lift {
    from {
      opacity: 0;
      transform: translateY(9px);
    }
    to {
      opacity: 1;
      transform: none;
    }
  }
  @keyframes warm {
    from {
      opacity: 0;
    }
    to {
      opacity: 1;
    }
  }
  .room::before {
    animation: warm 1.1s ease-out both;
  }
  .door > :global(*) {
    animation: lift 0.62s cubic-bezier(0.22, 0.68, 0.32, 1) both;
  }
  .door > :global(:nth-child(1)) {
    animation-delay: 0.04s;
  }
  .door > :global(:nth-child(2)) {
    animation-delay: 0.1s;
  }
  .door > :global(:nth-child(3)) {
    animation-delay: 0.16s;
  }
  .door > :global(:nth-child(4)) {
    animation-delay: 0.21s;
  }
  .door > :global(:nth-child(5)) {
    animation-delay: 0.26s;
  }
  .door > :global(:nth-child(6)) {
    animation-delay: 0.31s;
  }

  @media (prefers-reduced-motion: reduce) {
    .room::before,
    .door > :global(*) {
      animation: none;
    }
  }

  @media (max-width: 480px) {
    h1 {
      font-size: 32px;
    }
    .room {
      padding: var(--s6) var(--s4);
    }
  }
</style>
