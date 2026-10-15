import {
  Activity, BookOpen, FlaskConical, FolderGit2, Hexagon, Home as HomeIcon, KeyRound, Layers, Menu, Moon, Network,
  NotebookPen, PackageSearch, PanelLeftClose, PanelLeftOpen, Radar, ShieldCheck, SlidersHorizontal, Sun, Workflow, type LucideIcon,
} from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { Badge } from "./components/ui/badge";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import { Sheet } from "./components/ui/sheet";
import { TooltipProvider } from "./components/ui/tooltip";
import { LiveProvider, useLive } from "./live";
import { cn } from "./lib/utils";
import { loadNumber, saveNumber } from "./lib/useMedia";
import { applyTheme, initialTheme, ThemeContext, type Theme } from "./theme";
import { Capabilities } from "./views/Capabilities";
import { Home, type Target } from "./views/Home";
import { Knowledge } from "./views/Knowledge";
import { Mission } from "./views/Mission";
import { Observability } from "./views/Observability";
import { Projects } from "./views/Projects";
import { Security } from "./views/Security";
import { Memory } from "./views/Memory";
import { Onboarding } from "./views/Onboarding";
import { SettingsPage } from "./views/SettingsPage";
import { Workshop } from "./views/Workshop";

// The map and run graph pull in React Flow: load them only when opened.
const Brain = lazy(() => import("./views/Brain").then((m) => ({ default: m.Brain })));
const Graph = lazy(() => import("./views/Graph").then((m) => ({ default: m.Graph })));

type Tab =
  | "Start here" | "Project Map" | "Knowledge" | "Memory" | "Projects"
  | "Mission Control" | "Run Graph" | "Observability"
  | "Capabilities" | "Workshop" | "Onboarding" | "Security" | "Settings";

interface Meta { icon: LucideIcon; slug: string }
const META: Record<Tab, Meta> = {
  "Start here": { icon: HomeIcon, slug: "start" },
  "Project Map": { icon: Network, slug: "map" },
  Knowledge: { icon: BookOpen, slug: "knowledge" },
  Memory: { icon: NotebookPen, slug: "memory" },
  Projects: { icon: FolderGit2, slug: "projects" },
  "Mission Control": { icon: Activity, slug: "mission" },
  "Run Graph": { icon: Workflow, slug: "run-graph" },
  Observability: { icon: Radar, slug: "observability" },
  Capabilities: { icon: Layers, slug: "capabilities" },
  Workshop: { icon: FlaskConical, slug: "workshop" },
  Onboarding: { icon: PackageSearch, slug: "onboarding" },
  Security: { icon: ShieldCheck, slug: "security" },
  Settings: { icon: SlidersHorizontal, slug: "settings" },
};
const GROUPS: { label: string; tabs: Tab[] }[] = [
  { label: "Explore", tabs: ["Start here", "Project Map", "Knowledge", "Memory", "Projects"] },
  { label: "Activity", tabs: ["Mission Control", "Run Graph", "Observability"] },
  { label: "System", tabs: ["Capabilities", "Workshop", "Onboarding", "Security", "Settings"] },
];
const bySlug = (slug: string): Tab | undefined => (Object.keys(META) as Tab[]).find((t) => META[t].slug === slug);

const RAIL_KEY = "eios-rail-expanded";

/** The navigation. Collapsed it is an icon rail; expanded it shows labels. */
function Rail({ tab, go, expanded, theme, onTheme, token, onToken, onToggle, forceExpanded }: {
  tab: Tab; go: (t: Tab) => void; expanded: boolean; theme: Theme; onTheme: () => void; token: string; onToken: (v: string) => void;
  onToggle?: () => void; forceExpanded?: boolean;
}) {
  const wide = expanded || !!forceExpanded;
  return (
    <div className="flex h-full flex-col gap-3 p-2">
      <button onClick={() => go("Start here")} className={cn("flex h-11 items-center gap-2.5 rounded px-2 hover:bg-accent", !wide && "justify-center")} aria-label="Engineering OS, start page">
        <Hexagon className="size-6 shrink-0 text-primary" strokeWidth={2.2} />
        {wide && <span className="label-caps text-[13px]">Engineering <span className="text-primary">OS</span></span>}
      </button>
      <nav aria-label="Main" className="flex-1 space-y-4 overflow-y-auto">
        {GROUPS.map((g) => (
          <div key={g.label}>
            {wide ? <p className="label-caps px-2.5 pb-1 text-[10px] text-muted-foreground">{g.label}</p> : <hr className="mx-2 mb-2" />}
            <ul className="space-y-0.5">
              {g.tabs.map((t) => {
                const Icon = META[t].icon;
                const active = t === tab;
                return (
                  <li key={t}>
                    <button
                      aria-current={active ? "page" : undefined}
                      title={wide ? undefined : t}
                      aria-label={t}
                      onClick={() => go(t)}
                      className={cn(
                        "flex h-10 w-full items-center gap-3 rounded px-2.5 text-[13.5px] font-semibold",
                        !wide && "justify-center px-0",
                        active ? "bg-accent text-primary" : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                      )}
                    >
                      <Icon className="size-[18px] shrink-0" />{wide && t}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>
      <div className="space-y-2 border-t pt-2">
        {wide && (
          <div className="space-y-1.5 px-1">
            <label htmlFor="admin-token" className="label-caps flex items-center gap-1.5 text-[10px] text-muted-foreground"><KeyRound className="size-3" />Admin token</label>
            <Input id="admin-token" type="password" autoComplete="off" placeholder="only for approvals" value={token} onChange={(e) => onToken(e.target.value)} className="h-8 text-[13px]" />
          </div>
        )}
        <Button variant="ghost" className={cn("w-full justify-start gap-3 px-2.5", !wide && "justify-center px-0")} onClick={onTheme} title="Switch theme" aria-label="Switch theme">
          {theme === "dark" ? <Sun /> : <Moon />}{wide && (theme === "dark" ? "Light mode" : "Dark mode")}
        </Button>
        {onToggle && (
          <Button variant="ghost" className={cn("w-full justify-start gap-3 px-2.5", !wide && "justify-center px-0")} onClick={onToggle} aria-label={expanded ? "Collapse menu" : "Expand menu"} title={expanded ? "Collapse menu" : "Expand menu"}>
            {expanded ? <PanelLeftClose /> : <PanelLeftOpen />}{wide && "Collapse"}
          </Button>
        )}
      </div>
    </div>
  );
}

function LiveDot() {
  const live = useLive();
  return <Badge tone={live.online ? "good" : "bad"} dot>{live.online ? "live" : "offline"}</Badge>;
}

export function App() {
  const [tab, setTab] = useState<Tab>(() => bySlug(location.hash.replace(/^#\/?/, "")) ?? "Start here");
  const [runId, setRunId] = useState<string | null>(null);
  const [project, setProject] = useState("");
  const [file, setFile] = useState("");
  // The admin token lives in memory only: never persisted, never sent anywhere but the API.
  const [adminToken, setAdminToken] = useState("");
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [menu, setMenu] = useState(false); // phone drawer
  const [expanded, setExpanded] = useState(() => loadNumber(RAIL_KEY, 1) === 1);
  useEffect(() => { applyTheme(theme, false); }, [theme]);
  useEffect(() => {
    const onHash = () => { const t = bySlug(location.hash.replace(/^#\/?/, "")); if (t) setTab(t); };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const go = useCallback((t: Tab) => {
    setTab(t);
    setMenu(false);
    history.replaceState(null, "", `#/${META[t].slug}`);
    window.scrollTo?.({ top: 0 });
  }, []);
  const open = (t: Target, id: string, path = "") => { setProject(id); setFile(path); go(t); };
  const toggleTheme = () => { const next: Theme = theme === "dark" ? "light" : "dark"; setTheme(next); applyTheme(next); };
  const toggleRail = () => { setExpanded((e) => { saveNumber(RAIL_KEY, e ? 0 : 1); return !e; }); };
  const full = tab === "Project Map";
  const railProps = { tab, go, theme, onTheme: toggleTheme, token: adminToken, onToken: setAdminToken };

  return (
    <ThemeContext.Provider value={theme}>
      <TooltipProvider>
        <LiveProvider>
          <div className="flex min-h-dvh">
            {/* menu on the left */}
            <aside className={cn("sticky top-0 hidden h-dvh shrink-0 border-r bg-card lg:block", expanded ? "w-56" : "w-[60px]")} aria-label="Menu">
              <Rail {...railProps} expanded={expanded} onToggle={toggleRail} />
            </aside>
            <Sheet open={menu} onOpenChange={setMenu} title="Menu" side="left">
              <Rail {...railProps} expanded forceExpanded />
            </Sheet>

            <div className="min-w-0 flex-1">
              <header className="sticky top-0 z-40 flex h-14 items-center gap-3 border-b bg-background/95 px-3 backdrop-blur sm:px-5">
                <Button variant="outline" size="sm" className="lg:hidden" onClick={() => setMenu(true)} aria-label="Open menu"><Menu />Menu</Button>
                <h1 className="label-caps min-w-0 flex-1 truncate text-[13px]">{tab}</h1>
                <LiveDot />
              </header>

              <main className={cn(!full && "mx-auto w-full max-w-[1500px] px-3 py-5 sm:px-6 sm:py-6")}>
                <Suspense fallback={<p className="p-6 text-sm text-muted-foreground">Loading…</p>}>
                  {tab === "Start here" && <Home onOpen={open} />}
                  {tab === "Project Map" && <Brain key={`${project}|${file}`} initialProject={project} initialFile={file} onBack={() => go("Start here")} />}
                  {tab === "Knowledge" && <Knowledge key={project} initialProject={project} onOpenMap={(id, path) => open("Project Map", id, path)} />}
                  {tab === "Memory" && <Memory key={project} adminToken={adminToken} initialProject={project} />}
                  {tab === "Projects" && <Projects initialProject={project} />}
                  {tab === "Mission Control" && <Mission runId={runId} onRun={setRunId} />}
                  {tab === "Run Graph" && <Graph runId={runId} onRun={setRunId} />}
                  {tab === "Capabilities" && <Capabilities />}
                  {tab === "Workshop" && <Workshop adminToken={adminToken} />}
                  {tab === "Onboarding" && <Onboarding adminToken={adminToken} />}
                  {tab === "Security" && <Security adminToken={adminToken} />}
                  {tab === "Settings" && <SettingsPage adminToken={adminToken} />}
                  {tab === "Observability" && <Observability initialRunId={runId} onOpen={(id) => { setRunId(id); go("Mission Control"); }} />}
                </Suspense>
              </main>
            </div>
          </div>
        </LiveProvider>
      </TooltipProvider>
    </ThemeContext.Provider>
  );
}
