/**
 * Settings: pick an ovx profile, and say where files should land.
 *
 * There is no credential field here and no sign-in button, which is the whole
 * design. Signing in happens in a terminal, with `ovx --login`, and this page
 * only chooses which of ovx's profiles to borrow.
 */

import { OvxError, listProfiles, ping } from "../lib/ovx";
import {
  DEFAULT_FOLDER,
  DEFAULT_SHARED_SCOPE,
  DEFAULT_USER_SCOPE,
  loadSettings,
  saveSettings,
} from "../lib/settings";

const el = <T extends HTMLElement>(id: string): T => {
  const found = document.getElementById(id);
  if (!found) throw new Error(`options.html is missing #${id}`);
  return found as T;
};

const profileEl = el<HTMLSelectElement>("profile");
const urlEl = el<HTMLInputElement>("url");
const userScopeEl = el<HTMLInputElement>("user-scope");
const sharedScopeEl = el<HTMLInputElement>("shared-scope");
const defaultFolderEl = el<HTMLInputElement>("default-folder");
const recheckBtn = el<HTMLButtonElement>("recheck-btn");
const stateEl = el<HTMLElement>("ovx-state");
const ovxHintEl = el<HTMLElement>("ovx-hint");
const statusEl = el<HTMLElement>("status");

function setStatus(message: string, kind: "" | "error" | "success" = ""): void {
  statusEl.textContent = message;
  statusEl.className = kind;
}

function setState(text: string, reachable: boolean | null): void {
  stateEl.textContent = text;
  stateEl.className =
    reachable === null ? "state" : `state ${reachable ? "connected" : "disconnected"}`;
}

/** Fill the profile picker, keeping the stored choice when it still exists. */
function fillProfiles(profiles: string[], chosen: string): void {
  profileEl.replaceChildren(
    ...profiles.map((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      return option;
    }),
  );

  if (!profiles.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "ovx has no profiles";
    profileEl.replaceChildren(option);
    profileEl.disabled = true;
    return;
  }

  profileEl.disabled = false;
  // A stored profile that ovx no longer knows about must not stay selected: it
  // would fail on every save with an error about a profile nobody can see.
  profileEl.value = profiles.includes(chosen) ? chosen : (profiles[0] ?? "");
  if (profileEl.value !== chosen) void saveSettings({ profile: profileEl.value });
}

/** Ask ovx whether it is there, and what it knows. */
async function refresh(): Promise<void> {
  const settings = await loadSettings();
  urlEl.value = settings.url;
  userScopeEl.value = settings.userScope;
  sharedScopeEl.value = settings.sharedScope;
  defaultFolderEl.value = settings.defaultFolder;

  setState("Checking…", null);
  ovxHintEl.textContent = "";

  try {
    const version = await ping();
    const profiles = await listProfiles();
    setState(`ovx ${version} is answering`, true);
    fillProfiles(profiles, settings.profile);
    if (!profiles.length) {
      ovxHintEl.textContent = "Create one with `ovx -n`, then check again.";
    }
  } catch (error) {
    setState("ovx is not answering", false);
    if (error instanceof OvxError) {
      ovxHintEl.textContent = error.hint
        ? `${error.message} ${error.hint}`
        : error.message;
    } else {
      ovxHintEl.textContent = error instanceof Error ? error.message : String(error);
    }
    fillProfiles([], settings.profile);
  }
}

/** Remember a field as it is edited, and say so briefly. */
function persist(input: HTMLInputElement | HTMLSelectElement, key: string): void {
  input.addEventListener("change", () => {
    void saveSettings({ [key]: input.value.trim() }).then(() => {
      setStatus("Saved.", "success");
      setTimeout(() => setStatus(""), 1500);
    });
  });
}

persist(profileEl, "profile");
persist(urlEl, "url");
persist(userScopeEl, "userScope");
persist(sharedScopeEl, "sharedScope");
persist(defaultFolderEl, "defaultFolder");

// A blanked scope means "do not offer this one", which is a real choice. But a
// blanked *user* scope with no shared one leaves nowhere to save, so the
// placeholder shows what it would go back to.
userScopeEl.placeholder = DEFAULT_USER_SCOPE;
sharedScopeEl.placeholder = DEFAULT_SHARED_SCOPE;
// Blank is a real choice here — it means the scope root — so the placeholder
// shows what it would otherwise have been rather than implying a value.
defaultFolderEl.placeholder = DEFAULT_FOLDER;

recheckBtn.addEventListener("click", () => {
  void refresh();
});

void refresh();
