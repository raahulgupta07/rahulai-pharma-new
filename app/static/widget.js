/*
 * City Pharma Agent embed widget — drop-in floating chat bubble.
 *
 * Usage (host page, e.g. Laravel layout.blade):
 *   <script src="https://YOUR_BACKEND/api/embed/widget.js"
 *     data-embed-id="..." data-public-key="..."
 *     data-user='{"id":"42","store_id":"20060-CCBHSC"}'   <-- store scoping
 *     data-user-sig="<hmac signed server-side>"
 *     data-title="CityCare Agent" data-greeting="Ask about stock..."
 *     data-accent="#006869" data-stream="true" async></script>
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
  var title = s.getAttribute('data-title') || 'CityCare Agent';
  var greeting = s.getAttribute('data-greeting') || 'Hi! Ask about stock, prices, or substitutes.';
  var accent = s.getAttribute('data-accent') || '#006869';
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
  // City Pharma neutrals (hex so they resolve even on older host browsers).
  var INK = '#23262b';      // oklch(19% .015 240)
  var INK3 = '#7c828b';     // oklch(58% .012 240)
  var LINE = '#dde0e4';     // oklch(88% .008 240)
  var PAGEBG = '#fafbfc';   // oklch(98% .004 240)
  var DISPLAY = "'Space Grotesk','IBM Plex Sans',-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif";
  // Body stack keeps 'Noto Sans Myanmar' so Burmese glyphs always render.
  var BODY = "'IBM Plex Sans','Noto Sans Myanmar',-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif";

  // Load the City Pharma type families (graceful fallback if a host CSP blocks it).
  var fl = document.createElement('link');
  fl.rel = 'stylesheet';
  fl.href = 'https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600&family=Noto+Sans+Myanmar:wght@400;500&display=swap';
  document.head.appendChild(fl);

  var css = '\
.cca-btn{position:fixed;bottom:24px;right:24px;width:58px;height:58px;border-radius:50%;background:' + accent + ';color:#fff;border:0;cursor:pointer;box-shadow:0 10px 30px rgba(15,60,58,.35);display:flex;align-items:center;justify-content:center;z-index:2147483000;animation:cca-pulse 2.6s infinite}\
.cca-btn svg{width:26px;height:26px}\
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
@keyframes cca-pulse{0%,100%{box-shadow:0 10px 30px rgba(15,60,58,.35),0 0 0 0 ' + accent35 + '}50%{box-shadow:0 10px 30px rgba(15,60,58,.35),0 0 0 8px ' + accent00 + '}}\
@keyframes cca-fade{from{opacity:0;transform:translateY(10px) scale(.98)}to{opacity:1;transform:translateY(0) scale(1)}}\
@media (prefers-reduced-motion:reduce){.cca-btn,.cca-open{animation:none}}';
  var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  var CHAT_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>';
  var MED_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="5"/><path d="M12 8v8M8 12h8"/></svg>';
  var X_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';
  var SEND_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>';

  var btn = document.createElement('button');
  btn.className = 'cca-btn'; btn.innerHTML = CHAT_SVG; btn.setAttribute('aria-label', 'Open chat');
  var panel = document.createElement('div'); panel.className = 'cca-panel';
  panel.innerHTML =
    '<div class="cca-hd">' +
      '<span class="cca-hd-ic">' + MED_SVG + '</span>' +
      '<div class="cca-hd-tx"><b>' + title + '</b>' +
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

  btn.onclick = function () { panel.classList.toggle('cca-open'); if (panel.classList.contains('cca-open')) input.focus(); };
  panel.querySelector('.cca-x').onclick = function () { panel.classList.remove('cca-open'); };

  function add(role, text) {
    var d = document.createElement('div'); d.className = 'cca-b ' + (role === 'u' ? 'cca-u' : 'cca-a');
    d.textContent = text; msgs.appendChild(d); msgs.scrollTop = msgs.scrollHeight; return d;
  }

  function session() {
    if (token) return Promise.resolve(token);
    var body = { embed_id: embedId, public_key: publicKey };
    if (user) { body.user = user; body.signature = userSig; }
    return fetch(base + '/api/embed/session/create', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    }).then(function (r) { return r.json(); }).then(function (j) { token = j.session_token; return token; });
  }

  function send() {
    var msg = input.value.trim(); if (!msg) return;
    input.value = ''; add('u', msg);
    var bubble = add('a', '...'); var full = '';
    // `retried` guards against an infinite re-mint loop — we retry at most once.
    function attempt(retried) {
      full = '';
      return session().then(function (t) {
        if (!stream) {
          return fetch(base + '/api/embed/chat', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_token: t, message: msg })
          }).then(function (r) {
            if (r.status === 401 && !retried) { token = null; return attempt(true); }
            return r.json().then(function (j) { bubble.textContent = j.content || '(no response)'; });
          });
        }
        return fetch(base + '/api/embed/chat/stream', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_token: t, message: msg })
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
                var data = '';
                frame.split('\n').forEach(function (l) { if (l.indexOf('data:') === 0) data += l.slice(5).trim(); });
                if (!data || data === '[DONE]') continue;
                try { var j = JSON.parse(data); if (j.delta) { full += j.delta; bubble.textContent = full; msgs.scrollTop = msgs.scrollHeight; } } catch (e) {}
              }
              return pump();
            });
          }
          return pump();
        });
      });
    }
    attempt(false).catch(function (e) { bubble.textContent = 'Error: ' + e.message; });
  }

  sendBtn.onclick = send;
  input.addEventListener('keydown', function (e) { if (e.key === 'Enter') send(); });
})();
