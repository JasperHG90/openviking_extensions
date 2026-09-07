/**
 * What page we are on, and who we are.
 *
 * The router is a hash router on purpose: the server answers every unknown path
 * with the app shell, and a hash keeps a deep link working without asking the
 * proxy in front to know the client's routes.
 */

import type { SessionState } from "../../shared/schemas";

export type Route =
  | { page: "home" }
  | { page: "files" }
  | { page: "folder"; uri: string }
  | { page: "file"; uri: string }
  | { page: "search" }
  | { page: "memories" }
  | { page: "sessions" }
  | { page: "add" }
  | { page: "account" };

/** Parse a location hash into a route, defaulting to Home. */
export function parseRoute(hash: string): Route {
  const raw = hash.replace(/^#\/?/, "");
  const [page, encoded] = raw.split("?");
  const uri = encoded ? decodeURIComponent(encoded.replace(/^uri=/, "")) : "";

  switch (page) {
    case "files":
      return { page: "files" };
    case "folder":
      // Folders open inside Files now, next to their summary, so an old
      // /folder link lands on the tree rather than a page of its own.
      return { page: "files" };
    case "file":
      return uri ? { page: "file", uri } : { page: "files" };
    case "search":
      return { page: "search" };
    case "memories":
      return { page: "memories" };
    case "sessions":
      return { page: "sessions" };
    case "add":
      return { page: "add" };
    case "account":
      return { page: "account" };
    default:
      return { page: "home" };
  }
}

/** Build the hash for a route, so links and the router agree on the format. */
export function hashFor(route: Route): string {
  if (route.page === "folder" || route.page === "file") {
    return `#/${route.page}?uri=${encodeURIComponent(route.uri)}`;
  }
  return `#/${route.page}`;
}

/** Navigate, which the hashchange listener then turns into a render. */
export function go(route: Route): void {
  window.location.hash = hashFor(route);
}

export type Theme = "dark" | "light";

/**
 * The reading room is lit low by default.
 *
 * The system preference is a reasonable starting point but not the last word:
 * a person on a light desktop may still want the dark room to read in, so the
 * choice is remembered once made.
 */
function initialTheme(): Theme {
  const saved = localStorage.getItem("ovdash-theme");
  if (saved === "dark" || saved === "light") return saved;
  return "dark";
}

/** Shared, reactive application state. */
export const app = $state({
  theme: initialTheme() as Theme,
  route: parseRoute(window.location.hash) as Route,
  session: null as SessionState | null,
  railOpen: true,
  toast: "" as string,
});

let toastTimer: ReturnType<typeof setTimeout> | undefined;

/** Show a brief message at the corner of the screen. */
export function toast(message: string): void {
  app.toast = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    app.toast = "";
  }, 2600);
}

window.addEventListener("hashchange", () => {
  app.route = parseRoute(window.location.hash);
});

/** Switch the room's lighting and remember it. */
export function toggleTheme(): void {
  app.theme = app.theme === "dark" ? "light" : "dark";
  localStorage.setItem("ovdash-theme", app.theme);
  document.documentElement.dataset.theme = app.theme;
}

// Applied before the first paint so the page never flashes the other theme.
document.documentElement.dataset.theme = app.theme;
