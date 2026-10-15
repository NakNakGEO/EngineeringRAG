import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Cpu, KeyRound, RotateCcw, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import { explainError as explain } from "../lib/adminError";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Badge, ErrorNote, Panel } from "../ui";

interface LlmStatus {
  configured: boolean;
  default: string | null;
  providers: { name: string; locality: string; default_model: string }[];
  local: { source: "ui" | "env" | "none"; base_url: string | null; model: string | null; timeout_seconds: number };
  how_to_configure: string;
}
interface LlmTest { ok: boolean; provider: string | null; model: string | null; latency_ms: number | null; reply: string | null; error: string | null }

const DEFAULT_URL = "http://host.docker.internal:11434/v1";
const SOURCE: Record<LlmStatus["local"]["source"], string> = {
  ui: "chosen here",
  env: "from the .env file",
  none: "not set",
};

/** The language model used for the short file summaries: pick a server and a model on this PC. */
export function ModelCard({ adminToken, className }: { adminToken: string; className?: string }) {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["llm"], queryFn: () => api<LlmStatus>("/llm"), retry: false });
  const s = status.data;
  const [url, setUrl] = useState("");
  const [model, setModel] = useState("");
  const [timeout, setTimeoutSeconds] = useState(240);
  // start from what is in use
  useEffect(() => {
    if (!s) return;
    setUrl((u) => u || s.local.base_url || DEFAULT_URL);
    setModel((m) => m || s.local.model || "");
    setTimeoutSeconds(s.local.timeout_seconds);
  }, [s]);

  const models = useQuery({
    queryKey: ["llm-models", url],
    enabled: false,
    queryFn: () => api<string[]>(`/llm/models?base_url=${encodeURIComponent(url)}`, { adminToken }),
    retry: false,
  });
  const save = useMutation({
    mutationFn: () => api<LlmStatus>("/llm/local", { method: "PUT", adminToken, body: JSON.stringify({ base_url: url, model, timeout_seconds: timeout }) }),
    onSuccess: (data) => { qc.setQueryData(["llm"], data); test.reset(); },
  });
  const reset = useMutation({
    mutationFn: () => api<LlmStatus>("/llm/local", { method: "DELETE", adminToken }),
    onSuccess: (data) => { qc.setQueryData(["llm"], data); setUrl(data.local.base_url ?? DEFAULT_URL); setModel(data.local.model ?? ""); },
  });
  const test = useMutation({ mutationFn: () => api<LlmTest>("/llm/test", { method: "POST" }) });
  const found = models.data ?? [];
  const dirty = !!s && (url !== (s.local.base_url ?? "") || model !== (s.local.model ?? ""));

  return (
    <Panel
      title="Language model"
      description="Writes the short file summaries. It runs on the server you pick; on this PC nothing leaves it."
      className={className}
    >
      <ErrorNote error={status.error} />
      {s && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Cpu className="size-4 text-muted-foreground" aria-hidden />
          {s.configured ? (
            <>
              <b className="font-medium">{s.providers.find((p) => p.name === s.default)?.default_model ?? s.providers[0]?.default_model}</b>
              <Badge value="on this PC" />
              <span className="text-xs text-muted-foreground">{SOURCE[s.local.source]}</span>
            </>
          ) : (
            <span className="text-muted-foreground">No model is connected yet.</span>
          )}
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
        <label className="space-y-1">
          <span className="label-caps text-[10px] text-muted-foreground">Server address</span>
          <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder={DEFAULT_URL} spellCheck={false} aria-label="Server address" />
        </label>
        <Button variant="outline" className="self-end" onClick={() => models.refetch()} disabled={!url || models.isFetching}>
          <Search />{models.isFetching ? "Looking…" : "Find models"}
        </Button>
      </div>
      {models.error && <p role="alert" className="text-[13px] text-danger">{explain(models.error)}</p>}

      <label className="block space-y-1">
        <span className="label-caps text-[10px] text-muted-foreground">Model</span>
        {found.length > 0 ? (
          <select
            value={model}
            onChange={(e) => setModel(e.target.value)}
            aria-label="Model"
            className="h-9 w-full rounded-md border bg-card px-2 text-sm"
          >
            {!found.includes(model) && <option value={model}>{model || "choose a model"}</option>}
            {found.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        ) : (
          <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder="for example gemma4:e2b" spellCheck={false} aria-label="Model" />
        )}
        <span className="block text-[11.5px] text-muted-foreground">Small models are enough: the notes are only a few words.</span>
      </label>

      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={() => save.mutate()} disabled={!url || !model || save.isPending}>{save.isPending ? "Saving…" : dirty ? "Save and use it" : "Save"}</Button>
        <Button variant="outline" onClick={() => test.mutate()} disabled={!s?.configured || test.isPending}>{test.isPending ? "Testing…" : "Test it"}</Button>
        {s?.local.source === "ui" && (
          <Button variant="ghost" onClick={() => reset.mutate()} disabled={reset.isPending}><RotateCcw />Use the .env value again</Button>
        )}
        {!adminToken && <span className="inline-flex items-center gap-1 text-xs text-muted-foreground"><KeyRound className="size-3.5" />Saving needs the admin token (sidebar).</span>}
      </div>

      {save.error && <p role="alert" className="text-[13px] text-danger">{explain(save.error)}</p>}
      {reset.error && <p role="alert" className="text-[13px] text-danger">{explain(reset.error)}</p>}
      {test.data && (
        <p className={test.data.ok ? "text-[13px] text-success" : "text-[13px] text-danger"} role="status">
          {test.data.ok ? `Works: ${test.data.model} answered in ${test.data.latency_ms} ms.` : `Failed: ${test.data.error}`}
        </p>
      )}
      <ErrorNote error={test.error} />
      <p className="text-xs text-muted-foreground">Only a server on this PC or your own network is accepted here. Keys for remote services stay in the .env file.</p>
    </Panel>
  );
}
