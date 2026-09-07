/**
 * A small cache in front of the API.
 *
 * Without it every visit to a page refetches everything, so walking Home →
 * Files → Home paid for the whole tree twice. Against a real OpenViking those
 * calls are seconds, not milliseconds — a recursive listing is about a second
 * and search's grep face is twenty — so the difference is the whole feel of
 * the app.
 *
 * Deliberately small and in-memory: it lives for the tab, has no eviction
 * beyond a TTL, and anything that writes clears what it invalidates. A cache
 * that outlived the tab would need a story for staleness that this does not.
 */

interface Entry {
  at: number;
  value: unknown;
  /** Kept so concurrent callers share one request rather than racing. */
  pending?: Promise<unknown>;
}

const store = new Map<string, Entry>();

/**
 * Bumped by every invalidation.
 *
 * A load already in flight when the cache is cleared would otherwise resolve
 * afterwards and write its now-stale value back in — deleting a memory while
 * the list was loading put the deleted one back for a full TTL. A load only
 * stores its result if no invalidation happened while it was running.
 */
let generation = 0;

/** How long a cached answer stays good, in milliseconds. */
export const TTL = 60_000;

/**
 * Return a cached value, or fetch and cache it.
 *
 * Two callers asking for the same key at once share one request: the promise
 * is stored before it resolves, which is what stops the tree being fetched
 * twice when two components mount together.
 *
 * @param key - Cache key. Include every parameter that changes the answer.
 * @param load - How to fetch it when the cache cannot answer.
 * @param ttl - Override the default lifetime.
 */
export async function cached<T>(
  key: string,
  load: () => Promise<T>,
  ttl = TTL,
): Promise<T> {
  const hit = store.get(key);
  const now = Date.now();

  if (hit?.pending) return hit.pending as Promise<T>;
  if (hit && now - hit.at < ttl) return hit.value as T;

  const startedAt = generation;
  const pending = load()
    .then((value) => {
      // Dropped rather than stored if the cache was cleared meanwhile. The
      // caller still gets the value; it just does not outlive the request.
      if (generation === startedAt) {
        store.set(key, { at: Date.now(), value });
      } else {
        store.delete(key);
      }
      return value;
    })
    .catch((error: unknown) => {
      // A failure must not be cached, or one blip poisons the page for a minute.
      store.delete(key);
      throw error;
    });

  store.set(key, { at: now, value: hit?.value, pending });
  return pending as Promise<T>;
}

/**
 * Drop cached entries.
 *
 * @param prefix - Clears only keys starting with it. Omit to clear everything.
 */
export function invalidate(prefix?: string): void {
  generation += 1;
  if (!prefix) {
    store.clear();
    return;
  }
  for (const key of [...store.keys()]) {
    if (key.startsWith(prefix)) store.delete(key);
  }
}

/** Whether a key currently has a usable value, for tests and diagnostics. */
export function isCached(key: string, ttl = TTL): boolean {
  const hit = store.get(key);
  return Boolean(hit && hit.value !== undefined && Date.now() - hit.at < ttl);
}
