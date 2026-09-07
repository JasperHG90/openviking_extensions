<script lang="ts">
  /**
   * The signed-out page.
   *
   * Two shapes, decided by the server. Against a Vault-backed OpenViking the
   * person types their Vault username and password here: the server logs them
   * in and mints the token OpenViking accepts, so nobody needs the CLI. Against
   * a provider whose tokens OpenViking accepts directly, signing in is a
   * full-page redirect instead and the dashboard never sees a password.
   */
  import Icon from "../lib/Icon.svelte";
  import PageHead from "../lib/PageHead.svelte";
  import { app } from "../lib/state.svelte";

  const signedOut = $derived(app.session && !app.session.signedIn ? app.session : null);
  const mode = $derived(signedOut?.mode ?? "redirect");
  const loginUrl = $derived(signedOut?.loginUrl ?? "/auth/login");

  let username = $state("");
  let password = $state("");
  let busy = $state(false);
  let failure = $state("");

  async function submit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    busy = true;
    failure = "";

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
        failure = body?.error?.message ?? `Sign-in failed (HTTP ${response.status})`;
        return;
      }
      // The password never survives the request that used it.
      password = "";
      window.location.reload();
    } catch (error) {
      failure = (error as Error).message;
    } finally {
      busy = false;
    }
  }
</script>

<PageHead
  eyebrow="Sign in"
  title="Your context, in one place"
  desc="Files, memories and agent runs from OpenViking. Sign in to see yours."
/>

{#if mode === "vault-userpass"}
  <div class="callout c-teal" style="margin-top:26px">
    <span class="ci"><Icon name="key" /></span>
    <p>
      Sign in with your Vault username and password. The dashboard uses them
      once to mint your OpenViking token, keeps the token on the server, and
      never stores the password.
    </p>
  </div>

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

    {#if failure}
      <div class="callout c-sheet" style="margin:14px 0 0">
        <span class="ci"><Icon name="close" /></span>
        <p>{failure}</p>
      </div>
    {/if}

    <p style="margin-top:20px">
      <button class="tbtn primary" type="submit" disabled={busy}>
        <Icon name="exit" />
        {busy ? "Signing in…" : "Sign in"}
      </button>
    </p>
  </form>
{:else}
  <div class="callout c-teal" style="margin-top:26px">
    <span class="ci"><Icon name="key" /></span>
    <p>
      You are sent to your identity provider to sign in. The dashboard holds
      your OpenViking credential on the server and never puts it in your
      browser.
    </p>
  </div>

  <p style="margin-top:26px">
    <a
      class="tbtn primary"
      href={`${loginUrl}?returnTo=${encodeURIComponent(`/${window.location.hash || "#/home"}`)}`}
    >
      <Icon name="exit" /> Sign in
    </a>
  </p>
{/if}

<style>
  .field {
    display: block;
    max-width: 340px;
    margin-top: 18px;
  }
  .lbl {
    display: block;
    font-family: var(--fm);
    font-size: 11px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin-bottom: 6px;
  }
  .field input {
    width: 100%;
    font: inherit;
    font-size: 15px;
    color: var(--ink);
    background: var(--surface);
    border: none;
    border-radius: 10px;
    padding: 12px 14px;
    border: 1px solid var(--rule);
  }
  .field input:focus {
    outline: none;
    border-color: var(--accent);
  }
  .field input:disabled {
    opacity: 0.6;
  }
  a.tbtn {
    text-decoration: none;
    display: inline-flex;
  }
</style>
