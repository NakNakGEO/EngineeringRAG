import { Highlight, Prism, themes } from "prism-react-renderer";
import { useEffect, useMemo, useRef } from "react";
import { cn } from "../lib/utils";

// Names used by the indexer -> names Prism knows. Anything else is shown as plain text.
const LANG: Record<string, string> = {
  python: "python", typescript: "tsx", tsx: "tsx", javascript: "jsx", jsx: "jsx", json: "json", yaml: "yaml",
  markdown: "markdown", sql: "sql", html: "markup", xml: "markup", css: "css", shell: "bash", bash: "bash",
  php: "php", java: "java", csharp: "csharp", go: "go", rust: "rust", ruby: "ruby", toml: "toml",
};

export function CodeView({
  text, startLine = 1, language, mark, dark, className, wrap = false,
}: { text: string; startLine?: number; language: string; mark?: [number, number] | null; dark: boolean; className?: string; wrap?: boolean }) {
  const lang = useMemo(() => {
    const wanted = LANG[language.toLowerCase()] ?? "plain";
    return wanted !== "plain" && (Prism.languages as Record<string, unknown>)[wanted] ? wanted : "plain";
  }, [language]);
  const markRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    markRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [mark?.[0], text]);
  const width = String(startLine + text.split("\n").length).length;

  return (
    <Highlight code={text} language={lang} theme={dark ? themes.vsDark : themes.github}>
      {({ tokens, getLineProps, getTokenProps, style }) => (
        <pre
          className={cn("m-0 max-h-[68dvh] overflow-auto rounded-md border p-0 font-mono text-[12.5px] leading-5", className)}
          style={{ ...style, background: "transparent" }}
        >
          {tokens.map((line, i) => {
            const no = startLine + i;
            const marked = mark ? no >= mark[0] && no <= mark[1] : false;
            const props = getLineProps({ line });
            return (
              <div
                key={i}
                {...props}
                ref={marked && no === mark![0] ? markRef : undefined}
                className={cn("flex px-0", wrap ? "min-w-0" : "min-w-max", marked && "bg-primary/15")}
              >
                <span className="sticky left-0 shrink-0 select-none bg-card px-3 text-right text-muted-foreground/70" style={{ minWidth: `${width + 2}ch` }}>
                  {no}
                </span>
                <span className={cn("pr-4", wrap ? "min-w-0 whitespace-pre-wrap break-words" : "whitespace-pre")}>
                  {line.map((token, k) => (
                    <span key={k} {...getTokenProps({ token })} />
                  ))}
                  {line.length === 0 && " "}
                </span>
              </div>
            );
          })}
        </pre>
      )}
    </Highlight>
  );
}
