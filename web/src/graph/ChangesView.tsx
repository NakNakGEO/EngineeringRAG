import { FileDiff as FileDiffIcon, GitCommitHorizontal } from "lucide-react";
import { Skeleton } from "../components/ui/misc";
import { useFileDiff, type FileDiff } from "../data";
import { cn } from "../lib/utils";

const WORDS: Record<FileDiff["status"], string> = {
  modified: "Changed since the last commit",
  added: "New file, not committed yet",
  deleted: "Deleted, not committed yet",
  unchanged: "No uncommitted changes",
  missing: "Not available",
};

/** The file as saved on disk against the last commit, like the changes view of an editor. */
export function ChangesView({ projectId, path }: { projectId: string; path: string }) {
  const q = useFileDiff(projectId, path, false);
  const d = q.data;
  if (q.isLoading) return <Skeleton className="h-40 w-full" />;
  if (q.error || !d) return <p className="text-[13px] text-danger">Could not read the changes of this file.</p>;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px]">
        <span className="font-semibold">{WORDS[d.status]}</span>
        {d.additions != null && d.status !== "unchanged" && (
          <span className="font-mono text-[12px]"><b className="text-success">+{d.additions}</b> <b className="text-danger">−{d.deletions}</b></span>
        )}
        {d.base_commit && <span className="inline-flex items-center gap-1 text-[11.5px] text-muted-foreground"><GitCommitHorizontal className="size-3.5" />against {d.base_commit}</span>}
      </div>

      {d.status === "unchanged" && (
        <p className="rounded-md border bg-muted/30 px-3 py-2 text-[13px] text-muted-foreground">
          {d.line_endings_only
            ? "Only the line endings differ (Windows CRLF against LF). The code itself is the same as the last commit."
            : "The saved file is the same as the last commit."}
        </p>
      )}
      {d.too_large && <p className="text-[13px] text-muted-foreground">This file is too large to compare here.</p>}

      {d.hunks.length > 0 && (
        <div className="overflow-auto rounded-md border bg-card font-mono text-[12px] leading-[1.55]" role="table" aria-label="Changed lines">
          {d.hunks.map((h, i) => (
            <div key={i} role="rowgroup">
              <div className="border-y bg-muted/50 px-3 py-0.5 text-[11px] text-muted-foreground first:border-t-0">
                line {h.new_start}{h.lines.some((l) => l.t === "-") ? ` (was ${h.old_start})` : ""}
              </div>
              {h.lines.map((l, j) => (
                <div
                  key={j}
                  role="row"
                  className={cn("flex min-w-max", l.t === "+" && "bg-success/10", l.t === "-" && "bg-danger/10")}
                >
                  <span className="w-10 shrink-0 select-none pr-2 text-right text-muted-foreground/70">{l.o ?? ""}</span>
                  <span className="w-10 shrink-0 select-none pr-2 text-right text-muted-foreground/70">{l.n ?? ""}</span>
                  <span className={cn("w-4 shrink-0 select-none text-center", l.t === "+" && "text-success", l.t === "-" && "text-danger")}>{l.t === " " ? "" : l.t}</span>
                  <span className="whitespace-pre pr-3">{l.s || " "}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
      {d.truncated && <p className="text-[12px] text-muted-foreground">Only the first changed lines are shown.</p>}

      <p className="flex items-start gap-1.5 text-[11.5px] text-muted-foreground">
        <FileDiffIcon className="mt-0.5 size-3.5 shrink-0" />
        This compares what is saved on disk with the last commit. Edits you have not saved in your editor are not visible here.
      </p>
    </div>
  );
}
