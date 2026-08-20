<script>
  import { onMount } from 'svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import ErrorState from '$lib/ErrorState.svelte';
  import { UNKNOWN, int, ms, pct, share, usd } from '$lib/charts/format.js';

  /* Every colour on this page is READ from the running stylesheet and every
     ratio is COMPUTED in this browser. Nothing here is typed.

     That distinction is the whole point. `app.css` carries measured ratios in
     its comments, and a comment is a number somebody wrote down once. Two of
     this console's tokens have already shipped one hundredth of a ratio under
     AA — --c-ink-3, which held #6B7280 in both themes, and the rail's own
     #7D83AE — and in both cases the value looked right and the arithmetic was
     never re-run. This page re-runs it on every load, in whichever theme the
     reader is actually in. */

  /** The tokens, by what they are for. Roles are described; VALUES are read. */
  const GROUPS = [
    {
      title: 'Surfaces',
      why: 'Three depths, and no more. A fourth was invented locally on several pages before the scale existed — there is no --color-surface-3.',
      tokens: [
        ['--c-page', 'The page behind everything'],
        ['--c-surface', 'Cards, panels, table bodies'],
        ['--c-surface-2', 'Insets: table hover, chips, skeletons']
      ]
    },
    {
      title: 'Text',
      why: 'Three weights of attention. Every one of them is a light/dark PAIR — a single value asked to work on both white and near-black is what caused most of this console’s 820 measured contrast failures.',
      tokens: [
        ['--c-ink', 'Numbers and sentences the reader is here for'],
        ['--c-ink-2', 'Supporting sentences'],
        ['--c-ink-3', 'Labels, captions, table headers — the smallest text there is']
      ]
    },
    {
      title: 'Action',
      why: 'The CityCare logo indigo. The console had drifted to a generic blue; the brand mark measures better on white than the blue did.',
      tokens: [
        ['--c-accent', 'Links, primary buttons, the first chart series'],
        ['--c-accent-hover', 'The same, pressed'],
        ['--c-accent-soft', 'The tint behind accent-coloured chips'],
        ['--c-on-accent', 'What sits ON a filled accent surface — it flips in dark mode'],
        ['--c-accent-2', 'A comparison series: present, and not competing'],
        ['--c-accent-2-soft', 'Its tint']
      ]
    },
    {
      title: 'Tone',
      why: 'Good news is quiet. --c-success is the accent family on purpose — a healthy service gets no green, and only a problem gets a warm colour.',
      tokens: [
        ['--c-success', 'Working, in place, recorded'],
        ['--c-success-soft', ''],
        ['--c-warning', 'Worth a look'],
        ['--c-warning-soft', ''],
        ['--c-danger', 'Not answering, refused, wrong'],
        ['--c-danger-soft', ''],
        ['--c-info', 'Stated, not judged'],
        ['--c-info-soft', '']
      ]
    },
    {
      title: 'Lines',
      why: 'Two hairlines: one that separates panels, one that separates rows inside them.',
      tokens: [
        ['--c-line', 'Panel and card edges'],
        ['--c-line-2', 'Row separators']
      ]
    },
    {
      title: 'Charts',
      why: 'Six categorical fills plus a fold band. They only have to be told apart — but not at 3:1, which no palette can do: WCAG contrast is a luminance ratio, so mutual 3:1 runs off the top of the gamut at the fourth colour and at the third once each one must also clear 3:1 against the page. These are separated perceptually instead (CIEDE2000), and two fills that touch in a stacked bar are separated by a surface-coloured stroke. Series 1 IS the accent by construction, so a chart’s first line and the console’s action colour cannot drift. A chart with more categories than colours groups the tail into “Other (n)” — it never reuses a fill.',
      tokens: [
        ['--c-series-1', 'First series'],
        ['--c-series-2', 'Second series'],
        ['--c-series-3', 'Third series'],
        ['--c-series-4', 'Fourth series'],
        ['--c-series-5', 'Fifth series'],
        ['--c-series-6', 'Sixth series'],
        ['--c-series-other', 'Grouped tail — “Other (n)”']
      ]
    },
    {
      title: 'The rail',
      why: 'Dark in BOTH themes. It is a fixed piece of the product, not a surface that follows the reader’s preference — the light/dark switch moves the page beside it, never the rail.',
      tokens: [
        ['--c-rail-bg', 'The rail itself'],
        ['--c-rail-ink', 'The active page'],
        ['--c-rail-ink-2', 'Every other page'],
        ['--c-rail-ink-3', 'Group labels and the version row — the smallest type in the console'],
        ['--c-rail-link', 'The one link out']
      ]
    },
    {
      title: 'The sign-in panel',
      why: 'The other surface that is dark in both themes: it is a product demo, not a page. It has its own accent because the page accent measured below AA on it, and half the console’s users were reading the demo’s own words under the line.',
      tokens: [
        ['--c-show-bg', 'The darkest stop of its gradient'],
        ['--c-show-bg-2', 'The middle stop'],
        ['--c-show-bg-3', 'The lightest stop — the worst case for light text, so it is what the pairs below are measured against'],
        ['--c-show-ink', 'Its headline'],
        ['--c-show-ink-2', 'Its body'],
        ['--c-show-ink-3', 'Its captions'],
        ['--c-show-accent', 'Its accent — deliberately NOT --c-accent'],
        ['--c-show-success', 'Its “live” dot. The same colour as its accent, because good news is quiet there too']
      ]
    }
  ];

  /* The text/background pairs this console actually puts on screen. A ratio for
     a pair nobody renders proves nothing; a pair that renders and is not on
     this list is unmeasured. */
  const PAIRS = [
    { fg: '--c-ink', bg: '--c-page', use: 'A sentence on the page' },
    { fg: '--c-ink', bg: '--c-surface', use: 'A number in a card' },
    { fg: '--c-ink', bg: '--c-surface-2', use: 'A row under the cursor' },
    { fg: '--c-ink-2', bg: '--c-page', use: 'Supporting text on the page' },
    { fg: '--c-ink-2', bg: '--c-surface', use: 'Supporting text in a card' },
    { fg: '--c-ink-2', bg: '--c-surface-2', use: 'Supporting text in an inset' },
    { fg: '--c-ink-3', bg: '--c-page', use: 'A caption on the page' },
    { fg: '--c-ink-3', bg: '--c-surface', use: 'A table header' },
    { fg: '--c-ink-3', bg: '--c-surface-2', use: 'A label inside a chip' },
    { fg: '--c-accent', bg: '--c-surface', use: 'A link' },
    { fg: '--c-accent', bg: '--c-page', use: 'A link on the page' },
    { fg: '--c-accent', bg: '--c-accent-soft', use: 'An accent chip' },
    { fg: '--c-on-accent', bg: '--c-accent', use: 'A primary button' },
    { fg: '--c-success', bg: '--c-success-soft', use: 'A “working” chip' },
    { fg: '--c-warning', bg: '--c-warning-soft', use: 'A “worth a look” chip' },
    { fg: '--c-danger', bg: '--c-danger-soft', use: 'A “not answering” chip' },
    { fg: '--c-info', bg: '--c-info-soft', use: 'A stated-fact chip' },
    { fg: '--c-rail-ink', bg: '--c-rail-bg', use: 'The page you are on' },
    { fg: '--c-rail-ink-2', bg: '--c-rail-bg', use: 'Every other page' },
    { fg: '--c-rail-ink-3', bg: '--c-rail-bg', use: 'A rail group label, at the smallest step in the scale' },
    { fg: '--c-rail-link', bg: '--c-rail-bg', use: 'The link out of the rail' },
    { fg: '--c-show-ink', bg: '--c-show-bg-3', use: 'The sign-in headline' },
    { fg: '--c-show-ink-2', bg: '--c-show-bg-3', use: 'Its body text' },
    { fg: '--c-show-ink-3', bg: '--c-show-bg-3', use: 'Its captions' },
    { fg: '--c-show-accent', bg: '--c-show-bg-3', use: 'Its accent words' }
  ];

  /** AA for normal text. Nothing here is judged by the 3:1 large-text
      allowance: the console's largest step is drawn in --c-ink, which
      clears 4.5 anyway, so nothing would be gained by lowering the bar for
      it and something would be lost. */
  const AA = 4.5;

  /** The type scale, in the order it grows. Roles are described; SIZES are read.

      The scale replaced 28 distinct sizes across 1,092 declarations, and the
      reason it is dense at the bottom and sparse at the top is a measurement:
      92.5% of every use in this console sits between 11px and 14px. A step
      added here without re-running that measurement is somebody making a local
      decision that looks right on one screen, which is what the 28 were. */
  const STEPS = [
    ['--text-micro', 'Uppercase eyebrows, and the rail\u2019s group labels \u2014 the smallest type in the product'],
    ['--text-label', 'Table headers and chip text'],
    ['--text-meta', 'Dense table cells and captions'],
    ['--text-body-sm', 'Secondary body, and most explanatory paragraphs on this console'],
    ['--text-body', 'Body'],
    ['--text-title', 'Card and section titles'],
    ['--text-heading', 'Page headings'],
    ['--text-display', 'KPI numbers'],
    ['--text-display-lg', 'A single headline number'],
    ['--text-hero', 'The largest type there is']
  ];

  /** The three faces, and a string in each that is really rendered in it.
   *
   *  Declaring a face is not the same as having one. A missing family falls
   *  back silently, the text stays readable, and the only symptom is that
   *  everything is a little wider or a little shorter than it was designed to
   *  be — which is exactly how a deck of slides in this workspace shipped
   *  with collided titles. The Burmese face matters most: this console is
   *  bilingual, and Myanmar script in a substitute face changes line height,
   *  not just width. */
  const FACES = [
    {
      family: 'IBM Plex Sans',
      via: '--font-sans',
      sample: 'Paracetamol 500mg',
      role: 'Every sentence, label and number on this console'
    },
    {
      family: 'Noto Sans Myanmar',
      via: '--font-sans',
      sample: '\u1006\u1031\u1038\u1006\u102d\u102f\u1004\u103a \u1005\u102c\u101b\u1004\u103a\u1038',
      role: 'Burmese \u2014 half of what this product answers in'
    },
    {
      family: 'JetBrains Mono',
      via: '--font-mono',
      sample: '1,284 \u00b7 MMK 45,000',
      role: 'Token names, ids, and every column of figures'
    }
  ];

  /* THE FIVE STATES a panel on this console can be in.

     Every sample below is the REAL component or the REAL helper, not a picture
     of one. A reference page that redraws what it documents drifts from the
     product the first time the product changes, and then it is worse than
     having no reference page: it is a confident description of something that
     is no longer true.

     Four of the five have one owner. `Nothing to show` has none — every page
     writes its own sentence, and that is stated here rather than papered over
     with a mock-up of a component that does not exist. */

  /** The panels the console shows when a request does not return data.
   *
   *  These are `ErrorState` itself, given the status codes it branches on. The
   *  wrapper is inert: they are the live component, so their buttons and links
   *  work, and a reference page must not navigate somebody who clicked a
   *  sample. */
  const FAILURES = [
    { status: 401, kind: 'Refused', what: 'the stores page',
      why: 'A 12-hour token expired. Nothing broke, and nothing was lost.' },
    { status: 403, kind: 'Refused', what: 'branch stock',
      why: 'A store-scoped account reaching past its scope. This is the scoping WORKING.' },
    { status: 404, kind: 'Refused', what: 'the observability board',
      why: 'The backend predates this page. A fact about the build, not a fault.' },
    { status: 500, kind: 'Failed', what: 'the turn log',
      why: 'The backend answered, and what it answered with was a traceback.' },
    { status: 0, kind: 'Failed', what: 'anything at all',
      why: 'Nothing answered. This is the only state that means "restart something".' }
  ];

  /* THE RULES. The redesign's own Foundations artboard heads this block
     "Four rules" and then lists five. Rather than pick one to drop, all five
     are here — and each is CHECKED against the running console rather than
     restated. A rule nobody checks is a rule the next screen breaks. */

  /** The logo cyan. Not a token any more: the v2 palette dropped it, and this
      is the mark's own value, kept here only so the rule can be tested against
      it. It is an input to a measurement, not a measurement. */
  const LOGO_CYAN = '#00ADEF';

  /** Surfaces that can hold Myanmar script, with the classes they really wear.
      Rendered below at those classes and measured, because the rule is about
      leading and leading is only visible as a ratio to the size beside it. */
  const BURMESE = [
    { id: 'md', label: 'A chat answer', where: 'Chat · the assistant’s reply', klass: 'md' },
    {
      id: 'turn',
      label: 'A recorded answer',
      where: 'Conversations · the turn drawer',
      klass: 'bilingual text-body-sm whitespace-pre-wrap text-ink-2'
    },
    {
      id: 'question',
      label: 'A question heading',
      where: 'Conversations · the question it answered',
      klass: 'bilingual page-title text-body font-extrabold text-ink'
    },
        // Rendered as a real cell inside a real `.tbl`, because a table cell's
    // leading comes from the table's own rule and a div would not inherit it.
    { id: 'cell', label: 'A table cell', where: 'Any table of questions', klass: 'tbl-cell' },
    {
      id: 'chip',
      label: 'A suggestion chip',
      where: 'Chat · the prompts it offers, and its follow-ups',
      klass: 'bilingual rounded-full border border-line bg-surface-2 px-3 py-1.5 text-meta text-ink-2'
    }
  ];

  /** The design's number for Burmese leading, READ from the stylesheet.
   *
   *  It used to be typed here, which made this page the second place the rule
   *  lived — and the second place is always the one that goes stale. It is a
   *  token now, so the surfaces below and the bar they are held to cannot
   *  drift apart. */
  let minLeading = $state(null);

  /** Measured against each other: Myanmar with stacked marks, Latin with a
      capital and a descender, so neither side is a flattering sample. */
  const MY_SAMPLE = 'ဆေးဆိုင်ငြိမ့်';
  const EN_SAMPLE = 'Paracetamol jgpqy';

  let theme = $state('light');
  let values = $state({});
  let measured = $state([]);
  let steps = $state([]);
  let painted = $state([]);
  let leading = $state([]);
  let cyan = $state(null);
  let faces = $state([]);
  let ready = $state(false);

  /** Resolve a token to what the browser will actually paint.
   *
   *  Read through a probe element rather than off `document.documentElement`:
   *  a custom property whose value is another `var()` — `--c-series-1` is
   *  `var(--c-accent)` — can come back unresolved, and `#2F3293` and
   *  `rgb(47, 50, 147)` are the same colour written two ways. Painting it and
   *  reading the computed `color` gives one normalised answer for both. */
  function resolveAll(names) {
    const el = document.createElement('span');
    el.style.position = 'absolute';
    el.style.visibility = 'hidden';
    document.body.appendChild(el);
    const out = {};
    for (const n of names) {
      el.style.color = '';
      el.style.color = `var(${n})`;
      out[n] = getComputedStyle(el).color || null;
    }
    el.remove();
    return out;
  }

  function parse(css) {
    const m = /rgba?\(([^)]+)\)/.exec(css || '');
    if (!m) return null;
    const p = m[1].split(/[\s,/]+/).filter(Boolean).map(Number);
    if (p.length < 3 || p.slice(0, 3).some((n) => !Number.isFinite(n))) return null;
    const a = p.length > 3 && Number.isFinite(p[3]) ? p[3] : 1;
    return { r: p[0], g: p[1], b: p[2], a };
  }

  const hex = (c) =>
    c && c.a === 1
      ? '#' + [c.r, c.g, c.b].map((n) => Math.round(n).toString(16).padStart(2, '0')).join('').toUpperCase()
      : null;

  /** WCAG 2.1 relative luminance. */
  function lum({ r, g, b }) {
    const f = (v) => {
      const s = v / 255;
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  }

  function ratio(fg, bg) {
    if (!fg || !bg) return null;
    // A translucent colour has no ratio of its own — it takes one from
    // whatever is behind it. Returning a number here would be inventing the
    // backdrop, so this returns nothing and the row says why.
    if (fg.a !== 1 || bg.a !== 1) return null;
    const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    return (a + 0.05) / (b + 0.05);
  }

  /** Leading, as a ratio to the size beside it, read off the rendered samples.
   *
   *  A line-height in pixels says nothing on its own — the same value is
   *  generous under a caption and tight under a heading, which is why this is
   *  a ratio. Myanmar script stacks marks above and below the Latin
   *  band, so a line that fits English can clip Burmese at the same leading —
   *  and half of what this product answers is Burmese. */
  async function measureLeading() {
    // The ratio is the design's own unit, and on its own it understates this.
    // What a reader actually sees is the GAP: how much of the line box is left
    // over once the glyphs are drawn. Myanmar stacks marks above and below the
    // Latin band, so the same line height that leaves a Latin line breathing
    // room leaves a Burmese one almost none — and the two are measured here
    // with the real faces, through canvas text metrics, because a DOM rect
    // reports the line box and would report both scripts identically.
    await Promise.all([
      document.fonts.load('1em "Noto Sans Myanmar"', MY_SAMPLE).catch(() => null),
      document.fonts.load('1em "IBM Plex Sans"', EN_SAMPLE).catch(() => null)
    ]);
    const ctx = document.createElement('canvas').getContext('2d');
    const ink = (text, size, family) => {
      if (!ctx || !Number.isFinite(size)) return null;
      ctx.font = `${size}px ${family}`;
      const m = ctx.measureText(text);
      const h = m.actualBoundingBoxAscent + m.actualBoundingBoxDescent;
      return Number.isFinite(h) ? h : null;
    };

    leading = BURMESE.map((b) => {
      const el = document.querySelector(`[data-burmese="${b.id}"]`);
      if (!el) return { ...b, size: null, lead: null, ratio: null, myGap: null, enGap: null };
      const cs = getComputedStyle(el);
      const size = parseFloat(cs.fontSize);
      const lead = parseFloat(cs.lineHeight);
      const r = Number.isFinite(size) && Number.isFinite(lead) && size > 0 ? lead / size : null;
      const family = cs.fontFamily;
      const my = ink(MY_SAMPLE, size, family);
      const en = ink(EN_SAMPLE, size, family);
      return {
        ...b,
        size,
        lead,
        ratio: r,
        myGap: my == null || !Number.isFinite(lead) ? null : lead - my,
        enGap: en == null || !Number.isFinite(lead) ? null : lead - en
      };
    });
  }

  /** The bar itself, read from the token that sets it. */
  function measureMinLeading() {
    const v = getComputedStyle(document.documentElement).getPropertyValue('--leading-bilingual').trim();
    const n = parseFloat(v);
    minLeading = Number.isFinite(n) ? n : null;
  }

  /** The logo cyan against the paper it is never allowed to sit on. */
  function measureCyan() {
    const c = parse(LOGO_CYAN.length ? paintOnce(LOGO_CYAN) : null);
    const white = parse(paintOnce('#FFFFFF'));
    cyan = { rgb: c, onWhite: ratio(c, white) };
  }

  /** Normalise any colour the browser understands to `rgb(...)`. */
  function paintOnce(value) {
    const el = document.createElement('span');
    el.style.position = 'absolute';
    el.style.visibility = 'hidden';
    el.style.color = value;
    document.body.appendChild(el);
    const out = getComputedStyle(el).color;
    el.remove();
    return out;
  }

  /** What each failure panel is actually painted, read off the rendered panel.
   *
   *  `ErrorState` splits its own palette on one rule: a refusal is not a
   *  failure. 401, 403 and 404 are facts about who you are or what this build
   *  has, and painting them red says the server broke — a lie the reader then
   *  has to un-learn. Only 5xx and a dead connection are red.
   *
   *  That rule lives in one `$derived` in one component and would survive any
   *  amount of prose here saying it was true. So it is not asserted: the two
   *  groups are read back off the pixels, and if a refusal is ever painted in
   *  the failure colour this page says so. */
  function measurePanels() {
    const out = [];
    for (const f of FAILURES) {
      const el = document.querySelector(`[data-state-sample="${f.status}"] [role="alert"]`);
      if (!el) {
        out.push({ ...f, bg: null, border: null });
        continue;
      }
      const cs = getComputedStyle(el);
      out.push({ ...f, bg: cs.backgroundColor, border: cs.borderTopColor });
    }
    painted = out;
  }

  /** Read each step as the browser computes it, not as the file declares it.
   *
   *  A token that does not exist makes `font-size: var(--x)` invalid at
   *  computed-value time, and the element then INHERITS a size — a plausible
   *  number for a step that is not there. So existence is checked separately,
   *  against the custom property itself, and a missing step is reported as
   *  missing rather than as whatever it inherited. */
  function measureType() {
    const el = document.createElement('span');
    el.style.position = 'absolute';
    el.style.visibility = 'hidden';
    el.style.whiteSpace = 'pre';
    el.textContent = 'Hg';
    document.body.appendChild(el);
    const root = getComputedStyle(document.documentElement);
    const out = [];
    for (const [name, role] of STEPS) {
      const declared = root.getPropertyValue(name).trim();
      el.style.fontSize = '';
      el.style.fontSize = `var(${name})`;
      const px = declared ? parseFloat(getComputedStyle(el).fontSize) : null;
      const prev = out.length ? out[out.length - 1].px : null;
      out.push({
        name,
        role,
        declared: declared || null,
        px: Number.isFinite(px) ? px : null,
        // The step up from the one below it. The scale's whole claim is that
        // it is dense where the console lives and sparse above it, and that
        // claim is only checkable as ratios.
        step: prev && px ? px / prev : null
      });
    }
    el.remove();
    steps = out;
  }

  /** Whether a face is really being used, measured rather than asked.
   *
   *  `document.fonts.check()` answers about the font list; the width of a
   *  string answers about the pixels. Both are reported, and when they
   *  disagree the row says so instead of picking the friendlier one. */
  async function measureFaces() {
    // Ask for each face BEFORE measuring it. A webfont nothing on the page has
    // used yet is not loaded yet, and a width taken at that moment is the
    // fallback's width — which reads as "this face is missing". Noto Sans
    // Myanmar is exactly that case: no Burmese renders on this page until the
    // sample below does, so measuring first accused a face that was on its way.
    await Promise.all(
      FACES.map((f) => document.fonts.load(`48px "${f.family}"`, f.sample).catch(() => null))
    );
    const el = document.createElement('span');
    el.style.position = 'absolute';
    el.style.visibility = 'hidden';
    el.style.whiteSpace = 'pre';
    el.style.fontSize = '48px';
    document.body.appendChild(el);
    const out = FACES.map((f) => {
      el.textContent = f.sample;
      el.style.fontFamily = `"${f.family}", serif`;
      const withFace = el.getBoundingClientRect().width;
      // A family name nothing can match, so this is the fallback on its own.
      el.style.fontFamily = '"__no_such_face__", serif';
      const withoutFace = el.getBoundingClientRect().width;
      const painted = Math.abs(withFace - withoutFace) > 0.5;
      let listed = null;
      try {
        listed = document.fonts.check(`16px "${f.family}"`);
      } catch {
        listed = null;
      }
      return { ...f, withFace, withoutFace, painted, listed };
    });
    el.remove();
    faces = out;
  }

  function measure() {
    theme = document.documentElement.classList.contains('dark') ? 'dark' : 'light';
    const names = [
      ...new Set([...GROUPS.flatMap((g) => g.tokens.map((t) => t[0])), ...PAIRS.flatMap((p) => [p.fg, p.bg])])
    ];
    const css = resolveAll(names);
    const parsed = {};
    for (const n of names) parsed[n] = parse(css[n]);
    values = Object.fromEntries(names.map((n) => [n, { css: css[n], rgb: parsed[n], hex: hex(parsed[n]) }]));
    measured = PAIRS.map((p) => {
      const r = ratio(parsed[p.fg], parsed[p.bg]);
      return { ...p, ratio: r, pass: r === null ? null : r >= AA };
    });
    measureType();
    measureFaces();
    measurePanels();
    measureMinLeading();
    measureLeading();
    measureCyan();
    ready = true;
  }

  onMount(() => {
    measure();
    // The reader can switch the theme while this page is open, and a ratio
    // printed for the other theme is worse than no ratio at all.
    const obs = new MutationObserver(measure);
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => obs.disconnect();
  });

  // The same dash the rest of the console uses for a value nobody measured,
  // taken from the module that owns it rather than typed again here.
  const fmt = (r) => (typeof r === 'number' ? `${r.toFixed(2)}:1` : UNKNOWN);

  let failing = $derived(measured.filter((m) => m.pass === false));
  let unmeasurable = $derived(measured.filter((m) => m.pass === null));
  /** The smallest margin in the system. Two tokens have shipped one hundredth
      under AA; naming the tightest pair is how the next one gets caught. */
  let tightest = $derived(
    measured
      .filter((m) => typeof m.ratio === 'number')
      .sort((a, b) => a.ratio - b.ratio)[0] ?? null
  );
  /** A refusal wearing the failure colour. The whole rule, checked. */
  let refusalColours = $derived(
    new Set(painted.filter((p) => p.kind === 'Refused' && p.bg).map((p) => p.bg))
  );
  let failureColours = $derived(
    new Set(painted.filter((p) => p.kind === 'Failed' && p.bg).map((p) => p.bg))
  );
  let confused = $derived(painted.filter((p) => p.kind === 'Refused' && failureColours.has(p.bg)));
  let unpainted = $derived(painted.filter((p) => !p.bg));

  /** Rule 1, checked rather than stated: the two must not print the same. */
  let dashHolds = $derived(int(null) === UNKNOWN && int(0) !== UNKNOWN && pct(0) !== UNKNOWN);
  /** Rule 4: no token in the palette may resolve to the mark's cyan. */
  let cyanTokens = $derived(
    cyan?.rgb
      ? Object.entries(values)
          .filter(([, v]) => v?.rgb && v.rgb.r === cyan.rgb.r && v.rgb.g === cyan.rgb.g && v.rgb.b === cyan.rgb.b)
          .map(([k]) => k)
      : []
  );
  /** Rule 5: the surfaces that would clip Myanmar marks. */
  /** Below the bar. Both sides must be known: `minLeading < null` is false, so
      an unread token would quietly report every surface as passing. */
  let tightLeading = $derived(
    minLeading === null ? [] : leading.filter((l) => l.ratio !== null && l.ratio < minLeading)
  );
  /** The bar itself could not be read. Say so rather than showing a verdict. */
  let barUnknown = $derived(ready && minLeading === null);
  let unmeasuredLeading = $derived(leading.filter((l) => l.ratio === null));

  let missingSteps = $derived(steps.filter((s) => !s.declared).map((s) => s.name));
  /** The band the console actually lives in: 92.5% of its type sits between
      11px and 14px, and four of the ten steps are in there on purpose. */
  let dense = $derived(steps.filter((s) => s.px !== null && s.px >= 11 && s.px <= 14));
  let substituted = $derived(faces.filter((f) => f.painted === false));
  let disputed = $derived(faces.filter((f) => f.listed !== null && f.listed !== f.painted));

  let missing = $derived(
    Object.entries(values)
      .filter(([, v]) => !v?.rgb)
      .map(([k]) => k)
  );
</script>

<PageHeader
  title={'Foundations'}
  subtitle={'The colour, type and wording rules this console is built from. Every value below is read from the running stylesheet and every ratio is worked out in this browser — nothing on this page is typed in.'}
/>

<section class="mb-6 rounded-panel border border-line bg-surface p-5">
  <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
    <h2 class="text-title font-semibold text-ink">Contrast, measured here</h2>
    <span class="text-meta text-ink-3">
      {theme === 'dark' ? 'Dark mode' : 'Light mode'} · {PAIRS.length} pairs · AA for normal text is {AA}:1
    </span>
  </div>

  {#if ready}
    <p class="mt-2 max-w-3xl text-body-sm leading-relaxed text-ink-2">
      {#if failing.length}
        <strong class="text-danger">{failing.length} of {measured.length} pairs are below {AA}:1</strong> in this
        theme.
      {:else}
        All {measured.length} pairs clear {AA}:1 in this theme.
      {/if}
      {#if tightest}
        The tightest is <code class="font-mono text-meta">{tightest.fg}</code> on
        <code class="font-mono text-meta">{tightest.bg}</code> at {fmt(tightest.ratio)}. A colour that misses by a
        hundredth is a colour that misses — that has shipped here twice — so the margin is worth watching, not
        just the pass.
      {/if}
      {#if unmeasurable.length}
        {unmeasurable.length} pair{unmeasurable.length === 1 ? '' : 's'} could not be measured, and say so below
        rather than being counted as passing.
      {/if}
    </p>

    <div class="mt-4 overflow-x-auto">
      <table class="tbl">
        <thead>
          <tr>
            <th>Where it is seen</th>
            <th>Text</th>
            <th>On</th>
            <th class="num">Measured</th>
            <th>AA</th>
          </tr>
        </thead>
        <tbody>
          {#each measured as m (m.fg + m.bg)}
            <tr>
              <td class="text-ink">{m.use}</td>
              <td>
                <span class="inline-flex items-center gap-2">
                  <span
                    class="inline-block h-3.5 w-3.5 shrink-0 rounded-xs border border-line"
                    style={`background:var(${m.fg})`}
                  ></span>
                  <code class="font-mono text-label text-ink-3">{m.fg}</code>
                </span>
              </td>
              <td>
                <span class="inline-flex items-center gap-2">
                  <span
                    class="inline-block h-3.5 w-3.5 shrink-0 rounded-xs border border-line"
                    style={`background:var(${m.bg})`}
                  ></span>
                  <code class="font-mono text-label text-ink-3">{m.bg}</code>
                </span>
              </td>
              <td class="num tnum {m.pass === false ? 'text-danger' : 'text-ink'}">{fmt(m.ratio)}</td>
              <td>
                {#if m.pass === true}
                  <span class="rounded-xs bg-success-soft px-1.5 py-0.5 text-label font-medium text-success"
                    >Passes</span
                  >
                {:else if m.pass === false}
                  <span class="rounded-xs bg-danger-soft px-1.5 py-0.5 text-label font-medium text-danger"
                    >Below {AA}:1</span
                  >
                {:else}
                  <span class="rounded-xs bg-surface-2 px-1.5 py-0.5 text-label text-ink-3"
                    >Not measurable — one of these is translucent, so it has no ratio of its own</span
                  >
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else}
    <div class="mt-4 space-y-2">
      {#each Array(6) as _, i (i)}<div class="skel"></div>{/each}
    </div>
  {/if}
</section>

<section class="rounded-panel border border-line bg-surface p-5">
  <h2 class="text-title font-semibold text-ink">The colours, and what each is for</h2>
  <p class="mt-2 max-w-3xl text-body-sm leading-relaxed text-ink-2">
    Every value is read from the running stylesheet, so this page cannot describe a palette the console is not
    using. Each token is a light/dark pair by construction: one value asked to work on both white and near-black
    is what caused most of the 820 contrast failures this console was measured with.
  </p>

  {#if missing.length}
    <p class="mt-3 rounded-card bg-danger-soft px-3 py-2 text-body-sm text-danger">
      {missing.length} token{missing.length === 1 ? ' is' : 's are'} named here and not defined in the stylesheet:
      <code class="font-mono text-meta">{missing.join(', ')}</code>. Anything drawn with
      {missing.length === 1 ? 'it' : 'them'} is falling back to whatever it inherits.
    </p>
  {/if}

  <div class="mt-4 space-y-6">
    {#each GROUPS as g (g.title)}
      <div>
        <h3 class="text-body font-semibold text-ink">{g.title}</h3>
        <p class="mt-1 max-w-3xl text-body-sm leading-relaxed text-ink-3">{g.why}</p>
        <div class="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {#each g.tokens as [name, role] (name)}
            {@const v = values[name]}
            <div class="flex items-start gap-3 rounded-card border border-line bg-page p-3">
              <span
                class="mt-0.5 h-9 w-9 shrink-0 rounded-control border border-line"
                style={`background:var(${name})`}
              ></span>
              <div class="min-w-0">
                <div class="font-mono text-meta text-ink">{name}</div>
                <div class="font-mono text-label text-ink-3">{v?.hex ?? v?.css ?? UNKNOWN}</div>
                {#if role}<div class="mt-1 text-body-sm leading-snug text-ink-2">{role}</div>{/if}
              </div>
            </div>
          {/each}
        </div>
      </div>
    {/each}
  </div>
</section>

<section class="mt-6 rounded-panel border border-line bg-surface p-5">
  <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
    <h2 class="text-title font-semibold text-ink">Type, measured here</h2>
    <span class="text-meta text-ink-3">{STEPS.length} steps · every size read from the running stylesheet</span>
  </div>

  <p class="mt-2 max-w-3xl text-body-sm leading-relaxed text-ink-2">
    Ten steps, named by what they are for rather than by how big they are: <code class="font-mono text-meta"
      >--text-meta</code
    > says a value is a caption, <code class="font-mono text-meta">text-xs</code> only says it is small. The scale
    replaced 28 sizes across 1,092 declarations, and it is dense at the bottom because that is where this console
    lives — adding a step without re-running that measurement is how the 28 happened.
  </p>

  {#if missingSteps.length}
    <p class="mt-3 rounded-card bg-danger-soft px-3 py-2 text-body-sm text-danger">
      {missingSteps.length} step{missingSteps.length === 1 ? ' is' : 's are'} named here and not defined:
      <code class="font-mono text-meta">{missingSteps.join(', ')}</code>. Anything set in
      {missingSteps.length === 1 ? 'it' : 'them'} inherits its size instead, which looks like a working step.
    </p>
  {/if}

  {#if ready}
    <div class="mt-4 overflow-x-auto">
      <table class="tbl">
        <thead>
          <tr>
            <th>Step</th>
            <th class="num">Measured</th>
            <th class="num">Up from the step below</th>
            <th>What it is for</th>
            <th class="whitespace-nowrap">At that size</th>
          </tr>
        </thead>
        <tbody>
          {#each steps as st (st.name)}
            <tr>
              <td class="whitespace-nowrap"><code class="font-mono text-label text-ink-3">{st.name}</code></td>
              <td class="num tnum {st.px === null ? 'text-danger' : 'text-ink'}"
                >{st.px === null ? 'not defined' : `${st.px}px`}</td
              >
              <td class="num tnum text-ink-3">{st.step ? `${Math.round((st.step - 1) * 100)}%` : UNKNOWN}</td>
              <td class="text-ink-2">{st.role}</td>
              <td>
                {#if st.px !== null}
                  <span class="whitespace-nowrap text-ink" style={`font-size:var(${st.name})`}
                    >Paracetamol · 1,284</span
                  >
                {:else}
                  <span class="text-ink-3">{UNKNOWN}</span>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <p class="mt-3 max-w-3xl text-body-sm leading-relaxed text-ink-3">
      {dense.length} of the {steps.length} steps sit between 11px and 14px, which is where 92.5% of this console's
      type was measured to be. The last sample in each row is drawn at its own step, so a size that reads wrong here
      is wrong everywhere it is used.
    </p>

    <h3 class="mt-6 text-body font-semibold text-ink">The faces, and whether they are really being drawn</h3>
    <p class="mt-1 max-w-3xl text-body-sm leading-relaxed text-ink-2">
      A declared face is not a loaded one. When a family is missing the text stays readable and everything is
      quietly a little wider or shorter than it was drawn to be. Each row below renders its sample twice — once in
      the face, once in the fallback alone — and reports whether the pixels differ.
    </p>

    {#if substituted.length}
      <p class="mt-3 rounded-card bg-danger-soft px-3 py-2 text-body-sm text-danger">
        {substituted.length} of the {faces.length} faces {substituted.length === 1 ? 'is' : 'are'} not being drawn:
        <code class="font-mono text-meta">{substituted.map((f) => f.family).join(', ')}</code>. Everything set in
        {substituted.length === 1 ? 'it' : 'them'} is rendering in a substitute.
      </p>
    {/if}
    {#if disputed.length}
      <p class="mt-3 rounded-card bg-warning-soft px-3 py-2 text-body-sm text-warning">
        For {disputed.map((f) => f.family).join(', ')} the font list and the painted pixels disagree. The pixels are
        what the reader gets, and the row below shows both rather than choosing.
      </p>
    {/if}

    <div class="mt-3 grid gap-3 lg:grid-cols-3">
      {#each faces as f (f.family)}
        <div class="rounded-card border border-line bg-page p-3">
          <div class="flex items-baseline justify-between gap-2">
            <span class="font-mono text-meta text-ink">{f.family}</span>
            {#if f.painted}
              <span class="rounded-xs bg-success-soft px-1.5 py-0.5 text-label font-medium text-success">Drawn</span>
            {:else}
              <span class="rounded-xs bg-danger-soft px-1.5 py-0.5 text-label font-medium text-danger"
                >Substituted</span
              >
            {/if}
          </div>
          <div class="mt-2 text-title text-ink" style={`font-family:var(${f.via})`}>{f.sample}</div>
          <div class="mt-2 text-body-sm leading-snug text-ink-2">{f.role}</div>
          <div class="mt-1 text-label text-ink-3">
            Via <code class="font-mono">{f.via}</code> · {f.withFace.toFixed(1)}px wide against
            {f.withoutFace.toFixed(1)}px in the fallback alone · the font list says
            {f.listed === null ? 'nothing' : f.listed ? 'present' : 'absent'} (which some browsers answer
            yes to for a family they have never seen — the widths are the evidence)
          </div>
        </div>
      {/each}
    </div>
  {:else}
    <div class="mt-4 space-y-2">
      {#each Array(5) as _, i (i)}<div class="skel"></div>{/each}
    </div>
  {/if}
</section>

<section class="mt-6 rounded-panel border border-line bg-surface p-5">
  <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
    <h2 class="text-title font-semibold text-ink">The five states, drawn by the real components</h2>
    <span class="text-meta text-ink-3">nothing below is a mock-up</span>
  </div>

  <p class="mt-2 max-w-3xl text-body-sm leading-relaxed text-ink-2">
    A panel on this console is in one of five states, and four of them are not “data”. Every sample below is the
    component or the helper the product itself uses, given the input that produces that state — a reference page
    that redraws what it documents is a confident description of something that stopped being true.
  </p>

  <!-- 1 -->
  <div class="mt-5">
    <h3 class="text-body font-semibold text-ink">1 · Loading</h3>
    <p class="mt-1 max-w-3xl text-body-sm leading-relaxed text-ink-3">
      Bars in the shape of the content, not a spinner: a spinner says “something is happening”, a skeleton says
      what is about to be there. This is the <code class="font-mono text-meta">.skel</code> rule from
      <code class="font-mono text-meta">app.css</code>, the same one every page uses.
    </p>
    <div class="mt-3 max-w-md space-y-2 rounded-card border border-line bg-page p-3">
      <div class="skel" style="width:60%"></div>
      <div class="skel"></div>
      <div class="skel" style="width:80%"></div>
    </div>
  </div>

  <!-- 2 -->
  <div class="mt-6">
    <h3 class="text-body font-semibold text-ink">2 · Not recorded</h3>
    <p class="mt-1 max-w-3xl text-body-sm leading-relaxed text-ink-3">
      A value nobody measured. Every formatter in
      <code class="font-mono text-meta">charts/format.js</code> returns
      <code class="font-mono text-meta">{UNKNOWN}</code> for an input that is not a finite number, and
      <code class="font-mono text-meta">Number(null)</code> and <code class="font-mono text-meta">x || 0</code> are
      banned in that file and its callers — both turn “we never measured this” into “we measured nothing
      happening”. The cells below are the real helpers, called with nothing.
    </p>
    <div class="mt-3 grid max-w-3xl gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {#each [['int(null)', int(null)], ['ms(null)', ms(null)], ['pct(null)', pct(null)], ['share(3, null)', share(3, null)], ['int(0)', int(0)], ['pct(0)', pct(0)]] as [call, out] (call)}
        <div class="rounded-card border border-line bg-page p-3">
          <div class="font-mono text-label text-ink-3">{call}</div>
          <div class="mt-1 font-mono text-title tnum text-ink">{out}</div>
        </div>
      {/each}
    </div>
    <p class="mt-2 max-w-3xl text-body-sm leading-relaxed text-ink-3">
      The bottom row is the point: a measured zero prints as a zero. If both printed the same thing, a cost with no
      configured price would read as free, and nobody would notice for months.
    </p>
  </div>

  <!-- 3 -->
  <div class="mt-6">
    <h3 class="text-body font-semibold text-ink">3 · Nothing to show</h3>
    <p class="mt-1 max-w-3xl text-body-sm leading-relaxed text-ink-3">
      We looked, the query worked, and there were no rows. Different from the state above it: that one is missing
      knowledge, this one is knowledge. It is also the one state on this console with
      <strong class="text-ink-2">no shared component</strong> — every page writes its own sentence, so “No files
      yet”, “No conversations yet.” and “No graph data is available yet.” are all the same state in three voices.
      That gap is written here rather than hidden behind a mock-up of a component that does not exist.
    </p>
    <div class="mt-3 max-w-md rounded-card border border-line bg-page px-6 py-8 text-center">
      <p class="text-body-sm font-semibold text-ink">No files yet</p>
      <p class="mx-auto mt-1.5 max-w-sm text-meta text-ink-3">
        Nothing has been sent. This is a working pipeline with an empty inbox, not a fault.
      </p>
    </div>
  </div>

  <!-- 4 and 5 -->
  <div class="mt-6">
    <h3 class="text-body font-semibold text-ink">4 and 5 · Refused, and failed</h3>
    <p class="mt-1 max-w-3xl text-body-sm leading-relaxed text-ink-3">
      Both come back as a non-200 and they are not the same thing. A refusal is a fact about who you are or what
      this build has; a failure is something breaking. Every panel below is
      <code class="font-mono text-meta">ErrorState</code> itself, given the status it branches on. They are live
      components, so the group is inert — a reference page must not sign anybody out because they clicked a sample.
    </p>

    {#if ready}
      {#if confused.length}
        <p class="mt-3 rounded-card bg-danger-soft px-3 py-2 text-body-sm text-danger">
          {confused.length} refusal{confused.length === 1 ? ' is' : 's are'} painted in the failure colour:
          {confused.map((c) => c.status).join(', ')}. A refusal drawn red tells the reader the server broke.
        </p>
      {:else if refusalColours.size && failureColours.size}
        <p class="mt-3 text-body-sm leading-relaxed text-ink-2">
          Read back off the rendered panels: the {painted.filter((p) => p.kind === 'Refused' && p.bg).length} refusals
          that rendered are painted
          <code class="font-mono text-meta">{[...refusalColours].join(', ')}</code> and the
          {painted.filter((p) => p.kind === 'Failed' && p.bg).length} failures
          <code class="font-mono text-meta">{[...failureColours].join(', ')}</code>. Different, measured here, not
          asserted — the rule lives in one line of one component and prose would outlive it.
        </p>
      {/if}
      {#if unpainted.length}
        <p class="mt-3 rounded-card bg-warning-soft px-3 py-2 text-body-sm text-warning">
          {unpainted.length} sample panel{unpainted.length === 1 ? ' did' : 's did'} not render, so
          {unpainted.length === 1 ? 'its colour is' : 'their colours are'} unknown rather than passing.
        </p>
      {/if}
    {/if}

    <div class="mt-3 grid gap-4 lg:grid-cols-2">
      {#each FAILURES as f (f.status)}
        <div>
          <div class="mb-1.5 flex flex-wrap items-baseline gap-2">
            <code class="font-mono text-label text-ink-3">{f.status === 0 ? 'no response' : f.status}</code>
            <span
              class="rounded-xs px-1.5 py-0.5 text-label font-medium {f.kind === 'Refused'
                ? 'bg-warning-soft text-warning'
                : 'bg-danger-soft text-danger'}">{f.kind}</span
            >
            <span class="text-meta text-ink-3">{f.why}</span>
          </div>
          <div data-state-sample={f.status} inert>
            <ErrorState error={{ status: f.status, message: '' }} what={f.what} retry={() => {}} />
          </div>
        </div>
      {/each}
    </div>
  </div>
</section>

<section class="mt-6 rounded-panel border border-line bg-surface p-5">
  <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
    <h2 class="text-title font-semibold text-ink">The four rules, of which there are five</h2>
    <span class="text-meta text-ink-3">each one checked, not restated</span>
  </div>

  <p class="mt-2 max-w-3xl text-body-sm leading-relaxed text-ink-2">
    The redesign's own Foundations artboard heads this block “Four rules” and then lists five. None of the five is
    a duplicate and none is obviously the spare, so rather than pick one to drop, all five are here — with the
    miscount left visible, because a silent correction is how a reader stops trusting the rest of the page. Each
    rule below carries a verdict measured in this browser, or an honest note that it is checked somewhere else. A
    rule nobody checks is a rule the next screen breaks.
  </p>

  <div class="mt-4 space-y-4">
      <!-- 1 -->
      <div class="rounded-card border border-line bg-page p-4">
        <div class="flex flex-wrap items-baseline gap-x-3">
          <h3 class="text-body font-semibold text-ink">1 · A number nobody recorded is an em-dash</h3>
          {#if dashHolds}
            <span class="rounded-xs bg-success-soft px-1.5 py-0.5 text-label font-medium text-success">Holds</span>
          {:else}
            <span class="rounded-xs bg-danger-soft px-1.5 py-0.5 text-label font-medium text-danger">Broken</span>
          {/if}
        </div>
        <p class="mt-1 max-w-3xl text-body-sm leading-relaxed text-ink-2">
          Zero is a count. Blank is not. Anywhere the two could be confused, the screen says which one it is.
        </p>
        <p class="mt-1.5 max-w-3xl text-body-sm leading-relaxed text-ink-3">
          {#if dashHolds}
            Checked by calling the console's own formatters: an unmeasured value comes back
            <code class="font-mono text-meta">{int(null)}</code> and a measured zero comes back
            <code class="font-mono text-meta">{int(0)}</code>. Section three above shows the pair.
          {:else}
            The formatters no longer separate the two. A cost with no configured price now reads as free.
          {/if}
        </p>
      </div>

      <!-- 2 -->
      <div class="rounded-card border border-line bg-page p-4">
        <div class="flex flex-wrap items-baseline gap-x-3">
          <h3 class="text-body font-semibold text-ink">2 · A refusal is not a failure</h3>
          {#if confused.length}
            <span class="rounded-xs bg-danger-soft px-1.5 py-0.5 text-label font-medium text-danger">Broken</span>
          {:else if refusalColours.size && failureColours.size}
            <span class="rounded-xs bg-success-soft px-1.5 py-0.5 text-label font-medium text-success">Holds</span>
          {:else}
            <span class="rounded-xs bg-surface-2 px-1.5 py-0.5 text-label text-ink-3">Not measured</span>
          {/if}
        </div>
        <p class="mt-1 max-w-3xl text-body-sm leading-relaxed text-ink-2">
          Expired sessions, blocked branches and rejected files get the warm colour. Red means something broke.
        </p>
        <p class="mt-1.5 max-w-3xl text-body-sm leading-relaxed text-ink-3">
          Read off the rendered panels in section four, not asserted here. The rule survives one edit to one
          component, and prose about it would outlive it.
        </p>
      </div>

      <!-- 3 -->
      <div class="rounded-card border border-line bg-page p-4">
        <div class="flex flex-wrap items-baseline gap-x-3">
          <h3 class="text-body font-semibold text-ink">3 · Every number carries its reading</h3>
          <span class="rounded-xs bg-info-soft px-1.5 py-0.5 text-label font-medium text-info"
            >Checked in the tests, not here</span
          >
        </div>
        <p class="mt-1 max-w-3xl text-body-sm leading-relaxed text-ink-2">
          One line under each figure says what good looks like. A number without an interpretation is a number
          nobody acts on.
        </p>
        <p class="mt-1.5 max-w-3xl text-body-sm leading-relaxed text-ink-3">
          This one is about every KPI card on every other page, so this browser cannot see it. The
          <code class="font-mono text-meta">Kpi</code> component makes the footnote a part of the card rather than
          a tooltip, and the test suite fails if any card is rendered without one. Printing a made-up verdict here
          would be worse than admitting where the check lives.
        </p>
      </div>

      <!-- 4 -->
      <div class="rounded-card border border-line bg-page p-4">
        <div class="flex flex-wrap items-baseline gap-x-3">
          <h3 class="text-body font-semibold text-ink">4 · The logo cyan never carries text</h3>
          {#if cyanTokens.length}
            <span class="rounded-xs bg-danger-soft px-1.5 py-0.5 text-label font-medium text-danger">Broken</span>
          {:else}
            <span class="rounded-xs bg-success-soft px-1.5 py-0.5 text-label font-medium text-success">Holds</span>
          {/if}
        </div>
        <p class="mt-1 max-w-3xl text-body-sm leading-relaxed text-ink-2">
          It is the mark, an underline and a highlight on a dark surface — never a word anybody has to read on
          paper.
        </p>
        <p class="mt-1.5 max-w-3xl text-body-sm leading-relaxed text-ink-3">
          <span class="inline-block h-3 w-3 translate-y-0.5 rounded-xs border border-line" style={`background:${LOGO_CYAN}`}
          ></span>
          <code class="font-mono text-meta">{LOGO_CYAN}</code> measures
          <strong class="text-ink-2">{fmt(cyan?.onWhite)}</strong> on white, worked out here — under half of what
          AA asks for.
          {#if cyanTokens.length}
            It is held by {cyanTokens.join(', ')}, which the console draws text in.
          {:else}
            No token in this palette resolves to it. The one cyan that does carry text is the rail's, lifted for
            the navy behind it, and its ratio is in the first table.
          {/if}
        </p>
      </div>

      <!-- 5 -->
      <div class="rounded-card border border-line bg-page p-4">
        <div class="flex flex-wrap items-baseline gap-x-3">
          <h3 class="text-body font-semibold text-ink">5 · Burmese gets its own leading</h3>
          {#if barUnknown}
            <span class="rounded-xs bg-surface-2 px-1.5 py-0.5 text-label text-ink-3"
              >--leading-bilingual is not defined, so there is nothing to check against</span
            >
          {:else if tightLeading.length}
            <span class="rounded-xs bg-danger-soft px-1.5 py-0.5 text-label font-medium text-danger"
              >{tightLeading.length} of {leading.length} surfaces below {minLeading}</span
            >
          {:else if leading.length}
            <span class="rounded-xs bg-success-soft px-1.5 py-0.5 text-label font-medium text-success">Holds</span>
          {/if}
        </div>
        <p class="mt-1 max-w-3xl text-body-sm leading-relaxed text-ink-2">
          Myanmar script stacks marks above and below the Latin band. Any line that can hold it runs at
          {minLeading} or more.
        </p>
        <p class="mt-1.5 max-w-3xl text-body-sm leading-relaxed text-ink-3">
          Each row below is a real surface, wearing the classes it wears in the product, holding real Burmese. The
          leading is read as a ratio to the size beside it — a line-height in pixels means nothing on its own. The
          last two columns are what a reader sees: how much of the line box is left once the glyphs are drawn, for
          Myanmar and for Latin, measured with the real faces at that exact size.
          {#if unmeasuredLeading.length}
            {unmeasuredLeading.length} surface{unmeasuredLeading.length === 1 ? '' : 's'} did not render and
            {unmeasuredLeading.length === 1 ? 'is' : 'are'} unknown rather than passing.
          {/if}
        </p>

        <div class="mt-3 overflow-x-auto">
          <table class="tbl">
            <thead>
              <tr>
                <th>Surface</th>
                <th>Where</th>
                <th class="num">Size</th>
                <th class="num">Leading</th>
                <th class="num">Ratio</th>
                <th class="num">Room left, Burmese</th>
                <th class="num">Room left, Latin</th>
                <th>At that leading</th>
              </tr>
            </thead>
            <tbody>
              <!-- The rows come from BURMESE, not from the measurements: the
                   samples have to EXIST before anything can be read off them,
                   and a table driven by its own results starts empty and stays
                   empty. -->
              {#each BURMESE as b (b.id)}
                {@const l = leading.find((x) => x.id === b.id) ?? {}}
                <tr>
                  <td class="text-ink">{b.label}</td>
                  <td class="text-ink-3">{b.where}</td>
                  <td class="num tnum text-ink-3">{l.size == null ? UNKNOWN : `${l.size}px`}</td>
                  <td class="num tnum text-ink-3">{l.lead == null ? UNKNOWN : `${l.lead}px`}</td>
                  <td class="num tnum {l.ratio != null && l.ratio < minLeading ? 'text-danger' : 'text-ink'}"
                    >{l.ratio == null ? UNKNOWN : l.ratio.toFixed(2)}</td
                  >
                  <td class="num tnum {l.myGap != null && l.myGap < 2 ? 'text-danger' : 'text-ink'}"
                    >{l.myGap == null ? UNKNOWN : `${l.myGap.toFixed(1)}px`}</td
                  >
                  <td class="num tnum text-ink-3">{l.enGap == null ? UNKNOWN : `${l.enGap.toFixed(1)}px`}</td>
                  <td>
                    {#if b.klass === 'tbl-cell'}
                      <table class="tbl"
                        ><tbody
                          ><tr
                            ><td class="bilingual" data-burmese={b.id}
                              >ဆေးဆိုင် စာရင်း၊ ဆေးလက်ကျန်</td
                            ></tr
                          ></tbody
                        ></table
                      >
                    {:else}
                      <div class={b.klass} data-burmese={b.id}>ဆေးဆိုင် စာရင်း၊ ဆေးလက်ကျန်</div>
                    {/if}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>

        {#if tightLeading.length}
          <p class="mt-3 rounded-card bg-danger-soft px-3 py-2 text-body-sm leading-relaxed text-danger">
            {tightLeading.map((t) => t.label.toLowerCase()).join(', ')} run tighter than {minLeading}. Half of what
            this product answers is Burmese, and on those surfaces its marks have less room than the design asks
            for. This is a real gap, written down rather than rounded up to a pass.
          </p>
        {/if}
      </div>
  </div>
</section>
