/** A small, safe XML reader for the document view: elements, attributes and text. It never expands
 * entities beyond the five built-in ones, never loads a DTD or an external file, and stops at a
 * fixed number of elements. Pure, so it is tested without a browser. */

export interface XmlNode { name: string; attrs: [string, string][]; text: string; children: XmlNode[]; line: number }
export type XmlResult = { ok: true; root: XmlNode; elements: number; truncated: boolean } | { ok: false; error: string };

export const MAX_ELEMENTS = 3000;
const MAX_TEXT = 400;
const IDENTITY = ["Include", "Update", "name", "Name", "id", "Id", "key", "Key", "x:Name", "ref", "label", "title", "path"];

export const isMarkdownPath = (p: string) => /\.(md|markdown|mdx)$/i.test(p);
export const isXmlPath = (p: string) =>
  /\.(xml|csproj|vbproj|fsproj|props|targets|xaml|xsd|xslt?|resx|plist|nuspec|wsdl)$/i.test(p) || /(^|\/)(web|app|nuget|packages)\.config$/i.test(p);

/** The attribute that names an element (`Include="Serilog"`), if there is one. */
export function identityOf(n: XmlNode): [string, string] | null {
  for (const k of IDENTITY) {
    const hit = n.attrs.find(([key, v]) => key === k && v);
    if (hit) return hit;
  }
  return null;
}

const ENTITIES: Record<string, string> = { lt: "<", gt: ">", amp: "&", quot: '"', apos: "'" };
const decode = (s: string) =>
  s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e: string) => {
    if (e[0] !== "#") return ENTITIES[e.toLowerCase()] ?? m;
    const code = e[1] === "x" || e[1] === "X" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
    return Number.isFinite(code) && code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : m;
  });

const NAME = /[A-Za-z_][\w:.-]*/y;
const ATTR = /([A-Za-z_][\w:.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')/g;

export function parseXml(source: string): XmlResult {
  const src = source.replace(/^﻿/, "");
  if (/<!ENTITY/i.test(src)) return { ok: false, error: "it declares entities, which are not expanded" };
  const lineAt = (i: number) => { let n = 1; for (let k = src.indexOf("\n"); k !== -1 && k < i; k = src.indexOf("\n", k + 1)) n++; return n; };
  const stack: XmlNode[] = [];
  let root: XmlNode | null = null;
  let count = 0;
  let i = 0;
  const addText = (t: string) => {
    const top = stack[stack.length - 1];
    const clean = decode(t).replace(/\s+/g, " ").trim();
    if (top && clean && top.text.length < MAX_TEXT) top.text = (top.text ? `${top.text} ` : "") + clean.slice(0, MAX_TEXT);
  };
  while (i < src.length) {
    const lt = src.indexOf("<", i);
    if (lt === -1) { addText(src.slice(i)); break; }
    if (lt > i) addText(src.slice(i, lt));
    if (src.startsWith("<!--", lt)) { const e = src.indexOf("-->", lt + 4); if (e === -1) return { ok: false, error: "a comment is never closed" }; i = e + 3; continue; }
    if (src.startsWith("<![CDATA[", lt)) { const e = src.indexOf("]]>", lt + 9); if (e === -1) return { ok: false, error: "a CDATA section is never closed" }; addText(src.slice(lt + 9, e)); i = e + 3; continue; }
    if (src.startsWith("<?", lt)) { const e = src.indexOf("?>", lt + 2); if (e === -1) return { ok: false, error: "a declaration is never closed" }; i = e + 2; continue; }
    if (src.startsWith("<!", lt)) { const e = src.indexOf(">", lt + 2); if (e === -1) return { ok: false, error: "a declaration is never closed" }; i = e + 1; continue; }
    if (src[lt + 1] === "/") {
      const e = src.indexOf(">", lt + 2);
      if (e === -1) return { ok: false, error: `a closing tag is never finished at line ${lineAt(lt)}` };
      const name = src.slice(lt + 2, e).trim();
      const open = stack.pop();
      if (!open || open.name !== name) return { ok: false, error: `</${name}> at line ${lineAt(lt)} does not match ${open ? `<${open.name}>` : "any open element"}` };
      i = e + 1;
      continue;
    }
    NAME.lastIndex = lt + 1;
    const m = NAME.exec(src);
    if (!m) return { ok: false, error: `a "<" at line ${lineAt(lt)} does not start an element` };
    // the end of the tag: the first ">" outside quotes
    let j = lt + 1 + m[0].length;
    let quote = "";
    for (; j < src.length; j++) {
      const c = src[j]!;
      if (quote) { if (c === quote) quote = ""; } else if (c === '"' || c === "'") quote = c; else if (c === ">") break;
    }
    if (j >= src.length) return { ok: false, error: `the tag <${m[0]}> at line ${lineAt(lt)} is never finished` };
    const inner = src.slice(lt + 1 + m[0].length, j);
    const selfClosing = inner.trimEnd().endsWith("/");
    const node: XmlNode = { name: m[0], attrs: [], text: "", children: [], line: lineAt(lt) };
    for (const a of inner.matchAll(ATTR)) node.attrs.push([a[1]!, decode(a[2] ?? a[3] ?? "")]);
    if (++count > MAX_ELEMENTS) return { ok: true, root: root ?? node, elements: MAX_ELEMENTS, truncated: true };
    const parent = stack[stack.length - 1];
    if (parent) parent.children.push(node);
    else if (!root) root = node;
    if (!selfClosing) stack.push(node);
    i = j + 1;
  }
  if (stack.length) return { ok: false, error: `<${stack[stack.length - 1]!.name}> is never closed` };
  if (!root) return { ok: false, error: "there is no element in it" };
  return { ok: true, root, elements: count, truncated: false };
}
