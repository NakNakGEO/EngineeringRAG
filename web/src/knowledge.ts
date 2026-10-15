/** Pure view-model helpers of the Knowledge tab (unit-tested; no React). */

export interface KnowledgeHit {
  item: {
    id: string; title: string; kind: string; trust: string; health: string; vault: string;
    content: string; confidence: number;
  };
  score: number;
}

/** One entry of POST /retrieval/search (code symbols, files and knowledge, fused and reranked). */
export interface CodeItem {
  candidate: {
    id: string; kind: string; title: string; content: string; path: string | null;
    start_line: number | null; end_line: number | null; trust: string | null;
    health: string | null; confidence: number | null; scope: string | null;
  };
  score: number;
  sources: string[];
  reasons: string[];
}

/** What the result list and the detail panel show, whichever endpoint produced it. */
export interface SearchResult {
  id: string;
  title: string;
  kind: string;
  trust: string | null;
  health: string | null;
  where: string;
  score: number;
  confidence: number | null;
  body: string;
  sources: string[];
}

export const fromKnowledge = (h: KnowledgeHit): SearchResult => ({
  id: h.item.id, title: h.item.title, kind: h.item.kind, trust: h.item.trust,
  health: h.item.health, where: h.item.vault, score: h.score, confidence: h.item.confidence,
  body: h.item.content, sources: [],
});

export const location = (c: CodeItem["candidate"]): string => {
  if (!c.path) return c.scope ?? "";
  const lines = c.start_line === null ? "" : `:${c.start_line}${c.end_line && c.end_line !== c.start_line ? `-${c.end_line}` : ""}`;
  return `${c.path}${lines}`;
};

export const fromCode = (i: CodeItem): SearchResult => ({
  id: i.candidate.id, title: i.candidate.title, kind: i.candidate.kind, trust: i.candidate.trust,
  health: i.candidate.health, where: location(i.candidate), score: i.score,
  confidence: i.candidate.confidence, body: i.candidate.content, sources: i.sources,
});

export interface VaultState {
  projects: number; // projects whose coverage is known
  files: number; // indexed files
  linked: number; // files with at least one live knowledge item
  empty: boolean; // no knowledge item is linked to any indexed file
}

const count = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** Vault state from `/projects/{id}/coverage` payloads; null while unknown (loading, no project). */
export function vaultState(coverages: (Record<string, unknown> | undefined)[]): VaultState | null {
  let projects = 0, files = 0, linked = 0;
  for (const c of coverages) {
    const withKnowledge = count(c?.files_with_knowledge);
    if (!c || withKnowledge === null) continue;
    projects += 1;
    files += count(c.files_total) ?? 0;
    linked += withKnowledge;
  }
  return projects === 0 ? null : { projects, files, linked, empty: linked === 0 };
}
