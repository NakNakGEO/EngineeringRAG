import { ApiError } from "../api";

/** A plain-language message for a failed call; the admin-token cases say exactly what to do. */
export function explainError(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 403) return "This needs the admin token, and none is set. Put EIOS_ADMIN_TOKEN in the .env file, restart the api (docker compose up -d api worker), then type the same token in the sidebar.";
    if (e.status === 401) return "The admin token is missing or wrong: paste the EIOS_ADMIN_TOKEN value from the .env file into the sidebar field (it is forgotten when you reload the page).";
    return e.message;
  }
  return e instanceof Error ? e.message : String(e);
}
