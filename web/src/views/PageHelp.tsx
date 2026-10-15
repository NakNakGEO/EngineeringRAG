import { ChevronDown, CircleHelp } from "lucide-react";
import { useState, type ReactNode } from "react";
import { cn } from "../lib/utils";

const storageKey = (id: string) => `eios-help-${id}`;
const startsOpen = (id: string) => {
  try { return localStorage.getItem(storageKey(id)) !== "closed"; } catch { return true; }
};

/** A plain-language "what is this page, and why is it here". Open the first time; remembers if hidden. */
export function PageHelp({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  const [open, setOpen] = useState(() => startsOpen(id));
  const toggle = () => {
    const next = !open;
    setOpen(next);
    try { localStorage.setItem(storageKey(id), next ? "open" : "closed"); } catch { /* private window: just not remembered */ }
  };
  return (
    <section className="rounded-lg border bg-muted/30 px-4 py-3 text-[13px] leading-relaxed">
      <button type="button" onClick={toggle} aria-expanded={open} className="flex w-full items-center gap-2 text-left font-medium">
        <CircleHelp className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        <span>{title}</span>
        <ChevronDown className={cn("ml-auto size-4 shrink-0 transition-transform", open && "rotate-180")} aria-hidden />
      </button>
      {open && <div className="mt-2 space-y-2 text-muted-foreground">{children}</div>}
    </section>
  );
}
