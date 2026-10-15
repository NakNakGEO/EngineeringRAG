import { useMutation, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Skeleton } from "../components/ui/misc";
import { useFileSummary, type SummaryState } from "../data";
import { cn } from "../lib/utils";

const ROWS: [keyof NonNullable<SummaryState["summary"]>, string][] = [
  ["purpose", "Used for"],
  ["connects", "Connects to"],
  ["useful", "Worth knowing"],
];

/** Three short lines about a file, written once by the local model and written again only when the
 * file changes. Without a model it says so; it never invents text. */
export function Summary({ projectId, path, className }: { projectId: string; path: string; className?: string }) {
  const q = useFileSummary(projectId, path);
  const qc = useQueryClient();
  const key = ["summary", projectId, path];
  const make = useMutation({
    mutationFn: (force: boolean) =>
      api<SummaryState>(`/projects/${projectId}/file/summary`, { method: "POST", body: JSON.stringify({ path, force }) }),
    onSuccess: (data) => qc.setQueryData(key, data),
  });
  const state = make.data ?? q.data;
  // write the summary for the version of the file that is open, once per version
  const asked = useRef("");
  useEffect(() => {
    const s = q.data;
    if (!s || (s.status !== "missing" && s.status !== "stale")) return;
    const id = `${path}|${s.content_hash}`;
    if (asked.current === id || make.isPending) return;
    asked.current = id;
    make.mutate(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q.data?.status, q.data?.content_hash, path]);

  const working = make.isPending || (!state && q.isLoading);
  // a local model that was idle has to be loaded first: say so instead of looking stuck
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!make.isPending) { setSlow(false); return; }
    const t = setTimeout(() => setSlow(true), 6000);
    return () => clearTimeout(t);
  }, [make.isPending]);
  const summary = state?.summary;
  return (
    <section aria-label="Summary" className={cn("rounded-lg border bg-muted/30 px-3 py-2.5", className)}>
      {summary ? (
        <dl className={cn("space-y-1.5 text-[13px]", working && "opacity-60")}>
          {ROWS.filter(([k]) => summary[k]).map(([k, label]) => (
            <div key={k} className="grid grid-cols-[88px_1fr] gap-2">
              <dt className="text-[11px] font-semibold tracking-wide text-muted-foreground uppercase">{label}</dt>
              <dd className="min-w-0 leading-snug">{summary[k]}</dd>
            </div>
          ))}
        </dl>
      ) : working ? (
        <div className="space-y-2" aria-busy>
          <Skeleton className="h-3.5 w-3/4" />
          <Skeleton className="h-3.5 w-full" />
        </div>
      ) : state?.status === "no_model" ? (
        <p className="text-[12.5px] text-muted-foreground">No language model is connected, so there is no summary. <a className="underline underline-offset-2 hover:text-foreground" href="#/settings">Add one in Settings</a>.</p>
      ) : state?.status === "error" ? (
        <p className="text-[12.5px] text-danger">Could not write a summary: {state.error}</p>
      ) : state?.status === "missing" ? (
        <p className="text-[12.5px] text-muted-foreground">This file is not indexed.</p>
      ) : null}

      <div className="mt-2 flex items-center gap-2 text-[11px] text-muted-foreground">
        <Sparkles className="size-3" aria-hidden />
        <span className="min-w-0 flex-1 truncate">
          {working ? (slow ? "loading the model, the first summary can take a minute…" : "writing…") : state?.model ? `${state.model} · ${state.provider === "local" ? "on this PC" : state.provider} · rewritten when the file changes` : "AI summary"}
          {!working && state?.status === "stale" ? " · out of date" : ""}
        </span>
        <a href="#/settings" className="shrink-0 underline-offset-2 hover:text-foreground hover:underline">model settings</a>
        {state && state.status !== "no_model" && (
          <button
            className="rounded p-1 hover:bg-accent disabled:opacity-40"
            onClick={() => make.mutate(true)}
            disabled={make.isPending}
            aria-label="Write this summary again"
            title="Write it again"
          >
            <RefreshCw className={cn("size-3", make.isPending && "animate-spin")} />
          </button>
        )}
      </div>
    </section>
  );
}
