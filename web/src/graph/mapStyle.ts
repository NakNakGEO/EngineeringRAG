/** Shared look of the project map: layers, colours and the shapes of node data. */
/** How a dot relates to the selected file. */
export type Rel = "self" | "out" | "in" | "folder" | null;

export interface DotData extends Record<string, unknown> { color: string; size: number; label: string; showLabel: boolean; changed: boolean; dim: boolean; picked: boolean; note: boolean; rel: Rel }
export interface DeptData extends Record<string, unknown> { color: string; label: string; count: number; d: number; expanded: boolean; more: number; dim: boolean; reach: number; relOut: number; relIn: number; hot: number; src: boolean }
export interface HubData extends Record<string, unknown> { label: string; count: number; picked: boolean }
export interface TreeData extends Record<string, unknown> { label: string; kind: string; open: boolean; expandable: boolean; picked: boolean; color: string; changed: boolean; more: boolean }
export interface TagData extends Record<string, unknown> { name: string; count: number; color: string }
export interface TitleData extends Record<string, unknown> { deptId: string; top: string; leaf: string; count: number; color: string; relOut: number; relIn: number; hot: number }
export interface BubbleData extends Record<string, unknown> { rs: number; rt: number; text?: string; bend?: number }
export type DeptRel = Map<string, { out: number; in: number; hot: number }>;


// Stacking, bottom to top: an open area's disc, lines (so a selected file's lines are seen leaving
// its dot across the disc), closed bubbles, folder labels, dots.
export const Z = { disc: 10, line: 12, bubble: 15, tag: 40, dot: 50, picked: 70, title: 80, hint: 90 } as const;

export const OUT_COLOR = "#ff7a1a"; // this file imports ...
export const IN_COLOR = "#4da3ff"; //  ... imported by


/** "trading/tests" -> { top: "trading", leaf: "tests" }; lower case reads better than the stored caps. */
export function areaName(label: string): { top: string; leaf: string } {
  const name = label.toLowerCase();
  const i = name.lastIndexOf("/");
  return i < 0 ? { top: "", leaf: name } : { top: name.slice(0, i), leaf: name.slice(i + 1) };
}

/** The label shown next to a dot while the mouse is on it. */
export interface HintData extends Record<string, unknown> { name: string; dir: string; relation: string; color: string }
