import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "../../lib/utils";

/** Side panel for small screens (the navigation drawer). */
export function Sheet({
  open, onOpenChange, title, children, side = "left", className,
}: { open: boolean; onOpenChange: (open: boolean) => void; title: string; children: ReactNode; side?: "left" | "right"; className?: string }) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/50" />
        <Dialog.Content
          aria-describedby={undefined}
          className={cn(
            "fixed inset-y-0 z-50 flex w-[300px] max-w-[88vw] flex-col border bg-card shadow-xl focus:outline-none",
            side === "left" ? "left-0 border-l-0" : "right-0 border-r-0",
            className,
          )}
        >
          <Dialog.Title className="sr-only">{title}</Dialog.Title>
          {children}
          <Dialog.Close className="absolute top-3 right-3 rounded-md p-1.5 text-muted-foreground hover:bg-accent" aria-label="Close">
            <X className="size-4" />
          </Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
