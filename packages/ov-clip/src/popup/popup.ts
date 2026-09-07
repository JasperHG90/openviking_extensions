/**
 * The popup: read the tab, ask where it goes, send it.
 *
 * Extraction starts the moment the popup opens and runs alongside the calls to
 * ovx, because the reader is looking at a form either way and the page is the
 * slow part.
 */

import { type Extraction, extractActiveTab, looksLikePdf } from "../lib/extract";
import { joinTarget } from "../lib/naming";
import {
  OvError,
  type Scope,
  type Session,
  addResource,
  listFolders,
  normalizeUrl,
} from "../lib/ov-api";
import { OvxError, fetchToken } from "../lib/ovx";
import {
  type PlannedFile,
  type SavePlan,
  base64ToBytes,
  planArticleSave,
  planDocumentSave,
} from "../lib/save";
import { type Settings, loadSettings, saveSettings, scopesFor } from "../lib/settings";
import type { ExtractResult } from "../types";

const el = <T extends HTMLElement>(id: string): T => {
  const found = document.getElementById(id);
  if (!found) throw new Error(`popup.html is missing #${id}`);
  return found as T;
};

const titleEl = el<HTMLInputElement>("title");
const scopeEl = el<HTMLSelectElement>("scope");
const folderEl = el<HTMLInputElement>("folder");
const folderListEl = el<HTMLDataListElement>("folder-suggestions");
const noteEl = el<HTMLTextAreaElement>("note");
const tagsEl = el<HTMLInputElement>("tags");
const authorEl = el<HTMLInputElement>("author");
const publishDateEl = el<HTMLInputElement>("publish-date");
const saveBtn = el<HTMLButtonElement>("save-btn");
const formEl = el<HTMLFormElement>("save-form");
const disconnectedEl = el<HTMLElement>("disconnected");
const disconnectedWhyEl = el<HTMLElement>("disconnected-why");
const urlPreviewEl = el<HTMLElement>("url-preview");
const targetPreviewEl = el<HTMLElement>("target-preview");
const statusEl = el<HTMLElement>("status");
const signedInEl = el<HTMLElement>("signed-in");

let settings: Settings | null = null;
let scopes: Scope[] = [];
let extraction: Extraction | null = null;

function setStatus(
  message: string,
  kind: "" | "error" | "working" | "success" = "",
): void {
  statusEl.textContent = message;
  statusEl.className = kind;
}

/** Report a failure in the words the person can act on. */
function reportFailure(error: unknown, whileDoing: string): void {
  if (error instanceof OvxError) {
    // ovx knows what is wrong and which command fixes it, so pass its own
    // words through rather than inventing worse ones.
    showUnavailable(error.message, error.hint);
    return;
  }
  if (error instanceof OvError && error.needsLogin) {
    showUnavailable(error.message, `Run \`ovx --login ${settings?.profile ?? ""}\`.`);
    return;
  }
  const message = error instanceof Error ? error.message : String(error);
  setStatus(`${whileDoing}: ${message}`, "error");
}

function showUnavailable(message: string, hint = ""): void {
  disconnectedEl.hidden = false;
  formEl.hidden = true;
  disconnectedWhyEl.textContent = hint ? `${message} ${hint}` : message;
}

async function init(): Promise<void> {
  // Started first and awaited last: reading the page is the slow half.
  const pending = extractActiveTab().catch((error: unknown) => {
    reportFailure(error, "Could not read the page");
    return null;
  });

  settings = await loadSettings();
  if (!settings.profile) {
    showUnavailable("No ovx profile chosen yet.", "Open settings and pick one.");
    await pending;
    return;
  }

  scopes = scopesFor(settings);
  if (!scopes.length) {
    showUnavailable("No scope is configured to save into.", "Set one in settings.");
    await pending;
    return;
  }

  disconnectedEl.hidden = true;
  formEl.hidden = false;

  fillScopes(settings);
  applyExtraction(await pending);

  // Last, because everything above works without it and this is the part that
  // can fail. A token that cannot be had is worth saying plainly rather than
  // discovering when Save is pressed.
  await warmUp();
}

/** Fetch a token now, so a broken setup is visible before anything is typed. */
async function warmUp(): Promise<void> {
  const active = settings;
  if (!active) return;
  try {
    const session = await currentSession();
    signedInEl.textContent = `Saving through ovx profile "${active.profile}" to ${session.url}`;
    void refreshFolderSuggestions(session);
  } catch (error) {
    reportFailure(error, "ovx");
  }
}

/**
 * Ask ovx for a token and work out where to spend it.
 *
 * Called per save rather than cached: the token is a local pipe away, and a
 * copy held here could go stale while ovx's is fresh.
 */
async function currentSession(): Promise<Session> {
  const active = settings;
  if (!active) throw new Error("Settings are not loaded yet.");

  const minted = await fetchToken(active.profile);
  // The profile's own URL wins unless the settings override it, so there is
  // one place the address lives rather than two that can disagree.
  const url = normalizeUrl(active.url || minted.url);
  if (!url) {
    throw new OvxError(
      `ovx profile "${active.profile}" names no url.`,
      "Set one on the profile, or fill the address in settings.",
    );
  }
  return { url, token: minted.token };
}

function fillScopes(active: Settings): void {
  scopeEl.replaceChildren(
    ...scopes.map((scope) => {
      const option = document.createElement("option");
      option.value = scope.id;
      option.textContent = scope.label;
      return option;
    }),
  );
  if (scopes.some((scope) => scope.id === active.lastScope)) {
    scopeEl.value = active.lastScope;
  }
  folderEl.value = active.defaultFolder;
  updateTargetPreview();
}

/** The scope currently selected, or the first one offered. */
function currentScope(): Scope | null {
  return scopes.find((scope) => scope.id === scopeEl.value) ?? scopes[0] ?? null;
}

async function refreshFolderSuggestions(session: Session): Promise<void> {
  const scope = currentScope();
  if (!scope) return;
  // A missing suggestion list is a smaller problem than a save that stops for
  // it, so this failure is swallowed rather than shown.
  const folders = await listFolders(session, scope.uri).catch(() => []);
  folderListEl.replaceChildren(
    ...folders.map((folder) => {
      const option = document.createElement("option");
      option.value = folder;
      return option;
    }),
  );
}

function applyExtraction(found: Extraction | null): void {
  extraction = found;
  if (!found) return;

  const { result } = found;
  urlPreviewEl.textContent = result.url;
  urlPreviewEl.title = result.url;
  titleEl.value = result.title;
  authorEl.value = result.byline ?? "";
  publishDateEl.value = result.publishedTime ?? "";

  if (found.kind === "document") {
    setStatus("This page is a document. It will be uploaded as it is.");
  }
  if (found.warning) setStatus(found.warning, "error");

  saveBtn.disabled = false;
}

/** Build the plan for whatever the popup is currently holding. */
async function buildPlan(scope: Scope): Promise<SavePlan> {
  if (!extraction) throw new Error("Nothing was read from this page yet.");

  const common = {
    article: extraction.result satisfies ExtractResult,
    title: titleEl.value.trim() || extraction.result.title || "Untitled",
    scopeUri: scope.uri,
    folder: folderEl.value,
    tags: tagsEl.value
      .split(",")
      .map((tag) => tag.trim())
      .filter(Boolean),
    note: noteEl.value,
    savedAt: new Date().toISOString(),
  };

  if (extraction.kind === "document") {
    // With the site's cookies, or a document behind a login downloads as the
    // login page.
    const response = await fetch(extraction.result.url, { credentials: "include" });
    if (!response.ok) {
      throw new Error(`Could not download the document (HTTP ${response.status}).`);
    }
    const bytes = await response.arrayBuffer();
    // Judged on the bytes, not the header: a site that wants you signed in
    // answers with its login page and whatever content type it likes.
    if (!looksLikePdf(bytes)) {
      throw new Error(
        "That address answered with a web page rather than a PDF. " +
          "You may need to sign in to the site first.",
      );
    }
    return planDocumentSave({ ...common, bytes, contentType: "application/pdf" });
  }

  return planArticleSave({
    ...common,
    byline: authorEl.value.trim(),
    publishedTime: publishDateEl.value.trim(),
  });
}

/** Turn a planned file into the bytes an upload sends. */
function bytesFor(file: PlannedFile): ArrayBuffer | Uint8Array {
  if (file.text !== undefined) return new TextEncoder().encode(file.text);
  if (file.base64 !== undefined) return base64ToBytes(file.base64);
  if (file.bytes !== undefined) return file.bytes;
  throw new Error(`${file.filename} has no contents`);
}

formEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const scope = currentScope();
  if (!settings || !scope) {
    setStatus("Nowhere to save yet.", "error");
    return;
  }

  saveBtn.disabled = true;
  saveBtn.textContent = "Saving…";
  setStatus("");

  try {
    const [session, plan] = await Promise.all([currentSession(), buildPlan(scope)]);
    // Not named `document`: that is the DOM's, and shadowing it here would be
    // a trap for whatever gets added to this block next.
    const [main, ...rest] = plan.files;
    if (!main) throw new Error("Nothing to save.");

    // The main file goes first and alone: if it fails, nothing else has landed
    // and the reader is told the save did not happen.
    const landed = await addResource(session, {
      bytes: bytesFor(main),
      filename: main.filename,
      contentType: main.contentType,
      to: main.to,
    });

    // Then anything beside it — today just a PDF's note companion, in its own
    // destination. One that fails is worth mentioning, not worth undoing a
    // saved document for.
    const missed: string[] = [];
    for (const file of rest) {
      try {
        await addResource(session, {
          bytes: bytesFor(file),
          filename: file.filename,
          contentType: file.contentType,
          to: file.to,
        });
      } catch {
        missed.push(file.filename);
      }
    }

    await saveSettings({
      lastScope: scope.id,
    });

    saveBtn.textContent = "Saved";
    saveBtn.classList.add("saved");
    // Where it went comes from the server: OpenViking names the directory and
    // renames the file from the article's own title, so the plan's target is
    // only where it was aimed.
    if (missed.length) {
      setStatus(`Saved to ${landed}, but ${missed.join(", ")} did not.`, "success");
    } else {
      setStatus(`Saved to ${landed}`, "success");
      setTimeout(() => window.close(), 900);
    }
  } catch (error) {
    reportFailure(error, "Save failed");
    saveBtn.textContent = "Save";
    saveBtn.disabled = false;
  }
});

for (const trigger of ["open-settings", "connect-btn"]) {
  el<HTMLElement>(trigger).addEventListener("click", (event) => {
    event.preventDefault();
    void browser.runtime.openOptionsPage();
  });
}

/**
 * Show where the file is about to go.
 *
 * A folder name is cleaned before it becomes a URI segment, so typing
 * "My Clips" — or picking it from the suggestions, which list what OpenViking
 * already holds — saves into `My-Clips`. Showing the resolved destination turns
 * that from a surprise found later into a line read now.
 */
function updateTargetPreview(): void {
  const scope = currentScope();
  if (!scope) {
    targetPreviewEl.textContent = "";
    return;
  }
  try {
    targetPreviewEl.textContent = `Saves to ${joinTarget(scope.uri, folderEl.value)}`;
    folderEl.setCustomValidity("");
  } catch (error) {
    targetPreviewEl.textContent = "";
    folderEl.setCustomValidity(error instanceof Error ? error.message : "Bad folder");
  }
}

folderEl.addEventListener("input", updateTargetPreview);
scopeEl.addEventListener("change", () => {
  updateTargetPreview();
  void currentSession()
    .then(refreshFolderSuggestions)
    .catch(() => {
      // Suggestions are a convenience; a failure here is already reported by
      // whatever else needed the token.
    });
});

void init();
