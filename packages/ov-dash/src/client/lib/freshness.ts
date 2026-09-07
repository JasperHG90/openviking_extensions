/**
 * Noticing when the browser is running a bundle the server has replaced.
 *
 * Vite hashes asset filenames, so a rebuild changes them. If a browser is
 * holding an old copy of the shell, it keeps loading the old script — which
 * looks exactly like a page that hangs, or a feature that was never added.
 * Cache headers stop new copies going stale, but they cannot fix a shell that
 * was already cached before those headers existed.
 *
 * So the page checks itself: fetch the shell, compare the script it names with
 * the script actually running, and reload once if they differ. Once, guarded by
 * sessionStorage, because a reload that finds the same mismatch would loop.
 */

const GUARD = "ovdash-reloaded-for";

/** The URL of the module currently executing, as the browser resolved it. */
function runningScript(): string {
  return new URL(import.meta.url).pathname;
}

/** The script the server's current shell points at. */
async function servedScript(): Promise<string | null> {
  const response = await fetch("/", {
    cache: "reload",
    credentials: "same-origin",
  }).catch(() => null);
  if (!response?.ok) return null;

  const html = await response.text().catch(() => "");
  const match = /<script[^>]+src="([^"]+)"/.exec(html);
  return match?.[1] ?? null;
}

/**
 * Reload once if this page is running a superseded bundle.
 *
 * Failures are ignored: a freshness check that breaks the app it is checking
 * would be worse than the staleness it exists to catch.
 */
export async function reloadIfStale(): Promise<void> {
  try {
    const served = await servedScript();
    if (!served) return;

    const running = runningScript();
    // Compare filenames: the served path is absolute, the running one may
    // carry an origin, and only the hashed name actually identifies the build.
    const name = (path: string) => path.split("/").pop() ?? path;
    if (name(served) === name(running)) return;

    if (sessionStorage.getItem(GUARD) === name(served)) return;
    sessionStorage.setItem(GUARD, name(served));
    window.location.reload();
  } catch {
    // Nothing here is worth failing the page for.
  }
}
