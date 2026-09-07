import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

/**
 * Svelte's own config, kept separate from the Vite config so `svelte-check`
 * can find the preprocessor without loading the whole build.
 */
export default {
  preprocess: vitePreprocess(),
};
