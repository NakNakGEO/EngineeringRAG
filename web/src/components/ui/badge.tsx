import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";
import { cn } from "../../lib/utils";

const badgeVariants = cva("inline-flex items-center gap-1.5 whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-semibold tracking-wide uppercase", {
  variants: {
    tone: {
      neutral: "bg-muted text-muted-foreground",
      good: "bg-success-soft text-success",
      warn: "bg-warning-soft text-warning",
      bad: "bg-danger-soft text-danger",
      info: "bg-info-soft text-info",
      outline: "border border-input text-muted-foreground",
    },
  },
  defaultVariants: { tone: "neutral" },
});

export type Tone = NonNullable<VariantProps<typeof badgeVariants>["tone"]>;

export function Badge({ className, tone, dot, ...props }: HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants> & { dot?: boolean }) {
  return (
    <span className={cn(badgeVariants({ tone }), className)} {...props}>
      {dot && <span className="size-1.5 rounded-full bg-current opacity-80" />}
      {props.children}
    </span>
  );
}

/** Status words in the data (completed, failed, DIRTY, OBSERVED …) mapped to a tone. */
export function toneOf(value: string | null | undefined): Tone {
  const v = (value ?? "").toString();
  if (/fail|deny|denied|broken|quarantine|contradict|stale|reject|dirty|error/i.test(v)) return "bad";
  if (/complete|allow|^ok$|trusted|verified|current|selected|passed|approved|accepted|routable|true|healthy/i.test(v)) return "good";
  if (/pending|waiting|require|experimental|running|started|proposed|unverified|new\b/i.test(v)) return "warn";
  if (/observed|raw|writer|info/i.test(v)) return "info";
  return "neutral";
}
