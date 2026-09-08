/**
 * The session store.
 *
 * This is what makes signing out mean something, so the properties worth
 * pinning are the ones a sign-out depends on: an entry goes when it is dropped,
 * an entry goes when its time is up, and two sessions are never the same
 * session. Expiry in particular had no test at all — the cookie's own `exp`
 * happens to refuse a stale id first, so both halves of the store's expiry
 * could be deleted with every other test still passing.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionStore } from "../src/server/store";
import type { Viewer } from "../src/shared/schemas";

const VIEWER: Viewer = {
  sub: "jasper",
  name: "jasper",
  email: "",
  account: "lab",
  user: "jasper",
};

function inSeconds(seconds: number): number {
  return Math.floor(Date.now() / 1000) + seconds;
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("holding a session", () => {
  it("gives back what it was given", () => {
    const store = new SessionStore();
    const id = store.create(VIEWER, inSeconds(60), "ov_token");
    expect(store.read(id)).toEqual({
      viewer: VIEWER,
      token: "ov_token",
      expiresAt: inSeconds(60),
    });
  });

  it("holds a session with no credential, for the modes that have none", () => {
    const store = new SessionStore();
    const id = store.create(VIEWER, inSeconds(60));
    expect(store.read(id)?.token).toBeUndefined();
  });

  it("never issues the same id twice", () => {
    const store = new SessionStore();
    const ids = new Set(
      Array.from({ length: 500 }, () => store.create(VIEWER, inSeconds(60))),
    );
    expect(ids.size).toBe(500);
    // Long enough that a truncated or low-entropy id would show up as a clash.
    for (const id of ids) expect(id.length).toBeGreaterThanOrEqual(43);
  });

  it("knows nothing about an id it never issued", () => {
    expect(new SessionStore().read("not-an-id")).toBeNull();
  });
});

describe("ending a session", () => {
  it("forgets it when it is dropped", () => {
    const store = new SessionStore();
    const id = store.create(VIEWER, inSeconds(60));
    store.drop(id);
    expect(store.read(id)).toBeNull();
  });

  it("drops one without touching the rest", () => {
    const store = new SessionStore();
    const mine = store.create(VIEWER, inSeconds(60));
    const theirs = store.create(VIEWER, inSeconds(60));
    store.drop(mine);
    expect(store.read(mine)).toBeNull();
    expect(store.read(theirs)).not.toBeNull();
  });

  it("does not mind being dropped twice, or dropped for nothing", () => {
    const store = new SessionStore();
    const id = store.create(VIEWER, inSeconds(60));
    store.drop(id);
    expect(() => {
      store.drop(id);
      store.drop("never-existed");
    }).not.toThrow();
  });
});

describe("running out of time", () => {
  it("refuses a session the moment it expires, not a second later", () => {
    const store = new SessionStore();
    const id = store.create(VIEWER, inSeconds(60));

    vi.advanceTimersByTime(59_000);
    expect(store.read(id)).not.toBeNull();

    // At the boundary, not past it: `expiresAt` is when it has ended.
    vi.advanceTimersByTime(1_000);
    expect(store.read(id)).toBeNull();
  });

  it("refuses an expired session inside the sweep's blind spot", () => {
    // The sweep only walks the map once a minute, so between walks the entry is
    // still sitting there and `read` alone has to refuse it. Without this, the
    // per-entry check could be deleted and every other test would still pass —
    // the sweep would be quietly doing its job for it.
    const store = new SessionStore();
    const id = store.create(VIEWER, inSeconds(5));

    vi.advanceTimersByTime(6_000);
    expect(store.read(id)).toBeNull();
  });

  it("stops holding an expired session in memory", () => {
    const store = new SessionStore();
    store.create(VIEWER, inSeconds(60));
    store.create(VIEWER, inSeconds(7200));
    expect(store.size()).toBe(2);

    vi.advanceTimersByTime(61_000);
    // The long one survives; the short one is not merely refused but forgotten.
    expect(store.size()).toBe(1);
  });

  it("sweeps without being written to", () => {
    // Nothing signs in for hours. The expired entries still have to go: each
    // holds an OpenViking token, and a token nobody can reach is still a token.
    const store = new SessionStore();
    const id = store.create(VIEWER, inSeconds(60));
    vi.advanceTimersByTime(3_600_000);

    store.read(id);
    expect(store.size()).toBe(0);
  });
});

describe("when too many pile up", () => {
  it("keeps the newest and says so", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const store = new SessionStore();

    // One past the cap. The oldest goes; a store that grew without limit would
    // hold every one of these, each with a credential in it.
    const ids = Array.from({ length: 10_001 }, () =>
      store.create(VIEWER, inSeconds(3600)),
    );

    expect(store.size()).toBe(10_000);
    expect(store.read(ids[0] as string)).toBeNull();
    expect(store.read(ids.at(-1) as string)).not.toBeNull();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("session store full"));
  });

  it("throws away the expired before it throws away anybody live", () => {
    const store = new SessionStore();
    store.create(VIEWER, inSeconds(60));
    vi.advanceTimersByTime(61_000);

    const live = store.create(VIEWER, inSeconds(3600));
    // The dead one made room; nobody was signed out to fit the new one in.
    expect(store.size()).toBe(1);
    expect(store.read(live)).not.toBeNull();
  });
});
