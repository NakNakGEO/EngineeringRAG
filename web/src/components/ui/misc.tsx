import { AlertCircle, Inbox } from "lucide-react";
import type { HTMLAttributes, ReactNode } from "react";
import { cn } from "../../lib/utils";

export const Skeleton = ({ className, ...p }: HTMLAttributes<HTMLDivElement>) => (
  <div className={cn("animate-pulse rounded-md bg-muted", className)} {...p} />
);

export function EmptyState({ children, icon, className }: { children: ReactNode; icon?: ReactNode; className?: string }) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-2 px-4 py-10 text-center text-sm text-muted-foreground", className)}>
      {icon ?? <Inbox className="size-7 opacity-60" aria-hidden />}
      <p className="max-w-sm">{children}</p>
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <p role="alert" className="flex items-start gap-2 rounded-md border border-danger/30 bg-danger-soft px-3 py-2 text-[13px] text-danger">
      <AlertCircle className="mt-0.5 size-4 shrink-0" />
      {error instanceof Error ? error.message : String(error)}
    </p>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="min-w-0 rounded-md border bg-card px-3.5 py-3">
      <p className="label-caps truncate text-[10.5px] text-muted-foreground">{label}</p>
      <p className="mt-1 truncate text-xl font-semibold tracking-tight">{value}</p>
      {hint && <p className="truncate text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

export const Kbd = ({ children }: { children: ReactNode }) => (
  <kbd className="rounded border bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">{children}</kbd>
);
