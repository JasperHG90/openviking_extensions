/**
 * What the extension remembers between saves.
 *
 * None of it is secret. The credential comes from ovx per save and is never
 * written down, so this is only preferences: which profile to ask about, where
 * to put things, and what you picked last time.
 */

import type { Scope } from "./ov-api";

/**
 * The person's own document tree.
 *
 * `viking://~` is OpenViking's home alias, resolving to whoever the token says
 * you are — which is what lets the extension work without knowing a user id,
 * since that comes from the token's claims and not from anything the browser
 * can see.
 *
 * The `/resources` is not decoration. The home root also holds `memories/`,
 * `sessions/`, `peers/` and `privacy/`, and importing to it is refused outright
 * with "to must target resource content". `resources` is the one that holds
 * documents. Measured against a real server, not assumed.
 */
export const DEFAULT_USER_SCOPE = "viking://~/resources";

/** The tree shared with everyone in the account. */
export const DEFAULT_SHARED_SCOPE = "viking://resources";

/**
 * The folder a clip goes into, below whichever scope is chosen.
 *
 * A default rather than the last folder used. Remembering the last one sounds
 * helpful and is not: one save into `papers` and every later clip lands there
 * too, silently, and the setting the person configured stops having any
 * visible effect. Every popup opens here; typing something else applies to
 * that save alone. Empty means the scope root.
 */
export const DEFAULT_FOLDER = "clips";

export interface Settings {
  /** ovx profile to ask for a token, as `ovx -l` lists them. */
  profile: string;
  /**
   * OpenViking address, when it should differ from the profile's.
   *
   * Normally empty: ovx reports the profile's own `url` with the token, so
   * there is one place the address lives rather than two that can disagree.
   */
  url: string;
  userScope: string;
  sharedScope: string;
  /** Scope id chosen last time, so the popup opens where you left it. */
  lastScope: string;
  /** Folder every clip goes into unless it is changed for that save. */
  defaultFolder: string;
}

const DEFAULTS: Settings = {
  profile: "",
  url: "",
  userScope: DEFAULT_USER_SCOPE,
  sharedScope: DEFAULT_SHARED_SCOPE,
  lastScope: "user",
  defaultFolder: DEFAULT_FOLDER,
};

/** Read the settings, filling in anything absent. */
export async function loadSettings(): Promise<Settings> {
  const stored = (await browser.storage.local.get({ ...DEFAULTS })) as Record<
    string,
    unknown
  >;
  const str = (key: keyof Settings) =>
    typeof stored[key] === "string" ? (stored[key] as string) : (DEFAULTS[key] as string);
  return {
    profile: str("profile"),
    url: str("url"),
    userScope: str("userScope"),
    sharedScope: str("sharedScope"),
    lastScope: str("lastScope"),
    defaultFolder: str("defaultFolder"),
  };
}

/** Write some of the settings, leaving the rest alone. */
export async function saveSettings(patch: Partial<Settings>): Promise<void> {
  await browser.storage.local.set(patch);
}

/**
 * Work out where this person may save.
 *
 * @param settings - Supplies the configured roots.
 * @returns The scopes to offer, own tree first. A root set to empty is left
 *   out, which is how a deployment with nothing shared says so.
 */
export function scopesFor(settings: Settings): Scope[] {
  const scopes: Scope[] = [];
  if (settings.userScope.trim()) {
    scopes.push({ id: "user", label: "My files", uri: settings.userScope.trim() });
  }
  if (settings.sharedScope.trim()) {
    scopes.push({
      id: "shared",
      label: "Shared with the account",
      uri: settings.sharedScope.trim(),
    });
  }
  return scopes;
}
