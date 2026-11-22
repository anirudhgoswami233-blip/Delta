"use client";

/**
 * A deliberately small markdown renderer for the race agent's text: headings,
 * paragraphs, bullet and numbered lists, tables, **bold**, *italic* and `code`.
 *
 * It builds React elements and never injects HTML, so model or feed text can
 * not smuggle markup into the page.
 */

import { Fragment, type ReactNode } from "react";

function inline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  const pattern = /(\*\*[^*]+\*\*|\*[^*\s][^*]*\*|`[^`]+`)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let n = 0;
  while ((match = pattern.exec(text))) {
    if (match.index > last) out.push(text.slice(last, match.index));
    const token = match[0];
    const key = `${keyBase}-${n++}`;
    if (token.startsWith("**")) {
      out.push(<strong key={key} className="font-semibold text-ink">{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("`")) {
      out.push(<code key={key} className="num rounded bg-graphite-800 px-1 text-[0.95em]">{token.slice(1, -1)}</code>);
    } else {
      out.push(<em key={key}>{token.slice(1, -1)}</em>);
    }
    last = match.index + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const isTableRow = (line: string) => /^\s*\|.*\|\s*$/.test(line);
const cells = (line: string) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

export function Markdown({ text, className = "" }: { text: string; className?: string }) {
  const lines = text.replace(/\r/g, "").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let key = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      const cls = level <= 2 ? "mt-3 text-sm font-semibold text-ink" : "mt-2 text-xs font-semibold uppercase tracking-wider text-ink-muted";
      blocks.push(<p key={key++} className={cls}>{inline(heading[2], `h${key}`)}</p>);
      i += 1;
      continue;
    }
    if (isTableRow(line)) {
      const rows: string[][] = [];
      while (i < lines.length && isTableRow(lines[i])) {
        const row = cells(lines[i]);
        if (!row.every((c) => /^:?-{2,}:?$/.test(c))) rows.push(row);
        i += 1;
      }
      const [head, ...body] = rows;
      blocks.push(
        <div key={key++} className="my-2 overflow-x-auto">
          <table className="data-table">
            <thead><tr>{head?.map((c, j) => <th key={j}>{inline(c, `th${key}-${j}`)}</th>)}</tr></thead>
            <tbody>
              {body.map((row, r) => (
                <tr key={r}>{row.map((c, j) => <td key={j} className="whitespace-normal">{inline(c, `td${key}-${r}-${j}`)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*•]|\d+[.)])\s+/, ""));
        i += 1;
      }
      const List = ordered ? "ol" : "ul";
      blocks.push(
        <List key={key++} className={`my-1.5 space-y-1 pl-4 ${ordered ? "list-decimal" : "list-disc"} marker:text-ink-faint`}>
          {items.map((item, j) => <li key={j}>{inline(item, `li${key}-${j}`)}</li>)}
        </List>,
      );
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4})\s/.test(lines[i]) && !isTableRow(lines[i])
           && !/^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) {
      para.push(lines[i]);
      i += 1;
    }
    blocks.push(
      <p key={key++} className="my-1.5">
        {para.map((p, j) => <Fragment key={j}>{j > 0 && " "}{inline(p, `p${key}-${j}`)}</Fragment>)}
      </p>,
    );
  }
  return <div className={`text-xs leading-relaxed text-ink-muted ${className}`}>{blocks}</div>;
}
