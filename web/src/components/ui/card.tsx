import type { HTMLAttributes } from "react";
import { cn } from "../../lib/utils";

export const Card = ({ className, ...p }: HTMLAttributes<HTMLDivElement>) => (
  <div className={cn("min-w-0 rounded-md border bg-card text-foreground", className)} {...p} />
);
export const CardHeader = ({ className, ...p }: HTMLAttributes<HTMLDivElement>) => (
  <div className={cn("flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 pt-3.5 pb-3", className)} {...p} />
);
/** Panel titles are small, uppercase and letter-spaced, like the OS dashboard it is modelled on. */
export const CardTitle = ({ className, ...p }: HTMLAttributes<HTMLHeadingElement>) => (
  <h3 className={cn("label-caps flex items-center gap-2 leading-6", className)} {...p} />
);
export const CardDescription = ({ className, ...p }: HTMLAttributes<HTMLParagraphElement>) => (
  <p className={cn("mt-0.5 text-[13px] leading-5 text-muted-foreground", className)} {...p} />
);
export const CardContent = ({ className, ...p }: HTMLAttributes<HTMLDivElement>) => (
  <div className={cn("px-4 pb-4", className)} {...p} />
);
