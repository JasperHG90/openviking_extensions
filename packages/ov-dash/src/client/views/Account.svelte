<script lang="ts">
  import { api } from "../lib/api";
  import Icon from "../lib/Icon.svelte";
  import PageHead from "../lib/PageHead.svelte";
  import { app, toast } from "../lib/state.svelte";

  const session = $derived(app.session?.signedIn ? app.session : null);
  const viewer = $derived(session?.viewer ?? null);
  const canSignOut = $derived(session?.canSignOut ?? false);

  let busy = $state(false);

  async function signOut(): Promise<void> {
    busy = true;
    try {
      await api.signOut();
      // A full navigation, not a hash change: everything held in memory for
      // the person signing out goes with the page.
      window.location.href = "/";
    } catch (error) {
      busy = false;
      toast((error as Error).message);
    }
  }
</script>

<PageHead eyebrow="Account" title="Account" />

{#if viewer}
  <div class="pp">
    <div class="r"><span class="k">Name</span><span class="v">{viewer.name}</span></div>
    <div class="r"><span class="k">Email</span><span class="v">{viewer.email}</span></div>
    <div class="r">
      <span class="k">OpenViking user</span>
      <span class="v mono">{viewer.user}</span>
    </div>
    <div class="r">
      <span class="k">Account</span>
      <span class="v mono">{viewer.account}</span>
    </div>
  </div>

  <div class="callout c-teal">
    <span class="ci"><Icon name="key" /></span>
    <p>
      Your OpenViking API key stays on the server. The dashboard looks it up for
      each request from the identity above, so it is never sent to your browser
      and never appears in a page you could copy it out of.
    </p>
  </div>

  {#if canSignOut}
    <p style="margin-top:24px">
      <button class="mini2" disabled={busy} onclick={signOut}>
        <Icon name="exit" />
        {busy ? "Signing out…" : "Sign out"}
      </button>
    </p>
  {:else}
    <!--
      No button, because there is nothing here that could end the session. It
      would clear a cookie neither of these modes reads, and leave the person
      signed in — which is what it did before this said so.
    -->
    <div class="callout c-sheet">
      <span class="ci"><Icon name="user" /></span>
      <p>
        This dashboard is not holding your session — your identity arrives with
        every request, from the proxy in front of it or from its own
        configuration. Signing out is done wherever you signed in.
      </p>
    </div>
  {/if}

{:else}
  <p class="pdesc">You are not signed in.</p>
{/if}

<style>
  .mini2 {
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }
  .mini2:hover {
    color: var(--rose);
    box-shadow: 0 0 0 1px var(--rose);
  }
</style>
