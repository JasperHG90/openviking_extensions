/**
 * A `browser` good enough to test against.
 *
 * The real `storage.local` is asynchronous and defaults-aware; this copies
 * exactly that much of it, so the code under test is exercised through the API
 * it actually calls rather than through a mock of itself. `sendNativeMessage`
 * is a hook, because what ovx answers is the interesting variable.
 */

/** One storage area, holding whatever was put in it. */
class Area {
  private data = new Map<string, unknown>();

  /**
   * Read keys.
   *
   * @param query - Keys with the value to return when they are absent, which
   *   is the shape the extension always calls with.
   */
  get = async (query: Record<string, unknown>): Promise<Record<string, unknown>> => {
    const out: Record<string, unknown> = {};
    for (const [key, fallback] of Object.entries(query)) {
      out[key] = this.data.has(key) ? this.data.get(key) : fallback;
    }
    return out;
  };

  set = async (values: Record<string, unknown>): Promise<void> => {
    for (const [key, value] of Object.entries(values)) this.data.set(key, value);
  };

  remove = async (keys: string[]): Promise<void> => {
    for (const key of keys) this.data.delete(key);
  };

  /** Everything currently held, for asserting what was written. */
  dump(): Record<string, unknown> {
    return Object.fromEntries(this.data);
  }

  clear(): void {
    this.data.clear();
  }
}

/** One request the extension made of ovx. */
export interface NativeCall {
  host: string;
  message: unknown;
}

export interface BrowserStub {
  storage: { local: Area; session: Area };
  runtime: {
    sendNativeMessage: (host: string, message: unknown) => Promise<unknown>;
    sendMessage: (message: unknown) => Promise<unknown>;
    openOptionsPage: () => Promise<void>;
  };
  /** Everything sent to a native host, in order. */
  nativeCalls: NativeCall[];
}

/**
 * Install a fake `browser` global and hand it back for inspection.
 *
 * @param answer - What ovx replies with. Throwing from here is how "the host
 *   is not installed" is simulated, which is what Firefox actually does.
 * @returns The stub, with storage readable and native calls recorded.
 */
export function installBrowser(
  answer: (message: unknown) => Promise<unknown> = async () => ({ ok: true }),
): BrowserStub {
  const nativeCalls: NativeCall[] = [];
  const stub: BrowserStub = {
    storage: { local: new Area(), session: new Area() },
    runtime: {
      sendNativeMessage: (host, message) => {
        nativeCalls.push({ host, message });
        return answer(message);
      },
      sendMessage: async () => ({ ok: false }),
      openOptionsPage: async () => {},
    },
    nativeCalls,
  };
  (globalThis as { browser?: unknown }).browser = stub;
  return stub;
}

/** Remove the fake global again. */
export function removeBrowser(): void {
  (globalThis as { browser?: unknown }).browser = undefined;
}
