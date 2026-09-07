<script lang="ts">
  import { api } from "../lib/api";
  import Icon from "../lib/Icon.svelte";
  import PageHead from "../lib/PageHead.svelte";
  import { app } from "../lib/state.svelte";

  const viewer = $derived(app.session?.signedIn ? app.session.viewer : null);

  async function signOut(): Promise<void> {
    await api.signOut();
    window.location.href = "/";
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

  <p style="margin-top:24px">
    <button class="mini2" onclick={signOut}>
      <Icon name="exit" /> Sign out
    </button>
  </p>
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
