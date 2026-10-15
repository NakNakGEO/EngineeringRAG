import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Github, Globe, KeyRound, RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { explainError } from "../lib/adminError";
import { Badge, ErrorNote, Panel } from "../ui";

interface WebStatus { source: "ui" | "env" | "none"; hosts: string[]; enabled: boolean }
interface Tried { status: string; error: string | null; output: { url: string; title: string; text: string; content_type: string; truncated: boolean } | null }
const SOURCE = { ui: "chosen here", env: "from the .env file", none: "not set" } as const;
/** The two hosts tool onboarding needs to read a GitHub repository. */
const GITHUB = ["github.com", "codeload.github.com"];
const parseHosts = (text: string) => text.split(/[\s,]+/).filter(Boolean);

/** Which sites assistants may read pages from. Empty means web research is off. */
export function WebCard({ adminToken }: { adminToken: string }) {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["web"], queryFn: () => api<WebStatus>("/web"), retry: false });
  const [text, setText] = useState("");
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    if (status.data && !loaded) { setText(status.data.hosts.join("\n")); setLoaded(true); }
  }, [status.data, loaded]);
  const save = useMutation({
    mutationFn: (hosts: string[]) => api<WebStatus>("/web/hosts", { method: "PUT", adminToken, body: JSON.stringify({ hosts }) }),
    onSuccess: (d) => { qc.setQueryData(["web"], d); setText(d.hosts.join("\n")); },
  });
  const reset = useMutation({
    mutationFn: () => api<WebStatus>("/web/hosts", { method: "DELETE", adminToken }),
    onSuccess: (d) => { qc.setQueryData(["web"], d); setText(d.hosts.join("\n")); },
  });
  const [url, setUrl] = useState("");
  const tryIt = useMutation({ mutationFn: () => api<Tried>("/web/try", { method: "POST", body: JSON.stringify({ url }) }) });
  const s = status.data;

  return (
    <Panel
      title="Web research"
      description="Sites an assistant may read text from (https only; pages, never programs). Everything it reads is stored as unverified evidence."
    >
      <ErrorNote error={status.error} />
      {s && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Globe className="size-4 text-muted-foreground" aria-hidden />
          <Badge value={s.enabled ? "on" : "off"} />
          <span className="text-xs text-muted-foreground">{s.enabled ? `${s.hosts.length} site(s), ${SOURCE[s.source]}` : "no site is allowed, so assistants cannot read the web through the system"}</span>
        </div>
      )}
      <p className="text-xs text-muted-foreground">There is no on/off switch: web research is on while at least one site is on this list and off when the list is empty.</p>
      <label className="block space-y-1">
        <span className="label-caps text-[10px] text-muted-foreground">Allowed sites, one per line (example.com or *.example.com)</span>
        <textarea
          value={text} onChange={(e) => setText(e.target.value)} rows={4} spellCheck={false} aria-label="Allowed sites"
          className="w-full rounded-md border bg-card px-3 py-2 font-mono text-[13px]" placeholder={"Empty = off. For example:\ngithub.com"}
        />
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={() => save.mutate(parseHosts(text))} disabled={save.isPending}>{save.isPending ? "Saving…" : "Save the list"}</Button>
        <Button variant="outline" onClick={() => save.mutate([...new Set([...parseHosts(text), ...GITHUB])])} disabled={save.isPending}>
          <Github />Allow GitHub (for onboarding)
        </Button>
        {s?.source === "ui" && <Button variant="ghost" onClick={() => reset.mutate()} disabled={reset.isPending}><RotateCcw />Use the .env value again</Button>}
        {!adminToken && <span className="inline-flex items-center gap-1 text-xs text-muted-foreground"><KeyRound className="size-3.5" />Saving needs the admin token (sidebar).</span>}
      </div>
      {(save.error || reset.error) && <p role="alert" className="text-[13px] text-danger">{explainError(save.error ?? reset.error)}</p>}

      <div className="space-y-2 border-t pt-3">
        <span className="label-caps text-[10px] text-muted-foreground">Try a page</span>
        <div className="flex gap-2">
          <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://github.com/owner/repo" aria-label="Page to try" spellCheck={false} />
          <Button variant="outline" onClick={() => tryIt.mutate()} disabled={!url || tryIt.isPending}>{tryIt.isPending ? "Reading…" : "Read it"}</Button>
        </div>
        {tryIt.data && (tryIt.data.status === "ok" && tryIt.data.output ? (
          <div className="rounded-md border bg-muted/30 p-2.5 text-[13px]">
            <p className="font-semibold">{tryIt.data.output.title || tryIt.data.output.url}</p>
            <p className="mt-1 line-clamp-6 whitespace-pre-wrap text-muted-foreground">{tryIt.data.output.text}</p>
          </div>
        ) : (
          <p role="status" className="text-[13px] text-danger">{(tryIt.data.error ?? "refused").replace(/^DomainError: /, "")}</p>
        ))}
      </div>
    </Panel>
  );
}
