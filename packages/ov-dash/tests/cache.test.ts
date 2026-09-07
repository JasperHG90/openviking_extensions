/**
 * The client cache.
 *
 * It holds one person's files and memories in a tab, so the rules about when
 * it stops holding them are the ones worth pinning down.
 */

import { afterEach, describe, expect, it } from "vitest";
import { TTL, cached, invalidate, isCached } from "../src/client/lib/cache";

afterEach(() => invalidate());

describe("caching", () => {
  it("serves a second caller without fetching twice", async () => {
    let calls = 0;
    const load = async () => {
      calls += 1;
      return "value";
    };
    expect(await cached("k", load)).toBe("value");
    expect(await cached("k", load)).toBe("value");
    expect(calls).toBe(1);
  });

  it("shares one request between callers that arrive together", async () => {
    let calls = 0;
    const load = async () => {
      calls += 1;
      await new Promise((r) => setTimeout(r, 20));
      return calls;
    };
    const [a, b] = await Promise.all([cached("k", load), cached("k", load)]);
    expect(calls).toBe(1);
    expect(a).toBe(b);
  });

  it("does not cache a failure", async () => {
    await expect(
      cached("k", async () => {
        throw new Error("upstream down");
      }),
    ).rejects.toThrow("upstream down");
    expect(isCached("k")).toBe(false);
  });

  it("expires an entry once its TTL has passed", async () => {
    await cached("k", async () => "old", 1);
    await new Promise((r) => setTimeout(r, 5));
    expect(isCached("k", 1)).toBe(false);
    expect(TTL).toBeGreaterThan(0);
  });

  it("clears only the prefix it was given", async () => {
    await cached("file:a", async () => 1);
    await cached("tree", async () => 2);
    invalidate("file:");
    expect(isCached("file:a")).toBe(false);
    expect(isCached("tree")).toBe(true);
  });

  it("does not let a load in flight resurrect what was just invalidated", async () => {
    // Deleting a memory while the list is loading used to put the deleted one
    // back for a full TTL, because the older request resolved afterwards.
    let release: (v: string) => void = () => {};
    const slow = new Promise<string>((r) => {
      release = r;
    });

    const inFlight = cached("memories", () => slow);
    invalidate("memories");
    release("the stale list");

    await expect(inFlight).resolves.toBe("the stale list");
    expect(isCached("memories")).toBe(false);
  });
});
