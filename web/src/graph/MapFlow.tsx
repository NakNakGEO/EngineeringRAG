import { Background, Controls, ReactFlow, ReactFlowProvider, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useMemo, useRef, useState } from "react";
import { Fit, NO_INSET, type Frames } from "./Camera";
import { buildNodes, buildTree } from "./mapBuild";
import { buildEdges } from "./mapEdges";
import { edgeTypes, nodeTypes } from "./MapNodes";
import { IN_COLOR, OUT_COLOR, Z, type DeptData, type DeptRel, type DotData, type HintData, type TitleData } from "./mapStyle";
import { computeLayout, departmentsOf, type LayoutMode } from "./layout";
import type { Brain } from "./model";
import { areaRelations, coveredAreas, relatives } from "./relations";

export type MapView = "departments" | "folders";

export interface MapFlowProps {
  brain: Brain;
  view: MapView;
  mode: LayoutMode;
  expanded: ReadonlySet<string>;
  openDirs: ReadonlySet<string>;
  spacing: number;
  showNames: boolean;
  showImports: boolean;
  /** size every ring by what is open (expand all) instead of opening areas in place */
  spread: boolean;
  selected: string | null;
  highlight: ReadonlySet<string> | null;
  /** file paths that changed recently: drawn with an amber ring (static, not animated) */
  changed: ReadonlySet<string>;
  dark: boolean;
  onSelect: (id: string | null) => void;
  onToggleDept: (id: string) => void;
  onToggleDir: (id: string) => void;
  /** change this string to re-fit the view (project, layout or view switched) */
  fitKey: string;
  /** centre on a node once (e.g. picked from search) */
  focus: { id: string; n: number } | null;
  /** the picked file was opened from a link in the panel: do not slide the map for it */
  quiet?: boolean;
  /** px of the map's edges hidden under panels / the header: framing keeps clear of them */
  inset?: { left: number; right: number; top?: number };
}


function Inner(p: MapFlowProps) {
  const [hover, setHover] = useState<string | null>(null);
  const [hoverDot, setHoverDot] = useState<string | null>(null); // the dot under the mouse
  const departments = useMemo(() => (p.view === "departments" ? departmentsOf(p.brain) : null), [p.view, p.brain]);
  const tree = useMemo(
    () => (p.view === "folders" ? buildTree(p) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [p.view, p.brain, p.openDirs, p.selected, p.changed, p.dark],
  );
  const layout = useMemo(
    () => (p.view === "departments" ? computeLayout(p.brain, p.mode, p.expanded, { spacing: p.spacing, spread: p.spread }) : null),
    [p.view, p.brain, p.mode, p.expanded, p.spacing, p.spread],
  );
  const rel = useMemo(() => relatives(p.brain, p.selected), [p.brain, p.selected]);
  const deptRel = useMemo(
    () => (layout && departments ? areaRelations(layout, rel, departments.of, hover) : (new Map() as DeptRel)),
    [layout, departments, rel, hover],
  );
  // Hover must never change which bubbles exist (they would pop in and out under the cursor):
  // a bubble under an open disc is revealed only because of the selected file.
  const selRel = useMemo(
    () => (layout && departments ? areaRelations(layout, rel, departments.of, null) : (new Map() as DeptRel)),
    [layout, departments, rel],
  );
  const covered = useMemo(() => {
    const under = layout ? coveredAreas(layout) : new Set<string>();
    for (const id of under) {
      const r = selRel.get(id);
      if (r && (r.out > 0 || r.in > 0)) under.delete(id); // related to the picked file: shown on top of the disc
    }
    return under;
  }, [layout, selRel]);
  const built = useMemo(
    () => (layout && departments ? buildNodes(p, layout, rel, departments.of, deptRel, hover, covered) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [layout, departments, rel, deptRel, covered, p.highlight, p.changed, p.dark, p.showNames, p.selected, p.brain],
  );
  const edges = useMemo(
    () => (layout && built && departments ? buildEdges(p, layout, built.shown, rel, hover, deptRel, departments.of, covered) : tree?.edges ?? []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [layout, built, departments, rel, hover, deptRel, covered, p.showImports, p.selected, p.brain, tree],
  );
  const nodeCache = useRef(new Map<string, { sig: string; node: Node }>());
  const nodes = useMemo(() => {
    const raw = built?.nodes ?? tree?.nodes ?? [];
    const next = new Map<string, { sig: string; node: Node }>();
    const out = raw.map((n) => {
      const sig = JSON.stringify([n.position, n.zIndex, n.width, n.height, n.style, n.data]);
      const prev = nodeCache.current.get(n.id);
      const node = prev && prev.sig === sig ? prev.node : n;
      next.set(n.id, { sig, node });
      return node;
    });
    nodeCache.current = next;
    return out;
  }, [built, tree]);
  const centers = built?.centers ?? tree?.centers ?? new Map<string, [number, number]>();
  // the name card of the dot under the mouse (one extra node; the dots themselves do not change)
  const shownNodes = useMemo(() => {
    const dot = hoverDot ? nodes.find((n) => n.id === hoverDot) : undefined;
    const c = hoverDot ? centers.get(hoverDot) : undefined;
    if (!dot || !c || dot.type !== "dot") return nodes;
    const d = dot.data as DotData;
    if (d.rel === "self") return nodes; // the picked file already has its card
    const name = d.label.split("/").at(-1) ?? d.label;
    const dir = d.label.includes("/") ? d.label.slice(0, d.label.length - name.length - 1) : "";
    const relation = d.rel === "out" ? "the picked file imports this" : d.rel === "in" ? "imports the picked file" : d.rel === "folder" ? "same folder as the picked file" : "";
    const color = d.rel === "out" ? OUT_COLOR : d.rel === "in" ? IN_COLOR : "var(--muted-foreground)";
    const hint: Node = {
      id: "hint", type: "hint", position: { x: c[0] + d.size / 2 + 10, y: c[1] - 26 }, draggable: false, selectable: false, focusable: false, zIndex: Z.hint,
      data: { name, dir, relation, color } satisfies HintData,
    };
    return [...nodes, hint];
  }, [nodes, centers, hoverDot]);
  const frames = useMemo<Frames>(() => new Map((layout?.depts ?? []).map((d) => [d.id, [d.x, d.y, d.r] as [number, number, number]])), [layout]);
  return (
    <ReactFlow
      nodes={shownNodes}
      edges={edges}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      minZoom={0.03}
      maxZoom={3}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable={false}
      zoomOnDoubleClick={false}
      onlyRenderVisibleElements
      colorMode={p.dark ? "dark" : "light"}
      proOptions={{ hideAttribution: true }}
      onNodeMouseEnter={(_, node) => {
        if (node.id.startsWith("dept:") && !(node.data as DeptData).expanded) setHover(node.id);
        else if (node.type === "dot") setHoverDot(node.id);
      }}
      onNodeMouseLeave={() => { setHover(null); setHoverDot(null); }}
      onNodeClick={(_, node) => {
        if (node.id.startsWith("title:")) { setHover(null); p.onToggleDept((node.data as TitleData).deptId); }
        else if (node.id.startsWith("dept:")) {
          // an open area is closed from its title pill only: clicks on its empty space must not collapse it
          if ((node.data as DeptData).expanded) return;
          setHover(null);
          p.onToggleDept(node.id);
        }
        else if (node.id.startsWith("dir:")) { p.onToggleDir(node.id); p.onSelect(node.id); }
        else if (node.id.startsWith("tag:") || node.id.endsWith(":more")) return;
        else p.onSelect(node.id);
      }}
      onPaneClick={() => p.onSelect(null)}
    >
      <Background gap={28} size={1} color="var(--border)" />
      <Controls showInteractive={false} position="bottom-right" />
      <Fit fitKey={p.fitKey} focus={p.focus} selected={p.selected} quiet={!!p.quiet} centers={centers} frames={frames} inset={p.inset ?? NO_INSET} />
    </ReactFlow>
  );
}

export function MapFlow(props: MapFlowProps) {
  return (
    <ReactFlowProvider>
      <Inner {...props} />
    </ReactFlowProvider>
  );
}
