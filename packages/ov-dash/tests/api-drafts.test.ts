/**
 * Unsaved typing, and the operations that make it invalid.
 *
 * These sit on `api.ts` rather than on a component because that is where the
 * rules moved to, and why. Delete, forget and move each make a uri stop being the
 * uri it was, and each was originally handled in whichever component happened to
 * call it — so the second component, the one that forgets a memory, did not
 * handle it at all, and deleting a file left its draft parked for the life of the
 * tab: unreachable by every button, still counted by the warning on closing the
 * tab, and ready to be offered back as "Resume editing" if anything recreated the
 * uri. A rule kept in one place cannot be half-applied, and this is that place.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../src/client/lib/api";
import { invalidate } from "../src/client/lib/cache";
import { drafts } from "../src/client/lib/drafts";

const SOUL = "viking://user/jasper/memories/soul.md";
const NOTES = "viking://user/jasper/resources/notes";

/** Answer the dashboard's own API, recording what was asked. */
function stubApi(answer: (path: string, init?: RequestInit) => Response) {
  const seen: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const path = typeof input === "string" ? input : input.toString();
      seen.push(`${init?.method ?? "GET"} ${path}`);
      return answer(path, init);
    }),
  );
  return seen;
}

const noContent = () => new Response(null, { status: 204 });

const json = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

beforeEach(() => {
  drafts.clear();
  invalidate();
});

afterEach(() => {
  vi.unstubAllGlobals();
  drafts.clear();
});

describe("deleting a file", () => {
  it("forgets the unsaved typing for it", async () => {
    // Three clicks away in the UI: Edit, type, Close editor — which parks the
    // draft, and is the only route to Delete, since Delete is disabled while the
    // editor is open — then Delete.
    drafts.keep(SOUL, "half a thought", "");
    stubApi(noContent);

    await api.remove(SOUL);

    expect(drafts.has(SOUL)).toBe(false);
    expect(drafts.pending).toBe(0);
  });

  it("forgets the typing for everything inside a deleted folder", async () => {
    drafts.keep(`${NOTES}/a.md`, "aaa", "");
    drafts.keep(`${NOTES}/deep/b.md`, "bbb", "");
    drafts.keep("viking://user/jasper/resources/notes-archive/c.md", "ccc", "");
    stubApi(noContent);

    await api.remove(NOTES);

    expect(drafts.pending).toBe(1);
    // The sibling that merely shares a prefix is not inside anything.
    expect(drafts.has("viking://user/jasper/resources/notes-archive/c.md")).toBe(true);
  });

  it("keeps the typing when the delete failed", async () => {
    // The file is still there, so the draft is still about something real.
    drafts.keep(SOUL, "half a thought", "");
    stubApi(() => new Response("no", { status: 502 }));

    await expect(api.remove(SOUL)).rejects.toThrow();
    expect(drafts.has(SOUL)).toBe(true);
  });
});

describe("forgetting a memory", () => {
  it("forgets its unsaved typing too", async () => {
    // The path that had nothing: this route lives on the Memories page, which
    // never knew about drafts at all while the rule was kept in components.
    drafts.keep(SOUL, "half a thought", "");
    stubApi(noContent);

    await api.forget(SOUL);

    expect(drafts.has(SOUL)).toBe(false);
  });

  it("keeps it when the forget failed", async () => {
    drafts.keep(SOUL, "half a thought", "");
    stubApi(() => new Response("no", { status: 502 }));

    await expect(api.forget(SOUL)).rejects.toThrow();
    expect(drafts.has(SOUL)).toBe(true);
  });
});

describe("moving a file", () => {
  it("carries the unsaved typing to where it landed", async () => {
    const from = `${NOTES}/a.md`;
    const to = "viking://user/jasper/resources/kept/a.md";
    drafts.keep(from, "half a thought", "");
    stubApi(() => json({ uri: to }));

    expect(await api.move(from, "viking://user/jasper/resources/kept")).toBe(to);

    expect(drafts.has(from)).toBe(false);
    expect(drafts.open(to, "")).toBe("half a thought");
  });

  it("carries the typing for files inside a moved folder", async () => {
    const to = "viking://user/jasper/resources/kept/notes";
    drafts.keep(`${NOTES}/a.md`, "aaa", "");
    drafts.keep(`${NOTES}/deep/b.md`, "bbb", "");
    stubApi(() => json({ uri: to }));

    await api.move(NOTES, "viking://user/jasper/resources/kept");

    expect(drafts.open(`${to}/a.md`, "")).toBe("aaa");
    expect(drafts.open(`${to}/deep/b.md`, "")).toBe("bbb");
  });

  it("leaves the typing where it is when nothing moved", async () => {
    // 204 means it was already in that folder, so no uri changed.
    drafts.keep(SOUL, "half a thought", "");
    stubApi(noContent);

    await api.move(SOUL, "viking://user/jasper/memories");

    expect(drafts.open(SOUL, "")).toBe("half a thought");
  });

  it("leaves the typing where it is when the move failed", async () => {
    drafts.keep(SOUL, "half a thought", "");
    stubApi(() => new Response("no", { status: 409 }));

    await expect(api.move(SOUL, NOTES)).rejects.toThrow();
    expect(drafts.open(SOUL, "")).toBe("half a thought");
  });
});

describe("saving a file", () => {
  it("leaves the draft alone, because the pane owns that decision", async () => {
    /*
     * Deliberately not symmetrical with the three above. A save is the one
     * operation where the uri survives, so the draft is still about a real file —
     * and the person may have typed more while the write was in flight, which is
     * text nothing has stored. Only the pane knows what it sent, so the pane makes
     * that call, with `drafts.dropSaved`. Dropping it here would destroy exactly
     * the typing that guard exists to protect.
     */
    drafts.keep(SOUL, "half a thought", "");
    stubApi(() => json({ modTime: "2026-09-14T06:20:00Z", size: 24 }));

    await api.save(SOUL, "half a thought, finished");

    expect(drafts.has(SOUL)).toBe(true);
  });
});

describe("signing out", () => {
  it("clears every draft, so one person's typing cannot reach the next", async () => {
    /*
     * The same reasoning as the `invalidate()` beside it: what is held was read as
     * the person signing out. Today `Account.svelte` does a full `location.href`
     * navigation, so the Map would die with the page anyway — but that is an
     * invariant of a component, not of this function, and a client-side route would
     * leak the typing to whoever signs in next.
     *
     * It also stops the browser asking "Leave site?" on the way out about work
     * nobody can save any more, which is what the new close-warning would otherwise
     * do on every sign-out.
     */
    drafts.keep(SOUL, "half a thought", "");
    drafts.keep(`${NOTES}/a.md`, "another", "");
    stubApi(noContent);

    await api.signOut();

    expect(drafts.pending).toBe(0);
  });

  it("keeps them when the sign-out failed, because nobody is signed out", async () => {
    /*
     * The asymmetry with `invalidate()` one line above, which *is* unconditional.
     * A wrong invalidate costs a refetch; a wrong clear costs work nobody can get
     * back. And both reasons to clear hold only on success: the leak threat needs a
     * session that really ended, and the "Leave site?" suppression needs the
     * navigation that only follows a successful sign-out.
     *
     * `Account.svelte` catches a failure, toasts, and leaves the person on the
     * page — still signed in, still able to work. Clearing before the check meant a
     * 502 or a dropped connection silently destroyed their edit under a toast about
     * signing out. This test asserted that behaviour and pinned it; it pins the
     * opposite now.
     */
    drafts.keep(SOUL, "half a thought", "");
    stubApi(() => new Response("no", { status: 502 }));

    await expect(api.signOut()).rejects.toThrow();
    expect(drafts.open(SOUL, "")).toBe("half a thought");
  });
});
