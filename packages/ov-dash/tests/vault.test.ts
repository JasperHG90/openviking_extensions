/**
 * Signing in through Vault.
 *
 * The property worth pinning is what the sign-in leaves behind. It logs into
 * Vault with a password, uses the resulting token for exactly one call, and
 * then has no further use for it — but Vault keeps a userpass token alive for
 * its whole lease, weeks on a stock mount. A dashboard people sign into from
 * anywhere would otherwise accumulate one live Vault token per sign-in, none of
 * them reachable from the sign-out button.
 */

import { describe, expect, it, vi } from "vitest";
import { loadConfig } from "../src/server/env";
import { VaultError, signIn } from "../src/server/vault";

const VAULT = "https://vault.example";

function config(extra: Record<string, string> = {}) {
  return loadConfig({
    OV_URL: "http://openviking:1933",
    PUBLIC_ORIGIN: "http://localhost:8080",
    SESSION_SECRET: "a".repeat(32),
    AUTH_MODE: "vault-userpass",
    VAULT_ADDR: VAULT,
    ...extra,
  });
}

/** A minted identity token carrying the claims OpenViking maps identity from. */
function token(exp = Math.floor(Date.now() / 1000) + 3600): string {
  const claims = Buffer.from(
    JSON.stringify({ ov_account: "lab", ov_user: "jasper", exp }),
  ).toString("base64url");
  return `eyJhbGciOiJSUzI1NiJ9.${claims}.sig`;
}

/**
 * Stand in for Vault, recording every call.
 *
 * @param mint - What the mint endpoint answers with.
 * @param revoke - What `revoke-self` answers with.
 */
function stubVault(
  mint: () => Response = () => new Response(JSON.stringify({ data: { token: token() } })),
  revoke: () => Response = () => new Response(null, { status: 204 }),
) {
  const calls: { url: string; method: string; vaultToken?: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL, init?: RequestInit) => {
      const url = String(input);
      const headers = (init?.headers ?? {}) as Record<string, string>;
      calls.push({
        url,
        method: init?.method ?? "GET",
        vaultToken: headers["x-vault-token"],
      });
      if (url.includes("/auth/userpass/login/")) {
        return new Response(JSON.stringify({ auth: { client_token: "s.login" } }));
      }
      if (url.includes("/auth/token/revoke-self")) return revoke();
      return mint();
    }),
  );
  return calls;
}

describe("the Vault login token does not outlive the sign-in", () => {
  it("is handed back once the OpenViking token is minted", async () => {
    const calls = stubVault();
    const credential = await signIn(config(), "jasper", "pw");

    expect(credential.viewer.user).toBe("jasper");
    const revoked = calls.find((call) => call.url.endsWith("/v1/auth/token/revoke-self"));
    expect(revoked).toBeDefined();
    expect(revoked?.method).toBe("POST");
    // Revoked with the token being revoked, not with some other credential.
    expect(revoked?.vaultToken).toBe("s.login");
    vi.unstubAllGlobals();
  });

  it("is handed back after the mint, not before it", async () => {
    const calls = stubVault();
    await signIn(config(), "jasper", "pw");

    const order = calls.map((call) => call.url);
    const mintAt = order.findIndex((url) => url.includes("/identity/oidc/token/"));
    const revokeAt = order.findIndex((url) => url.includes("/auth/token/revoke-self"));
    // Revoking first would leave nothing to mint with.
    expect(mintAt).toBeGreaterThanOrEqual(0);
    expect(revokeAt).toBeGreaterThan(mintAt);
    vi.unstubAllGlobals();
  });

  it("is handed back even when the mint fails", async () => {
    // A mint that throws leaves exactly the same live token behind as one that
    // succeeds, which is why the revoke sits in a finally.
    const calls = stubVault(() => new Response("nope", { status: 403 }));
    await expect(signIn(config(), "jasper", "pw")).rejects.toThrow(VaultError);

    expect(calls.some((call) => call.url.endsWith("/v1/auth/token/revoke-self"))).toBe(
      true,
    );
    vi.unstubAllGlobals();
  });

  it("does not fail the sign-in when Vault refuses to revoke", async () => {
    // An untidy token is worse than nothing; denying somebody a session they
    // have already earned is worse than untidy.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    stubVault(undefined, () => new Response("no", { status: 500 }));

    const credential = await signIn(config(), "jasper", "pw");
    expect(credential.viewer.user).toBe("jasper");
    expect(warn).toHaveBeenCalled();

    warn.mockRestore();
    vi.unstubAllGlobals();
  });

  it("does not fail the sign-in when Vault cannot be reached to revoke", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL) => {
        const url = String(input);
        if (url.includes("/auth/userpass/login/")) {
          return new Response(JSON.stringify({ auth: { client_token: "s.login" } }));
        }
        if (url.includes("/auth/token/revoke-self")) throw new Error("connection reset");
        return new Response(JSON.stringify({ data: { token: token() } }));
      }),
    );

    await expect(signIn(config(), "jasper", "pw")).resolves.toMatchObject({
      viewer: { user: "jasper" },
    });
    expect(warn).toHaveBeenCalled();

    warn.mockRestore();
    vi.unstubAllGlobals();
  });

  it("gives the revoke its own short deadline, not OpenViking's", async () => {
    // OV_TIMEOUT_MS is a minute, raised so OpenViking's slow grep can finish.
    // Nothing in the sign-in's answer waits on the revoke, so borrowing that
    // budget would turn a Vault that blackholes this call into a minute of
    // spinner on an otherwise successful sign-in.
    const deadlines: number[] = [];
    const real = AbortSignal.timeout.bind(AbortSignal);
    const spy = vi.spyOn(AbortSignal, "timeout").mockImplementation((ms: number) => {
      deadlines.push(ms);
      return real(ms);
    });
    stubVault();

    await signIn(config({ OV_TIMEOUT_MS: "60000" }), "jasper", "pw");

    // Login and mint may take the long budget; the revoke must not.
    expect(deadlines).toContain(3_000);
    expect(deadlines.filter((ms) => ms === 3_000)).toHaveLength(1);

    spy.mockRestore();
    vi.unstubAllGlobals();
  });

  it("says nothing when the mount issues batch tokens", async () => {
    // A batch token is a self-contained blob with nothing server-side to
    // revoke, and Vault answers 400. Warning about it on every single sign-in
    // would train an operator to ignore the log.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    stubVault(
      undefined,
      () =>
        new Response(JSON.stringify({ errors: ["batch tokens cannot be revoked"] }), {
          status: 400,
        }),
    );

    await expect(signIn(config(), "jasper", "pw")).resolves.toMatchObject({
      viewer: { user: "jasper" },
    });
    expect(warn).not.toHaveBeenCalled();

    warn.mockRestore();
    vi.unstubAllGlobals();
  });

  it("still speaks up about a 400 that is not the batch-token one", async () => {
    // Suppressing the whole status would hide a misconfigured mount or
    // namespace, which is a 400 an operator can and should act on.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    stubVault(
      undefined,
      () =>
        new Response(JSON.stringify({ errors: ["no handler for route"] }), {
          status: 400,
        }),
    );

    await signIn(config(), "jasper", "pw");
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("no handler for route"));

    warn.mockRestore();
    vi.unstubAllGlobals();
  });

  it("never asks Vault to revoke a token it did not get", async () => {
    // A refused login returns no token, so there is nothing to hand back and
    // the request must not be made with an undefined header.
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL) => {
        calls.push(String(input));
        return new Response(
          JSON.stringify({ errors: ["invalid username or password"] }),
          {
            status: 400,
          },
        );
      }),
    );
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    await expect(signIn(config(), "jasper", "wrong")).rejects.toThrow(
      /were not accepted/,
    );
    expect(calls.some((url) => url.includes("revoke-self"))).toBe(false);

    warn.mockRestore();
    vi.unstubAllGlobals();
  });
});
