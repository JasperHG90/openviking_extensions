<script lang="ts">
  import { api } from "../lib/api";
  import Icon from "../lib/Icon.svelte";
  import PageHead from "../lib/PageHead.svelte";
  import { app, toast } from "../lib/state.svelte";

  const session = $derived(app.session?.signedIn ? app.session : null);
  const viewer = $derived(session?.viewer ?? null);
  const canSignOut = $derived(session?.canSignOut ?? false);
  const expiresAt = $derived(session?.expiresAt ?? null);

  /** When the session ends, in this browser's own locale and time zone. */
  const expiry = $derived(
    expiresAt === null
      ? ""
      : new Date(expiresAt * 1000).toLocaleString(undefined, {
          dateStyle: "medium",
          timeStyle: "short",
        }),
  );

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
    <!-- A Vault sign-in carries no address, so the row would label an empty space. -->
    {#if viewer.email}
      <div class="r"><span class="k">Email</span><span class="v">{viewer.email}</span></div>
    {/if}
    <div class="r">
      <span class="k">OpenViking user</span>
      <span class="v mono">{viewer.user}</span>
    </div>
    <div class="r">
      <span class="k">Account</span>
      <span class="v mono">{viewer.account}</span>
    </div>
    <!--
      A time, and nothing about where it comes from. What a session is made of
      is the operator's business and it is in the README; the only part of it
      anyone here can act on is when they will have to sign in again.
    -->
    {#if expiry}
      <div class="r">
        <span class="k">Signed in until</span>
        <span class="v">{expiry}</span>
      </div>
    {/if}
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
      No button, because nothing here could end the session: the identity
      arrives with every request. A button that cleared a cookie nobody reads
      would leave the person signed in and looking for the reason.
    -->
    <p class="pdesc" style="margin-top:24px">You sign out where you signed in.</p>
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
