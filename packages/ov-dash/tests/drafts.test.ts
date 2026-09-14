/**
 * Unsaved typing, and the promise that a stray click does not lose it.
 *
 * The reading pane's first editor dropped the draft when you clicked the next
 * row in the tree — silently, in one click, while Delete two buttons along asks
 * twice. These pin the rules that replaced it. The component itself has no test
 * harness here, which is exactly why the rules live in a module of their own
 * rather than inside it.
 */

import { describe, expect, it } from "vitest";
import { DraftBook, drafts as theBook } from "../src/client/lib/drafts";

const SOUL = "viking://user/jasper/memories/soul.md";
const PROFILE = "viking://user/jasper/memories/profile.md";

describe("keeping unsaved typing", () => {
  it("gives back what was typed, not what was stored", () => {
    const drafts = new DraftBook();
    drafts.keep(SOUL, "Be kind, and brief.", "Be kind.");
    expect(drafts.open(SOUL, "Be kind.")).toBe("Be kind, and brief.");
    expect(drafts.has(SOUL)).toBe(true);
  });

  it("falls back to the stored text when nothing is parked", () => {
    const drafts = new DraftBook();
    expect(drafts.open(SOUL, "Be kind.")).toBe("Be kind.");
    expect(drafts.has(SOUL)).toBe(false);
  });

  it("does not park a draft nobody changed", () => {
    // Opening the editor and closing it again is not unsaved work. Parked, it
    // would light the "you have something here" hint on every file merely
    // looked at, and the warning on closing the tab would fire for nothing.
    const drafts = new DraftBook();
    drafts.keep(SOUL, "Be kind.", "Be kind.");
    expect(drafts.has(SOUL)).toBe(false);
    expect(drafts.pending).toBe(0);
  });

  it("forgets a draft edited back to what was stored", () => {
    // The undo case: type, then undo, then leave. There is nothing unsaved, and
    // saying there is would be as wrong as losing it.
    const drafts = new DraftBook();
    drafts.keep(SOUL, "Be kind, and brief.", "Be kind.");
    drafts.keep(SOUL, "Be kind.", "Be kind.");
    expect(drafts.has(SOUL)).toBe(false);
  });

  it("keeps one file's typing apart from another's", () => {
    // One component shows every file in turn, so this is the whole point: a
    // shared slot would offer soul.md the words typed into profile.md.
    const drafts = new DraftBook();
    drafts.keep(SOUL, "soul words", "");
    drafts.keep(PROFILE, "profile words", "");
    expect(drafts.open(SOUL, "")).toBe("soul words");
    expect(drafts.open(PROFILE, "")).toBe("profile words");
    expect(drafts.pending).toBe(2);
  });

  it("drops a draft when it is saved or thrown away", () => {
    const drafts = new DraftBook();
    drafts.keep(SOUL, "Be kind, and brief.", "Be kind.");
    drafts.drop(SOUL);
    expect(drafts.has(SOUL)).toBe(false);
    expect(drafts.open(SOUL, "Be kind.")).toBe("Be kind.");
    expect(drafts.pending).toBe(0);
  });

  it("judges change against the given baseline, not the text itself", () => {
    /*
     * The baseline is a parameter so the caller can pass the text the editor
     * *opened on* rather than whatever the pane holds now — `detail` can be
     * replaced under an open editor by a reread or somebody else's write. These
     * two cases only differ in `from`, which is what makes the parameter earn its
     * place: the same typing parks or does not depending on the baseline alone.
     */
    const drafts = new DraftBook();
    drafts.keep(SOUL, "the same words", "the same words");
    expect(drafts.has(SOUL)).toBe(false);
    drafts.keep(SOUL, "the same words", "what the agent wrote instead");
    expect(drafts.has(SOUL)).toBe(true);
  });

  it("counts what is unsaved, so a warning can be honest about it", () => {
    const drafts = new DraftBook();
    expect(drafts.pending).toBe(0);
    drafts.keep(SOUL, "a", "");
    drafts.keep(PROFILE, "b", "");
    expect(drafts.pending).toBe(2);
    drafts.drop(SOUL);
    expect(drafts.pending).toBe(1);
  });

  it("ignores a file with no uri rather than parking under an empty key", () => {
    // The pane's `openUri` is "" before anything is picked, and a draft filed
    // there would be offered to whichever file was opened next.
    const drafts = new DraftBook();
    drafts.keep("", "stray words", "");
    expect(drafts.pending).toBe(0);
    expect(drafts.has("")).toBe(false);
  });

  it("dropping a file nothing is parked for is not an error", () => {
    const drafts = new DraftBook();
    expect(() => drafts.drop(SOUL)).not.toThrow();
    expect(drafts.pending).toBe(0);
  });
});

describe("what a save may forget", () => {
  it("forgets the parked draft when it is the text that was stored", () => {
    const drafts = new DraftBook();
    drafts.keep(SOUL, "Be kind, and brief.", "Be kind.");
    drafts.dropSaved(SOUL, "Be kind, and brief.");
    expect(drafts.has(SOUL)).toBe(false);
  });

  it("keeps it when the typing moved on after the save was sent", () => {
    /*
     * The loss this replaced. Type, press Save, type more, then click the next row
     * in the tree: leaving parks the *newer* text, and an unconditional drop then
     * deleted text that was never sent anywhere — under a toast saying "Saved".
     * A save may throw away what it stored and nothing else.
     */
    const drafts = new DraftBook();
    drafts.keep(SOUL, "Be kind, and brief. And warm.", "Be kind.");
    drafts.dropSaved(SOUL, "Be kind, and brief.");
    expect(drafts.open(SOUL, "Be kind.")).toBe("Be kind, and brief. And warm.");
  });

  it("does nothing when there is no draft parked for it", () => {
    const drafts = new DraftBook();
    expect(() => drafts.dropSaved(SOUL, "anything")).not.toThrow();
    expect(drafts.pending).toBe(0);
  });
});

describe("following a file that moved", () => {
  it("carries the draft to the new uri", () => {
    // Nothing stops a drag while the editor is open, so the uri a draft is filed
    // under can move out from under it. Left behind it is unreachable by every
    // button and still counted by the unsaved warning.
    const drafts = new DraftBook();
    drafts.keep(SOUL, "half a sentence", "");
    drafts.follow(SOUL, "viking://user/jasper/memories/archive/soul.md");

    expect(drafts.has(SOUL)).toBe(false);
    expect(drafts.open("viking://user/jasper/memories/archive/soul.md", "")).toBe(
      "half a sentence",
    );
    expect(drafts.pending).toBe(1);
  });

  it("carries drafts inside a folder that moved", () => {
    // Dragging any ancestor moves the file's uri just as surely as dragging it.
    const drafts = new DraftBook();
    drafts.keep("viking://user/jasper/resources/notes/a.md", "aaa", "");
    drafts.keep("viking://user/jasper/resources/notes/deep/b.md", "bbb", "");
    drafts.follow(
      "viking://user/jasper/resources/notes",
      "viking://user/jasper/resources/kept/notes",
    );

    expect(drafts.open("viking://user/jasper/resources/kept/notes/a.md", "")).toBe("aaa");
    expect(drafts.open("viking://user/jasper/resources/kept/notes/deep/b.md", "")).toBe(
      "bbb",
    );
    expect(drafts.pending).toBe(2);
  });

  it("leaves a sibling that merely shares a prefix where it is", () => {
    // `notes` is a prefix of `notes-archive`, and moving the first must not
    // rewrite the second's key into a path nothing is stored at.
    const drafts = new DraftBook();
    drafts.keep("viking://user/jasper/resources/notes-archive/a.md", "aaa", "");
    drafts.follow(
      "viking://user/jasper/resources/notes",
      "viking://user/jasper/resources/kept/notes",
    );
    expect(drafts.open("viking://user/jasper/resources/notes-archive/a.md", "")).toBe(
      "aaa",
    );
  });

  it("treats a trailing slash as the same uri, at every entry point", () => {
    /*
     * One spelling, one key — and the rule has to hold on every method, not the
     * two that happened to need it first. `fs/ls` answers without trailing slashes
     * so nothing produces one today, but the server trims on the way in, so the two
     * sides disagreed about normalisation.
     *
     * Trimming only `forget` and `follow` was worse than trimming nothing: an
     * untrimmed `keep` parked under a key `has` could not see, so the draft was
     * held, invisible to the Edit button, and counted by the close warning forever.
     */
    const drafts = new DraftBook();
    const bare = "viking://user/jasper/memories/soul.md";
    const slashed = `${bare}/`;

    // Stored one way, read the other — and then stored the *other* way and read
    // back the first. Only the second direction reaches the trims on the lookup
    // side: store-slashed-read-bare exercises `keep` alone, because `keep` has
    // already normalised the key by the time anything asks for it. The first
    // version of this test only went one way and left `has`, `open` and
    // `dropSaved` unpinned, under a name claiming every entry point.
    drafts.keep(slashed, "typed", "");
    expect(drafts.has(bare)).toBe(true);
    expect(drafts.open(bare, "")).toBe("typed");
    expect(drafts.pending).toBe(1);
    drafts.drop(bare);

    drafts.keep(bare, "typed", "");
    expect(drafts.has(slashed)).toBe(true);
    expect(drafts.open(slashed, "")).toBe("typed");

    // Each way out, reached by the spelling it was not stored under.
    drafts.dropSaved(slashed, "typed");
    expect(drafts.pending).toBe(0);

    drafts.keep(bare, "typed", "");
    drafts.drop(slashed);
    expect(drafts.pending).toBe(0);

    drafts.keep(bare, "typed", "");
    drafts.forget(slashed);
    expect(drafts.pending).toBe(0);
  });

  it("treats a trailing slash as the same folder, for the prefix-wide pair", () => {
    const drafts = new DraftBook();
    drafts.keep("viking://user/jasper/resources/notes/a.md", "aaa", "");
    drafts.forget("viking://user/jasper/resources/notes/");
    expect(drafts.pending).toBe(0);

    drafts.keep("viking://user/jasper/resources/notes/b.md", "bbb", "");
    drafts.follow(
      "viking://user/jasper/resources/notes/",
      "viking://user/jasper/resources/kept/notes/",
    );
    expect(drafts.open("viking://user/jasper/resources/kept/notes/b.md", "")).toBe("bbb");
  });

  it("does nothing for a move that is not one", () => {
    const drafts = new DraftBook();
    drafts.keep(SOUL, "words", "");
    drafts.follow(SOUL, SOUL);
    drafts.follow("", "x");
    drafts.follow("x", "");
    expect(drafts.open(SOUL, "")).toBe("words");
    expect(drafts.pending).toBe(1);
  });
});

describe("the book the app actually uses", () => {
  it("is shared by every importer, so it outlives any one component", async () => {
    /*
     * The whole point of parking a draft. Held inside the reading pane it lived
     * only as long as that pane did — and App.svelte unmounts the page on every
     * route change, with the Files page and the File page each mounting their own
     * — so "leaving a file keeps your typing" meant "until you click the nav".
     *
     * Two importers, one book. `Reader.svelte` and `Files.svelte` both reach for
     * this and have to be talking about the same drafts, and a `new DraftBook()`
     * inside either of them would fail here. Written as a second import rather
     * than `toBe(theBook)`, which would be true of anything.
     */
    const again = await import("../src/client/lib/drafts");
    expect(again.drafts).toBe(theBook);
    expect(theBook).toBeInstanceOf(DraftBook);

    theBook.keep(SOUL, "typed on the Files page", "");
    expect(again.drafts.has(SOUL)).toBe(true);
    // Dropped through the other reference, which only works if it is one object.
    again.drafts.drop(SOUL);
    expect(theBook.pending).toBe(0);
  });
});
