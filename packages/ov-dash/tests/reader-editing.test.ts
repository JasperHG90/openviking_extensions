/**
 * @vitest-environment jsdom
 */

/**
 * The reading pane's editor, driven as a component.
 *
 * These exist because of what the module tests could not see. `DraftBook` was
 * correct in isolation every time; three defects in a row lived in how the Reader
 * *called* it — a book held per component instance so every route change threw the
 * drafts away, a save that dropped by whatever was open rather than by the file it
 * saved, and a `changed` flag measured against text the person had never seen. All
 * three passed 365 node tests.
 *
 * So this file is deliberately narrow: the wiring between the component and the
 * modules under it, and nothing about typography or layout.
 */

import { cleanup, render } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Reader from "../src/client/lib/Reader.svelte";
import { drafts } from "../src/client/lib/drafts";
import type { FileDetail, Saved } from "../src/shared/schemas";

const SOUL = "viking://user/jasper/memories/soul.md";
const PROFILE = "viking://user/jasper/memories/profile.md";

/**
 * One file as `/api/file` answers for it.
 *
 * @param at - Its stamp. Passed explicitly when a test needs to play the parent's
 *   re-read, which is what hands the pane a version reflecting a write.
 */
function fileDetail(
  uri: string,
  content: string,
  at: { modTime: string; size: number } = {
    modTime: "2026-09-13T10:00:00Z",
    size: content.length,
  },
): FileDetail {
  const name = uri.split("/").pop() ?? uri;
  return {
    node: {
      uri,
      name,
      isDir: false,
      size: at.size,
      modTime: at.modTime,
      kind: "MD",
      relPath: name,
    },
    content,
    abstract: "",
    folderSummary: "",
    binary: false,
  };
}

/** The buttons the pane draws, by their visible words. */
function button(label: string): HTMLButtonElement {
  const found = [...document.querySelectorAll("button")].find((element) =>
    element.textContent?.trim().startsWith(label),
  );
  if (!found) {
    const seen = [...document.querySelectorAll("button")]
      .map((element) => element.textContent?.trim())
      .join(" | ");
    throw new Error(`no button starting with "${label}". Saw: ${seen}`);
  }
  return found as HTMLButtonElement;
}

function editor(): HTMLTextAreaElement {
  const found = document.querySelector("textarea");
  if (!found) throw new Error("the editor is not open");
  return found as HTMLTextAreaElement;
}

/** Type into the editor the way a person does, so `bind:value` sees it. */
function type(text: string): void {
  const box = editor();
  box.value = text;
  box.dispatchEvent(new Event("input", { bubbles: true }));
}

/** Let Svelte flush the effects a click or a keystroke queued. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

/**
 * Everything on screen as one line of text.
 *
 * Collapsed, because the markup wraps its prose and a sentence split across two
 * source lines carries the newline and the indentation into `textContent` — so a
 * plain `toContain` on a sentence fails for reasons that have nothing to do with
 * the sentence.
 */
const onScreen = () => (document.body.textContent ?? "").replace(/\s+/g, " ");

/**
 * A stand-in for the server that holds one file and enforces the real contract.
 *
 * Faithful on the one rule that matters: a save is refused when the stamp it
 * carries does not match what is stored, and *not* when a stamp is merely present.
 * The first version of these fakes threw on any truthy `seen`, which made every
 * "did it send the right stamp" assertion unfalsifiable — a stale stamp and a
 * correct one produced the same 409, and a real defect survived a whole round of
 * review because of it.
 *
 * @param content - What the file holds to begin with.
 * @param at - Its stamp to begin with, matching what `fileDetail` reports.
 */
async function fakeServer(
  content: string,
  at = { modTime: "2026-09-13T10:00:00Z", size: 8 },
) {
  const state = { content, stamp: { ...at } };
  const attempts: { content: string; seen: unknown }[] = [];
  /** Set to have the post-write stat "fail", the way a busy cluster can. */
  let statFails = false;
  /** Held open when set, so the window between click and answer can be typed into. */
  let gate: Promise<void> | null = null;
  let release: () => void = () => {};
  let tick = 0;

  const save = async (_uri: string, text: string, seen?: Saved) => {
    attempts.push({ content: text, seen });
    if (gate) await gate;
    if (
      seen &&
      (seen.modTime !== state.stamp.modTime || seen.size !== state.stamp.size)
    ) {
      const { ApiError } = await import("../src/client/lib/api");
      throw new ApiError(
        "soul.md has changed since you opened it — something else wrote to it",
        "STALE_EDIT",
        409,
      );
    }
    tick += 1;
    state.content = text;
    state.stamp = { modTime: `2026-09-14T09:00:0${tick}Z`, size: text.length };
    // An empty modTime is what the route answers when its post-write stat threw.
    return statFails ? { modTime: "", size: 0 } : { ...state.stamp };
  };

  vi.spyOn(await import("../src/client/lib/api"), "api", "get").mockReturnValue({
    save,
    refresh: () => {},
    // biome-ignore lint/suspicious/noExplicitAny: only `save` is reached here.
  } as any);

  return {
    attempts,
    state,
    /** Somebody else writes the file, the way an agent or a cron does. */
    elseWrites(text: string) {
      tick += 1;
      state.content = text;
      state.stamp = { modTime: `2026-09-14T10:00:0${tick}Z`, size: text.length };
    },
    failTheStat() {
      statFails = true;
    },
    hold() {
      gate = new Promise<void>((resolve) => {
        release = resolve;
      });
    },
    release: () => release(),
  };
}

beforeEach(() => {
  // The book is module state shared with every test in the process, so each case
  // starts from an empty one. `clear()` rather than dropping a hardcoded pair of
  // uris, which is what this did first: it read as a reset and was not one, and
  // the moment a case parked under a third uri — the PNG, or the resource named
  // `memories` — it would have leaked into every case after it.
  drafts.clear();
});

afterEach(() => {
  cleanup();
  drafts.clear();
  vi.restoreAllMocks();
});

describe("opening and leaving the editor", () => {
  it("parks the typing under the file that was being edited", async () => {
    const { rerender } = render(Reader, { detail: fileDetail(SOUL, "Be kind.") });

    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();

    // The selection moves, the way clicking the next row in the tree moves it.
    await rerender({ detail: fileDetail(PROFILE, "A profile.") });

    expect(drafts.has(SOUL)).toBe(true);
    expect(drafts.open(SOUL, "Be kind.")).toBe("Be kind, and brief.");
    // And not under the file that is now open.
    expect(drafts.has(PROFILE)).toBe(false);
  });

  it("offers the parked typing back, and says so on the button", async () => {
    drafts.keep(SOUL, "Be kind, and brief.", "Be kind.");
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });

    expect(button("Resume editing")).toBeTruthy();
    button("Resume editing").click();
    await settle();

    expect(editor().value).toBe("Be kind, and brief.");
  });

  it("keeps the typing when Escape closes the editor", async () => {
    // Escape used to discard in one keystroke, with no question, while Delete two
    // buttons along asked twice.
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();

    editor().dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
    await settle();

    expect(document.querySelector("textarea")).toBeNull();
    expect(drafts.open(SOUL, "Be kind.")).toBe("Be kind, and brief.");
  });

  it("throws the typing away only after a second click", async () => {
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();

    button("Discard").click();
    await settle();
    // Still there after the first click: that one only asks the question.
    expect(editor().value).toBe("Be kind, and brief.");

    button("Yes, discard").click();
    await settle();
    expect(document.querySelector("textarea")).toBeNull();
    expect(drafts.has(SOUL)).toBe(false);
  });
});

describe("what counts as changed", () => {
  it("is measured against the text the editor opened on", async () => {
    /*
     * The defect this pins: `changed` read `detail.content`, which can be
     * replaced under an open editor — a reread landing, or an agent writing the
     * file. Against that, an untouched editor lit the Save button and printed
     * "Unsaved changes", inviting the lost update the README says is out of scope.
     */
    const { rerender } = render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();

    expect(button("Save").disabled).toBe(true);
    expect(onScreen()).toContain("No changes yet");

    // Somebody else's write arrives while the editor sits untouched.
    await rerender({ detail: fileDetail(SOUL, "Be kind, says the agent.") });
    await settle();

    expect(editor().value).toBe("Be kind.");
    expect(button("Save").disabled).toBe(true);
    expect(onScreen()).toContain("No changes yet");
  });

  it("enables Save as soon as the draft differs from what was opened", async () => {
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();

    expect(button("Save").disabled).toBe(false);
    expect(onScreen()).toContain("Unsaved changes");
  });
});

describe("saving", () => {
  it("drops the draft for the file it saved, not for whatever is open now", async () => {
    /*
     * A save is several awaits long — OpenViking's memory path writes, re-renders
     * the folder's templated overview, and enqueues an embedding — so clicking
     * another row before it lands is ordinary. `discard()` read the live `editing`,
     * which the selection change had already cleared, so the drop never happened:
     * the saved file stayed flagged as unsaved work forever.
     */
    const server = await fakeServer("Be kind.");
    server.hold();

    const { rerender } = render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();

    button("Save").click();
    await settle();
    // The selection moves while the write is still in flight.
    await rerender({ detail: fileDetail(PROFILE, "A profile.") });
    server.release();
    await settle();
    await settle();

    expect(drafts.has(SOUL)).toBe(false);
    expect(drafts.pending).toBe(0);
    expect(server.attempts.map((a) => a.content)).toEqual(["Be kind, and brief."]);
  });
});

describe("typing while a save is in flight", () => {
  it("keeps what was typed after the click, rather than clearing it", async () => {
    /*
     * The write is not instant — OpenViking's memory path writes, re-renders the
     * folder's templated overview, and enqueues an embedding — and the box stays
     * readable throughout, so typing on after clicking Save is ordinary. The first
     * version cleared `draft` on the way back whenever the file still matched, so
     * everything typed in that window went, under a toast saying "Saved".
     */
    const server = await fakeServer("Be kind.");
    server.hold();
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();

    button("Save").click();
    await settle();
    type("Be kind, and brief. And warm.");
    await settle();

    server.release();
    await settle();
    await settle();

    // Still open, still holding the newer text, and honest about it.
    expect(editor().value).toBe("Be kind, and brief. And warm.");
    expect(onScreen()).toContain("Unsaved changes");
  });

  it("measures further changes against what was actually stored", async () => {
    // Undoing back to the text the save sent is no longer a change: that version
    // is on disk now.
    const server = await fakeServer("Be kind.");
    server.hold();
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();

    button("Save").click();
    await settle();
    type("Be kind, and brief. And warm.");
    await settle();
    server.release();
    await settle();
    await settle();

    type("Be kind, and brief.");
    await settle();
    expect(onScreen()).toContain("No changes yet");
  });

  it("cannot have its editor reopened under it, because Edit is disabled", async () => {
    /*
     * The case that makes a session counter unnecessary. I added one on the theory
     * that leaving the file and coming back mid-write gave the second editor the
     * same uri as the first, so a uri guard would close it — but `busy` covers
     * `saving === openUri`, and every control including Edit is disabled by it. The
     * editor cannot be reopened while its own save is in flight, so the uri guard
     * is enough, and this is the assertion that says why.
     */
    const server = await fakeServer("Be kind.");
    server.hold();
    const { rerender } = render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();
    button("Save").click();
    await settle();

    await rerender({ detail: fileDetail(PROFILE, "A profile.") });
    await rerender({ detail: fileDetail(SOUL, "Be kind.") });

    // Parked on the way out, so the button offers it back — and refuses to act
    // until the write it is waiting on has landed.
    expect(button("Resume editing").disabled).toBe(true);

    server.release();
    await settle();
    await settle();

    // The save dropped it, so there is nothing left to resume.
    expect(drafts.has(SOUL)).toBe(false);
    expect(button("Edit").disabled).toBe(false);
  });
});

describe("leaving the page with an editor open", () => {
  it("parks the typing on unmount, not only when the selection moves", async () => {
    /*
     * The other exit, and the more common one. `App.svelte` swaps the whole view
     * on every nav click, so the pane is unmounted rather than handed a different
     * file — and the effect that parks on a selection change does not run. Making
     * the book a module singleton saved drafts that were *already* parked; the
     * state somebody is actually in when they click the nav is "typed, not
     * parked", and that died silently.
     */
    const { unmount } = render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();

    expect(drafts.has(SOUL)).toBe(false);
    unmount();
    await settle();

    expect(drafts.open(SOUL, "Be kind.")).toBe("Be kind, and brief.");
  });

  it("parks nothing on unmount when the editor was untouched", async () => {
    const { unmount } = render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    unmount();
    await settle();

    expect(drafts.pending).toBe(0);
  });

  it("keeps typing added after Save when the page is left before it lands", async () => {
    /*
     * The two guards fighting each other. The editor's copy was protected by
     * comparing against what was sent — but leaving parks the newer text, and the
     * save then resolved and dropped the parked entry unconditionally, deleting
     * text that had never been sent anywhere, under a toast saying "Saved".
     */
    const server = await fakeServer("Be kind.");
    server.hold();
    const { rerender } = render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();

    button("Save").click();
    await settle();
    type("Be kind, and brief. And warm.");
    await settle();

    // Away to another file while the write is still in flight.
    await rerender({ detail: fileDetail(PROFILE, "A profile.") });
    await settle();
    server.release();
    await settle();
    await settle();

    expect(drafts.open(SOUL, "Be kind.")).toBe("Be kind, and brief. And warm.");
  });

  it("forgets the parked draft when it is exactly what was saved", async () => {
    // The other half of the same rule: a save may throw away what it stored. This
    // is the case where leaving parks text identical to what went up.
    const server = await fakeServer("Be kind.");
    server.hold();
    const { rerender } = render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();

    button("Save").click();
    await settle();
    await rerender({ detail: fileDetail(PROFILE, "A profile.") });
    await settle();
    server.release();
    await settle();
    await settle();

    expect(drafts.has(SOUL)).toBe(false);
  });
});

describe("warning before the tab closes", () => {
  /** Whether a beforeunload now asks the browser to stop. */
  function wouldWarn(): boolean {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  }

  it("does not warn when there is nothing unsaved", async () => {
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    await settle();
    expect(wouldWarn()).toBe(false);

    // An editor opened and not typed into is not unsaved work either.
    button("Edit").click();
    await settle();
    expect(wouldWarn()).toBe(false);
  });

  it("warns while an editor holds typing", async () => {
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();

    expect(wouldWarn()).toBe(true);
  });

  it("still warns once the typing is parked on another file", async () => {
    // The drafts outlive the editor and the page; closing the tab is the one exit
    // that really loses them, which is the whole reason this warning exists.
    const { rerender } = render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();
    await rerender({ detail: fileDetail(PROFILE, "A profile.") });
    await settle();

    expect(drafts.has(SOUL)).toBe(true);
    expect(wouldWarn()).toBe(true);
  });

  it("stops warning once the typing is discarded", async () => {
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();
    button("Discard").click();
    await settle();
    button("Yes, discard").click();
    await settle();

    expect(wouldWarn()).toBe(false);
  });
});

describe("a second file while the first is still writing", () => {
  it("shows Save as disabled rather than live and dead", async () => {
    /*
     * `saving` is one slot, so `save()` returns early while it is occupied — and
     * `busy` only covers `saving === openUri`. On a *second* file the button was
     * therefore enabled and inert at once: it looked clickable, the click was
     * swallowed, and nothing happened at all — no request, no toast, no change.
     *
     * The describe button gets away with one slot because a second describe would
     * be refused upstream anyway. Two files can be written concurrently without
     * conflict, so that argument does not carry here, and the honest answer is to
     * say the pane is busy.
     */
    const server = await fakeServer("Be kind.");
    server.hold();
    const { rerender } = render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();
    button("Save").click();
    await settle();

    // Over to another file, which is a supported move: the draft is parked and
    // the editor reopens on demand.
    await rerender({ detail: fileDetail(PROFILE, "A profile.") });
    button("Edit").click();
    await settle();
    type("A profile, improved.");
    await settle();

    expect(button("Save").disabled).toBe(true);

    server.release();
    await settle();
    await settle();

    // And it comes back the moment the other file's write lands.
    expect(button("Save").disabled).toBe(false);
  });
});

describe("when somebody else wrote the file first", () => {
  it("sends the version it opened on, so the server can compare", async () => {
    const server = await fakeServer("Be kind.");
    server.elseWrites("somebody else got here first");
    const attempts = server.attempts;
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();
    button("Save").click();
    await settle();

    expect(attempts[0]?.seen).toEqual({ modTime: "2026-09-13T10:00:00Z", size: 8 });
  });

  it("says so in the editor rather than in a toast, and offers a way out", async () => {
    /*
     * A toast is gone in seconds and offers nothing to press. This is the one
     * refusal in the pane where both versions exist and somebody has to choose,
     * so it stays on screen with the two choices attached.
     */
    const server = await fakeServer("Be kind.");
    server.elseWrites("somebody else got here first");
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();
    button("Save").click();
    await settle();

    expect(onScreen()).toContain("has changed since you opened it");
    // The editor is still open, holding the text, so nothing has been lost.
    expect(editor().value).toBe("Be kind, and brief.");
    expect(button("Overwrite theirs")).toBeTruthy();
    expect(button("Open theirs, keep mine")).toBeTruthy();
  });

  it("overwrites on demand by dropping the stamp, not by ignoring the answer", async () => {
    const server = await fakeServer("Be kind.");
    server.elseWrites("somebody else got here first");
    const attempts = server.attempts;
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();
    button("Save").click();
    await settle();

    button("Overwrite theirs").click();
    await settle();
    await settle();

    expect(attempts).toHaveLength(2);
    expect(attempts[1]?.seen).toBeUndefined();
    expect(attempts[1]?.content).toBe("Be kind, and brief.");
    // Saved, so the editor closed and the clash is gone with it.
    expect(document.querySelector("textarea")).toBeNull();
    expect(onScreen()).not.toContain("has changed since you opened it");
  });

  it("keeps your text parked when you choose to look at theirs", async () => {
    // The choice has to be reversible: "Open theirs" must not be a way to lose
    // what you wrote, so it parks rather than discards.
    const server = await fakeServer("Be kind.");
    server.elseWrites("somebody else got here first");
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("Be kind, and brief.");
    await settle();
    button("Save").click();
    await settle();

    button("Open theirs, keep mine").click();
    await settle();

    expect(document.querySelector("textarea")).toBeNull();
    expect(drafts.open(SOUL, "")).toBe("Be kind, and brief.");
    expect(button("Resume editing")).toBeTruthy();
  });
});

describe("saving twice from one editor", () => {
  it("reopens on the version the re-read brought back, and saves cleanly", async () => {
    /*
     * The real sequence after a save: the parent nulls `detail`, re-reads, and
     * hands back the stored version — so reopening the editor takes its stamp from
     * a `detail` that already reflects the write. Nothing needs to be remembered
     * across the two sessions, and an earlier version that *did* remember one was
     * a liability: it outranked a fresher `detail` forever and made a file
     * permanently unsaveable once anything else wrote it.
     *
     * The rerender here is not scene-setting. It is the step that makes this the
     * real state rather than a state only a test can produce.
     */
    const server = await fakeServer("Be kind.");
    const attempts = server.attempts;
    const { rerender } = render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();

    type("Be kind, and brief.");
    await settle();
    button("Save").click();
    await settle();
    await settle();

    // What the parent's re-read brings back.
    await rerender({
      detail: fileDetail(SOUL, server.state.content, server.state.stamp),
    });
    button("Edit").click();
    await settle();
    type("Be kind, and brief. And warm.");
    await settle();
    button("Save").click();
    await settle();
    await settle();

    expect(attempts).toHaveLength(2);
    // The first carried what the file was when the pane loaded it.
    expect(attempts[0]?.seen).toEqual({ modTime: "2026-09-13T10:00:00Z", size: 8 });
    // The second carries what the re-read reported, which is the first save's work.
    expect(attempts[1]?.seen).toEqual({
      modTime: "2026-09-14T09:00:01Z",
      size: "Be kind, and brief.".length,
    });
    // Accepted, which is the point: the guard did not fire on its own user.
    expect(server.state.content).toBe("Be kind, and brief. And warm.");
  });

  it("does not hold a stamp that outlives the read it describes", async () => {
    /*
     * The defect this replaced, and it was unescapable. The pane kept the stamp
     * its own save produced, keyed by uri and never cleared, and preferred it over
     * `detail`. So: save the file, let an agent write it, let the pane re-read —
     * the editor now shows *their* text, and saving it sent the stamp from this
     * pane's older write. 409. "Open theirs, keep mine" re-read and led straight
     * back to the same 409, so the only exit was to overwrite the very text the
     * guard existed to protect.
     */
    const server = await fakeServer("Be kind.");
    const attempts = server.attempts;
    const { rerender } = render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("mine");
    await settle();
    button("Save").click();
    await settle();
    await settle();

    // An agent writes it, and the pane re-reads and shows their words.
    server.elseWrites("the agent's version");
    await rerender({
      detail: fileDetail(SOUL, server.state.content, server.state.stamp),
    });

    button("Edit").click();
    await settle();
    type("the agent's version, extended");
    await settle();
    button("Save").click();
    await settle();
    await settle();

    // Editing what is on screen saves what is on screen. No clash, because there
    // is none: this text was opened after their write, not before it.
    expect(onScreen()).not.toContain("has changed since you opened it");
    expect(attempts.at(-1)?.seen).toEqual(server.attempts.at(-1)?.seen);
    expect(server.state.content).toBe("the agent's version, extended");
  });

  it("carries it forward even when the editor stayed open through the save", async () => {
    // Typing on through a save keeps the editor open, and the next Save from that
    // same editor still has to compare against what was just written.
    const server = await fakeServer("Be kind.");
    const attempts = server.attempts;
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("one");
    await settle();
    button("Save").click();
    // No settle between: the second keystroke lands while the save is resolving.
    type("one and two");
    await settle();
    await settle();

    button("Save").click();
    await settle();
    await settle();

    expect(attempts).toHaveLength(2);
    expect(attempts[1]?.seen).toEqual({ modTime: "2026-09-14T09:00:01Z", size: 3 });
  });
});

describe("when the server cannot say what the file became", () => {
  it("stops guarding rather than guarding against a stamp it knows is wrong", async () => {
    /*
     * The route stats the file after writing it, and that stat can fail — lock
     * contention or a 5xx right after a write is exactly when it would. It answers
     * an empty stamp, which means "I do not know what this file is now".
     *
     * Keeping the pre-write stamp in that case looks safe and is not: the write
     * certainly changed the file, so the old stamp is *knowably* wrong, and every
     * later save from this editor would be refused as a clash with its own work
     * until somebody pressed Overwrite. No stamp costs one unguarded save; a
     * stamp known to be wrong costs the editor.
     */
    const server = await fakeServer("Be kind.");
    server.failTheStat();
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();
    type("first");
    await settle();

    /*
     * Typed through the save, so the editor stays open — which is the case that
     * matters. Reopening a closed editor takes its stamp from `detail`, and by
     * then the parent's re-read has been round the server again and knows the
     * answer. Only the editor that never closed is relying on what the save said.
     */
    button("Save").click();
    type("second");
    await settle();
    await settle();

    button("Save").click();
    await settle();
    await settle();

    // No stamp sent, so no false clash — and the text landed.
    expect(server.attempts.at(-1)?.seen).toBeUndefined();
    expect(onScreen()).not.toContain("has changed since you opened it");
    expect(server.state.content).toBe("second");
  });
});

describe("what the editor is offered for", () => {
  it("is not offered for a binary, which the pane has no text for", () => {
    const shot = fileDetail("viking://user/jasper/resources/shot/shot.png", "");
    render(Reader, {
      detail: { ...shot, binary: true, node: { ...shot.node, kind: "PNG" } },
    });
    expect(onScreen()).not.toContain("Edit");
  });

  it("says why rather than hiding the button on a file too long to save", () => {
    // The server would answer 413, and the only way back would be copying out of
    // the textarea by hand — so the button is not offered, and its absence is
    // explained rather than left looking like a bug.
    render(Reader, { detail: fileDetail(SOUL, "x".repeat(2_000_001)) });
    expect(onScreen()).toContain("Too long to edit here");
  });
});

describe("what is disabled while the editor is open", () => {
  it("stops a describe replacing the text under the editor", async () => {
    // Both of these replace what the pane holds. A describe finishing mid-edit
    // used to swap `detail` under a live textarea.
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    expect(button("Describe again").disabled).toBe(false);
    expect(button("Delete").disabled).toBe(false);

    button("Edit").click();
    await settle();

    expect(button("Describe again").disabled).toBe(true);
    expect(button("Delete").disabled).toBe(true);
  });
});

describe("which kind of file is being saved", () => {
  it("says a memory keeps its metadata and is not re-described", async () => {
    render(Reader, { detail: fileDetail(SOUL, "Be kind.") });
    button("Edit").click();
    await settle();

    expect(onScreen()).toContain("keeps its stored metadata");
    expect(onScreen()).toContain("does not rewrite what it says");
  });

  it("says a resource is read again afterwards", async () => {
    // A folder called `memories` under resources is still a resource, and the
    // note has to say the other thing for it.
    const uri = "viking://user/jasper/resources/memories/notes.md";
    render(Reader, { detail: fileDetail(uri, "Notes.") });
    button("Edit").click();
    await settle();

    expect(onScreen()).toContain("reads it again afterwards");
    expect(onScreen()).not.toContain("keeps its stored metadata");
  });
});
