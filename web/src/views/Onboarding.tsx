import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PackageSearch } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { EmptyState, ErrorNote } from "../components/ui/misc";
import { explainError } from "../lib/adminError";
import { cn } from "../lib/utils";
import { Badge, Panel } from "../ui";
import { PageHelp } from "./PageHelp";

interface Finding { severity: "high" | "medium" | "low"; rule: string; path: string; line: number; why: string; text: string }
interface Report {
  files: number; languages: Record<string, number>; build_or_install_files: string[]; binaries: { path: string; kind: string }[];
  findings: Finding[]; total_findings?: number; truncated?: boolean; by_severity: Record<string, number>; verdict: string; readme_start: string; license_file: string | null; top_level: string[];
}
interface Candidate {
  id: string; source_url: string; ref: string; archive_sha256: string; archive_bytes: number; state: "quarantined" | "reviewed" | "rejected";
  report: Report; requested_by: string; created_at: string; decided_by: string | null; note: string;
}
const SEV = { high: "text-danger", medium: "text-warning", low: "text-muted-foreground" } as const;

function Item({ c, newer, adminToken, who, onDecided }: { c: Candidate; newer: boolean; adminToken: string; who: string; onDecided: () => void }) {
  const [open, setOpen] = useState(false);
  const decide = useMutation({
    mutationFn: (state: "reviewed" | "rejected") => api(`/onboarding/${c.id}/decision`, { method: "POST", adminToken, body: JSON.stringify({ state, by: who, note: "" }) }),
    onSuccess: onDecided,
  });
  const r = c.report;
  return (
    <li className="space-y-2 rounded-lg border bg-card p-3.5">
      <div className="flex flex-wrap items-center gap-2">
        <Badge value={c.state} />
        <b className="min-w-0 flex-1 truncate text-sm">{c.source_url.replace("https://github.com/", "")}</b>
        <span className="rounded border px-1.5 py-0.5 text-[10.5px] font-medium text-muted-foreground" title="This system only looks at downloaded tools. It does not install or run them.">not installed</span>
        {newer && <span className="rounded border px-1.5 py-0.5 text-[10.5px] text-muted-foreground" title="A newer inspection of the same repository exists below or above.">older report</span>}
        <span className="text-xs text-muted-foreground">{c.ref} · {(c.archive_bytes / 1024).toFixed(0)} KB · {r.files} files</span>
      </div>
      <p className={cn("text-[13px] font-medium", r.by_severity.high ? "text-danger" : r.by_severity.medium ? "text-warning" : "text-muted-foreground")}>{r.verdict}</p>
      <p className="text-xs text-muted-foreground">
        {Object.entries(r.languages).slice(0, 6).map(([k, v]) => `${k} ${v}`).join(" · ")}
        {r.license_file ? ` · licence: ${r.license_file}` : " · no licence file"}
      </p>
      {(r.build_or_install_files.length > 0 || r.binaries.length > 0) && (
        <p className="text-xs text-muted-foreground">
          {r.build_or_install_files.length > 0 && <>Runs something at install/build time: {r.build_or_install_files.slice(0, 6).join(", ")}. </>}
          {r.binaries.length > 0 && <>Contains programs: {r.binaries.slice(0, 4).map((b) => `${b.path} (${b.kind})`).join(", ")}.</>}
        </p>
      )}
      <button className="text-xs underline underline-offset-2" onClick={() => setOpen((v) => !v)}>{open ? "Hide" : "Show"} {r.truncated ? `the ${r.findings.length} most serious of ${r.total_findings} findings` : `the ${r.findings.length} finding(s)`} and the readme</button>
      {open && (
        <div className="space-y-2 rounded-md border bg-muted/30 p-2.5">
          {r.readme_start && <p className="whitespace-pre-wrap text-[12.5px] text-muted-foreground">{r.readme_start}</p>}
          <ul className="space-y-1 font-mono text-[11.5px]">
            {r.findings.slice(0, 40).map((f, i) => (
              <li key={i}><span className={cn("font-semibold uppercase", SEV[f.severity])}>{f.severity}</span> {f.rule} · {f.path}:{f.line}<span className="block truncate text-muted-foreground">{f.text}</span></li>
            ))}
          </ul>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2 pt-1">
        {c.state === "quarantined" ? (
          <>
            <Button size="sm" variant="outline" disabled={!who.trim() || decide.isPending} onClick={() => decide.mutate("reviewed")}>Mark reviewed</Button>
            <Button size="sm" variant="outline" disabled={!who.trim() || decide.isPending} onClick={() => decide.mutate("rejected")}>Reject</Button>
          </>
        ) : <span className="text-xs text-muted-foreground">{c.state} by {c.decided_by}</span>}
        <span className="ml-auto font-mono text-[10.5px] text-muted-foreground" title={c.archive_sha256}>sha256 {c.archive_sha256.slice(0, 12)}…</span>
      </div>
      {decide.error && <p role="alert" className="text-[13px] text-danger">{explainError(decide.error)}</p>}
    </li>
  );
}

/** Step one of bringing in an outside tool: download it to look at it. Nothing is installed or run. */
export function Onboarding({ adminToken }: { adminToken: string }) {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["onboarding"], queryFn: () => api<Candidate[]>("/onboarding"), refetchInterval: 15_000 });
  const [url, setUrl] = useState("");
  const [ref, setRef] = useState("HEAD");
  const [who, setWho] = useState("");
  const inspect = useMutation({
    mutationFn: () => api<Candidate>("/onboarding/inspect", { method: "POST", adminToken, body: JSON.stringify({ url, ref }) }),
    onSuccess: () => { setUrl(""); void qc.invalidateQueries({ queryKey: ["onboarding"] }); },
  });
  const items = list.data ?? [];
  // the same repository inspected again: only the newest report is current
  const hasNewer = (c: Candidate) => items.some((o) => o.source_url === c.source_url && o.ref === c.ref && o.created_at > c.created_at);
  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <PageHelp id="onboarding" title="What happens when I click Download?">
        <p><b>Download and inspect</b> fetches the repository into memory, looks for secrets, risky calls, install hooks and programs, stores the report and a fingerprint, and throws the download away. <b>Nothing is saved to disk, installed or run.</b></p>
        <p><b>Reviewed</b> only records that you looked at the report. It does not install anything, so a reviewed tool is still &quot;not installed&quot;.</p>
        <p><b>Why it stops here:</b> a downloaded tool is someone else&apos;s code. Running it needs a sandbox and your approval, which is the Workshop step and is not built yet. Until then you install a tool yourself, on your own machine, after reading its report.</p>
      </PageHelp>
      <Panel
        title="Tool onboarding"
        description="Download a repository so it can be looked at before anything else. It is read in memory, scanned for secrets, risky calls and install hooks, and thrown away. It is never installed or run here."
      >
        <div className="grid gap-2 sm:grid-cols-[1fr_120px_auto]">
          <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://github.com/owner/repository" aria-label="Repository address" spellCheck={false} />
          <Input value={ref} onChange={(e) => setRef(e.target.value)} placeholder="HEAD" aria-label="Branch or tag" spellCheck={false} />
          <Button onClick={() => inspect.mutate()} disabled={!url.trim() || inspect.isPending}><PackageSearch />{inspect.isPending ? "Inspecting…" : "Download and inspect"}</Button>
        </div>
        <p className="text-xs text-muted-foreground">Needs github.com and codeload.github.com in the web research list (Settings) and the admin token.</p>
        {inspect.error && (
          <p role="alert" className="text-[13px] text-danger">
            {explainError(inspect.error)}{" "}
            {/web research is off|not allowed/i.test(explainError(inspect.error)) && (
              <a href="#/settings" className="font-semibold underline">Open Settings and allow GitHub</a>
            )}
          </p>
        )}
      </Panel>

      <div className="flex items-center gap-2 text-sm">
        <label htmlFor="reviewer" className="text-muted-foreground">Reviewing as</label>
        <Input id="reviewer" className="w-48" value={who} onChange={(e) => setWho(e.target.value)} placeholder="your name" />
      </div>
      <ErrorNote error={list.error} />
      {list.data?.length === 0 && <EmptyState>Nothing has been inspected yet.</EmptyState>}
      <ul className="space-y-3">
        {items.map((c) => <Item key={c.id} c={c} newer={hasNewer(c)} adminToken={adminToken} who={who} onDecided={() => qc.invalidateQueries({ queryKey: ["onboarding"] })} />)}
      </ul>
      <p className="text-xs text-muted-foreground">
        Marking a repository reviewed only records your decision. Trying it out is the next step: the Workshop runs it in the sandbox with a wrapper and tests, and an executable there needs a human approval.
      </p>
    </div>
  );
}
