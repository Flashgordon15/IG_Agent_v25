/** Lightweight markdown renderer for the daily operator digest (no extra deps). */

function inlineFormat(text) {
  const parts = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)/g;
  let last = 0;
  let match;
  let key = 0;
  while ((match = re.exec(text)) !== null) {
    if (match.index > last) {
      parts.push(text.slice(last, match.index));
    }
    const token = match[0];
    if (token.startsWith("**")) {
      parts.push(
        <strong key={key++} className="font-semibold text-foreground">
          {token.slice(2, -2)}
        </strong>,
      );
    } else if (token.startsWith("`")) {
      parts.push(
        <code key={key++} className="rounded bg-card px-1 py-0.5 font-mono text-[10px] text-accent">
          {token.slice(1, -1)}
        </code>,
      );
    } else if (token.startsWith("*")) {
      parts.push(
        <em key={key++} className="text-muted">
          {token.slice(1, -1)}
        </em>,
      );
    }
    last = match.index + token.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts.length ? parts : text;
}

function parseTableRow(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function isTableSep(line) {
  return /^\|?\s*:?-{2,}/.test(line.trim());
}

export function renderDigestMarkdown(markdown) {
  const lines = String(markdown || "").split("\n");
  const blocks = [];
  let i = 0;
  let key = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      i += 1;
      continue;
    }

    if (trimmed === "---") {
      blocks.push(<hr key={key++} className="my-4 border-border" />);
      i += 1;
      continue;
    }

    if (trimmed.startsWith("# ")) {
      blocks.push(
        <h1 key={key++} className="text-base font-bold text-foreground">
          {inlineFormat(trimmed.slice(2))}
        </h1>,
      );
      i += 1;
      continue;
    }

    if (trimmed.startsWith("## ")) {
      blocks.push(
        <h2 key={key++} className="mt-4 text-[13px] font-bold uppercase tracking-wide text-accent">
          {inlineFormat(trimmed.slice(3))}
        </h2>,
      );
      i += 1;
      continue;
    }

    if (trimmed.startsWith("### ")) {
      blocks.push(
        <h3 key={key++} className="mt-3 text-[12px] font-semibold text-foreground">
          {inlineFormat(trimmed.slice(4))}
        </h3>,
      );
      i += 1;
      continue;
    }

    if (trimmed.startsWith("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const header = parseTableRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        rows.push(parseTableRow(lines[i]));
        i += 1;
      }
      blocks.push(
        <div key={key++} className="my-2 overflow-x-auto rounded-lg border border-border">
          <table className="min-w-full text-left text-[11px]">
            <thead className="bg-card/80">
              <tr>
                {header.map((cell, idx) => (
                  <th key={idx} className="px-2.5 py-1.5 font-semibold text-muted">
                    {inlineFormat(cell)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, ridx) => (
                <tr key={ridx} className="border-t border-border/60">
                  {row.map((cell, cidx) => (
                    <td key={cidx} className="px-2.5 py-1.5 text-foreground">
                      {inlineFormat(cell)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    if (/^\d+\.\s/.test(trimmed)) {
      const items = [];
      while (i < lines.length && /^\d+\.\s/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^\d+\.\s*/, ""));
        i += 1;
      }
      blocks.push(
        <ol key={key++} className="my-2 list-decimal space-y-1 pl-5 text-[11px] text-foreground">
          {items.map((item, idx) => (
            <li key={idx}>{inlineFormat(item)}</li>
          ))}
        </ol>,
      );
      continue;
    }

    if (trimmed.startsWith("- ")) {
      const items = [];
      while (i < lines.length && lines[i].trim().startsWith("- ")) {
        items.push(lines[i].trim().slice(2));
        i += 1;
      }
      blocks.push(
        <ul key={key++} className="my-2 list-disc space-y-1 pl-5 text-[11px] text-foreground">
          {items.map((item, idx) => (
            <li key={idx}>{inlineFormat(item)}</li>
          ))}
        </ul>,
      );
      continue;
    }

    const para = [];
    while (i < lines.length && lines[i].trim() && !lines[i].trim().startsWith("#")) {
      if (lines[i].trim().startsWith("|") || lines[i].trim().startsWith("- ")) break;
      if (/^\d+\.\s/.test(lines[i].trim())) break;
      para.push(lines[i].trim());
      i += 1;
    }
    if (para.length) {
      blocks.push(
        <p key={key++} className="my-1 text-[11px] leading-relaxed text-foreground">
          {inlineFormat(para.join(" "))}
        </p>,
      );
    } else {
      i += 1;
    }
  }

  return blocks;
}
