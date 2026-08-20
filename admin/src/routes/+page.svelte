<script>
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { getJSON } from '$lib/api.js';
  import { Smartphone, ShieldCheck, ArrowRight } from '@lucide/svelte';
  import { browserTz, deltaOf, openSection } from '$lib/activity/shared.js';
  import Kpi from '$lib/charts/Kpi.svelte';
  import { ms as fmtMs, pct as fmtPct, int as fmtInt, UNKNOWN } from '$lib/charts/format.js';

  let loading = $state(true);
  let range = $state('7d');
  let summary = $state(null);
  let summaryError = $state(null);
  let fresh = $state({});
  let health = $state(null);
  let healthError = $state(null);
  let embeds = $state(null);
  let embedsError = $state(null);
  let feed = $state(null);
  let feedError = $state(null);
  let branches = $state(null);
  let branchesError = $state(null);

  // The three pills used to write `range` and nothing read it: the highlight
  // moved and every number on the page stayed still. They now name a real
  // window, which `/admin/analytics/summary` measures over.
  //
  // NOT `/metrics/history` — that is twelve minutes of in-memory buckets that
  // reset with the process, so it can answer "7 days" only by pretending.
  const RANGES = [
    { id: '7d', days: 7, label: '7 days' },
    { id: '30d', days: 30, label: '30 days' },
    { id: '90d', days: 90, label: '90 days' }
  ];
  const TZ = browserTz();

  /** `YYYY-MM-DD`, `back` days ago, in the reader's own zone.
   *
   *  Date-only bounds on purpose: `/admin/analytics/*` reads a bare date in the
   *  `tz` it is handed and treats the `end` day as inclusive, so "7 days" is
   *  today plus the six before it — not seven boundaries cut at UTC midnight,
   *  which in Yangon would file every morning under the previous day.
   */
  function day(back) {
    const t = new Date();
    t.setDate(t.getDate() - back);
    const p = (n) => String(n).padStart(2, '0');
    return `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())}`;
  }

  function windowOf(id) {
    const days = RANGES.find((r) => r.id === id)?.days ?? 7;
    return `start=${day(days - 1)}&end=${day(0)}&tz=${encodeURIComponent(TZ)}`;
  }

  async function loadSummary() {
    summaryError = null;
    embedsError = null;
    // Both are windowed, so both move with the pills. The branch card counts
    // questions over the same window Signals does — two counts of the same
    // thing over different windows on one screen is a defect, not a nuance.
    const [sum, emb] = await Promise.allSettled([
      getJSON('/admin/analytics/summary?' + windowOf(range)),
      getJSON('/admin/analytics/embeds?' + windowOf(range))
    ]);
    if (sum.status === 'fulfilled') summary = sum.value;
    else {
      summary = null;
      summaryError = sum.reason;
    }
    if (emb.status === 'fulfilled') embeds = Array.isArray(emb.value) ? emb.value : [];
    else {
      embeds = null;
      embedsError = emb.reason;
    }
  }

  function setRange(id) {
    if (id === range) return;
    range = id;
    loadSummary();
  }

  async function load() {
    loading = true;
    healthError = null;
    try {
      // No /metrics and no /ready. Every block that read them is gone, and
      // they were the page's only unrecoverable failure: a blip in a
      // process-wide counter would blank a page whose every remaining number
      // comes from somewhere else. Each section below states its own failure
      // in its own words instead.
      //
      // Freshness is one row and no aggregates, so the head can say when the
      // stock file landed without paying for /analytics/data-health.
      try {
        fresh = (await getJSON('/admin/data-freshness')) ?? {};
      } catch {
        fresh = {};
      }
      // The triage cards read current state, not a window: /analytics/data-health
      // counts stub rows, negative quantities and files that arrived but were
      // never recognised. It takes `tz` only — a date range there would put a
      // control on the page that changes nothing.
      try {
        health = await getJSON('/admin/analytics/data-health?tz=' + encodeURIComponent(TZ));
      } catch (e) {
        health = null;
        healthError = e;
      }
      // Current state, not history — same reason /analytics/data-health takes
      // no window. `limit=6` is the design's row count; `affected` comes back
      // whole so the header can say what is not on screen.
      try {
        branches = await getJSON('/admin/analytics/branch-stock?limit=6');
      } catch (e) {
        branches = null;
        branchesError = e;
      }
      // "Latest" is not a window: the rail asks for the newest handful of
      // events regardless of which pill is selected, so this loads once.
      //
      // /admin/activity is super_admin ONLY. An admin gets 403 here, which is
      // not a failure — it is the answer. The rail says so rather than showing
      // an empty list, which would read as "nothing has happened".
      try {
        const j = await getJSON('/admin/activity?limit=6&tz=' + encodeURIComponent(TZ));
        feed = Array.isArray(j?.rows) ? j.rows : [];
      } catch (e) {
        feed = null;
        feedError = e;
      }
      await loadSummary();
    } finally {
      // Nothing in here throws: every call above catches into its own state.
      // The page always renders — worst case, as four sections each saying
      // what it could not read.
      loading = false;
    }
  }

  onMount(load);

  const num = (v) => (typeof v === 'number' ? v : null);
  const fmt = (v) => (v === null || v === undefined ? '–' : v.toLocaleString());

  let turns = $derived(num(summary?.turns));
  let refusals = $derived(num(summary?.refusals));
  let rangeLabel = $derived(RANGES.find((r) => r.id === range)?.label ?? '7 days');

  /** The one place that decides when a stock file is old.
   *
   *  The subhead's "so answers are current" and the stale-file card are the same
   *  claim seen from two sides, so they read one constant: a page that says
   *  answers are current above a card saying the file is stale is worse than
   *  either sentence alone. */
  const STALE_HOURS = 24;

  const hoursSince = (ts) => {
    const h = (Date.now() - new Date(ts).getTime()) / 3.6e6;
    return Number.isFinite(h) ? h : null;
  };

  /** Stale by the number the READER sees, not by the raw one.
   *
   *  `ago()` rounds, so a 23.6-hour-old file prints as "24 hours ago". Testing
   *  the unrounded value put "The stock file landed 24 hours ago, so answers
   *  are current" on screen next to a threshold of 24 — the sentence and the
   *  rule disagreeing by a rounding step. Whatever the words say is what gets
   *  judged. */
  const isStale = (ts) => {
    const h = hoursSince(ts);
    return h === null ? null : Math.round(h) >= STALE_HOURS;
  };

  /** How long ago, in words. Rounded, because the head is a sentence. */
  function ago(ts) {
    const ms = Date.now() - new Date(ts).getTime();
    if (!Number.isFinite(ms)) return null;
    const m = Math.round(ms / 60000);
    if (m < 1) return 'less than a minute ago';
    if (m < 60) return `${m} minute${m === 1 ? '' : 's'} ago`;
    const h = Math.round(m / 60);
    if (h < 48) return `${h} hour${h === 1 ? '' : 's'} ago`;
    return `${Math.round(h / 24)} days ago`;
  }

  /** The headline is a claim about the numbers, so it never outruns them: with
   *  no summary it says the log could not be read rather than "everything is
   *  answering", which is the sentence the failed call would have proved. */
  let headline = $derived(
    summaryError
      ? 'Usage could not be read.'
      : turns === null
        ? `Reading the last ${rangeLabel}…`
        : turns === 0
          ? `No questions in the last ${rangeLabel}.`
          : refusals
            ? `${fmt(refusals)} of ${fmt(turns)} questions came back empty.`
            : 'Everything is answering.'
  );

  let subhead = $derived.by(() => {
    if (summaryError)
      return `The usage log did not answer${summaryError.status ? ` (HTTP ${summaryError.status})` : ''}, so nothing on this page counts questions.`;
    if (turns === null) return '';
    const asked =
      turns === 0
        ? `Nobody asked the assistant anything in the last ${rangeLabel}.`
        : `The assistant answered ${fmt(turns)} question${turns === 1 ? '' : 's'} in the last ${rangeLabel}, and every number in them came from the catalog.`;
    const at = fresh.inventory_at;
    const when = at ? ago(at) : null;
    // "so answers are current" is a judgement, and it is only true while the
    // file is fresh. An eight-day-old load gets its age stated and no verdict.
    const stale = at ? isStale(at) : null;
    const landed = !when
      ? 'No stock file load has been recorded, so how current these answers are is unknown.'
      : stale === false
        ? `The stock file landed ${when}, so answers are current.`
        : `The stock file last landed ${when} — that is the age of every stock number below.`;
    return `${asked} ${landed}`;
  });

  /** This console's own chat tester logs as an embed client too
   *  (`app/cache.py::INTERNAL_CHAT_EMBED_ID`). Counting every embed row as
   *  branch traffic would report the operator's own testing back to them as
   *  branch usage — and on this instance that is 11 of 12 turns, so the error
   *  would not be a rounding one. */
  const CONSOLE_EMBED = 'admin-chat';

  /** Who actually used the assistant, split from who used the console.
   *
   *  A NULL `embed_id` is a turn logged before the audit columns existed. It is
   *  neither branch nor console — it is unattributed, and it is counted as
   *  itself so the two numbers that ARE attributable stay true. */
  let branch = $derived.by(() => {
    if (!Array.isArray(embeds)) return null;
    const n = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
    const rows = embeds.filter((r) => r?.embed_id !== CONSOLE_EMBED && r?.embed_id != null);
    const stores = new Set(rows.map((r) => r?.store_id).filter((v) => v != null));
    const last = rows
      .map((r) => r?.last_seen)
      .filter(Boolean)
      .sort()
      .at(-1);
    return {
      turns: rows.reduce((a, r) => a + n(r?.turns), 0),
      stores: stores.size,
      lastSeen: last ?? null,
      console: embeds
        .filter((r) => r?.embed_id === CONSOLE_EMBED)
        .reduce((a, r) => a + n(r?.turns), 0),
      unattributed: embeds
        .filter((r) => r?.embed_id == null)
        .reduce((a, r) => a + n(r?.turns), 0)
    };
  });

  // Same five labels the Feed tab uses, so one event does not have two names in
  // one product. Anything else is an app event, whose action is a verb already.
  const FEED_LABEL = {
    login_ok: 'signed in',
    login_fail: 'could not sign in',
    login_locked: 'was locked out',
    sso_ok: 'signed in through SSO',
    sso_fail: 'could not sign in through SSO'
  };
  const BAD = /fail|denied|locked|blocked|error|set_aside|refused|reject/i;

  /** One event as a sentence. Never invents a subject: the pipeline has no
   *  actor, and "someone" would be a person who does not exist. */
  function feedText(r) {
    const action = String(r?.action ?? '');
    const said = FEED_LABEL[action] ?? action.replace(/_/g, ' ');
    const who = r?.actor;
    const what = r?.target && r.target !== who ? ` — ${r.target}` : '';
    return who ? `${who} ${said}${what}` : `${said}${what}`;
  }

  const feedTone = (r) =>
    BAD.test(String(r?.action ?? '')) ? 'bg-danger' : r?.source === 'auth' ? 'bg-accent-2' : 'bg-accent';

  let feedHref = $derived(base + '/analytics?' + new URLSearchParams(openSection('feed')));

  /** The branches on screen, with a bar length each.
   *
   *  The bar is scaled to the WORST branch shown, not to that branch's row
   *  count. A share-of-rows bar reads 0.6% for the worst branch in the estate
   *  and is indistinguishable from 0.1% for the best — the design's coverage
   *  bar, measured, sat between 99.4% and 100% across all 53. What a reader
   *  needs here is the ordering, so the bar draws the ordering and the number
   *  beside it stays absolute. */
  let branchRows = $derived.by(() => {
    const rows = branches?.rows;
    if (!Array.isArray(rows) || !rows.length) return [];
    const worst = Math.max(...rows.map((r) => (typeof r?.negative === 'number' ? r.negative : 0)), 1);
    return rows.map((r) => ({
      ...r,
      pct: Math.max(4, Math.round(((typeof r?.negative === 'number' ? r.negative : 0) / worst) * 100))
    }));
  });

  /** What needs a person, worst first.
   *
   *  Ordered by how directly each one is already making an answer wrong, not by
   *  how large its number is: a single thumbs-down is somebody telling us the
   *  answer was wrong, and outranks 400 stub rows that MIGHT produce a wrong
   *  one. Every entry names its own number — a card that says "check the
   *  catalog" with no count is a chore, not a finding.
   *
   *  Each threshold is a floor for "this is real", never a claim that anything
   *  under it is fine: one negative quantity is one impossible number the agent
   *  will read out, so the floor is one. */
  let needs = $derived.by(() => {
    const out = [];

    const down = num(summary?.feedback?.down);
    if (down)
      out.push({
        id: 'rated-down',
        tone: 'danger',
        title: `${fmt(down)} answer${down === 1 ? '' : 's'} marked wrong`,
        body: `Somebody read the answer and said it was wrong. That is the only signal here a person left by hand — the rest are counts of rows that might go wrong.`,
        cta: 'Read the answers',
        href: base + '/quality?tab=answers'
      });

    const at = fresh.inventory_at;
    const stale = at ? isStale(at) : null;
    if (at && stale === true)
      out.push({
        id: 'stale-stock',
        tone: 'warning',
        title: `Stock file is ${ago(at)}`,
        body: `Every quantity the assistant quotes is that old. It answers confidently either way, so nothing on the branch side shows the difference.`,
        cta: 'Check the file drop',
        href: base + '/ftp'
      });
    else if (!at)
      out.push({
        id: 'no-stock-load',
        tone: 'warning',
        title: 'No stock file load recorded',
        body: `No inventory file has been seen through the pipeline, so how current the quantities are cannot be established from here.`,
        cta: 'Check the file drop',
        href: base + '/ftp'
      });

    const f = health?.funnel;
    const missed = f ? num(f.arrived) - num(f.detected) : null;
    if (missed !== null && Number.isFinite(missed) && missed > 0)
      out.push({
        id: 'not-detected',
        tone: 'warning',
        title: `${fmt(missed)} of ${fmt(num(f.arrived))} files arrived and were not recognised`,
        body: `They landed in the drop and the pipeline did not know what they were, so nothing in them reached the catalog or the stock table.`,
        cta: 'See what landed',
        href: base + '/ftp'
      });

    const neg = num(health?.inventory?.negative);
    if (neg)
      out.push({
        id: 'negative-stock',
        tone: 'danger',
        title: `${fmt(neg)} rows hold a negative quantity`,
        body: `A negative count is not a low stock level, it is an impossible one — and the assistant reads it out as written.`,
        cta: 'Open inventory',
        href: base + '/data'
      });

    const stubs = num(health?.catalog?.stubs);
    const total = num(health?.catalog?.total);
    if (stubs)
      out.push({
        id: 'stub-rows',
        tone: 'info',
        title: `${fmt(stubs)} products have no usable name`,
        body: `${total ? `${fmt(stubs)} of ${fmt(total)} catalog rows` : 'These rows'} carry a code and little else, which is the closest thing there is to a predictor of "we have it but the assistant says we don't".`,
        cta: 'Open the catalog',
        href: base + '/data'
      });

    return out;
  });

  const SHOWN = 3;
  let shownNeeds = $derived(needs.slice(0, SHOWN));
  // The design gives this section three cards. A fourth finding is not thereby
  // less true, so the rest are listed under the grid rather than dropped: the
  // cap is a layout decision and must never become a filter.
  let restNeeds = $derived(needs.slice(SHOWN));

  const COUNT_WORDS = ['Nothing', 'One thing', 'Two things', 'Three things', 'Four things',
                       'Five things', 'Six things', 'Seven things', 'Eight things', 'Nine things'];
  const countPhrase = (n) => (n < COUNT_WORDS.length ? COUNT_WORDS[n] : `${n} things`);

  /** The second clause of the headline. It is only ever a count of the cards
   *  actually below it, so the head and the list cannot disagree. */
  let needsClause = $derived(
    needs.length ? `${countPhrase(needs.length)} need${needs.length === 1 ? 's' : ''} you.` : ''
  );

  /** A daily series for a sparkline, or `null`.
   *
   *  Only `by_day` can supply one: `deltas` carries movement, not a shape. So a
   *  KPI whose series `by_day` does not hold gets NO sparkline — Kpi reserves
   *  the slot so the row stays aligned. Drawing the wrong series under the right
   *  number (a p50 line beneath a p95 headline) is the failure this avoids; an
   *  empty slot only says we have no daily shape for it.
   */
  const seriesOf = (key) => {
    const rows = summary?.by_day;
    if (!Array.isArray(rows) || rows.length < 2) return null;
    const out = rows.map((r) => (typeof r?.[key] === 'number' ? r[key] : null));
    return out.some((v) => v !== null) ? out : null;
  };

  let sig = $derived.by(() => {
    const d = summary?.deltas ?? {};
    const t = num(summary?.turns);
    const distinct = num(summary?.distinct);
    const rate = num(summary?.repeat_rate);
    const ref = num(summary?.refusals);
    const p50 = num(summary?.p50_ms);
    const p95v = num(summary?.p95_ms);
    const cost = num(summary?.cost_usd);
    const tok = num(summary?.tokens?.total);

    // Every footnote below is a MEASURED reading, never a target. No owner has
    // given this product a "good" number for cost or speed, so the cards state
    // what the window did and stop — an invented threshold would read exactly
    // like an agreed one.
    return [
      {
        label: 'Questions asked',
        value: fmtInt(t),
        // Usage is neither good nor bad, so the chip stays neutral: a green
        // arrow here would call a quiet week a failure.
        good: 'none',
        delta: deltaOf(d.turns),
        spark: seriesOf('n'),
        tone: 'accent',
        foot:
          t === null
            ? 'Not read.'
            : t === 0
              ? `Nothing was asked in the last ${rangeLabel}.`
              : `${fmtInt(distinct)} distinct question${distinct === 1 ? '' : 's'}${
                  rate === null ? '' : ` · ${fmtPct(rate)} had been asked before`
                }.`
      },
      {
        label: 'Came back empty',
        value: fmtInt(ref),
        good: 'down',
        delta: deltaOf(d.refusals),
        spark: null,
        tone: ref ? 'bad' : 'ok',
        foot:
          ref === null || t === null
            ? 'Not read.'
            : t === 0
              ? 'No questions to refuse.'
              : ref === 0
                ? `Every one of ${fmtInt(t)} questions came back with an answer.`
                : `${fmtInt(ref)} of ${fmtInt(t)} questions (${fmtPct(ref / t)}) found nothing to say.`
      },
      {
        label: 'Half answered within',
        value: fmtMs(p50),
        good: 'down',
        delta: deltaOf(d.p50_ms),
        // by_day carries p50 per day and nothing else, so this is the one
        // latency series that can be drawn under its own number.
        spark: seriesOf('p50_ms'),
        tone: 'info',
        foot: p95v === null ? 'Not read.' : `19 in 20 arrive within ${fmtMs(p95v)}.`
      },
      {
        label: 'Model spend',
        value: cost === null ? UNKNOWN : '$' + cost.toFixed(2),
        good: 'down',
        delta: deltaOf(d.cost_usd),
        spark: null,
        tone: 'warn',
        deltaFmt: (v) => (v > 0 ? '+' : '−') + '$' + Math.abs(v).toFixed(2),
        foot:
          cost === null
            ? 'Not read.'
            : t
              ? `$${(cost / t).toFixed(3)} a question${tok === null ? '' : ` · ${fmtInt(tok)} tokens`}.`
              : 'No questions in this window.'
      }
    ];
  });

</script>

{#if loading}
  <!-- Shaped like what arrives: head, three triage cards, four signals, a
       320px rail. A skeleton that does not match its page is a layout jump. -->
  <div class="space-y-3">
    <div class="skel" style="height:34px;width:420px"></div>
    <div class="skel" style="height:18px;width:640px"></div>
    <div class="mt-7 grid items-start gap-[18px] lg:grid-cols-[minmax(0,1fr)_320px]">
      <div class="space-y-3">
        <div class="grid gap-3 md:grid-cols-3">
          {#each Array(3) as _}<div class="skel" style="height:170px"></div>{/each}
        </div>
        <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {#each Array(4) as _}<div class="skel" style="height:150px"></div>{/each}
        </div>
      </div>
      <div class="space-y-3">
        {#each [180, 120, 260] as h}<div class="skel" style="height:{h}px"></div>{/each}
      </div>
    </div>
  </div>
{:else}
  <div class="flex flex-wrap items-end gap-4">
    <div class="min-w-0">
      <!-- No `page-title` here: that class carries its own `letter-spacing:-0.011em`,
           and being defined outside Tailwind's layers it BEAT the tracking utility —
           the head measured -0.286px while the design asks for -0.572px. -->
      <h1 class="text-display font-semibold tracking-[-0.022em] text-ink">
        {headline}{#if needsClause}&nbsp;<span class="text-ink-2">{needsClause}</span>{/if}
      </h1>
      {#if subhead}
        <p class="mt-2 max-w-[660px] text-body leading-[1.55] text-ink-2">{subhead}</p>
      {/if}
    </div>
    <div
      class="ml-auto inline-flex rounded-card border border-line bg-surface p-[3px]"
      role="group"
      aria-label="Reporting window"
    >
      {#each RANGES as r}
        <button
          onclick={() => setRange(r.id)}
          aria-pressed={range === r.id}
          class="rounded-control px-3.5 py-1.5 text-meta transition-colors
            {range === r.id ? 'bg-ink font-semibold text-page' : 'font-medium text-ink-2 hover:text-ink'}"
        >
          {r.label}
        </button>
      {/each}
    </div>
  </div>

  <div class="mt-7 grid items-start gap-[18px] lg:grid-cols-[minmax(0,1fr)_320px]">
  <div class="min-w-0">

  <!-- NEEDS YOU -->
  <section aria-labelledby="needs-heading">
    <div class="mb-3 flex items-baseline gap-3">
      <h2 id="needs-heading" class="text-title font-semibold tracking-[-0.014em] text-ink">Needs you</h2>
      {#if restNeeds.length}
        <!-- Never silently truncate: three cards over four findings reads as
             "that is everything" unless the rest are counted out loud. -->
        <span class="text-meta text-ink-3">{restNeeds.length} more, listed below</span>
      {/if}
    </div>

    {#if needs.length}
      <div class="grid gap-3 md:grid-cols-3">
        {#each shownNeeds as n (n.id)}
          <article class="elev flex flex-col rounded-panel border border-line bg-surface p-[15px]">
            <span
              class="mb-2.5 inline-flex w-fit items-center gap-1.5 rounded-control px-2 py-[3px] text-micro font-semibold uppercase tracking-[0.06em]
                {n.tone === 'danger'
                  ? 'bg-danger-soft text-danger'
                  : n.tone === 'warning'
                    ? 'bg-warning-soft text-warning'
                    : 'bg-info-soft text-info'}"
            >
              <span class="size-1.5 rounded-full bg-current"></span>
              {n.tone === 'danger' ? 'Wrong now' : n.tone === 'warning' ? 'Going stale' : 'Worth fixing'}
            </span>
            <h3 class="text-body font-semibold leading-[1.35] text-ink">{n.title}</h3>
            <p class="mt-1.5 text-body-sm leading-[1.5] text-ink-2">{n.body}</p>
            <a
              href={n.href}
              class="mt-3 inline-flex w-fit items-center gap-1 text-meta font-semibold text-accent hover:underline"
            >
              {n.cta} →
            </a>
          </article>
        {/each}
      </div>
      {#if restNeeds.length}
        <ul class="mt-3 divide-y divide-line rounded-panel border border-line bg-surface">
          {#each restNeeds as n (n.id)}
            <li class="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-[15px] py-2.5">
              <span
                class="size-1.5 shrink-0 self-center rounded-full
                  {n.tone === 'danger' ? 'bg-danger' : n.tone === 'warning' ? 'bg-warning' : 'bg-info'}"
              ></span>
              <span class="text-body-sm font-semibold text-ink">{n.title}</span>
              <a href={n.href} class="ml-auto text-meta font-semibold text-accent hover:underline">
                {n.cta} →
              </a>
            </li>
          {/each}
        </ul>
      {/if}
    {:else if healthError && summaryError}
      <!-- Both sources failed, so "nothing needs you" would be a claim neither
           call supports. Say what could not be read instead. -->
      <div class="rounded-panel border border-line bg-surface p-[15px]">
        <p class="text-body font-semibold text-ink">The checks could not be run.</p>
        <p class="mt-1.5 text-body-sm leading-[1.5] text-ink-2">
          Neither the data checks{healthError.status ? ` (HTTP ${healthError.status})` : ''} nor the
          usage log{summaryError.status ? ` (HTTP ${summaryError.status})` : ''} answered, so this
          list is empty because nothing was looked at — not because nothing is wrong.
        </p>
      </div>
    {:else}
      <div class="rounded-panel border border-line bg-surface p-[15px]">
        <p class="text-body font-semibold text-ink">Nothing needs you.</p>
        <p class="mt-1.5 text-body-sm leading-[1.5] text-ink-2">
          The stock file is current, every file that arrived was recognised, no quantity is
          impossible and nobody has marked an answer wrong.{#if healthError}
            One caveat: the data checks did not answer{healthError.status
              ? ` (HTTP ${healthError.status})`
              : ''}, so this covers ratings only.{/if}
        </p>
      </div>
    {/if}
  </section>

  <!-- SIGNALS -->
  <section class="mt-7" aria-labelledby="signals-heading">
    <div class="mb-3 flex flex-wrap items-baseline gap-3">
      <h2 id="signals-heading" class="text-title font-semibold tracking-[-0.014em] text-ink">Signals</h2>
      <span class="text-meta text-ink-3">last {rangeLabel}, against the {rangeLabel} before</span>
      <a
        href={base + '/analytics'}
        class="ml-auto text-meta font-semibold text-accent hover:underline">All analytics →</a
      >
    </div>

    {#if summaryError}
      <!-- One failed call, four cards' worth of zeros. Say that instead. -->
      <div class="rounded-panel border border-line bg-surface p-[15px]">
        <p class="text-body font-semibold text-ink">Usage could not be read.</p>
        <p class="mt-1.5 text-body-sm leading-[1.5] text-ink-2">
          The usage log did not answer{summaryError.status ? ` (HTTP ${summaryError.status})` : ''},
          so these four numbers are missing rather than zero.
        </p>
      </div>
    {:else}
      <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {#each sig as k (k.label)}
          <Kpi
            label={k.label}
            value={k.value}
            foot={k.foot}
            spark={k.spark}
            tone={k.tone}
            delta={k.delta}
            good={k.good}
            deltaFmt={k.deltaFmt}
          />
        {/each}
      </div>
    {/if}
  </section>

  <!-- WHERE THE NUMBERS ARE WRONG -->
  <!--
    The design calls this section "Where stock is thin" and ranks branches by
    products out or low. Measured on the real estate before it was built: 2
    rows in 111,654 are out, 0 are blank, and the 1-19 "low" band holds 86% of
    every branch because the median quantity in this catalog is 6. That ranking
    is 53 near-ties. Impossible quantities have a real shape — 79 rows across
    32 branches, worst 13 — so the section ranks by those and is named after
    what it measures.
  -->
  <section class="mt-7 overflow-hidden rounded-panel border border-line bg-surface" aria-labelledby="wrong-heading">
    <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-line-2 px-[18px] py-[13px]">
      <h2 id="wrong-heading" class="text-body font-semibold tracking-[-0.012em] text-ink">
        Where the numbers are wrong
      </h2>
      <span class="text-meta text-ink-3">branches ranked by impossible quantities</span>
      {#if branches?.affected}
        <a href={base + '/data'} class="ml-auto text-meta font-semibold text-accent hover:underline">
          All {fmtInt(branches.affected)} affected →
        </a>
      {/if}
    </div>

    {#if branchesError}
      <p class="px-[18px] py-3.5 text-body-sm leading-[1.5] text-ink-2">
        The per-branch counts could not be read{branchesError.status
          ? ` (HTTP ${branchesError.status})`
          : ''}, so the impossible quantities counted above cannot be placed at a
        branch from here.
      </p>
    {:else if branchRows.length}
      <div role="table" aria-label="Branches ranked by impossible quantities">
        <div
          role="row"
          class="flex items-center gap-3 border-b border-line-2 px-[18px] py-2 text-micro font-semibold uppercase tracking-[0.06em] text-ink-3"
        >
          <span role="columnheader" class="min-w-0 flex-1">Branch</span>
          <span role="columnheader" class="w-[86px] shrink-0 text-right">Impossible</span>
          <span role="columnheader" class="w-[44px] shrink-0 text-right">Out</span>
          <span role="columnheader" class="w-[96px] shrink-0 text-right">Not recorded</span>
          <span role="columnheader" class="w-[130px] shrink-0">Share of the worst</span>
        </div>
        {#each branchRows as r (r.site_code)}
          <div role="row" class="flex items-center gap-3 border-t border-line-2 px-[18px] py-2.5">
            <span role="cell" class="min-w-0 flex-1 truncate font-mono text-body-sm text-ink">
              {r.site_code}
            </span>
            <span role="cell" class="w-[86px] shrink-0 text-right font-mono text-body-sm font-semibold text-danger">
              {fmtInt(r.negative)}
            </span>
            <!-- 0 here is measured, not missing: the query counts the band. -->
            <span
              role="cell"
              class="w-[44px] shrink-0 text-right font-mono text-body-sm {r.out ? 'text-warning' : 'text-ink-3'}"
            >
              {fmtInt(r.out)}
            </span>
            <span
              role="cell"
              class="w-[96px] shrink-0 text-right font-mono text-body-sm {r.unknown ? 'text-warning' : 'text-ink-3'}"
            >
              {fmtInt(r.unknown)}
            </span>
            <span role="cell" class="flex w-[130px] shrink-0 items-center gap-2.5">
              <span class="block h-1.5 flex-1 overflow-hidden rounded-full bg-surface-2">
                <span class="block h-full rounded-full bg-danger" style="width:{r.pct}%"></span>
              </span>
            </span>
          </div>
        {/each}
      </div>
      {#if branches?.affected > branchRows.length}
        <p class="border-t border-line-2 px-[18px] py-2.5 text-meta text-ink-3">
          {fmtInt(branches.affected - branchRows.length)} more branch{branches.affected - branchRows.length === 1
            ? ''
            : 'es'} hold at least one impossible quantity, of {fmtInt(branches.total)} with stock recorded.
        </p>
      {/if}
    {:else if branches}
      <p class="px-[18px] py-3.5 text-body-sm leading-[1.5] text-ink-2">
        {#if branches.total}
          No branch holds an impossible quantity. All {fmtInt(branches.total)} branches with stock
          recorded hold numbers the assistant can quote as written.
        {:else}
          No branch has any stock recorded, so there is nothing to rank.
        {/if}
      </p>
    {/if}
  </section>

  </div>

  <!-- RIGHT RAIL -->
  <div class="flex flex-col gap-3">
    <!-- Branch assistant -->
    <section
      class="elev rounded-panel border border-line bg-surface p-[15px]"
      aria-labelledby="branch-heading"
    >
      <div class="flex items-center gap-2">
        <Smartphone size={16} class="text-accent" />
        <h2 id="branch-heading" class="text-body font-semibold text-ink">Branch assistant</h2>
      </div>
      <p class="mt-2 text-body-sm leading-[1.55] text-ink-2">
        Branch staff never open this console. They ask the assistant inside your own site, and it
        answers for their branch only.
      </p>

      {#if embedsError}
        <p class="mt-3 text-body-sm leading-[1.5] text-ink-2">
          Embed traffic could not be read{embedsError.status
            ? ` (HTTP ${embedsError.status})`
            : ''}, so how much of the usage above came from a branch is unknown.
        </p>
      {:else if branch}
        {#if branch.turns}
          <div class="mt-3 flex flex-col gap-2">
            <div class="flex items-center gap-2 text-body-sm text-ink-2">
              <span class="size-[7px] shrink-0 rounded-full bg-accent"></span>
              {fmtInt(branch.stores)} branch{branch.stores === 1 ? '' : 'es'} asked
              <span class="ml-auto font-mono text-label text-ink-3"
                >{branch.lastSeen ? ago(branch.lastSeen) : UNKNOWN}</span
              >
            </div>
            <div class="flex items-center gap-2 text-body-sm text-ink-2">
              <span class="size-[7px] shrink-0 rounded-full bg-accent"></span>
              {fmtInt(branch.turns)} question{branch.turns === 1 ? '' : 's'}
              <span class="ml-auto font-mono text-label text-ink-3">{rangeLabel}</span>
            </div>
          </div>
        {:else}
          <!-- The count in Signals is right; who asked is the part that would
               otherwise be assumed. Say where it came from instead. -->
          <p class="mt-3 text-body-sm leading-[1.5] text-ink-2">
            No branch has asked anything in the last {rangeLabel}.{#if branch.console}&nbsp;The {fmtInt(branch.console)} question{branch.console === 1 ? '' : 's'} counted above {branch.console ===
              1
                ? 'was'
                : 'were'} asked here, in this console's own chat.{/if}
          </p>
        {/if}
        {#if branch.unattributed}
          <p class="mt-2 text-meta leading-[1.45] text-ink-3">
            {fmtInt(branch.unattributed)} older question{branch.unattributed === 1
              ? ' carries'
              : 's carry'} no embed id and {branch.unattributed === 1 ? 'is' : 'are'} in neither count.
          </p>
        {/if}
      {/if}

      <a
        href={base + '/widget'}
        class="mt-3.5 flex w-full items-center justify-center gap-1.5 rounded-control border border-line bg-surface-2 px-3 py-2.5 text-body-sm font-semibold text-ink hover:border-accent hover:text-accent"
      >
        See what a branch sees <ArrowRight size={14} />
      </a>
    </section>

    <!-- Read-only by design -->
    <section
      class="rounded-panel border border-[color-mix(in_srgb,var(--color-accent)_24%,transparent)]
             bg-accent-soft p-[15px]"
      aria-labelledby="readonly-heading"
    >
      <div class="flex items-center gap-2">
        <ShieldCheck size={16} class="text-accent" />
        <h2 id="readonly-heading" class="text-body font-semibold text-accent">Read-only by design</h2>
      </div>
      <p class="mt-2 text-body-sm leading-[1.55] text-accent">
        The assistant queries the catalog and answers. It cannot transfer stock, place an order or
        write an inventory row — nothing here can change what is on a shelf.
      </p>
    </section>

    <!-- Latest activity -->
    <section
      class="elev rounded-panel border border-line bg-surface p-[15px]"
      aria-labelledby="feed-heading"
    >
      <div class="flex items-center gap-2.5">
        <h2 id="feed-heading" class="text-body font-semibold text-ink">Latest activity</h2>
        <a href={feedHref} class="ml-auto text-meta font-semibold text-accent hover:underline"
          >Full feed →</a
        >
      </div>

      {#if feedError}
        <p class="mt-2.5 text-body-sm leading-[1.5] text-ink-2">
          {#if feedError.status === 403}
            Only a super admin can read the activity feed, so this list is empty because it was not
            shown to you — not because nothing has happened.
          {:else}
            The activity feed did not answer{feedError.status
              ? ` (HTTP ${feedError.status})`
              : ''}, so nothing is listed here.
          {/if}
        </p>
      {:else if feed && feed.length}
        <ul class="mt-3 flex flex-col gap-3">
          {#each feed as r, i (r.ts + '|' + r.source + '|' + r.action + '|' + i)}
            <li class="flex gap-2.5">
              <span class="mt-1.5 size-[7px] shrink-0 rounded-full {feedTone(r)}"></span>
              <div class="min-w-0 flex-1">
                <div class="text-body-sm leading-[1.45] text-ink">{feedText(r)}</div>
                <div class="mt-0.5 text-meta text-ink-3">
                  {ago(r.ts) ?? UNKNOWN} · {r.source}
                </div>
              </div>
            </li>
          {/each}
        </ul>
      {:else if feed}
        <p class="mt-2.5 text-body-sm leading-[1.5] text-ink-2">
          Nothing has been recorded yet — no sign-in, no admin action, no file.
        </p>
      {/if}
    </section>
  </div>
  </div>

{/if}
