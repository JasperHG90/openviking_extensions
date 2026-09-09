/**
 * Turning OpenViking's flat listing into the hierarchy the Files page draws.
 *
 * The server asks for a recursive listing and passes the entries straight
 * through, so the shape of the tree is decided here from each entry's path.
 * Doing it client-side means expanding a folder costs nothing — the children
 * are already loaded.
 */

import type { Node } from "../../shared/schemas";

export interface TreeNode extends Node {
  depth: number;
  children: TreeNode[];
}

/**
 * Build a hierarchy from a flat, recursive listing.
 *
 * Entries whose parent is missing from the listing are attached at the root
 * rather than dropped: a truncated listing should still show what it did
 * return.
 *
 * @param nodes - Flat entries, each carrying `relPath` relative to the scope.
 * @returns Root-level nodes, each with its children nested and depth stamped.
 */
export function buildTree(nodes: Node[]): TreeNode[] {
  const byPath = new Map<string, TreeNode>();
  const roots: TreeNode[] = [];

  const sorted = [...nodes].sort((a, b) => a.relPath.localeCompare(b.relPath));

  for (const node of sorted) {
    const clean = node.relPath.replace(/\/$/, "");
    if (!clean) continue;
    const entry: TreeNode = { ...node, depth: 0, children: [] };
    byPath.set(clean, entry);
  }

  for (const [path, entry] of byPath) {
    const cut = path.lastIndexOf("/");
    const parent = cut > 0 ? byPath.get(path.slice(0, cut)) : undefined;
    if (parent) parent.children.push(entry);
    else roots.push(entry);
  }

  const stamp = (list: TreeNode[], depth: number): void => {
    list.sort(directoriesFirst);
    for (const item of list) {
      item.depth = depth;
      stamp(item.children, depth + 1);
    }
  };
  stamp(roots, 0);

  return roots;
}

function directoriesFirst(a: TreeNode, b: TreeNode): number {
  if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;
  return a.name.localeCompare(b.name, undefined, { numeric: true });
}

/**
 * The folder a uri sits in.
 *
 * A uri with nothing above it answers with itself, so a caller comparing a node
 * against its parent stops rather than producing `viking:/`. Mirrors `parentOf`
 * on the server, which is the one OpenViking is actually asked about.
 */
export function parentUri(uri: string): string {
  const SCHEME = "viking://";
  const trimmed = uri.replace(/\/+$/, "");
  if (trimmed.length < SCHEME.length) return uri;
  const cut = trimmed.lastIndexOf("/");
  return cut < SCHEME.length ? trimmed : trimmed.slice(0, cut);
}

/** Every directory path in a tree, for Expand all. */
export function allFolderUris(nodes: TreeNode[]): string[] {
  const out: string[] = [];
  const walk = (list: TreeNode[]): void => {
    for (const node of list) {
      if (node.isDir) out.push(node.uri);
      walk(node.children);
    }
  };
  walk(nodes);
  return out;
}

/** Flatten a tree to the rows that are currently visible. */
export function visibleRows(nodes: TreeNode[], open: Set<string>): TreeNode[] {
  const out: TreeNode[] = [];
  const walk = (list: TreeNode[]): void => {
    for (const node of list) {
      out.push(node);
      if (node.isDir && open.has(node.uri)) walk(node.children);
    }
  };
  walk(nodes);
  return out;
}

/** Human sizes, in the mono column. Directories show nothing. */
export function formatSize(bytes: number, isDir: boolean): string {
  // A folder has no size worth printing, and a column of dashes is noise.
  if (isDir) return "";
  if (bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

/** Relative time, falling back to the raw string when it will not parse. */
export function formatWhen(iso: string): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;

  const seconds = Math.floor((Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ${hours === 1 ? "hour" : "hours"} ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} ${days === 1 ? "day" : "days"} ago`;
  if (days < 30) return `${Math.floor(days / 7)} week${days < 14 ? "" : "s"} ago`;
  return new Date(then).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
  });
}
