import "@fontsource-variable/inter";
import "@fontsource-variable/space-grotesk";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./index.css";
import { applyTheme, initialTheme } from "./theme";

// After a redeploy an already-open tab may ask for a file name that no longer exists: reload once.
window.addEventListener("vite:preloadError", () => location.reload());

// Before the first paint, so a dark-mode user never sees a white flash.
applyTheme(initialTheme(), false);

const client = new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: 2000, refetchOnWindowFocus: false } } });

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
