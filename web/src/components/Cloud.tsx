import { useEffect, useRef } from "react";

export interface CloudDot { group: string; weight: number; changed: boolean }

/** Deterministic pseudo-random in [0,1) from a number: the cloud looks the same every render. */
const rnd = (i: number, salt: number) => {
  const x = Math.sin(i * 127.1 + salt * 311.7) * 43758.5453;
  return x - Math.floor(x);
};

/** A still picture of the whole project: one dot per file, one colour sector per department,
 * on a faint polyhedron web. Drawn once per change; nothing moves. */
export function Cloud({ dots, colors, className }: { dots: CloudDot[]; colors: Map<string, string>; className?: string }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const draw = () => {
      const box = canvas.getBoundingClientRect();
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = Math.max(1, Math.floor(box.width * dpr));
      canvas.height = Math.max(1, Math.floor(box.height * dpr));
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, box.width, box.height);
      const cx = box.width / 2, cy = box.height / 2, R = Math.min(box.width, box.height) * 0.46;
      const fg = getComputedStyle(canvas).getPropertyValue("color") || "#888";

      // faint polyhedron web
      ctx.strokeStyle = fg;
      ctx.globalAlpha = 0.14;
      ctx.lineWidth = 1;
      const n = 22;
      const pts = Array.from({ length: n }, (_, i) => {
        const a = (i / n) * Math.PI * 2 + rnd(i, 1) * 0.4;
        const r = R * (0.72 + rnd(i, 2) * 0.28);
        return [cx + Math.cos(a) * r, cy + Math.sin(a) * r] as const;
      });
      pts.forEach((p, i) => {
        for (const k of [1, 3, 7]) {
          const q = pts[(i + k) % n]!;
          ctx.beginPath();
          ctx.moveTo(p[0], p[1]);
          ctx.lineTo(q[0], q[1]);
          ctx.stroke();
        }
      });

      // One soft blob per department, placed around the middle and overlapping its neighbours.
      const groups = [...new Set(dots.map((d) => d.group))];
      const share = new Map(groups.map((g) => [g, dots.filter((d) => d.group === g).length]));
      const total = Math.max(1, dots.length);
      const centre = new Map<string, { x: number; y: number; spread: number }>();
      groups.forEach((g, gi) => {
        const a = (gi / groups.length) * Math.PI * 2 + 0.6;
        const frac = (share.get(g) ?? 0) / total;
        const off = R * (gi === 0 ? 0.05 : 0.2 + 0.18 * rnd(gi, 7));
        centre.set(g, { x: cx + Math.cos(a) * off, y: cy + Math.sin(a) * off, spread: R * (0.14 + 0.34 * Math.sqrt(frac)) });
      });
      ctx.globalAlpha = 0.9;
      dots.forEach((d, i) => {
        const c = centre.get(d.group);
        if (!c) return;
        // sum of uniforms: a bell-shaped scatter, tight in the middle and thin at the edge
        const ox = (rnd(i, 3) + rnd(i, 4) + rnd(i, 5) - 1.5) * 2 * c.spread;
        const oy = (rnd(i, 6) + rnd(i, 8) + rnd(i, 9) - 1.5) * 2 * c.spread;
        ctx.fillStyle = colors.get(d.group) ?? fg;
        const size = d.changed ? 3.2 : 1 + Math.min(1.4, d.weight * 0.25);
        ctx.beginPath();
        ctx.arc(c.x + ox, c.y + oy, size, 0, Math.PI * 2);
        ctx.fill();
        if (d.changed) {
          ctx.strokeStyle = "#ffb020";
          ctx.lineWidth = 1.5;
          ctx.stroke();
        }
      });
      ctx.globalAlpha = 1;
    };
    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(canvas);
    return () => ro.disconnect();
  }, [dots, colors]);

  return <canvas ref={ref} className={className} style={{ color: "var(--muted-foreground)" }} aria-hidden />;
}
