import { ModelCard } from "./ModelCard";
import { WebCard } from "./WebCard";

/** Things you can change without editing files: the language model that writes the summaries, and which sites assistants may read. */
export function SettingsPage({ adminToken }: { adminToken: string }) {
  return (
    <div className="mx-auto max-w-2xl space-y-5">
      <ModelCard adminToken={adminToken} />
      <WebCard adminToken={adminToken} />
    </div>
  );
}
