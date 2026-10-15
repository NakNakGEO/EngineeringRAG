import { ChevronRight } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { identityOf, parseXml, type XmlNode } from "../lib/xml";
import { cn } from "../lib/utils";

function Element({ n, depth }: { n: XmlNode; depth: number }) {
  const [open, setOpen] = useState(depth < 2);
  const id = identityOf(n);
  const rest = n.attrs.filter(([k]) => k !== id?.[0]);
  const branch = n.children.length > 0;
  return (
    <li>
      <button
        type="button" onClick={() => branch && setOpen((v) => !v)} aria-expanded={branch ? open : undefined}
        title={`line ${n.line}`}
        className={cn("flex w-full flex-wrap items-baseline gap-x-2 rounded px-1 py-0.5 text-left text-[13px]", branch && "hover:bg-accent/60")}
      >
        {branch ? <ChevronRight className={cn("size-3.5 shrink-0 self-center transition-transform", open && "rotate-90")} aria-hidden /> : <span className="w-3.5 shrink-0" />}
        <span className="font-mono font-semibold text-primary">{n.name}</span>
        {id && <span className="font-medium text-foreground">{id[1]}</span>}
        {rest.slice(0, 4).map(([k, v]) => <span key={k} className="font-mono text-[11.5px] text-muted-foreground">{k}=<span className="text-foreground/80">{v.length > 50 ? `${v.slice(0, 49)}…` : v}</span></span>)}
        {rest.length > 4 && <span className="text-[11px] text-muted-foreground">+{rest.length - 4} more</span>}
        {branch && !open && <span className="text-[11px] text-muted-foreground">{n.children.length} inside</span>}
      </button>
      {n.text && <p className="ml-[22px] text-[13px] leading-5 text-foreground/85">{n.text}</p>}
      {branch && open && (
        <ul className="ml-[7px] border-l pl-3.5">
          {n.children.map((c, i) => <Element key={i} n={c} depth={depth + 1} />)}
        </ul>
      )}
    </li>
  );
}

/** An XML file read like a document: elements as a tree, names first, text under them. When it
 * cannot be read as a whole (cut off or not well-formed) the reason is shown and `fallback` is used. */
export function XmlTree({ text, fallback }: { text: string; fallback: ReactNode }) {
  const parsed = useMemo(() => parseXml(text), [text]);
  if (!parsed.ok) {
    return (
      <div className="space-y-2">
        <p className="rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">Shown as source because this file cannot be drawn as a tree: {parsed.error}.</p>
        {fallback}
      </div>
    );
  }
  return (
    <div className="space-y-2 rounded-md border bg-card px-3 py-2.5">
      <p className="text-xs text-muted-foreground">
        {parsed.elements.toLocaleString()} element{parsed.elements === 1 ? "" : "s"}{parsed.truncated ? ` (only the first ${parsed.elements.toLocaleString()} are shown)` : ""}. Click an element to open or close it; hover for its line number.
      </p>
      <ul><Element n={parsed.root} depth={0} /></ul>
    </div>
  );
}
