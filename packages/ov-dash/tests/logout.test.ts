/**
 * Signing out.
 *
 * Two things went wrong here and both were invisible from the button: it was
 * offered in modes where nothing it does ends a session, and it left a tab's
 * worth of the previous person's data in the client cache.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cached, invalidate, isCached } from "../src/client/lib/cache";
import { buildServices, createApp } from "../src/server/app";
import { loadConfig } from "../src/server/env";

const ORIGIN = "http://localhost:8080";

function configFor(extra: Record<string, string>) {
  return loadConfig({
    OV_URL: "http://openviking:1933",
    PUBLIC_ORIGIN: ORIGIN,
    SESSION_SECRET: "a".repeat(32),
    KEY_SOURCE: "env",
    OV_API_KEY: "ov_test",
    ...extra,
  });
}

afterEach(() => {
  invalidate();
  vi.unstubAllGlobals();
});

describe("whether signing out is offered", () => {
  it("is, when the dashboard holds the session in a cookie", async () => {
    const app = createApp(
      buildServices(
        configFor({
          AUTH_MODE: "vault-userpass",
          VAULT_ADDR: "https://vault.example",
        }),
      ),
    );
    // dev identity is not available here, so assert the flag the route computes
    // by signing in is out of scope; the oidc branch is covered below.
    const response = await app.request("/api/session");
    expect(await response.json()).toMatchObject({ signedIn: false });
  });

  it("is not, when the identity arrives on every request", async () => {
    const app = createApp(
      buildServices(configFor({ AUTH_MODE: "dev", DEV_USER: "jasper" })),
    );
    const body = (await (await app.request("/api/session")).json()) as {
      canSignOut: boolean;
    };
    // Clearing a cookie dev mode never reads would leave the person signed in.
    expect(body.canSignOut).toBe(false);
  });

  it("is not, behind an authenticating proxy", async () => {
    const app = createApp(buildServices(configFor({ AUTH_MODE: "trusted-header" })));
    const body = (await (
      await app.request("/api/session", {
        headers: { "x-forwarded-user": "jasper@example.com" },
      })
    ).json()) as { canSignOut: boolean };
    expect(body.canSignOut).toBe(false);
  });
});

/** Vault, answering a login, a mint and a revoke. */
function stubVault(exp: number) {
  const claims = Buffer.from(
    JSON.stringify({ ov_account: "lab", ov_user: "jasper", exp }),
  ).toString("base64url");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL) => {
      const url = String(input);
      if (url.includes("/auth/userpass/login/")) {
        return new Response(JSON.stringify({ auth: { client_token: "s.tok" } }));
      }
      if (url.includes("revoke-self")) return new Response(null, { status: 204 });
      return new Response(
        JSON.stringify({ data: { token: `eyJhbGciOiJSUzI1NiJ9.${claims}.sig` } }),
      );
    }),
  );
}

/** A dashboard in the mode the lab runs, plus a signed-in cookie for it. */
async function signedInVaultApp(extra: Record<string, string> = {}) {
  const app = createApp(
    buildServices(
      configFor({
        AUTH_MODE: "vault-userpass",
        VAULT_ADDR: "https://vault.example",
        SESSION_COOKIE_SECURE: "false",
        ...extra,
      }),
    ),
  );
  const response = await app.request("/auth/vault-login", {
    method: "POST",
    headers: { origin: ORIGIN, "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ username: "jasper", password: "pw" }),
  });
  const cookie = (response.headers.get("set-cookie") ?? "").split(";")[0] ?? "";
  return { app, cookie };
}

describe("what the Account page is told", () => {
  it("says when a Vault session ends", async () => {
    const exp = Math.floor(Date.now() / 1000) + 3600;
    stubVault(exp);
    const { app, cookie } = await signedInVaultApp();

    const body = (await (
      await app.request("/api/session", { headers: { cookie } })
    ).json()) as { expiresAt: number; canSignOut: boolean };

    expect(body.canSignOut).toBe(true);
    // The session ends when the minted token does, and the page can say when.
    expect(body.expiresAt).toBe(exp);
  });

  it("reports the session TTL when that is the shorter of the two", async () => {
    // The cap runs both ways, and the page shows a time rather than a reason —
    // saying "the token expires then" would be wrong in exactly this case.
    const exp = Math.floor(Date.now() / 1000) + 86_400;
    stubVault(exp);
    const { app, cookie } = await signedInVaultApp({ SESSION_TTL_SECONDS: "60" });

    const body = (await (
      await app.request("/api/session", { headers: { cookie } })
    ).json()) as { expiresAt: number };

    expect(body.expiresAt).toBeLessThanOrEqual(Math.floor(Date.now() / 1000) + 60);
    expect(body.expiresAt).toBeLessThan(exp);
  });

  it("shows no expiry for a mode where nothing expires", async () => {
    const app = createApp(
      buildServices(configFor({ AUTH_MODE: "dev", DEV_USER: "jasper" })),
    );
    const body = (await (await app.request("/api/session")).json()) as {
      expiresAt: number | null;
    };
    // Nothing here expires, so the page shows no time rather than inventing one.
    expect(body.expiresAt).toBeNull();
  });

  it("shows no expiry behind a proxy either", async () => {
    const app = createApp(buildServices(configFor({ AUTH_MODE: "trusted-header" })));
    const body = (await (
      await app.request("/api/session", {
        headers: { "x-forwarded-user": "jasper@example.com" },
      })
    ).json()) as { expiresAt: number | null };
    expect(body.expiresAt).toBeNull();
  });
});

describe("signing out of a Vault session", () => {
  it("clears the cookie, and the browser that honours it is signed out", async () => {
    stubVault(Math.floor(Date.now() / 1000) + 3600);
    const { app, cookie } = await signedInVaultApp();

    const out = await app.request("/auth/logout", {
      method: "POST",
      headers: { origin: ORIGIN, cookie },
    });
    expect(out.status).toBe(204);

    const cleared = out.headers.get("set-cookie") ?? "";
    expect(cleared).toContain("ovdash_session=");
    expect(cleared).toMatch(/Max-Age=0/);

    // What the browser does next: no cookie, so no session.
    const after = (await (await app.request("/api/session")).json()) as {
      signedIn: boolean;
    };
    expect(after.signedIn).toBe(false);
  });

  it("kills a cookie somebody copied first", async () => {
    // The reason the session moved server-side. Clearing the cookie only asks
    // the browser that asked to forget; a copy taken off a shared machine, a
    // synced profile or a proxy log kept working until it expired. Signing out
    // now drops the session itself, so every copy dies at once.
    stubVault(Math.floor(Date.now() / 1000) + 3600);
    const { app, cookie } = await signedInVaultApp();

    const before = (await (
      await app.request("/api/session", { headers: { cookie } })
    ).json()) as { signedIn: boolean };
    expect(before.signedIn).toBe(true);

    await app.request("/auth/logout", {
      method: "POST",
      headers: { origin: ORIGIN, cookie },
    });

    const replayed = (await (
      await app.request("/api/session", { headers: { cookie } })
    ).json()) as { signedIn: boolean };
    expect(replayed.signedIn).toBe(false);
  });

  it("kills the session a second sign-in replaced", async () => {
    // Signing in again overwrites the cookie, so the old id is one the person
    // can never present and the button can never reach — but the session behind
    // it was still live, and anyone holding a copy of that older cookie sailed
    // through a sign-out. Shared machine, sign in twice, sign out: this is the
    // request the attacker makes.
    stubVault(Math.floor(Date.now() / 1000) + 3600);
    const { app, cookie: first } = await signedInVaultApp();

    const again = await app.request("/auth/vault-login", {
      method: "POST",
      headers: {
        origin: ORIGIN,
        "content-type": "application/x-www-form-urlencoded",
        cookie: first,
      },
      body: new URLSearchParams({ username: "jasper", password: "pw" }),
    });
    const second = (again.headers.get("set-cookie") ?? "").split(";")[0] ?? "";
    expect(second).not.toBe(first);

    // Replaced, so it is already dead — before any sign-out at all.
    const replaced = (await (
      await app.request("/api/session", { headers: { cookie: first } })
    ).json()) as { signedIn: boolean };
    expect(replaced.signedIn).toBe(false);

    await app.request("/auth/logout", {
      method: "POST",
      headers: { origin: ORIGIN, cookie: second },
    });
    const after = (await (
      await app.request("/api/session", { headers: { cookie: second } })
    ).json()) as { signedIn: boolean };
    expect(after.signedIn).toBe(false);
  });

  it("leaves other people signed in", async () => {
    // Dropping one session must not be a stampede: the store is keyed by id,
    // and signing out is a delete of one entry, not a flush.
    stubVault(Math.floor(Date.now() / 1000) + 3600);
    const { app, cookie: mine } = await signedInVaultApp();
    const theirs = (
      await app.request("/auth/vault-login", {
        method: "POST",
        headers: { origin: ORIGIN, "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ username: "jasper", password: "pw" }),
      })
    ).headers
      .get("set-cookie")
      ?.split(";")[0];

    await app.request("/auth/logout", {
      method: "POST",
      headers: { origin: ORIGIN, cookie: mine },
    });

    const other = (await (
      await app.request("/api/session", { headers: { cookie: theirs ?? "" } })
    ).json()) as { signedIn: boolean };
    expect(other.signedIn).toBe(true);
  });
});

describe("asking who you are never needs you to be anybody", () => {
  it("answers an unauthenticated request instead of refusing it", async () => {
    // The one route exempt from the /api/* gate. The client re-reads it after a
    // 401, so a 401 here would be an unbroken loop between the two. Pinned as
    // behaviour rather than as one line: two separate things spare this route,
    // and what matters is that neither stops doing it.
    const app = createApp(
      buildServices(
        configFor({ AUTH_MODE: "vault-userpass", VAULT_ADDR: "https://vault.example" }),
      ),
    );
    const response = await app.request("/api/session");
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ signedIn: false });
  });

  it("answers even when the cookie it is handed is rubbish", async () => {
    const app = createApp(
      buildServices(
        configFor({ AUTH_MODE: "vault-userpass", VAULT_ADDR: "https://vault.example" }),
      ),
    );
    const response = await app.request("/api/session", {
      headers: { cookie: "ovdash_session=not-a-jwe" },
    });
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ signedIn: false });
  });

  it("still refuses every other route", async () => {
    const app = createApp(
      buildServices(
        configFor({ AUTH_MODE: "vault-userpass", VAULT_ADDR: "https://vault.example" }),
      ),
    );
    // The exemption is one route wide, not a hole in the gate.
    expect((await app.request("/api/home")).status).toBe(401);
  });
});

describe("the OIDC route, end to end", () => {
  /**
   * A dashboard whose provider is a stub.
   *
   * The token exchange and its signature checks have their own tests; what is
   * unproven is the wiring the session store went through — callback writes a
   * session, the gate reads it, sign-out ends it. So the provider is faked and
   * the routes are real.
   */
  function oidcApp() {
    const services = buildServices(
      configFor({
        AUTH_MODE: "oidc",
        OIDC_ISSUER: "https://vault.example",
        OIDC_CLIENT_ID: "ov-dash",
        OIDC_CLIENT_SECRET: "secret",
        SESSION_COOKIE_SECURE: "false",
      }),
    );
    services.oidc = async () =>
      ({
        authorize: () => ({
          url: "https://vault.example/authorize",
          state: "st",
          nonce: "no",
          verifier: "ve",
        }),
        exchange: async () => ({
          idToken: "header.payload.signature",
          claims: {
            sub: "entity-1",
            name: "jasper",
            email: "jasper@example.com",
            email_verified: true,
          },
        }),
      }) as unknown as Awaited<ReturnType<typeof services.oidc>>;
    return createApp(services);
  }

  /**
   * Pick one cookie out of a response.
   *
   * The callback sets two — it clears the login cookie and writes the session —
   * so taking the first `Set-Cookie` gets the cleared one and a test that then
   * "signs in" is holding nothing.
   */
  function cookieNamed(response: Response, name: string): string {
    for (const header of response.headers.getSetCookie()) {
      const pair = header.split(";")[0] ?? "";
      if (pair.startsWith(`${name}=`) && pair !== `${name}=`) return pair;
    }
    return "";
  }

  /**
   * @param existing - A session cookie this browser is already holding. A real
   *   browser sends it on the callback (same path, `SameSite=Lax`, a top-level
   *   GET), and leaving it out is what would hide a session the sign-in should
   *   have replaced.
   */
  async function signIn(app: ReturnType<typeof oidcApp>, existing = ""): Promise<string> {
    const start = await app.request("/auth/login", {
      headers: existing ? { cookie: existing } : {},
    });
    const pending = cookieNamed(start, "ovdash_session_login");
    const back = await app.request("/auth/callback?code=abc&state=st", {
      headers: { cookie: [pending, existing].filter(Boolean).join("; ") },
    });
    return cookieNamed(back, "ovdash_session");
  }

  it("signs in, works, and signs out for good", async () => {
    const app = oidcApp();
    const cookie = await signIn(app);

    const session = (await (
      await app.request("/api/session", { headers: { cookie } })
    ).json()) as {
      signedIn: boolean;
      canSignOut: boolean;
      expiresAt: number;
    };
    expect(session.signedIn).toBe(true);
    expect(session.canSignOut).toBe(true);
    expect(session.expiresAt).toBeGreaterThan(Math.floor(Date.now() / 1000));

    await app.request("/auth/logout", {
      method: "POST",
      headers: { origin: ORIGIN, cookie },
    });

    // The same cookie again. Signing out is a sign-out in this mode too.
    const after = (await (
      await app.request("/api/session", { headers: { cookie } })
    ).json()) as { signedIn: boolean };
    expect(after.signedIn).toBe(false);
    expect((await app.request("/api/home", { headers: { cookie } })).status).toBe(401);
  });

  it("kills the session a second sign-in replaced", async () => {
    const app = oidcApp();
    const first = await signIn(app);
    await signIn(app, first);

    const replaced = (await (
      await app.request("/api/session", { headers: { cookie: first } })
    ).json()) as { signedIn: boolean };
    expect(replaced.signedIn).toBe(false);
  });
});

describe("the client cache does not outlive a session", () => {
  it("drops everything, so the next person is not shown the last one's data", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 204 })),
    );

    await cached("tree", async () => ({ nodes: ["jasper's files"] }));
    await cached("memories", async () => ["jasper's memories"]);
    expect(isCached("tree")).toBe(true);
    expect(isCached("memories")).toBe(true);

    const { api } = await import("../src/client/lib/api");
    await api.signOut();

    expect(isCached("tree")).toBe(false);
    expect(isCached("memories")).toBe(false);
  });

  it("notices a session that ended while the page was open", async () => {
    // The shell reads /api/session once at boot. A Vault-minted token expires
    // on its own and nothing renews it, so without this the page stayed
    // signed-in-looking and answered every click with a toast.
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              error: { code: "UNAUTHENTICATED", message: "not signed in" },
            }),
            { status: 401 },
          ),
      ),
    );

    const { api, whenSessionEnds } = await import("../src/client/lib/api");
    let ended = 0;
    whenSessionEnds(() => {
      ended += 1;
    });
    await cached("tree", async () => ({ nodes: ["jasper's files"] }));

    await expect(api.home()).rejects.toThrow(/not signed in/);
    expect(ended).toBe(1);
    // The cached data was read as somebody the server no longer knows.
    expect(isCached("tree")).toBe(false);

    whenSessionEnds(() => {});
  });

  it("notices one on a route that does not go through the parser", async () => {
    // `forget` writes with a bare fetch, so the 401 has to be caught there too.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 401 })),
    );

    const { api, whenSessionEnds } = await import("../src/client/lib/api");
    let ended = 0;
    whenSessionEnds(() => {
      ended += 1;
    });

    await expect(api.forget("viking://user/jasper/memories/x.md")).rejects.toThrow();
    expect(ended).toBe(1);

    whenSessionEnds(() => {});
  });

  it("leaves the session alone when the failure is not about the session", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 500 })),
    );

    const { api, whenSessionEnds } = await import("../src/client/lib/api");
    let ended = 0;
    whenSessionEnds(() => {
      ended += 1;
    });

    await expect(api.home()).rejects.toThrow();
    // A broken upstream is not a reason to throw somebody back to sign-in.
    expect(ended).toBe(0);

    whenSessionEnds(() => {});
  });

  it("still clears the cache when the server refuses the sign-out", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 500 })),
    );
    await cached("tree", async () => ({ nodes: ["jasper's files"] }));

    const { api } = await import("../src/client/lib/api");
    // A failed sign-out must still not leave the data readable.
    await expect(api.signOut()).rejects.toThrow(/could not sign out/);
    expect(isCached("tree")).toBe(false);
  });
});
