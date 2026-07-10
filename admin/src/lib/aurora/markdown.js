// Compact, dependency-free Markdown -> HTML for chat answers.
// HTML is escaped FIRST, so the output is safe to use with {@html}.
// Supports: headings, bold/italic/code, fenced code, ordered/unordered lists,
// GFM pipe tables, links, and paragraphs. Also linkifies 10-digit article codes
// into clickable chips (data-code) for the source drawer.

const esc = (s) =>
  s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

const chip = (c) =>
  `<button type="button" class="md-code-chip" data-code="${c}">${c}</button>`;

// Sentinel for already-emitted HTML. `esc()` strips <, >, &, " from the source,
// and \x00 cannot survive JSON transport, so this can never collide with content.
const HOLE = (i) => `\x00${i}\x00`;

function inline(s) {
  // Emitted HTML is parked in `held` and restored last. Without this, the
  // bare-article-code pass below re-scans the digits inside a chip we just
  // emitted (they sit right after a `>`), producing nested <button> elements.
  const held = [];
  const park = (html) => {
    held.push(html);
    return HOLE(held.length - 1);
  };

  let out = s
    // Inline code — article codes render as clickable chips.
    //
    // The content may not contain `*` or whitespace. 53% of this catalog's
    // product names use a backtick as an apostrophe ("PARACAP PARACETAMOL
    // 10`S"), so a permissive [^`]+ pairs that stray backtick with the one
    // opening the article code that follows, swallowing the bold marker and
    // the paren between them:
    //     **NAME 10`S** (`1000000380965`)
    //   -> <strong>NAME 10<code>S</strong> (</code>1000000380965`)
    // Excluding `*` and whitespace makes that span unmatchable, so the regex
    // skips ahead and pairs the real code delimiters instead. The apostrophe
    // backtick is then left as the literal character it is meant to be.
    .replace(/`([^`\s*]+)`/g, (_m, c) =>
      park(/^\d{10,14}$/.test(c) ? chip(c) : `<code class="md-code">${c}</code>`)
    )
    // bold then italic
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>')
    // links [t](u)
    .replace(
      /\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$1" target="_blank" rel="noopener" class="md-link">$1</a>'
    )
    // bare article codes (10–14 digits) not already wrapped in a tag
    .replace(/(^|[\s(>])(\d{10,14})(?=[\s).,<]|$)/g, (_m, pre, c) => pre + park(chip(c)));

  // Restore innermost-last; parked HTML never contains a sentinel itself.
  return out.replace(/\x00(\d+)\x00/g, (_m, i) => held[Number(i)]);
}

export function renderMarkdown(src) {
  if (!src) return '';
  const lines = esc(src).split('\n');
  const out = [];
  let i = 0;

  const flushPara = (buf) => {
    if (buf.length) out.push(`<p>${inline(buf.join(' '))}</p>`);
    return [];
  };
  let para = [];

  while (i < lines.length) {
    const line = lines[i];

    // fenced code
    if (/^```/.test(line.trim())) {
      para = flushPara(para);
      const body = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) body.push(lines[i++]);
      i++;
      out.push(`<pre class="md-pre"><code>${body.join('\n')}</code></pre>`);
      continue;
    }

    // heading
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      para = flushPara(para);
      const lvl = Math.min(h[1].length + 1, 4); // h1->h2 etc (keep page h1 reserved)
      out.push(`<h${lvl} class="md-h">${inline(h[2])}</h${lvl}>`);
      i++;
      continue;
    }

    // table (header row + separator)
    if (line.includes('|') && i + 1 < lines.length && /^[\s|:-]+$/.test(lines[i + 1]) && lines[i + 1].includes('-')) {
      para = flushPara(para);
      const cells = (r) =>
        r.replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|')) rows.push(cells(lines[i++]));
      out.push(
        `<table class="md-table"><thead><tr>${head
          .map((c) => `<th>${inline(c)}</th>`)
          .join('')}</tr></thead><tbody>${rows
          .map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join('')}</tr>`)
          .join('')}</tbody></table>`
      );
      continue;
    }

    // lists (consecutive - * or 1.)
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      para = flushPara(para);
      const ordered = /^\s*\d+\.\s+/.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        items.push(inline(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, '')));
        i++;
      }
      const tag = ordered ? 'ol' : 'ul';
      out.push(`<${tag} class="md-list">${items.map((t) => `<li>${t}</li>`).join('')}</${tag}>`);
      continue;
    }

    // blank line -> paragraph break
    if (line.trim() === '') {
      para = flushPara(para);
      i++;
      continue;
    }

    para.push(line);
    i++;
  }
  flushPara(para);
  return out.join('\n');
}
