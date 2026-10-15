import { Highlight, Prism, themes } from "prism-react-renderer";
import type { ComponentProps } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import { cn } from "../lib/utils";
import { useTheme } from "../theme";

/** Fenced code blocks: highlighted when Prism knows the language, plain otherwise. */
function Fence({ code, lang }: { code: string; lang: string }) {
  const dark = useTheme() === "dark";
  const known = lang && (Prism.languages as Record<string, unknown>)[lang] ? lang : "plain";
  return (
    <Highlight code={code.replace(/\n$/, "")} language={known} theme={dark ? themes.vsDark : themes.github}>
      {({ tokens, getLineProps, getTokenProps, style }) => (
        <pre className="my-3 overflow-x-auto rounded-md border p-3 font-mono text-[12.5px] leading-5" style={{ ...style, background: "transparent" }}>
          {tokens.map((line, i) => (
            <div key={i} {...getLineProps({ line })}>
              {line.map((token, k) => <span key={k} {...getTokenProps({ token })} />)}
            </div>
          ))}
        </pre>
      )}
    </Highlight>
  );
}

type P<T extends keyof JSX.IntrinsicElements> = ComponentProps<T> & { node?: unknown };

/** Markdown with the app's own typography (no plugin): headings, lists, tables, code, links.
 * Single newlines are kept as line breaks, because the notes were written that way. */
export function Markdown({ children, className }: { children: string; className?: string }) {
  return (
    <div className={cn("text-[14px] leading-7 text-foreground/90 [&>*:first-child]:mt-0", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        components={{
          h1: ({ node: _n, ...p }: P<"h1">) => <h1 className="mt-6 mb-3 text-2xl font-semibold tracking-tight text-foreground" {...p} />,
          h2: ({ node: _n, ...p }: P<"h2">) => <h2 className="mt-6 mb-2 border-b pb-1 text-xl font-semibold tracking-tight text-foreground" {...p} />,
          h3: ({ node: _n, ...p }: P<"h3">) => <h3 className="mt-5 mb-2 text-base font-semibold text-foreground" {...p} />,
          h4: ({ node: _n, ...p }: P<"h4">) => <h4 className="mt-4 mb-1 text-sm font-semibold tracking-wide text-foreground uppercase" {...p} />,
          p: ({ node: _n, ...p }: P<"p">) => <p className="my-3" {...p} />,
          ul: ({ node: _n, ...p }: P<"ul">) => <ul className="my-3 list-disc space-y-1 pl-6 marker:text-muted-foreground" {...p} />,
          ol: ({ node: _n, ...p }: P<"ol">) => <ol className="my-3 list-decimal space-y-1 pl-6 marker:text-muted-foreground" {...p} />,
          li: ({ node: _n, ...p }: P<"li">) => <li className="pl-1" {...p} />,
          a: ({ node: _n, ...p }: P<"a">) => <a className="text-primary underline underline-offset-2 hover:opacity-80" target="_blank" rel="noreferrer noopener" {...p} />,
          blockquote: ({ node: _n, ...p }: P<"blockquote">) => <blockquote className="my-3 border-l-2 border-primary/60 pl-4 text-muted-foreground" {...p} />,
          hr: () => <hr className="my-5" />,
          table: ({ node: _n, ...p }: P<"table">) => <div className="my-4 overflow-x-auto rounded-md border"><table className="w-full border-collapse text-[13px]" {...p} /></div>,
          th: ({ node: _n, ...p }: P<"th">) => <th className="border-b bg-muted px-3 py-1.5 text-left font-semibold" {...p} />,
          td: ({ node: _n, ...p }: P<"td">) => <td className="border-b px-3 py-1.5 align-top" {...p} />,
          img: ({ node: _n, ...p }: P<"img">) => <img className="my-3 max-w-full rounded-md border" loading="lazy" alt="" {...p} />,
          code: ({ node: _n, className: cls, children: kids, ...p }: P<"code">) => {
            const text = String(kids ?? "");
            const lang = /language-([\w-]+)/.exec(cls ?? "")?.[1];
            if (lang || text.includes("\n")) return <Fence code={text} lang={lang ?? ""} />;
            return <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[12.5px] text-foreground" {...p}>{kids}</code>;
          },
          pre: ({ children: kids }: P<"pre">) => <>{kids}</>,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
