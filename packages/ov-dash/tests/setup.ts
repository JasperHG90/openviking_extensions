/**
 * What the browser provides and the test environment does not.
 *
 * Runs before every test file, node and jsdom alike, and does nothing in node —
 * there is no `window` there, and a node test that touched one would be testing
 * the wrong thing.
 */

if (typeof window !== "undefined" && !window.localStorage) {
  /*
   * A localStorage that lives for the file.
   *
   * jsdom under Node 22+ only has one when the process was started with
   * `--localstorage-file`, and `state.svelte.ts` reads the saved theme at module
   * load — so importing any component that reaches the shell's state threw
   * "Cannot read properties of undefined (reading 'getItem')" before a single
   * test ran. A Map is enough: nothing here asserts on what was stored.
   */
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => store.set(key, String(value)),
      removeItem: (key: string) => store.delete(key),
      clear: () => store.clear(),
      key: (index: number) => [...store.keys()][index] ?? null,
      get length() {
        return store.size;
      },
    },
    configurable: true,
  });
}

/**
 * A matchMedia that reports a plain, motion-friendly display.
 *
 * jsdom has none, and the shell asks for the colour-scheme preference while it is
 * working out the theme.
 */
if (typeof window !== "undefined" && !window.matchMedia) {
  Object.defineProperty(window, "matchMedia", {
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
    configurable: true,
  });
}
