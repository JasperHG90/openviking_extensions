/**
 * Asking ovx for a token.
 *
 * The claims worth testing are about what does *not* happen: nothing is
 * cached, nothing is stored, and a refusal comes back as ovx's own words
 * rather than as a generic failure the reader cannot act on.
 */

import { afterEach, describe, expect, it } from "vitest";
import { OvxError, fetchToken, listProfiles, ping } from "../src/lib/ovx";
import { type BrowserStub, installBrowser, removeBrowser } from "./helpers/browser-stub";

const TOKEN = "header.payload.signature";

afterEach(() => {
  removeBrowser();
});

/** Answer as a working ovx would. */
function workingOvx(): BrowserStub {
  return installBrowser(async (message) => {
    const action = (message as { action?: string }).action;
    if (action === "ping") return { ok: true, version: "1.2.3" };
    if (action === "profiles") return { ok: true, profiles: ["lab", "prod"] };
    if (action === "token") {
      return {
        ok: true,
        token: TOKEN,
        profile: (message as { profile?: string }).profile,
        url: "https://ov.example",
        account: "acme",
        user: "jasper",
      };
    }
    return { ok: false, error: "unknown action" };
  });
}

describe("reaching ovx", () => {
  it("talks to the host ovx --install-firefox-host registers", async () => {
    const stub = workingOvx();
    await ping();
    // If these disagree, Firefox reports "no such native application" and the
    // cause is invisible from either side.
    expect(stub.nativeCalls[0]?.host).toBe("ovx");
  });

  it("reports the version so the options page can say ovx is answering", async () => {
    workingOvx();
    expect(await ping()).toBe("1.2.3");
  });

  it("lists the profiles for the picker", async () => {
    workingOvx();
    expect(await listProfiles()).toEqual(["lab", "prod"]);
  });

  it("drops list entries that are not names", async () => {
    installBrowser(async () => ({ ok: true, profiles: ["lab", 7, null, "prod"] }));
    expect(await listProfiles()).toEqual(["lab", "prod"]);
  });

  it("says how to fix it when the host is not installed", async () => {
    // Firefox cannot tell "ovx missing" from "manifest missing" from "not
    // restarted yet", so the hint has to name all three.
    installBrowser(async () => {
      throw new Error("No such native application ovx");
    });
    const error = (await ping().catch((e: unknown) => e)) as OvxError;
    expect(error).toBeInstanceOf(OvxError);
    expect(error.missing).toBe(true);
    expect(error.hint).toContain("ovx --install-firefox-host");
    expect(error.hint).toContain("restart Firefox");
  });

  it("refuses an answer that is not an object", async () => {
    installBrowser(async () => "yes");
    await expect(ping()).rejects.toThrow(/unreadable/);
  });
});

describe("fetching a token", () => {
  it("passes the token and where to spend it straight through", async () => {
    workingOvx();
    expect(await fetchToken("lab")).toEqual({
      token: TOKEN,
      profile: "lab",
      url: "https://ov.example",
    });
  });

  it("takes no identity fields off the profile", async () => {
    // OpenViking reads identity from the token's claims, and this client sends
    // no identity headers, so `account` and `user` would be payload nobody
    // reads crossing a credential boundary. ovx stops sending them; this is
    // the half that stops wanting them.
    workingOvx();
    expect(Object.keys(await fetchToken("lab")).sort()).toEqual([
      "profile",
      "token",
      "url",
    ]);
  });

  it("asks for the profile it was given", async () => {
    const stub = workingOvx();
    await fetchToken("prod");
    expect(stub.nativeCalls[0]?.message).toEqual({ action: "token", profile: "prod" });
  });

  it("asks again every time rather than caching", async () => {
    // A token cached here could go stale while ovx's is fresh, and the call is
    // a local pipe — cheaper than the upload that follows it.
    const stub = workingOvx();
    await fetchToken("lab");
    await fetchToken("lab");
    expect(stub.nativeCalls).toHaveLength(2);
  });

  it("writes nothing down", async () => {
    const stub = workingOvx();
    await fetchToken("lab");
    expect(JSON.stringify(stub.storage.local.dump())).not.toContain(TOKEN);
    expect(JSON.stringify(stub.storage.session.dump())).not.toContain(TOKEN);
  });

  it("refuses to ask about no profile at all", async () => {
    const stub = workingOvx();
    await expect(fetchToken("")).rejects.toThrow(/No ovx profile/);
    expect(stub.nativeCalls).toHaveLength(0);
  });

  it("passes ovx's own words and hint through", async () => {
    // ovx knows which command fixes it; inventing worse advice here would be
    // strictly less useful.
    installBrowser(async () => ({
      ok: false,
      error: "profile 'lab' has no stored login",
      hint: "Run 'ovx --login lab' first.",
    }));
    const error = (await fetchToken("lab").catch((e: unknown) => e)) as OvxError;
    expect(error.message).toBe("profile 'lab' has no stored login");
    expect(error.hint).toBe("Run 'ovx --login lab' first.");
    // Reachable and refusing is not the same as absent, and needs different advice.
    expect(error.missing).toBe(false);
  });

  it("complains when ovx says ok but sends no token", async () => {
    installBrowser(async () => ({ ok: true, url: "https://ov.example" }));
    await expect(fetchToken("lab")).rejects.toThrow(/without a token/);
  });

  it("treats a missing ok as a refusal, not a success", async () => {
    installBrowser(async () => ({ token: TOKEN }));
    await expect(fetchToken("lab")).rejects.toThrow(OvxError);
  });
});
