import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  DEFAULT_FOLDER,
  DEFAULT_SHARED_SCOPE,
  DEFAULT_USER_SCOPE,
  loadSettings,
  saveSettings,
  scopesFor,
} from "../src/lib/settings";
import { type BrowserStub, installBrowser, removeBrowser } from "./helpers/browser-stub";

let stub: BrowserStub;

beforeEach(() => {
  stub = installBrowser();
});

afterEach(() => {
  removeBrowser();
});

describe("loading", () => {
  it("starts a clean install on the home alias", async () => {
    // `viking://~` resolves to whoever the token says you are, which is what
    // lets the extension work without knowing a user id.
    const settings = await loadSettings();
    // Not `viking://~`: that is the home root, holding memories/ and
    // sessions/, and OpenViking refuses it as an import destination outright.
    // Measured — see the integration suite.
    expect(settings.userScope).toBe("viking://~/resources");
    expect(settings.sharedScope).toBe("viking://resources");
    expect(settings.defaultFolder).toBe("clips");
    expect(settings.profile).toBe("");
  });

  it("keeps what was saved", async () => {
    await saveSettings({ profile: "lab", defaultFolder: "papers" });
    const settings = await loadSettings();
    expect(settings.profile).toBe("lab");
    expect(settings.defaultFolder).toBe("papers");
    // Untouched fields keep their defaults rather than becoming empty.
    expect(settings.userScope).toBe("viking://~/resources");
  });

  it("treats a stored value of the wrong type as absent", async () => {
    await stub.storage.local.set({ profile: 7, userScope: null });
    const settings = await loadSettings();
    expect(settings.profile).toBe("");
    expect(settings.userScope).toBe("viking://~/resources");
  });

  it("keeps a blanked folder blank, which means the scope root", async () => {
    // Not the same as never having set it: saving straight into the scope is a
    // real choice, so an empty string must survive rather than snap back to
    // the default.
    await saveSettings({ defaultFolder: "" });
    expect((await loadSettings()).defaultFolder).toBe("");
  });

  it("stores no credential", async () => {
    await saveSettings({ profile: "lab" });
    // The whole design: the token comes from ovx per save. Nothing that looks
    // like one should ever be in here.
    expect(Object.keys(stub.storage.local.dump())).not.toContain("token");
  });
});

describe("scopesFor", () => {
  const base = {
    profile: "lab",
    url: "",
    lastScope: "user",
    defaultFolder: DEFAULT_FOLDER,
  };

  it("offers both trees by default", () => {
    const scopes = scopesFor({
      ...base,
      userScope: DEFAULT_USER_SCOPE,
      sharedScope: DEFAULT_SHARED_SCOPE,
    });
    expect(scopes.map((scope) => scope.id)).toEqual(["user", "shared"]);
    expect(scopes[0]?.uri).toBe("viking://~/resources");
  });

  it("leaves out a root that was blanked", () => {
    // Which is how a deployment with nothing shared says so.
    expect(
      scopesFor({ ...base, userScope: DEFAULT_USER_SCOPE, sharedScope: "  " }).map(
        (scope) => scope.id,
      ),
    ).toEqual(["user"]);
    expect(
      scopesFor({ ...base, userScope: "", sharedScope: DEFAULT_SHARED_SCOPE }).map(
        (scope) => scope.id,
      ),
    ).toEqual(["shared"]);
  });

  it("offers nothing when both are blank, rather than a bad default", () => {
    expect(scopesFor({ ...base, userScope: "", sharedScope: "" })).toEqual([]);
  });

  it("trims what it offers, so a stray space is not part of the URI", () => {
    const scopes = scopesFor({
      ...base,
      userScope: "  viking://~/clips  ",
      sharedScope: "",
    });
    expect(scopes[0]?.uri).toBe("viking://~/clips");
  });
});
