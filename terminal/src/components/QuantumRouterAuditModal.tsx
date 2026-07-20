"use client";

import { useCallback, useEffect, useId, useState, type ReactNode } from "react";
import {
  QUANTUM_ROUTER_AUDIT_MD,
  ROUTER_AUDIT_BUTTON_LABEL,
} from "@/content/quantumRouterAudit";

type Props = {
  triggerClassName?: string;
};

function inlineFormat(text: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const re = /\*\*(.+?)\*\*|`(.+?)`/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let key = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      parts.push(text.slice(last, m.index));
    }
    if (m[1] != null) {
      parts.push(
        <strong key={`b-${key++}`} className="blueprint-strong">
          {m[1]}
        </strong>,
      );
    } else if (m[2] != null) {
      parts.push(
        <code key={`c-${key++}`} className="blueprint-code">
          {m[2]}
        </code>,
      );
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

function MarkdownBody({ source }: { source: string }) {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const nodes: ReactNode[] = [];
  let i = 0;
  let listBuf: { ordered: boolean; items: string[] } | null = null;

  const flushList = () => {
    if (!listBuf || listBuf.items.length === 0) {
      listBuf = null;
      return;
    }
    const Tag = listBuf.ordered ? "ol" : "ul";
    const cls = listBuf.ordered ? "blueprint-ol" : "blueprint-ul";
    nodes.push(
      <Tag key={`list-${nodes.length}`} className={cls}>
        {listBuf.items.map((item, idx) => (
          <li key={idx}>{inlineFormat(item)}</li>
        ))}
      </Tag>,
    );
    listBuf = null;
  };

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();
    if (!trimmed) {
      flushList();
      i += 1;
      continue;
    }
    if (trimmed.startsWith("```")) {
      flushList();
      const lang = trimmed.slice(3).trim();
      const codeLines: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        codeLines.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;
      nodes.push(
        <pre
          key={`pre-${nodes.length}`}
          className="blueprint-pre"
          data-lang={lang || undefined}
        >
          <code>{codeLines.join("\n")}</code>
        </pre>,
      );
      continue;
    }
    if (trimmed.startsWith("|") && trimmed.endsWith("|")) {
      flushList();
      const tableLines: string[] = [];
      while (i < lines.length) {
        const row = lines[i].trim();
        if (!(row.startsWith("|") && row.endsWith("|"))) break;
        tableLines.push(row);
        i += 1;
      }
      const parseRow = (row: string) =>
        row
          .slice(1, -1)
          .split("|")
          .map((c) => c.trim());
      const isSep = (cells: string[]) =>
        cells.every((c) => /^:?-{3,}:?$/.test(c));
      const header = parseRow(tableLines[0] || "");
      let bodyStart = 1;
      if (tableLines[1] && isSep(parseRow(tableLines[1]))) bodyStart = 2;
      const body = tableLines.slice(bodyStart).map(parseRow);
      nodes.push(
        <table key={`tbl-${nodes.length}`} className="blueprint-table">
          <thead>
            <tr>
              {header.map((h, hi) => (
                <th key={hi}>{inlineFormat(h)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {body.map((row, ri) => (
              <tr key={ri}>
                {row.map((cell, ci) => (
                  <td key={ci}>{inlineFormat(cell)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>,
      );
      continue;
    }
    if (trimmed.startsWith("# ")) {
      flushList();
      nodes.push(
        <h1 key={`h1-${i}`} className="blueprint-h1">
          {inlineFormat(trimmed.slice(2))}
        </h1>,
      );
      i += 1;
      continue;
    }
    if (trimmed.startsWith("## ")) {
      flushList();
      nodes.push(
        <h2 key={`h2-${i}`} className="blueprint-h2">
          {inlineFormat(trimmed.slice(3))}
        </h2>,
      );
      i += 1;
      continue;
    }
    if (trimmed.startsWith("### ")) {
      flushList();
      nodes.push(
        <h3 key={`h3-${i}`} className="blueprint-h3">
          {inlineFormat(trimmed.slice(4))}
        </h3>,
      );
      i += 1;
      continue;
    }
    if (trimmed === "---") {
      flushList();
      nodes.push(<hr key={`hr-${i}`} className="blueprint-hr" />);
      i += 1;
      continue;
    }
    const ul = trimmed.match(/^[-*]\s+(.+)$/);
    if (ul) {
      if (!listBuf || listBuf.ordered) {
        flushList();
        listBuf = { ordered: false, items: [] };
      }
      listBuf.items.push(ul[1]);
      i += 1;
      continue;
    }
    const ol = trimmed.match(/^\d+\.\s+(.+)$/);
    if (ol) {
      if (!listBuf || !listBuf.ordered) {
        flushList();
        listBuf = { ordered: true, items: [] };
      }
      listBuf.items.push(ol[1]);
      i += 1;
      continue;
    }
    if (trimmed.startsWith("> ")) {
      flushList();
      nodes.push(
        <blockquote key={`bq-${i}`} className="blueprint-quote">
          {inlineFormat(trimmed.slice(2))}
        </blockquote>,
      );
      i += 1;
      continue;
    }
    flushList();
    nodes.push(
      <p key={`p-${i}`} className="blueprint-p">
        {inlineFormat(trimmed)}
      </p>,
    );
    i += 1;
  }
  flushList();
  return <div className="blueprint-body">{nodes}</div>;
}

export function QuantumRouterAuditModal({ triggerClassName }: Props) {
  const [open, setOpen] = useState(false);
  const titleId = useId();

  const close = useCallback(() => setOpen(false), []);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open, close]);

  return (
    <>
      <button
        type="button"
        className={triggerClassName ?? "blueprint-trigger blueprint-trigger--router"}
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-expanded={open}
      >
        {ROUTER_AUDIT_BUTTON_LABEL}
      </button>

      {open ? (
        <div
          className="blueprint-overlay"
          role="presentation"
          onClick={close}
        >
          <div
            className="blueprint-modal blueprint-modal--router"
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            onClick={(e) => e.stopPropagation()}
          >
            <header className="blueprint-modal-head">
              <div>
                <p className="blueprint-kicker">
                  Multi-Market · Quantum Router Audit
                </p>
                <h2 id={titleId} className="blueprint-modal-title">
                  MULTI-MARKET DEPLOYMENT BLUEPRINT
                </h2>
              </div>
              <button
                type="button"
                className="blueprint-close"
                onClick={close}
                aria-label="Close router audit"
              >
                CLOSE
              </button>
            </header>
            <div className="blueprint-scroll">
              <MarkdownBody source={QUANTUM_ROUTER_AUDIT_MD} />
              <p className="blueprint-foot">
                Source of truth:{" "}
                <code className="blueprint-code">QUANTUM_ROUTER_AUDIT.md</code>
                {" · "}
                also served at{" "}
                <code className="blueprint-code">/QUANTUM_ROUTER_AUDIT.md</code>
                {" · "}
                code-only embed · no agent disk I/O
              </p>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
