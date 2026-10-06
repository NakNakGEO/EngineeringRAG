import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { Badge, Empty, ErrorNote, Panel, Stat, VirtualList } from "../ui";

interface Project { id: string; name: string; remote_url: string | null; last_branch: string | null; bootstrap_state: string; last_commit: string | null }
interface FileRow { id: string; path: string; language: string; line_count: number; status: string }

export function Projects() {
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects"), refetchInterval: 8000 });
  const [pid, setPid] = useState<string | null>(null);
  const coverage = useQuery({ queryKey: ["coverage", pid], enabled: !!pid, queryFn: () => api<Record<string, unknown>>(`/projects/${pid}/coverage`) });
  const files = useQuery({ queryKey: ["files", pid], enabled: !!pid, queryFn: () => api<FileRow[]>(`/projects/${pid}/files?limit=1000`) });
  const cov = coverage.data ?? {};
  return (
    <div className="grid">
      <Panel title="Projects">
        <ErrorNote error={projects.error} />
        {projects.data?.length === 0 && <Empty>No project bootstrapped yet (POST /projects/bootstrap or MCP bootstrap_project).</Empty>}
        <ul className="list">
          {projects.data?.map((p) => (
            <li key={p.id} className={p.id === pid ? "sel" : ""} onClick={() => setPid(p.id)}>
              <b>{p.name}</b> <Badge value={p.bootstrap_state} /> <small>{p.last_branch} {p.last_commit?.slice(0, 8)}</small>
            </li>
          ))}
        </ul>
      </Panel>
      <Panel title="Semantic coverage">
        {!pid ? <Empty>Select a project.</Empty> : (
          <div className="stats">
            {Object.entries(cov).filter(([, v]) => typeof v !== "object").map(([k, v]) => <Stat key={k} label={k} value={String(v)} />)}
          </div>
        )}
      </Panel>
      <Panel title={`Files (${files.data?.length ?? 0})`}>
        {files.data && (
          <VirtualList items={files.data} keyOf={(f) => f.id} render={(f) => (
            <span className="evrow"><code>{f.path}</code> <small>{f.language} · {f.line_count} lines</small> <Badge value={f.status} /></span>
          )} />
        )}
      </Panel>
    </div>
  );
}
