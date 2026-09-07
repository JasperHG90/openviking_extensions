import { describe, expect, it } from "vitest";
import { loadConfig } from "../src/server/env";
import { OidcClient, OidcError, viewerFromClaims } from "../src/server/oidc";

const config = loadConfig({
  OV_URL: "http://openviking:1933",
  SESSION_SECRET: "a".repeat(32),
  AUTH_MODE: "oidc",
  OIDC_ISSUER: "https://vault.example/v1/identity/oidc/provider/default",
  OIDC_CLIENT_ID: "ov-dash",
  OIDC_CLIENT_SECRET: "secret",
  KEY_SOURCE: "env",
  OV_API_KEY: "ov_test",
  PUBLIC_ORIGIN: "https://dash.example",
});

const DISCOVERY = {
  issuer: config.OIDC_ISSUER,
  authorization_endpoint:
    "https://vault.example/ui/vault/identity/oidc/provider/default/authorize",
  token_endpoint: "https://vault.example/v1/identity/oidc/provider/default/token",
  jwks_uri: "https://vault.example/v1/identity/oidc/provider/default/.well-known/keys",
  grant_types_supported: ["authorization_code"],
};

function withFetch(handler: typeof globalThis.fetch, run: () => Promise<void>) {
  const original = globalThis.fetch;
  globalThis.fetch = handler;
  return run().finally(() => {
    globalThis.fetch = original;
  });
}

describe("discovery", () => {
  it("reads the provider's document", async () => {
    await withFetch(
      (async () => new Response(JSON.stringify(DISCOVERY))) as typeof fetch,
      async () => {
        const client = await OidcClient.discover(config);
        expect(client.discovery.token_endpoint).toBe(DISCOVERY.token_endpoint);
      },
    );
  });

  it("refuses a provider that cannot do the authorization-code grant", async () => {
    await withFetch(
      (async () =>
        new Response(
          JSON.stringify({ ...DISCOVERY, grant_types_supported: ["implicit"] }),
        )) as typeof fetch,
      async () => {
        await expect(OidcClient.discover(config)).rejects.toThrow(
          /does not advertise the authorization_code grant/,
        );
      },
    );
  });

  it("reports a discovery document that is missing endpoints", async () => {
    await withFetch(
      (async () => new Response(JSON.stringify({ issuer: "x" }))) as typeof fetch,
      async () => {
        await expect(OidcClient.discover(config)).rejects.toThrow(/missing required/);
      },
    );
  });
});

describe("authorize", () => {
  it("sends PKCE, state and nonce, and a fresh set each time", async () => {
    await withFetch(
      (async () => new Response(JSON.stringify(DISCOVERY))) as typeof fetch,
      async () => {
        const client = await OidcClient.discover(config);
        const first = client.authorize();
        const second = client.authorize();

        const url = new URL(first.url);
        expect(url.searchParams.get("response_type")).toBe("code");
        expect(url.searchParams.get("code_challenge_method")).toBe("S256");
        expect(url.searchParams.get("code_challenge")).toBeTruthy();
        expect(url.searchParams.get("redirect_uri")).toBe(
          "https://dash.example/auth/callback",
        );
        expect(url.searchParams.get("state")).toBe(first.state);
        expect(url.searchParams.get("nonce")).toBe(first.nonce);

        // Two logins in two tabs must not share a verifier or a state.
        expect(second.state).not.toBe(first.state);
        expect(second.verifier).not.toBe(first.verifier);
        expect(second.nonce).not.toBe(first.nonce);
      },
    );
  });

  it("never puts the verifier in the URL", async () => {
    await withFetch(
      (async () => new Response(JSON.stringify(DISCOVERY))) as typeof fetch,
      async () => {
        const client = await OidcClient.discover(config);
        const { url, verifier } = client.authorize();
        expect(url).not.toContain(verifier);
      },
    );
  });
});

describe("viewerFromClaims", () => {
  it("takes the local part of the email by default", () => {
    const viewer = viewerFromClaims(config, {
      sub: "entity-id-8f2a",
      email: "jasper@example.com",
      email_verified: true,
    });
    expect(viewer.user).toBe("jasper");
    expect(viewer.account).toBe("jasper");
    // Vault's sub is an entity id, so it must not become the OpenViking user.
    expect(viewer.user).not.toBe("entity-id-8f2a");
  });

  it("honours an explicit account override", () => {
    const scoped = loadConfig({
      OV_URL: "http://openviking:1933",
      SESSION_SECRET: "a".repeat(32),
      AUTH_MODE: "dev",
      KEY_SOURCE: "env",
      OV_API_KEY: "k",
      OV_ACCOUNT: "lab",
    });
    const viewer = viewerFromClaims(scoped, {
      sub: "s",
      email: "jasper@example.com",
      email_verified: true,
    });
    expect(viewer.account).toBe("lab");
    expect(viewer.user).toBe("jasper");
  });

  it("can map from sub or preferred_username instead", () => {
    const bySub = loadConfig({
      OV_URL: "http://o:1933",
      SESSION_SECRET: "a".repeat(32),
      AUTH_MODE: "dev",
      KEY_SOURCE: "env",
      OV_API_KEY: "k",
      IDENTITY_FROM: "sub",
    });
    expect(viewerFromClaims(bySub, { sub: "abc", email: "x@y.z" }).user).toBe("abc");

    const byName = loadConfig({
      OV_URL: "http://o:1933",
      SESSION_SECRET: "a".repeat(32),
      AUTH_MODE: "dev",
      KEY_SOURCE: "env",
      OV_API_KEY: "k",
      IDENTITY_FROM: "preferred_username",
    });
    expect(
      viewerFromClaims(byName, { sub: "s", preferred_username: "jg", email: "" }).user,
    ).toBe("jg");
  });

  it("refuses rather than guessing when the claim is missing", () => {
    // email_verified is set so the verification gate passes and the empty
    // address is what this actually tests.
    const claims = { sub: "entity-id", email: "", email_verified: true };
    expect(() => viewerFromClaims(config, claims)).toThrow(OidcError);
    expect(() => viewerFromClaims(config, claims)).toThrow(/no usable email-local claim/);
  });
});

describe("the discovery document does not get to vouch for itself", () => {
  it("refuses a document declaring a different issuer", async () => {
    // Whatever answers the discovery fetch could otherwise name its own issuer
    // and its own JWKS, and every later check would validate against values it
    // chose — including the email claim that picks which Vault key is handed out.
    await withFetch(
      (async () =>
        new Response(
          JSON.stringify({ ...DISCOVERY, issuer: "https://evil.example" }),
        )) as typeof fetch,
      async () => {
        await expect(OidcClient.discover(config)).rejects.toThrow(
          /declares issuer https:\/\/evil\.example/,
        );
      },
    );
  });

  it("tolerates a trailing slash on either side", async () => {
    await withFetch(
      (async () =>
        new Response(
          JSON.stringify({ ...DISCOVERY, issuer: `${config.OIDC_ISSUER}/` }),
        )) as typeof fetch,
      async () => {
        await expect(OidcClient.discover(config)).resolves.toBeDefined();
      },
    );
  });
});
