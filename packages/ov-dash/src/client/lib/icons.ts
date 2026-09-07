/**
 * The icon set.
 *
 * Every glyph is drawn on the same 15-unit grid at one stroke weight, so the
 * sidebar, the tree and the row actions read as one hand rather than as three
 * icon packs. There are no emoji anywhere in this app, by design.
 */

const PATHS: Record<string, string> = {
  chevron: '<path d="M5 3L9.3 7 5 11" stroke-width="2.1"/>',
  folder:
    '<path d="M1.9 4.5a1.3 1.3 0 011.3-1.3h2.2l1.1 1.4h5.3a1.3 1.3 0 011.3 1.3v5.1a1.3 1.3 0 01-1.3 1.3H3.2A1.3 1.3 0 011.9 11V4.5z"/>',
  doc: '<path d="M3.9 2.3h4.2l3 3.1v7.3H3.9V2.3z"/><path d="M7.9 2.3v3.1h3.2"/>',
  download: '<path d="M7.5 2.4v6.4M5.1 6.6L7.5 9l2.4-2.4M3.1 11h8.8"/>',
  check: '<path d="M3.4 7.8l2.8 2.8 5.4-6"/>',
  home: '<path d="M2.2 6.8L7.5 2.4l5.3 4.4"/><path d="M3.9 6.1v6.5h7.2V6.1"/><path d="M6.1 12.6V9h2.8v3.6"/>',
  files: '<path d="M2 3.6h3.7l1.1 1.4h6.2v7.4H2V3.6z"/>',
  search: '<circle cx="6.7" cy="6.7" r="4"/><path d="M9.7 9.7l3.2 3.2"/>',
  memory: '<path d="M7.5 2.1l1.4 4 4 1.4-4 1.4-1.4 4-1.4-4-4-1.4 4-1.4z"/>',
  sessions: '<path d="M1.8 7.5h2.5l1.4-4 2.4 8 1.5-4h3.6"/>',
  plus: '<path d="M7.5 3v9M3 7.5h9"/>',
  panel:
    '<rect x="2.1" y="2.9" width="10.8" height="9.2" rx="1.5"/><path d="M6 2.9v9.2"/>',
  user: '<circle cx="7.5" cy="5.6" r="2.5"/><path d="M3.1 12.6a4.4 4.4 0 018.8 0"/>',
  trash:
    '<path d="M3.1 4.3h8.8"/><path d="M6 4.3V2.9h3v1.4"/><path d="M4.3 4.3v7.8h6.4V4.3"/>',
  upload: '<path d="M7.5 12.6V5.2M5.1 7.6L7.5 5.2l2.4 2.4M3.1 2.5h8.8"/>',
  close: '<path d="M4 4l7 7M11 4l-7 7"/>',
  key: '<circle cx="5.6" cy="9.4" r="2.8"/><path d="M7.6 7.4l5-5M10.6 4.4l1.5 1.5"/>',
  refresh: '<path d="M12.8 6.4a5.4 5.4 0 10-.5 3.6"/><path d="M12.9 2.6v3.9H9"/>',
  sun: '<circle cx="7.5" cy="7.5" r="3"/><path d="M7.5 1v1.6M7.5 12.4V14M1 7.5h1.6M12.4 7.5H14M3 3l1.1 1.1M10.9 10.9L12 12M12 3l-1.1 1.1M4.1 10.9L3 12"/>',
  moon: '<path d="M12.4 8.9A5.4 5.4 0 116.1 2.6a4.2 4.2 0 006.3 6.3z"/>',
  exit: '<path d="M6 12.6H3.1V2.4H6"/><path d="M9 4.9l2.6 2.6L9 10.1M11.6 7.5H5.9"/>',
};

/**
 * Render one icon as an inline SVG string.
 *
 * @param name - Key from the set. An unknown name renders nothing rather than
 *   throwing, so a typo costs an icon and not the page.
 * @param size - Pixel size; the stroke stays optically even across sizes.
 */
export function icon(name: string, size = 15): string {
  const path = PATHS[name];
  if (!path) return "";
  return `<svg width="${size}" height="${size}" viewBox="0 0 15 15" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
}

export const ICON_NAMES = Object.keys(PATHS);
