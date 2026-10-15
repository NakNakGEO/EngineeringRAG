import { useReactFlow, useStore } from "@xyflow/react";
import { useEffect, useRef } from "react";

export const NO_INSET = { left: 0, right: 0, top: 0 };

/** area id -> [centre x, centre y, radius] */
export type Frames = Map<string, [number, number, number]>;

/** The viewport that frames a box in the part of the map that is not under a panel. Worked out
 * from the layout and the pane size, so it is immediate and does not wait for node measurement.
 * Panels only count while they leave a usable strip (the details panel wins over the settings one). */
function viewportFor(
  box: { x0: number; y0: number; x1: number; y1: number }, w: number, h: number,
  inset: { left: number; right: number; top?: number }, margin: number, maxZoom = 1.1,
) {
  let { left, right } = inset;
  const top = h > 360 ? inset.top ?? 0 : 0;
  if (w - left - right < 320) right = 0;
  if (w - left < 240) left = 0;
  const availW = w - left - right, availH = h - top;
  const bw = Math.max(1, box.x1 - box.x0), bh = Math.max(1, box.y1 - box.y0);
  const zoom = Math.min(maxZoom, Math.max(0.03, Math.min((availW * (1 - 2 * margin)) / bw, (availH * (1 - 2 * margin)) / bh)));
  return { x: left + availW / 2 - ((box.x0 + box.x1) / 2) * zoom, y: top + availH / 2 - ((box.y0 + box.y1) / 2) * zoom, zoom };
}

export function Fit({ fitKey, focus, selected, quiet, centers, frames, inset }: { fitKey: string; focus: { id: string; n: number } | null; selected: string | null; quiet: boolean; centers: Map<string, [number, number]>; frames: Frames; inset: { left: number; right: number; top?: number } }) {
  const rf = useReactFlow();
  const paneW = useStore((st) => st.width);
  const paneH = useStore((st) => st.height);
  // always the latest values, for effects that fire later than the render that scheduled them
  const now = useRef({ inset, paneW, paneH, frames });
  now.current = { inset, paneW, paneH, frames };

  /** frame every area; falls back to React Flow's own fit when there is nothing to compute from */
  const frameAll = () => {
    const { inset: ins, paneW: w, paneH: h, frames: fr } = now.current;
    const circles = [...fr.values()];
    if (circles.length === 0 || w <= 0 || h <= 0) {
      void rf.fitView({ duration: 0, padding: 0.12, maxZoom: 1.1 });
      return;
    }
    const box = {
      x0: Math.min(...circles.map(([x, , r]) => x - r)), y0: Math.min(...circles.map(([, y, r]) => y - r)),
      x1: Math.max(...circles.map(([x, , r]) => x + r)), y1: Math.max(...circles.map(([, y, r]) => y + r)),
    };
    void rf.setViewport(viewportFor(box, w, h, ins, 0.06), { duration: 0 });
  };

  // The camera moves only for things the user asked to see: a new project / layout / view /
  // "expand all" (fitKey), a window resize, and a file picked from search. Opening or closing one
  // bubble never moves it.
  useEffect(() => {
    const t = setTimeout(frameAll, 60);
    let later: ReturnType<typeof setTimeout> | undefined;
    const onResize = () => { clearTimeout(later); later = setTimeout(frameAll, 150); };
    window.addEventListener("resize", onResize);
    return () => { clearTimeout(t); clearTimeout(later); window.removeEventListener("resize", onResize); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fitKey]);

  useEffect(() => {
    const c = focus ? centers.get(focus.id) : undefined;
    if (!focus || !c) return;
    const zoom = Math.max(rf.getZoom(), 0.9);
    const { inset: ins, paneW: w, paneH: h } = now.current;
    const left = ins.left;
    const right = w - left - ins.right < 320 ? 0 : ins.right;
    // centre on the part of the map that is not under a panel
    const top = h > 360 ? ins.top ?? 0 : 0;
    if (left > 0 || right > 0 || top > 0) void rf.setViewport({ x: left + (w - left - right) / 2 - c[0] * zoom, y: top + (h - top) / 2 - c[1] * zoom, zoom }, { duration: 0 });
    else void rf.setCenter(c[0], c[1], { zoom, duration: 0 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.n]);

  // A file picked on the map must not end up under the details panel it opens: if it would, slide
  // the map sideways just enough (never zooms, and does nothing when it is already in view).
  const lastPicked = useRef<string | null>(null);
  useEffect(() => {
    const picked = selected;
    if (!picked || picked === lastPicked.current) { lastPicked.current = picked; return; }
    lastPicked.current = picked;
    if (focus?.id === picked) return; // a search jump centres it already
    if (quiet) return; // opened from a link in the panel: the map stays exactly where it is
    const c = centers.get(picked);
    if (!c) return;
    const { inset: ins, paneW: w, paneH: h } = now.current;
    if (w <= 0 || h <= 0) return;
    const left = ins.left;
    const right = w - left - ins.right < 320 ? 0 : ins.right;
    const v = rf.getViewport();
    const sx = c[0] * v.zoom + v.x, sy = c[1] * v.zoom + v.y;
    const m = 72;
    const minX = left + m, maxX = w - right - m;
    const dx = sx < minX ? minX - sx : sx > maxX ? maxX - sx : 0;
    const top = h > 360 ? (ins.top ?? 0) : 0;
    const dy = sy < top + m ? top + m - sy : sy > h - m ? h - m - sy : 0;
    if (dx !== 0 || dy !== 0) void rf.setViewport({ x: v.x + dx, y: v.y + dy, zoom: v.zoom }, { duration: 0 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);
  return null;
}

