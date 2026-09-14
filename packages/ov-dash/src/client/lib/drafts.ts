/**
 * Unsaved edits, held per file until they are saved or thrown away.
 *
 * The reading pane shows one file at a time and the tree is right beside it, so
 * clicking the next row while half-way through an edit is the easy mistake, not
 * the rare one. The first version of the editor dropped the text on that click:
 * silently, with no confirmation, while Delete two buttons along asks twice. The
 * asymmetry was the tell.
 *
 * So leaving a file parks its draft here instead of discarding it, and coming
 * back offers it again. Only saving it, or saying plainly to discard it, takes it
 * out. Nothing here touches the network — this is the memory of what you typed.
 *
 * It lives for the tab, and the instance at the bottom of this file is what
 * makes that true. Held in a component instead, it lived for as long as that
 * component did — and the reading pane is unmounted by every route change and
 * mounted separately by the Files page and the File page, so "park the draft"
 * meant "keep it until you click anything in the nav". Module scope, the same as
 * `cache.ts`.
 *
 * A draft that outlived the tab would need a story about what happens when the
 * stored file changed underneath it, and there is no such story worth telling
 * for a text box. The same reasoning bounds one case deliberately left alone:
 * re-uploading a file at a uri whose draft was forgotten out of band is not
 * tracked, because tracking it needs exactly that story.
 */

/**
 * The key one uri is filed under: itself, without trailing slashes.
 *
 * One spelling, one key, applied at every entry point rather than at the two that
 * happened to need it first. `fs/ls` answers without trailing slashes so nothing
 * produces one today, but the server trims on the way in (`requireInScope`), so
 * the two sides disagreed about normalisation — and a rule applied to some methods
 * is worse than one applied to none. Trimming only `forget` and `follow` left
 * `keep("…/a.md/")` parked under a key `has("…/a.md")` could not see: the draft is
 * held, invisible to the Edit button, and counted by the close warning forever.
 */
function keyFor(uri: string): string {
  return uri.replace(/\/+$/, "");
}

/** A parked draft: what was typed, and what was on screen when it started. */
interface Parked {
  /** The text as last typed. */
  text: string;
  /**
   * The file's stored text when editing began.
   *
   * Kept so "has this actually changed" survives leaving and returning. Without
   * it a returning draft identical to the file would still be offered as
   * unsaved work, and the hint would claim changes nobody made.
   */
  from: string;
}

/**
 * The parked drafts, keyed by uri.
 *
 * A plain class rather than a store: every method is a question with one answer,
 * and the one instance that matters is `drafts` below. Exported as a class as
 * well so a test can hold a fresh one instead of leaking state between cases.
 */
export class DraftBook {
  private readonly parked = new Map<string, Parked>();

  /** Park what has been typed for one file, or forget it if nothing changed. */
  keep(uri: string, text: string, from: string): void {
    const at = keyFor(uri);
    if (!at) return;
    // An untouched draft is not unsaved work. Parking it would light the "you
    // have something here" hint on every file somebody merely opened.
    if (text === from) {
      this.parked.delete(at);
      return;
    }
    this.parked.set(at, { text, from });
  }

  /** Whether this file has typing nobody has saved. */
  has(uri: string): boolean {
    return this.parked.has(keyFor(uri));
  }

  /**
   * The text to open the editor with: the parked draft, or the stored file.
   *
   * @param stored - The file's own text, used when nothing is parked.
   */
  open(uri: string, stored: string): string {
    return this.parked.get(keyFor(uri))?.text ?? stored;
  }

  /** Throw away this file's draft. Discarding ends here. */
  drop(uri: string): void {
    this.parked.delete(keyFor(uri));
  }

  /**
   * Forget the draft for a file that was just stored — unless it has moved on.
   *
   * What a save may throw away is the text it actually sent, and nothing else.
   * An unconditional drop looks right and loses work: type, press Save, type
   * more, click the next row in the tree. Leaving parks the *newer* text, and the
   * write then resolves and deletes it, under a toast saying "Saved". The editor's
   * own copy is guarded by comparing against what was sent; this is the same
   * comparison for the parked copy, which is where that text goes once the pane
   * has moved on.
   *
   * @param saved - The text the save sent. The best available guess at what is
   *   stored, not a certainty: a memory write re-renders its links and re-appends
   *   its metadata trailer, so what landed can differ — and only a fresh read
   *   would settle it. The cost of the guess being wrong is a parked draft
   *   dropped on a false "nothing unsaved", so it is worth naming.
   */
  dropSaved(uri: string, saved: string): void {
    const at = keyFor(uri);
    if (this.parked.get(at)?.text === saved) this.parked.delete(at);
  }

  /**
   * Forget a uri that has stopped existing, and everything under it.
   *
   * A draft for a deleted file is worse than a lost one. Nothing can reach it —
   * the Edit button keys on what is open, and that row is gone from the tree —
   * while it still counts toward the warning on closing the tab, so the tab warns
   * about a file that is not there. And if the uri ever comes back, by a
   * re-upload or an agent writing it, the pane offers "Resume editing" holding the
   * *deleted* file's text, one click away from writing it over the new content.
   *
   * Prefix-wide for the same reason `follow` is: deleting a folder takes every
   * file under it.
   */
  forget(uri: string): void {
    const at = keyFor(uri);
    if (!at) return;
    this.parked.delete(at);
    for (const key of [...this.parked.keys()]) {
      if (key.startsWith(`${at}/`)) this.parked.delete(key);
    }
  }

  /** Forget everything. For a test that needs to start from nothing. */
  clear(): void {
    this.parked.clear();
  }

  /**
   * Follow a file to its new uri, so a move does not orphan its draft.
   *
   * Dragging a row in the tree changes the uri the pane is on, and a draft left
   * under the old one is unreachable by every button while still counting toward
   * the unsaved warning — a file nobody can open, warned about on the way out.
   *
   * @param from - The uri as it was. A folder's uri moves its contents too.
   * @param to - Where it landed.
   */
  follow(from: string, to: string): void {
    const was = keyFor(from);
    const now = keyFor(to);
    if (!was || !now || was === now) return;
    for (const [uri, draft] of [...this.parked]) {
      if (uri !== was && !uri.startsWith(`${was}/`)) continue;
      this.parked.delete(uri);
      this.parked.set(now + uri.slice(was.length), draft);
    }
  }

  /** How many files have unsaved typing, for a warning that has to count. */
  get pending(): number {
    return this.parked.size;
  }
}

/**
 * The one book the app uses.
 *
 * Module scope on purpose: see the note at the top of this file. A per-component
 * book is thrown away by every route change, which is the failure this exists to
 * prevent.
 */
export const drafts = new DraftBook();
