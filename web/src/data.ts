import { useQueries, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { buildBrain, type Overview } from "./graph/model";

export interface Project {
  id: string;
  name: string;
  remote_url: string | null;
  last_branch: string | null;
  last_commit: string | null;
  bootstrap_state: string;
  last_synced_at?: string | null;
}

export interface Note {
  id: string;
  title: string;
  kind: string;
  content: string;
  trust: string;
  health: string;
  confidence: number;
  limitations: string;
  vault: string;
  created_by: string;
  updated_at: string;
  tags: string[];
  project_id: string | null;
  metadata: { path?: string } & Record<string, unknown>;
}

export interface FileSymbol {
  name: string;
  qualified_name: string;
  kind: string;
  container: string | null;
  start_line: number;
  end_line: number;
  signature: string;
}
export interface Relation {
  direction: "out" | "in";
  kind: "imports" | "calls" | "references";
  confidence: number;
  own: string;
  line: number | null;
  names: string[];
  other: { key: string; kind: string; label: string; path: string | null };
}
export interface FileDetail {
  path: string;
  language: string;
  lines: number;
  size_bytes: number;
  scope: string;
  status: string;
  symbols: FileSymbol[];
  relations: Relation[];
}
export interface SummaryState {
  status: "ready" | "stale" | "missing" | "no_model" | "error";
  summary: { purpose: string; connects: string; useful: string } | null;
  model: string | null;
  provider: string | null;
  generated_at: string | null;
  content_hash: string | null;
  error: string | null;
}
export interface DiffLine { t: " " | "+" | "-"; o: number | null; n: number | null; s: string }
export interface FileDiff {
  path: string;
  status: "modified" | "added" | "deleted" | "unchanged" | "missing";
  base_commit: string | null;
  line_endings_only: boolean;
  additions: number | null;
  deletions: number | null;
  hunks: { old_start: number; new_start: number; lines: DiffLine[] }[];
  truncated: boolean;
  too_large: boolean;
}
export interface Source { path: string; scope: string; start_line: number; end_line: number; total_lines: number; text: string }
export interface Coverage {
  files_total?: number;
  files_with_symbols?: number;
  symbol_coverage?: number;
  files_with_knowledge?: number;
  languages?: Record<string, number>;
  [k: string]: unknown;
}

export const useProjects = () =>
  useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects"), refetchInterval: 10_000 });

/** The selected project id. It starts as `initial`, else the project with the most files, and then
 * stays put: the project list can reorder while syncs finish, and the page must not jump. */
export function useSelectedProject(initial = ""): [string, (id: string) => void, Project[]] {
  const projects = useProjects();
  const list = useMemo(() => [...(projects.data ?? [])].sort((a, b) => a.name.localeCompare(b.name)), [projects.data]);
  const sizes = useQueries({
    queries: list.map((p) => ({ queryKey: ["coverage", p.id], queryFn: () => api<Coverage>(`/projects/${p.id}/coverage`) })),
  });
  const [id, setId] = useState(initial);
  const settled = list.length > 0 && sizes.every((q) => !q.isLoading);
  useEffect(() => {
    if (id || !settled) return;
    const best = list
      .map((p, i) => ({ p, n: sizes[i]?.data?.files_total ?? 0 }))
      .sort((a, b) => b.n - a.n)[0];
    if (best) setId(best.p.id);
  }, [id, settled, list, sizes]);
  return [id, setId, list];
}

export const useNotes = (projectId: string) =>
  useQuery({
    queryKey: ["notes", projectId],
    enabled: !!projectId,
    queryFn: () => api<Note[]>(`/knowledge?project_id=${projectId}&limit=200`),
  });

export const useCoverage = (projectId: string) =>
  useQuery({ queryKey: ["coverage", projectId], enabled: !!projectId, queryFn: () => api<Coverage>(`/projects/${projectId}/coverage`) });

export const useOverview = (projectId: string) =>
  useQuery({
    queryKey: ["overview", projectId],
    enabled: !!projectId,
    staleTime: 20_000,
    queryFn: () => api<Overview>(`/projects/${projectId}/overview`),
  });

export const useFileDetail = (projectId: string, path: string | null) =>
  useQuery({
    queryKey: ["file", projectId, path],
    enabled: !!projectId && !!path,
    queryFn: () => api<FileDetail>(`/projects/${projectId}/file?path=${encodeURIComponent(path ?? "")}`),
    retry: false,
  });

export const useSource = (projectId: string, path: string | null, enabled: boolean) =>
  useQuery({
    queryKey: ["source", projectId, path],
    enabled: enabled && !!projectId && !!path,
    queryFn: () => api<Source>(`/projects/${projectId}/source?path=${encodeURIComponent(path ?? "")}&max_lines=600`),
    retry: false,
  });

/** The stored summary of a file and whether it still matches it. Never calls the model. */
export const useFileSummary = (projectId: string, path: string | null) =>
  useQuery({
    queryKey: ["summary", projectId, path],
    enabled: !!projectId && !!path,
    refetchInterval: 15_000, // a changed file turns "ready" into "stale" and gets rewritten
    queryFn: () => api<SummaryState>(`/projects/${projectId}/file/summary?path=${encodeURIComponent(path ?? "")}`),
    retry: false,
  });

/** Uncommitted changes of a file. `countsOnly` is the cheap version for the tab label. */
export const useFileDiff = (projectId: string, path: string | null, countsOnly: boolean) =>
  useQuery({
    queryKey: ["diff", projectId, path, countsOnly],
    enabled: !!projectId && !!path,
    refetchInterval: 8_000,
    queryFn: () =>
      api<FileDiff>(`/projects/${projectId}/file/diff?path=${encodeURIComponent(path ?? "")}${countsOnly ? "&counts_only=true" : ""}`),
    retry: false,
  });

/** The project as a graph: files, folders, imports and notes, rebuilt when any of them change. */
export function useBrain(projectId: string) {
  const projects = useProjects();
  const overview = useOverview(projectId);
  const notes = useNotes(projectId);
  const name = projects.data?.find((p) => p.id === projectId)?.name ?? "project";
  const brain = useMemo(
    () =>
      overview.data
        ? buildBrain(
            name,
            overview.data,
            (notes.data ?? []).map((n) => ({ id: n.id, title: n.title, kind: n.kind, path: n.metadata?.path ?? null })),
          )
        : null,
    [overview.data, notes.data, name],
  );
  return { brain, overview, notes, name, loading: overview.isLoading, error: overview.error ?? notes.error };
}
