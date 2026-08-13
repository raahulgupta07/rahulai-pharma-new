/*
 * Runs the REAL app/static/widget.js under a minimal DOM stub.
 *
 * Why a stub and not jsdom: the widget must stay a dependency-free classic
 * script, and this repo has no JS test runner and no jsdom (checked
 * admin/node_modules). Asserting on hand-written strings about what the widget
 * "would" render is exactly the mistake this project keeps paying for, so the
 * shipped file is executed instead — markdown goes in through the SSE delta
 * frames the backend really sends, and the HTML comes back off the elements the
 * widget really wrote to.
 *
 * Protocol: JSON job on stdin, JSON result on stdout.
 *   {"turns": [ {"message": "…", "frames": ["event: step\ndata: {…}", …]}, … ]}
 * Each turn's frames are fed to a fake /api/embed/chat/stream response body and
 * split on the "\n\n" the frozen wire contract guarantees. Turns run against
 * ONE widget instance so a whole corpus costs one node start.
 *
 * Result: {"turns": [{"md", "steps", "data", "userBubbleText"}, …]} — the
 * innerHTML of the elements the widget wrote to, verbatim.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const WIDGET = path.join(__dirname, '..', '..', 'app', 'static', 'widget.js');

// --- DOM stub -------------------------------------------------------------
// Only what the widget actually touches. innerHTML is stored as a string
// rather than parsed; childNodes therefore reports "non-empty" off either
// appended children or a non-empty innerHTML, which is all the widget asks it.
class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attrs = {};
    this.style = {};
    this._html = '';
    this._history = [];
    this._text = '';
    this._q = {};
    const set = new Set();
    this.classList = {
      add: (c) => set.add(c),
      remove: (c) => set.delete(c),
      contains: (c) => set.has(c),
      toggle: (c, on) => {
        const want = on === undefined ? !set.has(c) : !!on;
        if (want) set.add(c); else set.delete(c);
        return want;
      },
    };
  }
  // Every assignment is kept. The tool trace is deliberately TRANSIENT — the
  // widget wipes it the moment the answer text arrives — so reading the final
  // innerHTML would say the steps never rendered. The history is what the
  // customer actually saw.
  set innerHTML(v) { this._html = String(v); this._history.push(this._html); this.children = []; }
  get innerHTML() { return this._html; }
  set textContent(v) { this._text = String(v); this._html = ''; }
  get textContent() { return this._text; }
  get childNodes() { return this.children.length ? this.children : (this._html ? [{}] : []); }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener() {}
  focus() {}
  // Selectors are never ambiguous in the widget (one .cca-msgs, one input, …),
  // so handing back a stable element per selector is faithful enough.
  querySelector(sel) { return this._q[sel] || (this._q[sel] = new El('div')); }
}

const doc = {
  head: new El('head'),
  body: new El('body'),
  createElement: (t) => new El(t),
};
doc.currentScript = new El('script');
doc.currentScript.src = 'https://backend.example/api/embed/widget.js';
doc.currentScript.attrs = {
  'data-embed-id': 'emb1',
  'data-public-key': 'pk1',
  'data-stream': 'true',
};
global.document = doc;

// --- job ------------------------------------------------------------------
const job = JSON.parse(fs.readFileSync(0, 'utf8'));

let pending = [];

function streamBody(frames) {
  // Frames are joined with the "\n\n" separator the contract freezes, and
  // handed over in ONE chunk; the widget's own buffer does the splitting.
  const bytes = new TextEncoder().encode(frames.join('\n\n') + '\n\n');
  let sent = false;
  return {
    getReader: () => ({
      read: () =>
        Promise.resolve(sent ? { done: true } : ((sent = true), { done: false, value: bytes })),
    }),
  };
}

global.fetch = function (url) {
  if (url.indexOf('/session/create') >= 0) {
    return Promise.resolve({ status: 200, json: () => Promise.resolve({ session_token: 'tok' }) });
  }
  if (url.indexOf('/chat/stream') >= 0) {
    return Promise.resolve({ status: 200, body: streamBody(pending) });
  }
  return Promise.reject(new Error('unexpected fetch: ' + url));
};

// Load the shipped widget. It is an IIFE, so `require` runs it against the stub.
require(WIDGET);

const panel = doc.body.children.find((e) => e.className === 'cca-panel');
const msgs = panel.querySelector('.cca-msgs');
const input = panel.querySelector('.cca-in input');
const sendBtn = panel.querySelector('.cca-in button');

// The widget's send() is fire-and-forget; the fake stream resolves entirely on
// the microtask queue, so drain it before reading the DOM.
function drain(n) {
  return n === 0 ? Promise.resolve() : Promise.resolve().then(() => drain(n - 1));
}

function runTurn(turn) {
  pending = turn.frames || [];
  input.value = turn.message || 'do we have paracetamol';
  sendBtn.onclick();
  return drain(300).then(() => {
    const bub = msgs.children[msgs.children.length - 1];
    const [stepsEl, mdEl, dataEl] = bub.children;
    return {
      md: mdEl.innerHTML,
      steps: stepsEl.innerHTML,
      stepsHistory: stepsEl._history,
      data: dataEl.children.map((c) => c.innerHTML).join('\n'),
      userBubbleText: msgs.children[msgs.children.length - 2].textContent,
    };
  });
}

(job.turns || []).reduce(
  (chain, turn) => chain.then((acc) => runTurn(turn).then((r) => (acc.push(r), acc))),
  Promise.resolve([])
)
  .then((turns) => process.stdout.write(JSON.stringify({ turns: turns })))
  .catch((e) => {
    process.stderr.write(String((e && e.stack) || e));
    process.exit(1);
  });
