/*
 * City Pharma Agent embed widget — drop-in floating chat bubble.
 *
 * Usage (host page, e.g. Laravel layout.blade):
 *   <script src="https://YOUR_BACKEND/api/embed/widget.js"
 *     data-embed-id="..." data-public-key="..."
 *     data-user='{"id":"42","store_id":"20060-CCBHSC"}'   <-- store scoping
 *     data-user-sig="<hmac signed server-side>"
 *     data-title="CityCare Agent" data-greeting="Ask about stock..."
 *     data-accent="#2F3293" data-stream="true" async></script>
 *
 * The signed `store_id` makes the backend scope every answer to that store —
 * the user only sees their branch's data. No store_id = unscoped (all stores).
 */
(function () {
  var s = document.currentScript;
  var base = new URL(s.src).origin;
  var embedId = s.getAttribute('data-embed-id') || '';
  var publicKey = s.getAttribute('data-public-key') || '';
  var userRaw = s.getAttribute('data-user');
  var userSig = s.getAttribute('data-user-sig') || '';
  // `data-title` is a FROZEN attribute and always wins. It is read once here and
  // remembered as "the customer set it", so the brand default fetched below can
  // never overwrite a title that is sitting in a customer's live HTML.
  var titleAttr = s.getAttribute('data-title');
  var title = titleAttr || 'CityCare Agent';
  var greeting = s.getAttribute('data-greeting') || 'Hi! Ask about stock, prices, or substitutes.';
  // DEFAULT ONLY. A customer's explicit data-accent still wins, and live
  // embeds already carrying #006869 keep rendering teal until their snippet is
  // regenerated — that is intended, not drift to fix at runtime.
  var accent = s.getAttribute('data-accent') || '#2F3293';
  var stream = (s.getAttribute('data-stream') || 'true') !== 'false';
  var user = null;
  try { user = userRaw ? JSON.parse(userRaw) : null; } catch (e) { user = null; }

  var token = null;

  // --- palette helpers (widget lives on arbitrary customer sites, so it can
  //     rely on neither the admin's CSS vars nor Tailwind — everything scoped) ---
  function toRgba(hex, a) {
    if (typeof hex !== 'string') return null;
    var h = hex.trim().replace('#', '');
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    if (h.length !== 6 || /[^0-9a-fA-F]/.test(h)) return null;
    var r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
    return 'rgba(' + r + ',' + g + ',' + b + ',' + a + ')';
  }
  var accent35 = toRgba(accent, 0.35) || accent;
  var accent00 = toRgba(accent, 0) || 'transparent';
  var accent08 = toRgba(accent, 0.08) || '#eaebf7';
  // City Pharma neutrals (hex so they resolve even on older host browsers).
  var INK = '#23262b';      // oklch(19% .015 240)
  var INK2 = '#565b63';     // oklch(42% .014 240)
  var INK3 = '#7c828b';     // oklch(58% .012 240)
  var LINE = '#dde0e4';     // oklch(88% .008 240)
  var PAGEBG = '#fafbfc';   // oklch(98% .004 240)
  var OK = '#1a9d6a';       // step-complete check
  var DISPLAY = "'Nunito','IBM Plex Sans',-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif";
  // Body stack keeps 'Noto Sans Myanmar' so Burmese glyphs always render.
  var BODY = "'IBM Plex Sans','Noto Sans Myanmar',-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif";
  var MONO = "'SFMono-Regular',Menlo,Consolas,monospace";

  // Load the City Pharma type families (graceful fallback if a host CSP blocks it).
  var fl = document.createElement('link');
  fl.rel = 'stylesheet';
  fl.href = 'https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=Noto+Sans+Myanmar:wght@400;500&family=Nunito:wght@600;700&display=swap';
  document.head.appendChild(fl);

  var css = '\
.cca-btn{position:fixed;bottom:24px;right:24px;width:58px;height:58px;border-radius:50%;background:' + accent + ';color:#fff;border:2px solid #fff;box-sizing:border-box;cursor:pointer;box-shadow:0 10px 30px rgba(25,27,80,.35);display:flex;align-items:center;justify-content:center;z-index:2147483000;animation:cca-pulse 2.6s infinite}\
.cca-btn.cca-mark{background:#fff;border-color:' + accent + '}\
.cca-btn svg{width:26px;height:26px}\
.cca-btn img{width:38px;height:38px;object-fit:contain;border-radius:8px;background:transparent;padding:0;box-sizing:border-box}\
.cca-hd-ic img{display:block;width:100%;height:100%;object-fit:contain;border-radius:9px;background:#fff;padding:3px;box-sizing:border-box}\
.cca-panel{position:fixed;bottom:24px;right:24px;width:376px;max-width:calc(100vw - 32px);height:560px;max-height:calc(100vh - 48px);background:#fff;border-radius:18px;box-shadow:0 24px 70px rgba(10,20,25,.28);display:none;flex-direction:column;overflow:hidden;z-index:2147483000;font-family:' + BODY + ';color:' + INK + '}\
.cca-open{display:flex;animation:cca-fade .22s ease}\
.cca-hd{display:flex;align-items:center;gap:11px;padding:14px 16px;background:' + accent + ';color:#fff;flex-shrink:0}\
.cca-hd-ic{width:34px;height:34px;border-radius:9px;background:rgba(255,255,255,.18);display:flex;align-items:center;justify-content:center;flex-shrink:0}\
.cca-hd-ic svg{width:18px;height:18px}\
.cca-hd-tx{line-height:1.2;flex:1;min-width:0}\
.cca-hd-tx b{font-family:' + DISPLAY + ';font-weight:700;font-size:14.5px;color:#fff;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\
.cca-hd-sub{font-size:11.5px;opacity:.85;display:flex;align-items:center;gap:5px;margin-top:2px}\
.cca-dot{width:6px;height:6px;border-radius:50%;background:#8fe3b0;flex-shrink:0}\
.cca-x{width:30px;height:30px;border-radius:8px;border:0;background:rgba(255,255,255,.15);display:flex;align-items:center;justify-content:center;cursor:pointer;color:#fff;flex-shrink:0;padding:0}\
.cca-x svg{width:17px;height:17px}\
.cca-msgs{flex:1;overflow-y:auto;padding:16px;background:' + PAGEBG + ';display:flex;flex-direction:column;gap:8px}\
.cca-msgs::-webkit-scrollbar{width:8px}\
.cca-msgs::-webkit-scrollbar-thumb{background:' + LINE + ';border-radius:8px}\
.cca-b{max-width:84%;padding:10px 13px;font-size:13.5px;line-height:1.55;white-space:pre-wrap;word-wrap:break-word}\
.cca-u{align-self:flex-end;background:' + accent + ';color:#fff;border-radius:13px 13px 3px 13px}\
.cca-a{align-self:flex-start;background:#fff;border:1px solid ' + LINE + ';color:' + INK + ';border-radius:13px 13px 13px 3px}\
.cca-in{padding:11px 13px;border-top:1px solid ' + LINE + ';background:#fff;flex-shrink:0}\
.cca-inbox{display:flex;align-items:center;gap:8px;border:1px solid ' + LINE + ';border-radius:12px;padding:8px 11px;transition:border-color .15s}\
.cca-inbox:focus-within{border-color:' + accent + '}\
.cca-in input{flex:1;border:0;outline:0;background:transparent;font-size:13.5px;font-family:inherit;color:' + INK + '}\
.cca-in input::placeholder{color:' + INK3 + '}\
.cca-in button{width:30px;height:30px;border-radius:8px;border:0;background:' + accent + ';color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;padding:0}\
.cca-in button svg{width:16px;height:16px}\
.cca-ft{text-align:center;font-size:10.5px;color:' + INK3 + ';margin-top:8px}\
.cca-a.cca-wide{max-width:96%}\
.cca-steps{display:flex;flex-direction:column;gap:4px;white-space:normal}\
.cca-step{display:flex;align-items:center;gap:7px;font-size:12.5px;color:' + INK2 + ';line-height:1.4}\
.cca-step-t{flex:1;min-width:0}\
.cca-step-n{font-size:10.5px;color:' + INK3 + ';font-variant-numeric:tabular-nums;flex-shrink:0}\
.cca-ic{width:13px;height:13px;flex-shrink:0}\
.cca-spin{animation:cca-rot .9s linear infinite;color:' + accent + '}\
.cca-ok{color:' + OK + '}\
.cca-md{white-space:normal}\
.cca-md>*:first-child{margin-top:0}\
.cca-md>*:last-child{margin-bottom:0}\
.cca-md p{margin:0 0 7px}\
.cca-md h2,.cca-md h3,.cca-md h4{font-family:' + DISPLAY + ';font-weight:700;margin:11px 0 5px;line-height:1.3}\
.cca-md h2{font-size:15px}.cca-md h3{font-size:14px}.cca-md h4{font-size:13.5px}\
.cca-md ul,.cca-md ol{margin:0 0 7px;padding-left:19px}\
.cca-md li{margin:2px 0}\
.cca-md a{color:' + accent + ';text-decoration:underline}\
.cca-md code{font-family:' + MONO + ';font-size:12px;background:' + accent08 + ';border-radius:4px;padding:1px 4px}\
.cca-md pre{background:' + PAGEBG + ';border:1px solid ' + LINE + ';border-radius:8px;padding:8px 10px;overflow-x:auto;margin:0 0 7px}\
.cca-md pre code{background:none;padding:0;white-space:pre}\
.cca-tw{overflow-x:auto;margin:0 0 7px}\
.cca-md table{border-collapse:collapse;width:100%;font-size:12px}\
.cca-md th,.cca-md td{border:1px solid ' + LINE + ';padding:4px 7px;text-align:left;vertical-align:top}\
.cca-md th{background:' + PAGEBG + ';font-weight:600;font-size:10.5px;text-transform:uppercase;letter-spacing:.04em;color:' + INK3 + ';white-space:nowrap}\
.cca-data{margin-top:7px;white-space:normal}\
.cca-data-b{display:flex;align-items:center;gap:5px;border:0;background:none;padding:0;cursor:pointer;font-family:inherit;font-size:11.5px;color:' + INK3 + ';transition:color .15s}\
.cca-data-b:hover{color:' + accent + '}\
.cca-data-p{margin-top:7px;border-top:1px solid ' + LINE + ';padding-top:7px}\
.cca-data-h{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:' + INK3 + ';margin:7px 0 3px}\
.cca-data-h:first-child{margin-top:0}\
.cca-cite{display:flex;align-items:center;gap:5px;margin-top:7px;font-size:11px;color:' + INK3 + ';line-height:1.4}\
.cca-fu{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}\
.cca-fu-b{border:1px solid ' + LINE + ';background:' + PAGEBG + ';border-radius:999px;padding:5px 11px;cursor:pointer;font-family:inherit;font-size:11.5px;color:' + INK2 + ';transition:all .15s;line-height:1.3}\
.cca-fu-b:hover{border-color:' + accent + ';color:' + accent + '}\
.cca-unk{color:' + INK3 + ';font-style:italic}\
.cca-num{font-variant-numeric:tabular-nums}\
@keyframes cca-rot{to{transform:rotate(360deg)}}\
@keyframes cca-pulse{0%,100%{box-shadow:0 10px 30px rgba(25,27,80,.35),0 0 0 0 ' + accent35 + '}50%{box-shadow:0 10px 30px rgba(25,27,80,.35),0 0 0 8px ' + accent00 + '}}\
@keyframes cca-fade{from{opacity:0;transform:translateY(10px) scale(.98)}to{opacity:1;transform:translateY(0) scale(1)}}\
@media (prefers-reduced-motion:reduce){.cca-btn,.cca-open,.cca-spin{animation:none}}';
  var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  var CHAT_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>';
  var MED_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="5"/><path d="M12 8v8M8 12h8"/></svg>';
  var X_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  var SEND_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>';
  // trace icons: spinner on the running step, check on the finished ones
  var SPIN_SVG = '<svg class="cca-ic cca-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.2-8.6"/></svg>';
  var OK_SVG = '<svg class="cca-ic cca-ok" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
  var TBL_SVG = '<svg class="cca-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18"/></svg>';

  // ==========================================================================
  // Markdown  (the agent answers in Markdown: tables, bold, lists, headings)
  //
  // The widget is a dependency-free classic script dropped on third-party
  // pages, so this is a deliberate, minimal port of the admin's
  // `admin/src/lib/aurora/markdown.js` — same grammar, same escape-first rule.
  //
  // XSS: `esc()` runs over the WHOLE source before a single tag is emitted, so
  // every `<`, `>`, `&`, `"` and `'` in model output is already a character
  // entity by the time any pass below produces HTML. Nothing here ever puts an
  // unescaped substring of the source into the document.
  // ==========================================================================
  function esc(x) {
    return String(x == null ? '' : x)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // Inline spans. `t` is ALREADY escaped.
  function inline(t) {
    return t
      // Inline code. Two restrictions, both forced by real product names.
      //
      // (a) The content may contain no `*` and no whitespace: 53% of this
      // catalog's names use a backtick as an apostrophe (PARACAP PARACETAMOL
      // 10`S), and a permissive [^`]+ pairs that stray backtick with the one
      // opening a code span later on the line, eating the bold marker between
      // them. Excluding `*`/space makes that span unmatchable.
      //
      // (b) The opening backtick may not follow a letter or digit. (a) alone is
      // not enough — checked against all 2,790 backticked names in the live
      // catalog, exactly one still pairs its own two apostrophes with nothing
      // but printable characters between them:
      //     REAL SLIM SHAKE 20`S(VANILLA/S`BERRY/CHOCO)
      //   -> REAL SLIM SHAKE 20<code>S(VANILLA/S</code>BERRY/CHOCO)
      // An apostrophe backtick always follows the word it belongs to (20`S,
      // WOODS`), while a genuine span opens at a line start or after space,
      // `(` or `:`. Anchoring on that costs nothing: every code span the model
      // actually emits — an article code in parens, a tool name mid-sentence, a
      // table cell — still matches. Falling back to a literal backtick is the
      // safe failure; corrupting a product name is not.
      .replace(/(^|[^0-9A-Za-z])`([^`\s*]+)`/g, '$1<code>$2</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>')
      // Links. The URL group is anchored to a literal http(s):// scheme, so a
      // `javascript:` / `data:` payload simply does not match and is left as
      // inert escaped text. Quotes cannot close the attribute — esc() already
      // turned them into entities.
      .replace(
        /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
      );
  }

  function renderMarkdown(src) {
    if (!src) return '';
    var lines = esc(src).split('\n');
    var out = [];
    var para = [];
    var i = 0;

    function flushPara() {
      if (para.length) out.push('<p>' + inline(para.join(' ')) + '</p>');
      para = [];
    }
    function cells(r) {
      return r.replace(/^\||\|$/g, '').split('|').map(function (c) { return c.trim(); });
    }

    while (i < lines.length) {
      var line = lines[i];

      // fenced code
      if (/^```/.test(line.trim())) {
        flushPara();
        var body = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i].trim())) body.push(lines[i++]);
        i++;
        out.push('<pre><code>' + body.join('\n') + '</code></pre>');
        continue;
      }

      // heading (h1 -> h2, page keeps its own h1)
      var h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) {
        flushPara();
        var lvl = Math.min(h[1].length + 1, 4);
        out.push('<h' + lvl + '>' + inline(h[2]) + '</h' + lvl + '>');
        i++;
        continue;
      }

      // GFM pipe table: header row + |---|---| separator
      if (line.indexOf('|') >= 0 && i + 1 < lines.length &&
          /^[\s|:-]+$/.test(lines[i + 1]) && lines[i + 1].indexOf('-') >= 0) {
        flushPara();
        var head = cells(line);
        i += 2;
        var rows = [];
        while (i < lines.length && lines[i].indexOf('|') >= 0) rows.push(cells(lines[i++]));
        out.push(
          '<div class="cca-tw"><table><thead><tr>' +
          head.map(function (c) { return '<th>' + inline(c) + '</th>'; }).join('') +
          '</tr></thead><tbody>' +
          rows.map(function (r) {
            return '<tr>' + r.map(function (c) { return '<td>' + inline(c) + '</td>'; }).join('') + '</tr>';
          }).join('') +
          '</tbody></table></div>'
        );
        continue;
      }

      // lists (consecutive - * or 1.)
      if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
        flushPara();
        var tag = /^\s*\d+\.\s+/.test(line) ? 'ol' : 'ul';
        var items = [];
        while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
          items.push(inline(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, '')));
          i++;
        }
        out.push('<' + tag + '>' +
          items.map(function (t) { return '<li>' + t + '</li>'; }).join('') +
          '</' + tag + '>');
        continue;
      }

      if (line.trim() === '') { flushPara(); i++; continue; }
      para.push(line);
      i++;
    }
    flushPara();
    return out.join('\n');
  }

  // ==========================================================================
  // Tool trace — live status only (mirrors the admin chat): one line per step
  // saying WHAT the agent is doing, spinner on the current one, check on the
  // done ones. It is transient: the steps are REPLACED by the answer text, so
  // there is no post-answer dropdown to reopen.
  // ==========================================================================
  var TOOL_LABEL = {
    get_article_info: 'Looking up article info',
    summarize_article: 'Summarizing article',
    get_stock: 'Checking stock levels',
    top_by_stock: 'Ranking by stock',
    filter_by_price: 'Filtering by price',
    get_substitutes: 'Finding substitutes',
    search_by_name: 'Searching by name',
    search_by_meaning: 'Searching by meaning',
    related_drugs: 'Tracing the drug graph',
    drugs_for_same_condition: 'Finding drugs for the condition',
    find_at_other_stores: 'Checking other branches'
  };
  // The tool's argument folded into the phrase, so three lookups read as three
  // different lines instead of the same label three times.
  var STEP_TPL = {
    search_by_meaning: function (d) { return d ? 'Searching for “' + d + '”' : TOOL_LABEL.search_by_meaning; },
    search_by_name: function (d) { return d ? 'Searching for “' + d + '”' : TOOL_LABEL.search_by_name; },
    get_article_info: function (d) { return d ? 'Looking up ' + d : TOOL_LABEL.get_article_info; },
    summarize_article: function (d) { return d ? 'Summarizing ' + d : TOOL_LABEL.summarize_article; },
    get_stock: function (d) { return d ? 'Checking stock of ' + d : TOOL_LABEL.get_stock; },
    get_substitutes: function (d) { return d ? 'Finding substitutes for ' + d : TOOL_LABEL.get_substitutes; },
    drugs_for_same_condition: function (d) { return d ? 'Finding drugs for ' + d : TOOL_LABEL.drugs_for_same_condition; }
  };
  function toolLabel(raw) {
    if (!raw) return 'Searching the data';
    if (TOOL_LABEL[raw]) return TOOL_LABEL[raw];
    return raw.replace(/_/g, ' ').replace(/^\w/, function (c) { return c.toUpperCase(); });
  }
  /* Tools that return BRANCH rows (one row per shop) rather than product rows.
     Getting this backwards is what made the chip read "38 rows" for what was
     really "3 branches" — the count is only meaningful once you name the unit. */
  var SITE_TOOLS = { get_stock: 1, find_at_other_stores: 1, list_sites: 1 };

  /* What a citation should say. Naming the SOURCE ("product catalogue", "stock
     at 53 branches") is what a reader wants to know — a dump of 105 rows is a
     data export, not a citation, and "Show what I checked (105)" tells them
     nothing about whether to trust the answer. */
  var SOURCE_LABEL = {
    get_article_info: 'product catalogue',
    summarize_article: 'product catalogue',
    search_by_name: 'product catalogue',
    search_by_meaning: 'product catalogue',
    get_substitutes: 'similar products',
    related_drugs: 'related products',
    drugs_for_same_condition: 'related products',
    filter_by_price: 'price list',
    top_by_stock: 'stock levels',
    get_stock: 'stock levels',
    find_at_other_stores: 'branch stock',
    list_sites: 'branch list'
  };

  function citationText(results) {
    var names = [], seen = {}, sites = 0;
    for (var i = 0; i < results.length; i++) {
      var r = results[i];
      var lbl = SOURCE_LABEL[r.tool] || 'pharmacy data';
      if (!seen[lbl]) { seen[lbl] = 1; names.push(lbl); }
      if (SITE_TOOLS[r.tool]) sites += r.rows.length;
    }
    if (!names.length) return '';
    var src = names.length === 1 ? names[0]
      : names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
    return 'Checked against our ' + src + (sites ? ' across ' + sites + ' branch' + (sites === 1 ? '' : 'es') : '');
  }

  function stepText(s) {
    var t = STEP_TPL[s.label] ? STEP_TPL[s.label](s.detail) : toolLabel(s.label);
    return t + (s.count > 1 ? ' ×' + s.count : '');
  }

  var btn = document.createElement('button');
  btn.className = 'cca-btn'; btn.innerHTML = CHAT_SVG; btn.setAttribute('aria-label', 'Open chat');
  var panel = document.createElement('div'); panel.className = 'cca-panel';
  panel.innerHTML =
    '<div class="cca-hd">' +
      '<span class="cca-hd-ic">' + MED_SVG + '</span>' +
      '<div class="cca-hd-tx"><b>' + esc(title) + '</b>' +
        '<div class="cca-hd-sub"><span class="cca-dot"></span>Online · real stock &amp; price data</div></div>' +
      '<button class="cca-x" aria-label="Close">' + X_SVG + '</button>' +
    '</div>' +
    '<div class="cca-msgs"></div>' +
    '<div class="cca-in">' +
      '<div class="cca-inbox"><input placeholder="Ask in English or Burmese…">' +
      '<button aria-label="Send">' + SEND_SVG + '</button></div>' +
      '<div class="cca-ft">Powered by City Pharma · cites real inventory data</div>' +
    '</div>';
  document.body.appendChild(btn); document.body.appendChild(panel);

  var msgs = panel.querySelector('.cca-msgs');
  var input = panel.querySelector('.cca-in input');
  var sendBtn = panel.querySelector('.cca-in button');
  add('a', greeting);

  /* ---- brand defaults -----------------------------------------------------
   *
   * `/brand` is public and unauthenticated. It supplies the DEFAULT product
   * name and square icon, so a white-labelled deployment names itself without
   * every customer having to re-paste a data-title.
   *
   * Three rules this must not break:
   *
   *  1. `data-title` and `data-accent` are frozen attributes in live customer
   *     HTML. A supplied one WINS — the brand only fills in what was omitted.
   *     Accent is not touched here at all.
   *  2. The widget must work if /brand fails. Everything below is applied
   *     asynchronously ON TOP of a panel that has already rendered with the
   *     shipped defaults, so a 404, a CORS refusal or an offline backend simply
   *     leaves today's widget exactly as it is.
   *  3. Same origin only. The icon URL is joined to `base` (the origin this
   *     script was served from); an absolute URL pointing anywhere else is
   *     dropped rather than loaded onto a customer's page.
   */
  function brandAssetUrl(u) {
    if (typeof u !== 'string' || !u) return '';
    if (/^https?:\/\//i.test(u)) {
      try { return new URL(u).origin === base ? u : ''; } catch (e) { return ''; }
    }
    return base + (u.charAt(0) === '/' ? '' : '/') + u;
  }

  function applyBrand(b) {
    if (!b || typeof b !== 'object') return;
    var name = typeof b.product_name === 'string' ? b.product_name.trim() : '';
    if (name) {
      // The header title is the brand name ONLY when the customer omitted
      // data-title. The footer attribution is always the product name.
      if (!titleAttr) {
        var t = panel.querySelector('.cca-hd-tx b');
        if (t) t.textContent = name;
      }
      var ft = panel.querySelector('.cca-ft');
      if (ft) ft.textContent = 'Powered by ' + name + ' · cites real inventory data';
    }
    var icon = brandAssetUrl(b.assets && b.assets.icon);
    if (icon) {
      var alt = name || title;
      // The launcher carries the customer's mark instead of our glyph, so the
      // circle flips: white face, accent ring. Measured on the shipped asset —
      // a navy tile with white type — the mark was 1.15:1 against the accent
      // face it used to sit on, i.e. invisible, and it is invisible for ANY
      // customer who uploads a dark logo. On white it measures 12.13:1.
      //
      // The ring is not decoration. With an accent face the button read 1.85:1
      // against a dark customer page and failed WCAG 1.4.11 as a control. Face
      // and ring now cover opposite grounds: on a light page the accent ring is
      // the edge (10.51:1), on a dark page the white face is (19.40:1).
      btn.classList.add('cca-mark');
      btn.innerHTML = '';
      var bi = document.createElement('img');
      bi.src = icon; bi.alt = '';           // decorative: the button is labelled
      btn.appendChild(bi);
      var hi = panel.querySelector('.cca-hd-ic');
      if (hi) {
        hi.innerHTML = '';
        var pi = document.createElement('img');
        pi.src = icon; pi.alt = alt + ' logo';
        hi.appendChild(pi);
      }
    }
  }

  try {
    fetch(base + '/brand')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(applyBrand)
      .catch(function () { /* older backend / offline — shipped defaults stand */ });
  } catch (e) { /* no fetch on this host browser — shipped defaults stand */ }

  btn.onclick = function () { panel.classList.toggle('cca-open'); if (panel.classList.contains('cca-open')) input.focus(); };
  panel.querySelector('.cca-x').onclick = function () { panel.classList.remove('cca-open'); };

  function scroll() { msgs.scrollTop = msgs.scrollHeight; }

  function add(role, text) {
    var d = document.createElement('div'); d.className = 'cca-b ' + (role === 'u' ? 'cca-u' : 'cca-a');
    d.textContent = text; msgs.appendChild(d); scroll(); return d;
  }

  /* Conversation id for this widget instance.
   *
   * Without it every turn starts cold: the backend only engages multi-turn
   * memory for clients that send a real `session_id`, and the widget never
   * sent one. A pharmacist asking "Omez ရှိပါသလား" and then "ဈေးဘယ်လောက်လဲ"
   * got "which medicine?" back, because turn two had no idea turn one
   * happened (Feedback 9, the only High-priority ticket in the pack).
   *
   * Deliberately NOT the embed session token: that one is short-lived and gets
   * re-minted on a 401, which would silently reset the conversation mid-chat.
   * This id lives as long as the widget instance does.
   *
   * Not persisted to storage — a page reload starts a fresh conversation,
   * which is the right default for a shared shop-counter browser. */
  var convId = 'w-' + Date.now().toString(36) + '-' +
    Math.random().toString(36).slice(2, 10);

  function session() {
    if (token) return Promise.resolve(token);
    var body = { embed_id: embedId, public_key: publicKey };
    if (user) { body.user = user; body.signature = userSig; }
    return fetch(base + '/api/embed/session/create', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    }).then(function (r) { return r.json(); }).then(function (j) { token = j.session_token; return token; });
  }

  /* ---- one assistant turn: live steps -> answer -> "View data" ------------ */
  function turn() {
    var bub = document.createElement('div');
    bub.className = 'cca-b cca-a';
    var stepsEl = document.createElement('div'); stepsEl.className = 'cca-steps';
    var mdEl = document.createElement('div'); mdEl.className = 'cca-md';
    var dataEl = document.createElement('div'); dataEl.className = 'cca-data';
    bub.appendChild(stepsEl); bub.appendChild(mdEl); bub.appendChild(dataEl);
    msgs.appendChild(bub); scroll();

    var steps = [];    // [{label, detail, count, rows}]
    var results = [];  // [{tool, rows, subject}] — everything the tools returned

    function drawSteps() {
      stepsEl.innerHTML = steps.map(function (st, i) {
        var last = i === steps.length - 1;
        return '<div class="cca-step">' + (last ? SPIN_SVG : OK_SVG) +
          '<span class="cca-step-t">' + esc(stepText(st)) + (last ? '…' : '') + '</span>' +
          (st.rows == null ? '' :
            '<span class="cca-step-n">' + st.rows + ' row' + (st.rows === 1 ? '' : 's') + '</span>') +
          '</div>';
      }).join('');
      scroll();
    }

    return {
      /** the model is thinking but has told us nothing yet */
      pending: function () { stepsEl.innerHTML = '<div class="cca-step">' + SPIN_SVG + '<span class="cca-step-t">Thinking…</span></div>'; },
      /* Fold an identical consecutive step into a ×N count. The fast path names
         its arg `name`, the agent loop names it `detail` — read both. */
      step: function (j) {
        var detail = (j.args && (j.args.detail || j.args.name)) || '';
        var last = steps[steps.length - 1];
        if (last && last.label === j.label && last.detail === detail) last.count++;
        else steps.push({ label: j.label, detail: detail, count: 1, rows: null });
        drawSteps();
      },
      result: function (j) {
        results.push({ tool: j.tool, rows: j.rows || [], subject: j.subject || null });
        var last = steps[steps.length - 1];
        if (last) last.rows = (j.rows || []).length;
        drawSteps();
      },
      /** the answer arrived — the trace is transient, so it goes away now */
      text: function (t) {
        if (stepsEl.childNodes.length) stepsEl.innerHTML = '';
        mdEl.innerHTML = renderMarkdown(t);
        scroll();
      },
      plain: function (t) { stepsEl.innerHTML = ''; mdEl.textContent = t; scroll(); },
      /* A CITATION, not a data dump. This used to render every row the tools
         returned as an expandable table — 105 rows under "Show what I checked
         (105)". That is a data export: it answers "what is in your database",
         which nobody asked, instead of "where did this answer come from",
         which is the only thing a citation is for. Now it names the sources in
         one quiet line.

         Still built entirely from the `result` frames already received — it
         deliberately calls NO admin endpoint, because /admin/catalog/{code} is
         unscoped and would hand a store-scoped user every sibling branch's
         stock. */
      done: function () {
        var n = results.reduce(function (a, r) { return a + r.rows.length; }, 0);
        var cite = n ? citationText(results) : '';
        if (cite) {
          var c = document.createElement('div');
          c.className = 'cca-cite';
          c.innerHTML = TBL_SVG + '<span>' + esc(cite) + '</span>';
          dataEl.appendChild(c);
        }
        followUps(dataEl, results);
        scroll();
      }
    };
  }

  /* Follow-up chips. Built from the `subject` the SSE result frame already
     carries, so this costs NO extra model call and cannot invent a product:
     the subject is whatever the tools actually returned. A conversation that
     ends at one fact makes the reader retype the product name to ask the
     obvious next thing, which is the difference between a lookup box and an
     assistant. */
  function followUps(host, results) {
    var subject = null, tools = {};
    for (var i = 0; i < results.length; i++) {
      tools[results[i].tool] = 1;
      if (!subject && results[i].subject) subject = results[i].subject;
    }
    if (!subject || !subject.name) return;
    var name = subject.name;

    var qs = [];
    if (!tools.get_article_info && !tools.summarize_article)
      qs.push({ chip: 'What is it for?', ask: 'What is ' + name + ' used for?' });
    if (!tools.get_stock && !tools.find_at_other_stores)
      qs.push({ chip: 'Where can I get it?', ask: 'Which branches have ' + name + '?' });
    if (!tools.filter_by_price)
      qs.push({ chip: 'How much is it?', ask: 'How much is ' + name + '?' });
    if (!tools.get_substitutes)
      qs.push({ chip: 'Any alternatives?', ask: 'What are the alternatives to ' + name + '?' });
    /* There must ALWAYS be somewhere to go next. Once a turn has used most of
       the tools the list above empties, and the conversation dead-ends on the
       most detailed answer — exactly where a reader is most likely to have
       another question. These two work whatever was already asked. */
    if (!qs.length) {
      qs.push({ chip: 'How do I take it?', ask: 'How should I take ' + name + '?' });
      qs.push({ chip: 'Any side effects?', ask: 'What are the side effects of ' + name + '?' });
    }

    var wrap = document.createElement('div');
    wrap.className = 'cca-fu';
    qs.slice(0, 3).forEach(function (q) {
      var c = document.createElement('button');
      c.type = 'button';
      c.className = 'cca-fu-b';
      c.textContent = q.chip;
      c.onclick = function () {
        wrap.remove();          // a chip you already pressed is clutter
        input.value = q.ask;
        send();
      };
      wrap.appendChild(c);
    });
    host.appendChild(wrap);
  }

  function send() {
    var msg = input.value.trim(); if (!msg) return;
    input.value = ''; add('u', msg);
    var t = turn(); t.pending();
    var full = '';
    // `retried` guards against an infinite re-mint loop — we retry at most once.
    function attempt(retried) {
      full = '';
      return session().then(function (tok) {
        if (!stream) {
          return fetch(base + '/api/embed/chat', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_token: tok, message: msg, session_id: convId })
          }).then(function (r) {
            if (r.status === 401 && !retried) { token = null; return attempt(true); }
            return r.json().then(function (j) {
              if (j.content) t.text(j.content); else t.plain('(no response)');
            });
          });
        }
        return fetch(base + '/api/embed/chat/stream', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_token: tok, message: msg, session_id: convId })
        }).then(function (r) {
          // Expired/invalid session -> re-mint a fresh token and retry once.
          if (r.status === 401 && !retried) { token = null; return attempt(true); }
          var reader = r.body.getReader(), dec = new TextDecoder(), buf = '';
          function pump() {
            return reader.read().then(function (res) {
              if (res.done) return;
              buf += dec.decode(res.value, { stream: true });
              var i;
              while ((i = buf.indexOf('\n\n')) >= 0) {
                var frame = buf.slice(0, i); buf = buf.slice(i + 2);
                var data = '', evt = 'message';
                frame.split('\n').forEach(function (l) {
                  if (l.indexOf('event:') === 0) evt = l.slice(6).trim();
                  else if (l.indexOf('data:') === 0) data += l.slice(5).trim();
                });
                if (!data || data === '[DONE]') continue;
                // Unknown events are ignored — the SSE trace is additive.
                try {
                  var j = JSON.parse(data);
                  if (evt === 'step') t.step(j);
                  else if (evt === 'result') t.result(j);
                  else if (j.delta) { full += j.delta; t.text(full); }
                } catch (e) {}
              }
              return pump();
            });
          }
          return pump();
        });
      });
    }
    attempt(false).then(function () {
      if (stream && !full) t.plain('I looked up the data but didn’t produce a written answer — please try rephrasing.');
      t.done();
    }).catch(function (e) { t.plain('Error: ' + e.message); });
  }

  sendBtn.onclick = send;
  input.addEventListener('keydown', function (e) { if (e.key === 'Enter') send(); });
})();
