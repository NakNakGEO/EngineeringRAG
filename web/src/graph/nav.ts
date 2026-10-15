/** The trail of things picked in the details panel, so "back" can return to the last one.
 *  Pure data; the page keeps one `Nav` in state. */

/** How a pick happened: a click on the map, a link inside the panel, or a search / jump from elsewhere. */
export type Via = "map" | "link" | "search";
export interface NavEntry { id: string; via: Via }
export interface Nav { list: NavEntry[]; at: number }

export const EMPTY_NAV: Nav = { list: [], at: -1 };
const MAX_TRAIL = 60;

export const currentEntry = (nav: Nav): NavEntry | null => (nav.at >= 0 ? nav.list[nav.at] ?? null : null);
export const previousEntry = (nav: Nav): NavEntry | null => (nav.at > 0 ? nav.list[nav.at - 1] ?? null : null);

/** Pick `id`. Picking what is already open changes nothing; anything ahead of the cursor is dropped. */
export function navGo(nav: Nav, id: string, via: Via): Nav {
  if (currentEntry(nav)?.id === id) return nav;
  const list = [...nav.list.slice(0, nav.at + 1), { id, via }];
  const drop = Math.max(0, list.length - MAX_TRAIL);
  return { list: list.slice(drop), at: list.length - drop - 1 };
}

/** One step back / forward, skipping entries that no longer exist (`valid` says which do). */
function step(nav: Nav, dir: -1 | 1, valid: (id: string) => boolean): Nav {
  for (let i = nav.at + dir; i >= 0 && i < nav.list.length; i += dir) {
    if (valid(nav.list[i]!.id)) return { ...nav, at: i };
  }
  return nav;
}
export const navBack = (nav: Nav, valid: (id: string) => boolean = () => true) => step(nav, -1, valid);
export const navForward = (nav: Nav, valid: (id: string) => boolean = () => true) => step(nav, 1, valid);

export const canGoBack = (nav: Nav, valid: (id: string) => boolean = () => true) => step(nav, -1, valid) !== nav;
export const canGoForward = (nav: Nav, valid: (id: string) => boolean = () => true) => step(nav, 1, valid) !== nav;
