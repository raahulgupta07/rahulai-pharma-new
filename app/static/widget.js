/*
 * City Pharma Agent embed widget — drop-in floating chat bubble.
 *
 * Usage (host page, e.g. Laravel layout.blade):
 *   <script src="https://YOUR_BACKEND/api/embed/widget.js"
 *     data-embed-id="..." data-public-key="..."
 *     data-user='{"id":"42","store_id":"20060-CCBHSC"}'   <-- store scoping
 *     data-user-sig="<hmac signed server-side>"
 *     data-title="CityCare Agent" data-greeting="Ask about stock..."
 *     data-accent="#c96342" data-stream="true" async></script>
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
  var accent = s.getAttribute('data-accent') || '#c96342';
  var stream = (s.getAttribute('data-stream') || 'true') !== 'false';
  var user = null;
  try { user = userRaw ? JSON.parse(userRaw) : null; } catch (e) { user = null; }

  var token = null;

  var css = '\
.cca-btn{position:fixed;bottom:20px;right:20px;width:56px;height:56px;border-radius:50%;background:' + accent + ';color:#fff;border:0;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.18);font-size:24px;z-index:2147483000}\
.cca-panel{position:fixed;bottom:88px;right:20px;width:380px;max-width:calc(100vw - 32px);height:560px;max-height:calc(100vh - 120px);background:#fff;border:1px solid #e6e3dd;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.18);display:none;flex-direction:column;overflow:hidden;z-index:2147483000;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif}\
.cca-open{display:flex}\
.cca-hd{display:flex;align-items:center;gap:8px;padding:13px 16px;border-bottom:1px solid #e6e3dd}\
.cca-hd b{font-size:14px;color:#1a1a18;font-weight:600}\
.cca-dot{width:8px;height:8px;border-radius:50%;background:#3f8f5f}\
.cca-x{margin-left:auto;background:0;border:0;cursor:pointer;color:#9a9a93;font-size:18px}\
.cca-msgs{flex:1;overflow-y:auto;padding:16px;background:#faf9f7;display:flex;flex-direction:column;gap:10px}\
.cca-b{max-width:80%;padding:9px 13px;border-radius:14px;font-size:14px;line-height:1.5;white-space:pre-wrap;word-wrap:break-word}\
.cca-u{align-self:flex-end;background:' + accent + ';color:#fff;border-bottom-right-radius:4px}\
.cca-a{align-self:flex-start;background:#fff;border:1px solid #e6e3dd;color:#1a1a18;border-bottom-left-radius:4px}\
.cca-in{display:flex;gap:8px;padding:12px;border-top:1px solid #e6e3dd}\
.cca-in input{flex:1;border:1px solid #e6e3dd;border-radius:9px;padding:9px 12px;font-size:14px;outline:0}\
.cca-in button{background:' + accent + ';color:#fff;border:0;border-radius:9px;padding:0 16px;font-size:14px;cursor:pointer}';
  var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  var btn = document.createElement('button');
  btn.className = 'cca-btn'; btn.innerHTML = '&#128172;'; btn.setAttribute('aria-label', 'Open chat');
  var panel = document.createElement('div'); panel.className = 'cca-panel';
  panel.innerHTML =
    '<div class="cca-hd"><span class="cca-dot"></span><b>' + title + '</b><button class="cca-x" aria-label="Close">&times;</button></div>' +
    '<div class="cca-msgs"></div>' +
    '<div class="cca-in"><input placeholder="Message..."><button>Send</button></div>';
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
