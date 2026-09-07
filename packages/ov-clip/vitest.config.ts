import { defineConfig } from "vitest/config";

/**
 * The libraries under test parse HTML, so they need a DOM.
 *
 * jsdom gives `DOMParser`, `document` and the rest without a browser. The
 * integration suite is excluded here and run on purpose with
 * `npm run test:integration`: it needs a real OpenViking, which it will start
 * in a container, and that is not something `npm test` should do.
 */
export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts"],
    exclude: ["tests/integration/**"],
  },
});
