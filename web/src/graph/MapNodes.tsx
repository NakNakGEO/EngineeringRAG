import { BaseEdge, EdgeLabelRenderer, Handle, Position, type EdgeProps, type NodeProps } from "@xyflow/react";
import { BookOpen, FileText, Folder, X } from "lucide-react";
import { memo } from "react";
import { cn } from "../lib/utils";
import {
  areaName, IN_COLOR, OUT_COLOR,
  type BubbleData, type DeptData, type DotData, type HintData, type HubData, type Rel, type TagData, type TitleData, type TreeData,
} from "./mapStyle";

export const Anchor = () => (
  <>
    <Handle type="target" position={Position.Top} isConnectable={false} style={{ left: "50%", top: "50%" }} />
    <Handle type="source" position={Position.Top} isConnectable={false} style={{ left: "50%", top: "50%" }} />
  </>
);

const RING: Record<Exclude<Rel, null>, string> = {
  self: "0 0 0 2px var(--background), 0 0 0 4.5px var(--foreground)",
  out: `0 0 0 1.5px var(--background), 0 0 0 3.5px ${OUT_COLOR}`,
  in: `0 0 0 1.5px var(--background), 0 0 0 3.5px ${IN_COLOR}`,
  folder: "0 0 0 1px var(--background), 0 0 0 2px color-mix(in srgb, var(--foreground) 45%, transparent)",
};

const DotNode = memo(function DotNode({ data }: NodeProps) {
  const d = data as DotData;
  const shadow = d.changed ? "0 0 0 2px var(--background), 0 0 0 4px #ffb020" : d.rel ? RING[d.rel] : d.picked ? RING.self : undefined;
  const name = d.label.split("/").at(-1) ?? d.label;
  const dirFull = d.label.includes("/") ? d.label.slice(0, d.label.length - name.length - 1) : "";
  const segs = dirFull.split("/");
  const dir = segs.length > 3 ? `…/${segs.slice(-3).join("/")}` : dirFull; // the end of the path is the useful part
  return (
    <div className={cn("relative", d.dim && "opacity-35")} style={{ width: d.size, height: d.size }}>
      <Anchor />
      <div className="size-full rounded-full" style={{ background: d.note ? "var(--success)" : d.color, boxShadow: shadow }} />
      {d.rel === "self" ? (
        <span className="pointer-events-none absolute top-1/2 left-full ml-3 flex w-max max-w-[300px] -translate-y-1/2 flex-col gap-0.5 rounded-lg border bg-card px-2.5 py-1.5 shadow-lg" title={d.label}>
          <span className="truncate text-[12.5px] leading-tight font-semibold text-foreground">{name}</span>
          {dir && <span className="truncate font-mono text-[10.5px] leading-tight text-muted-foreground">{dir}</span>}
        </span>
      ) : d.showLabel ? (
        <span className="pointer-events-none absolute top-1/2 left-full ml-1.5 -translate-y-1/2 rounded bg-card/85 px-1.5 py-px text-[10.5px] whitespace-nowrap text-foreground/90">{name}</span>
      ) : null}
    </div>
  );
});

/** What an area has to do with the hovered area or the selected file; every badge has a line. */
function RelBadges({ d, inline = false }: { d: { relOut: number; relIn: number; hot: number }; inline?: boolean }) {
  if (!d.relOut && !d.relIn && !d.hot) return null;
  return (
    <span className={cn("pointer-events-none flex w-max gap-1 text-[11px] font-semibold whitespace-nowrap tabular-nums", !inline && "absolute -top-3.5 right-0")}>
      {d.hot > 0 && <span className="rounded-full border border-primary bg-card px-2 py-0.5 text-primary shadow-sm">{d.hot.toLocaleString()} imports</span>}
      {d.relOut > 0 && <span className="rounded-full px-2 py-0.5 text-black shadow-sm" style={{ background: OUT_COLOR }}>→ {d.relOut}</span>}
      {d.relIn > 0 && <span className="rounded-full px-2 py-0.5 text-black shadow-sm" style={{ background: IN_COLOR }}>← {d.relIn}</span>}
    </span>
  );
}

const DeptNode = memo(function DeptNode({ data }: NodeProps) {
  const d = data as DeptData;
  const related = d.hot > 0 || d.relOut > 0 || d.relIn > 0;
  const name = areaName(d.label);
  const ring = d.src ? "ring-4 ring-foreground/70" : related && "ring-4 ring-primary/50";
  if (d.expanded) {
    // Open: a disc behind its dots. Only its title pill (a separate node, above the folder labels)
    // closes it; clicking the empty disc does nothing.
    return (
      <div
        className={cn("relative size-full cursor-default rounded-full border-2 border-dashed", d.dim && "opacity-40", ring)}
        style={{ borderColor: d.color, background: `color-mix(in srgb, ${d.color} 6%, var(--card))` }}
      >
        <Anchor />
        {d.more > 0 && (
          <span className="pointer-events-none absolute left-1/2 -translate-x-1/2 rounded-full bg-card px-2 py-0.5 text-[10.5px] whitespace-nowrap text-muted-foreground" style={{ bottom: 10 }}>+{d.more.toLocaleString()} more not drawn</span>
        )}
      </div>
    );
  }
  return (
    <div
      className={cn("relative grid place-items-center rounded-full border-2 text-center hover:brightness-125", d.dim && "opacity-30", ring)}
      style={{ width: d.d, height: d.d, borderColor: d.color, background: `color-mix(in srgb, ${d.color} 14%, var(--card))` }}
      title={`${d.label.toLowerCase()} · ${d.count.toLocaleString()} files. Click to open`}
    >
      <Anchor />
      <RelBadges d={d} />
      <div className="max-w-full px-2.5 leading-tight">
        {name.top && <p className="truncate text-[9.5px] text-muted-foreground">{name.top}</p>}
        <p className="line-clamp-2 text-[11.5px] font-semibold [overflow-wrap:anywhere]">{name.leaf}</p>
        <p className="mt-0.5 text-[10.5px] text-muted-foreground tabular-nums">{d.count.toLocaleString()}</p>
      </div>
    </div>
  );
});

/** The title of an open area: name, count and ✕ (click to close), with its relation badges beside it. */
const TitleNode = memo(function TitleNode({ data }: NodeProps) {
  const d = data as TitleData;
  return (
    <div className="flex -translate-x-1/2 items-center gap-2 whitespace-nowrap" title="Click to close">
      <span className="flex items-center gap-2 rounded-full border-2 bg-card px-3 py-1 text-[12px] shadow-sm hover:bg-accent" style={{ borderColor: d.color }}>
        <i className="size-2 shrink-0 rounded-full" style={{ background: d.color }} />
        <span>{d.top && <span className="text-muted-foreground">{d.top}/</span>}<b className="font-semibold">{d.leaf}</b></span>
        <span className="text-muted-foreground tabular-nums">{d.count.toLocaleString()}</span>
        <X className="size-3.5 text-muted-foreground" />
      </span>
      <RelBadges d={d} inline />
    </div>
  );
});

/** Name card next to the dot under the mouse: file, folder, and how it relates to the picked file. */
const HintNode = memo(function HintNode({ data }: NodeProps) {
  const d = data as HintData;
  return (
    <div className="pointer-events-none flex w-max max-w-[340px] flex-col gap-0.5 rounded-lg border bg-card px-2.5 py-1.5 shadow-lg">
      <span className="truncate text-[12.5px] leading-tight font-semibold text-foreground">{d.name}</span>
      {d.dir && <span className="truncate font-mono text-[10.5px] leading-tight text-muted-foreground">{d.dir}</span>}
      {d.relation && <span className="text-[10.5px] leading-tight font-semibold" style={{ color: d.color }}>{d.relation}</span>}
    </div>
  );
});

const HubNode = memo(function HubNode({ data }: NodeProps) {
  const d = data as HubData;
  return (
    <div className={cn("grid size-[68px] place-items-center rounded-full border-2 border-primary bg-primary/15 text-center", d.picked && "ring-2 ring-foreground ring-offset-2 ring-offset-background")}>
      <Anchor />
      <div className="leading-tight">
        <p className="label-caps max-w-[60px] truncate text-[9.5px] text-primary">{d.label}</p>
        <p className="text-[10px] text-muted-foreground">{d.count.toLocaleString()}</p>
      </div>
    </div>
  );
});

/** The folder name above a family of dots: a small pill, name then count. */
const TagNode = memo(function TagNode({ data }: NodeProps) {
  const d = data as TagData;
  return (
    <span className="pointer-events-none flex -translate-x-1/2 items-center gap-1.5 rounded-full border bg-card/90 px-2 py-0.5 text-[11px] font-medium whitespace-nowrap text-foreground shadow-sm">
      <Anchor />
      <i className="size-1.5 shrink-0 rounded-full" style={{ background: d.color }} />
      {d.name}
      <span className="text-muted-foreground tabular-nums">{d.count.toLocaleString()}</span>
    </span>
  );
});

const TreeNode = memo(function TreeNode({ data }: NodeProps) {
  const d = data as TreeData;
  const Icon = d.kind === "dir" ? Folder : d.kind === "note" ? BookOpen : FileText;
  return (
    <div
      className={cn(
        "flex h-[26px] w-[250px] items-center gap-2 rounded border bg-card px-2 text-[12px]",
        d.picked && "border-foreground", d.more && "border-dashed text-muted-foreground", d.changed && "border-[#ffb020]",
      )}
      style={{ borderLeft: `3px solid ${d.color}` }}
    >
      <Anchor />
      {!d.more && <Icon className="size-3.5 shrink-0 text-muted-foreground" />}
      <span className="truncate">{d.expandable ? (d.open ? "▾ " : "▸ ") : ""}{d.label}</span>
    </div>
  );
});

/** A thin curve that starts and ends on the edge of its two bubbles (never across them). */
function BubbleEdge({ sourceX, sourceY, targetX, targetY, data, style }: EdgeProps) {
  const d = (data ?? { rs: 0, rt: 0 }) as BubbleData;
  const dx = targetX - sourceX, dy = targetY - sourceY;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len, uy = dy / len;
  if (len <= d.rs + d.rt + 4) return null;
  const sx = sourceX + ux * d.rs, sy = sourceY + uy * d.rs, tx = targetX - ux * d.rt, ty = targetY - uy * d.rt;
  const bend = (d.bend ?? 0) * len;
  const cx = (sx + tx) / 2 - uy * bend, cy = (sy + ty) / 2 + ux * bend;
  return (
    <>
      <BaseEdge path={bend ? `M ${sx} ${sy} Q ${cx} ${cy} ${tx} ${ty}` : `M ${sx} ${sy} L ${tx} ${ty}`} style={style} />
      {d.text && (
        <EdgeLabelRenderer>
          <div
            className="pointer-events-none absolute rounded border bg-card px-1.5 py-0.5 text-[10.5px] font-semibold whitespace-nowrap text-foreground"
            style={{ transform: `translate(-50%, -50%) translate(${(sx + tx) / 2}px, ${(sy + ty) / 2}px)` }}
          >
            {d.text}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

export const nodeTypes = { dot: DotNode, dept: DeptNode, hub: HubNode, tree: TreeNode, tag: TagNode, title: TitleNode, hint: HintNode };
export const edgeTypes = { bubble: BubbleEdge };

