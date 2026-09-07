import { defineConfig } from "vitest/config";

/**
 * The suite that talks to a real OpenViking.
 *
 * Node rather than jsdom: this exercises the client, not the DOM. One file, no
 * parallelism, and a long timeout — the container takes the best part of a
 * minute to boot and every test shares it.
 */
export default defineConfig({
  test: {
    environment: "node",
    include: ["tests/integration/**/*.integration.test.ts"],
    testTimeout: 120_000,
    hookTimeout: 240_000,
    fileParallelism: false,
  },
});
