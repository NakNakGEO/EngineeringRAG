import { useEffect, useRef, useState } from "react";
import { Capabilities } from "./views/Capabilities";
import { Graph } from "./views/Graph";
import { Knowledge } from "./views/Knowledge";
import { Mission } from "./views/Mission";
import { Observability } from "./views/Observability";
import { Projects } from "./views/Projects";
import { Security } from "./views/Security";
import { Workshop } from "./views/Workshop";

const TABS = ["Mission Control", "Execution Graph", "Projects", "Knowledge", "Capabilities", "Workshop", "Security", "Observability"] as const;
type Tab = (typeof TABS)[number];

export function App() {
  const [tab, setTab] = useState<Tab>("Mission Control");
  const [runId, setRunId] = useState<string | null>(null);
  // The admin token lives in memory only: never persisted, never sent anywhere but the API.
  const [adminToken, setAdminToken] = useState("");
  const nav = useRef<HTMLElement>(null);
  // On narrow screens the tab row scrolls sideways: keep the active tab in view.
  useEffect(() => {
    nav.current?.querySelector("button.on")?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [tab]);
  return (
    <div className="app">
      <header className="top">
        <h1>Engineering Intelligence OS</h1>
        <nav ref={nav} aria-label="views">{TABS.map((t) => <button key={t} className={t === tab ? "on" : ""} aria-current={t === tab ? "page" : undefined} onClick={() => setTab(t)}>{t}</button>)}</nav>
        <input type="password" placeholder="admin token" aria-label="admin token" title="admin token for human actions (approvals, workshop)" value={adminToken} onChange={(e) => setAdminToken(e.target.value)} autoComplete="off" />
      </header>
      <main>
        {tab === "Mission Control" && <Mission runId={runId} onRun={setRunId} />}
        {tab === "Execution Graph" && <Graph runId={runId} onRun={setRunId} />}
        {tab === "Projects" && <Projects />}
        {tab === "Knowledge" && <Knowledge />}
        {tab === "Capabilities" && <Capabilities />}
        {tab === "Workshop" && <Workshop adminToken={adminToken} />}
        {tab === "Security" && <Security adminToken={adminToken} />}
        {tab === "Observability" && <Observability initialRunId={runId} onOpen={(id) => { setRunId(id); setTab("Mission Control"); }} />}
      </main>
    </div>
  );
}
