<script>
  // Analytics — ten tabs over ONE filter bar, every chart clickable.
  //
  // Four rules run through this file. Each one is here because breaking it
  // produces a dashboard that looks right and is wrong:
  //
  //  1. UNKNOWN IS NOT ZERO. A metric with no data renders `—`. A cost with no
  //     configured price renders "not configured", never $0.00 — a zero reads
  //     as "free" and nobody notices for months.
  //  2. A RATE CARRIES ITS DENOMINATOR. "92% of 25 rated", never a bare 92%.
  //  3. A MISSING ENDPOINT IS NOT A CRASH. Every section loads independently and
  //     renders its own panel on 404/500; the rest of the tab keeps working.
  //  4. THE FILTER STATE LIVES IN THE URL, under the API's own parameter names,
  //     so a filtered view is a link.
  import { tick, untrack } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { base } from '$app/paths';
  import PageHeader from '$lib/PageHeader.svelte';
  import Badge from '$lib/Badge.svelte';
  import { getJSON, ApiError } from '$lib/api.js';
  import {
    LayoutDashboard,
    MessagesSquare,
    Users,
    Code2,
    Clock,
    Coins,
    Database,
    CheckCheck,
    ShieldCheck,
    HardDrive,
    RefreshCw,
    ChevronLeft,
    ChevronRight,
    Activity,
    Compass,
    List,
    KeyRound,
    TrendingUp
  } from '@lucide/svelte';

  import Kpi from '$lib/charts/Kpi.svelte';
  import Section from '$lib/charts/Section.svelte';
  import LineChart from '$lib/charts/LineChart.svelte';
  import StackedBars from '$lib/charts/StackedBars.svelte';
  import RankBars from '$lib/charts/RankBars.svelte';
  import Donut from '$lib/charts/Donut.svelte';
  import Heatmap from '$lib/charts/Heatmap.svelte';
  import Funnel from '$lib/charts/Funnel.svelte';
  import Table from '$lib/charts/Table.svelte';
  import FilterBar from '$lib/analytics/FilterBar.svelte';
  import ActivityFilters from '$lib/activity/ActivityFilters.svelte';
  import FeedTab from '$lib/activity/FeedTab.svelte';
  import AuditTab from '$lib/activity/AuditTab.svelte';
  import TrendsTab from '$lib/activity/TrendsTab.svelte';
  import ExploreTab from '$lib/activity/ExploreTab.svelte';
  import { browserTz, buildQuery as activityQuery, SOURCES } from '$lib/activity/shared.js';
  import {
    ACTIVITY_SECTIONS,
    FILTER_KEYS,
    ACTIVITY_KEYS,
    PANEL_LOCAL,
    hrefFor
  } from '$lib/analytics/routes.js';
  import TurnDrawer from '$lib/charts/TurnDrawer.svelte';
  import TraceView from '$lib/charts/TraceView.svelte';
  import GapCard from '$lib/charts/GapCard.svelte';
  import WarnBar from '$lib/charts/WarnBar.svelte';
  import TzChip from '$lib/charts/TzChip.svelte';
  import {
    UNKNOWN,
    isNum,
    int,
    ms,
    secs,
    pct,
    asPct,
    share,
    rateOf,
    usd,
    when,
    clock,
    dayLabel,
    isoDay,
    clip,
    langName,
    perMillion,
    ratio,
    browserZone,
    signedInt,
    signedPctOf,
    signedSecs,
    signedUsd
  } from '$lib/charts/format.js';

  // Chart colours, one place. `a2` (the logo cyan) was removed rather than
  // repointed: it was being used as a second DATA series, and DESIGN.md 2.3
  // retires it to a brand mark. `s2` is the real second series.
  //
  // Two entries here were fills a reader could not see, and both carried
  // meaning:
  //
  //  * `ok` was --color-success, which in DARK mode is byte-identical to
  //    --color-accent (#9BA0F0). "Questions" and "From cache" are two lines of
  //    the same chart (Volume over time) and were the same colour — ratio 1.00,
  //    dE2000 0.0. It is now series-3, which is 22.1 dE from series-1.
  //  * `muted` was --color-line-2, a HAIRLINE token: measured 1.18:1 on a card
  //    and 1.29:1 on the page in dark, and 1.14:1 / 1.05:1 in light, which is
  //    worse. It painted the "Miss" bar, the "Target 30%" line, the "Other
  //    languages" band and the "other modes" band — four things a reader is
  //    meant to read off the chart. It is renamed `other` and points at
  //    --color-series-other, a legible neutral (5.71:1 light, 3.65:1 dark on
  //    --color-surface-2). There is no `muted` any more, deliberately: the name
  //    is what invited a hairline into a data series.
  //
  // Every value below is a chart-series or verdict token. See the SERIES block
  // comment in app.css for what the palette guarantees and what it cannot.
  const C = {
    accent: 'var(--color-series-1)',
    s2: 'var(--color-series-2)',
    s3: 'var(--color-series-3)',
    s4: 'var(--color-series-4)',
    ok: 'var(--color-series-3)',
    bad: 'var(--color-danger)',
    warn: 'var(--color-warning)',
    other: 'var(--color-series-other)'
  };

  // -------------------------------------------------------------- sections
  //
  // This component draws a SET of sections, named by the page that mounts it.
  // It used to choose that set itself from `?tab=`, which is why the links
  // between sections broke: they named a section, `?tab=` only understood a
  // group, and an unrecognised value fell back to Overview. Sections are now
  // routes, so a link names a destination that exists.
  //
  // A page passes the sections it draws, in the order it draws them. It never
  // mixes chat-turn sections with event sections — they read different
  // endpoints under different filter names, and one bar carrying controls that
  // do nothing on half the page is how a filter comes to lie.
  let {
    sections,
    title,
    subtitle,
    /** Pin the event `source` filter (the security log is the auth slice). */
    lockSource = null,
    /** 2 when this loader is drawn INSIDE a tabbed page that owns the h1. */
    level = 1
  } = $props();

  const SECTION = {
    overview: { label: 'Overview', icon: LayoutDashboard },
    questions: { label: 'Questions', icon: MessagesSquare },
    users: { label: 'Users & sessions', icon: Users },
    quality: { label: 'Quality', icon: CheckCheck },
    diagnostics: { label: 'Diagnostics', icon: ShieldCheck },
    performance: { label: 'Performance', icon: Clock },
    cost: { label: 'Cost & tokens', icon: Coins },
    cache: { label: 'Cache', icon: Database },
    embeds: { label: 'Embeds', icon: Code2 },
    health: { label: 'Data health', icon: HardDrive },
    feed: { label: 'Feed', icon: List },
    audit: { label: 'Audit', icon: KeyRound },
    trends: { label: 'Trends', icon: TrendingUp },
    explore: { label: 'Explore', icon: Compass }
  };

  /**
   * Is this an event page or a chat-turn page? A PAGE-level fact, not a
   * per-render one: the two halves never appear together, so every use of it
   * below is answering "which endpoints does this page read".
   */
  let isActivityView = $derived(sections.some((id) => ACTIVITY_SECTIONS.has(id)));

  // ------------------------------------------------------ URL-held state
  // These are the API's own parameter names (contract §4). Keeping the URL key
  // and the query key identical is what stops a filter drifting between what
  // the page shows and what it asked for.
  //
  // `q` and `tool` are NOT in contract §4, but every deployed endpoint declares
  // them (app/admin.py `_log_filters`), so they are safe to send today. They
  // belong in the contract — see the note to the contract owner.
  //
  // `intent` is deliberately ABSENT. No endpoint declares it, and FastAPI drops
  // an undeclared query param and then answers 200 with unfiltered data — the
  // chip would say "Intent: stock" over a table showing every turn. Until it is
  // declared, the page must not send it and must not offer the control.
  // (the list itself lives in $lib/analytics/routes.js, so the cross-page
  //  link builder and this page cannot drift apart)

  // Flip to true only once `intent` is a declared FastAPI parameter on
  // /questions, /summary, /timeseries and /repeats. Nothing else changes.
  const INTENT_FILTER_DECLARED = false;

  // `embed=none` → `embed_id IS NULL`, landed in app/admin.py and pinned by a
  // test that a turn whose embed_id is the literal string "none" is NOT swept in.
  const EMBED_NONE_SUPPORTED = true;

  /**
   * The reader's own zone, sent as `tz` on every request that buckets by time.
   *
   * This is fixing a defect, not adding a preference. Postgres runs Etc/UTC and
   * buckets with `date_trunc('day', ts)`, while this page labels those buckets
   * in local time — so in Yangon every "day" on every chart actually runs
   * 06:30 → 06:30 and the first six and a half hours of each morning are
   * attributed to the day before. Nothing about the chart looks wrong.
   *
   * Read ONCE at module scope rather than per request: a value that could
   * change between two calls in the same `Promise.all` would put two different
   * midnights on the same screen.
   */
  const TZ = browserZone();

  let url = $derived($page.url);
  let range = $derived(url.searchParams.get('range') ?? '30');
  let f = $derived(Object.fromEntries(FILTER_KEYS.map((k) => [k, url.searchParams.get(k) ?? ''])));
  let day = $derived(url.searchParams.get('day') ?? '');
  // `issue` is declared on /diagnosis only, so it is NOT a shared filter and is
  // never sent to the other endpoints. It narrows the diagnosis queue in place.
  let issue = $derived(url.searchParams.get('issue') ?? '');
  let openTurnId = $derived(url.searchParams.get('turn'));
  // Which section of the active group to scroll to. A group of one ignores it.
  //
  // `sec`, NOT `sub`: `sub` is already the Explore panel's stacking subgroup,
  // and its own links carry `sub=source`. Sharing the name made an Explore link
  // silently overwrite the section it was asking for — a link that navigates,
  // renders, and lands somewhere else.
  let sec = $derived(url.searchParams.get('sec') ?? '');
  /** Is this section drawn on this page? Used by every panel below. */
  let has = $derived((id) => sections.includes(id));
  let traceId = $derived(url.searchParams.get('trace'));

  function nav(mut) {
    const u = new URL(url);
    mut(u.searchParams);
    goto(u.pathname + '?' + u.searchParams.toString(), {
      replaceState: true,
      noScroll: true,
      keepFocus: true
    });
  }
  const setParam = (k, v) => nav((p) => (v ? p.set(k, v) : p.delete(k)));
  /**
   * Go to the page that draws `section`, carrying the filters that still apply.
   *
   * Every cross-section link in this file goes through here. Naming the
   * DESTINATION SECTION rather than a page is deliberate: when a section moves
   * to another page, the links follow it, because they all resolve through one
   * map. The previous arrangement — links naming a tab id — is what left
   * every cross-link on the page landing on Overview.
   */
  function crossTo(section, mutate) {
    qOffset = 0;
    goto(base + hrefFor(section, url, mutate));
  }

  /**
   * Jump to a section drawn further down this page.
   *
   * The section is written to the URL as `sec` so the position is part of the
   * link — that is what lets a cross-page link land ON Audit rather than at the
   * top of the page that draws it.
   */
  async function jumpTo(id) {
    setParam('sec', id);
    await tick();
    document.getElementById('sec-' + id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  function setF(k, v) {
    qOffset = 0;
    setParam(k, v);
  }
  function clearFilters() {
    qOffset = 0;
    nav((p) => {
      for (const k of [...FILTER_KEYS, 'day']) p.delete(k);
    });
  }


  // ------------------------------------------------------- activity filters
  //
  // The event filter bar, on its own URL keys. It is deliberately NOT merged
  // into the analytics filter object: these names go to a different set of
  // endpoints (`/admin/activity/*`), and the one overlap — `actor`, `q` —
  // is cleared when the reader crosses between the two halves of the page.
  // (the list itself lives in $lib/analytics/routes.js, so the cross-page link
  //  builder and this page cannot drift apart)

  // Read ONCE at module scope, like TZ above and for the same reason: a zone
  // that could change between two calls in one Promise.all would put two
  // different midnights on the same screen.
  const ATZ = browserTz();

  // `lockSource` pins the event source rather than defaulting it. The security
  // log IS the auth slice of the feed — if the source were merely a default the
  // reader could clear it from the filter bar and end up on a page headed
  // "Security log" showing ingest events.
  let af = $derived({
    tz: ATZ,
    source:
      lockSource ??
      (SOURCES.some((x) => x.key === url.searchParams.get('source'))
        ? url.searchParams.get('source')
        : ''),
    actor: url.searchParams.get('actor') ?? '',
    action: url.searchParams.get('action') ?? '',
    from: url.searchParams.get('from') ?? '',
    to: url.searchParams.get('to') ?? '',
    q: url.searchParams.get('q') ?? ''
  });
  let aqs = $derived(activityQuery(af));

  // A manual refresh for the activity panels: they watch this number, so
  // "Refresh" re-fetches without any of them exposing a load function upward.
  let actNonce = $state(0);

  // The zone the ACTIVE activity panel's endpoint echoed back. The chip is
  // drawn from this and never from `ATZ`: a backend that has not declared `tz`
  // answers 200 with UTC buckets, and a chip rendered from our own request
  // would label those buckets local — the same defect with better manners.
  let actTz = $state(null);
  const reportTz = (zone) => (actTz = typeof zone === 'string' && zone ? zone : null);

  function actSet(patch, { replace = false } = {}) {
    const u = new URL(url);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === undefined || v === '') u.searchParams.delete(k);
      else u.searchParams.set(k, String(v));
    }
    goto(u.pathname + u.search, { noScroll: true, keepFocus: true, replaceState: replace });
  }
  function actClear() {
    clearTimeout(deb);
    actorInput = '';
    qInput = '';
    actSet({ source: null, actor: null, action: null, from: null, to: null, q: null, offset: null });
  }

  // Text inputs need a local mirror so typing is not fighting the URL, and are
  // pushed back with replaceState so a search does not bury the Back button
  // under one history entry per keystroke.
  let actorInput = $state('');
  let qInput = $state('');
  let syncedFrom = $state(' ');
  $effect(() => {
    const a = af.actor;
    const b = af.q;
    const stamp = `${a} ${b}`;
    if (stamp === syncedFrom) return;
    untrack(() => {
      actorInput = a;
      qInput = b;
      syncedFrom = stamp;
    });
  });
  let deb;
  function debouncedText() {
    clearTimeout(deb);
    deb = setTimeout(
      () => actSet({ actor: actorInput.trim(), q: qInput.trim(), offset: null }, { replace: true }),
      300
    );
  }

  // ---------------------------------------------------------------- params
  const RANGE_DAYS = { 1: 1, 7: 7, 30: 30, 90: 90, all: null };

  const addDays = (iso, n) => isoDay(new Date(new Date(`${iso}T00:00:00Z`).getTime() + n * 864e5).toISOString());

  /**
   * BARE DATES, and the last day we want is sent as-is.
   *
   * §4's amended date rule makes a bare `end` date include the whole day, and
   * `from`/`to` now mean the same thing, so the ambiguity that made me send an
   * explicit instant is gone. Adding a day here would now GAIN one.
   *
   * A bare date is also the safer of the two spellings, for a reason the rule
   * change does not mention: `2026-08-18T00:00:00Z` pins an instant in UTC, and
   * the server does its day arithmetic in its own timezone. Any offset between
   * them silently moves the boundary by those hours — turns near midnight fall
   * in or out depending on where the server thinks it is. A bare date has no
   * instant to disagree about: the server resolves the whole day itself.
   *
   * Only `start`/`end` are sent, never `from`/`to` as well — §4 makes the two
   * spellings with conflicting values a 400, and there is nothing to gain by
   * being that caller.
   */
  let window_ = $derived.by(() => {
    const d = RANGE_DAYS[range];
    if (d == null) return {};
    const today = isoDay(new Date().toISOString());
    return { start: addDays(today, -d), end: today };
  });

  /**
   * The query every request shares — contract §4 names, nothing else.
   *
   * `rows: true` additionally applies the day drill-down. Charts never take it:
   * clicking a point would otherwise collapse the very chart that was clicked
   * into a single column.
   */
  function qs(extra = {}, { rows = false } = {}) {
    const p = new URLSearchParams();
    p.set('tz', TZ);
    const { start, end } = window_;
    if (start) p.set('start', start);
    if (end) p.set('end', end);
    if (rows && day) {
      // Exactly one day. Same bare date both sides — under the amended rule that
      // is the whole of that day, which is what clicking a point on a daily
      // chart means.
      p.set('start', day);
      p.set('end', day);
    }
    for (const k of FILTER_KEYS) if (f[k]) p.set(k, f[k]);
    for (const [k, v] of Object.entries(extra)) if (v != null && v !== '') p.set(k, String(v));
    return p.toString();
  }

  // ---------------------------------------------------------------- fetching
  // Each panel keeps its own status so one missing route cannot blank the page:
  // 'loading' | 'ok' | 'missing' (404 — this backend predates the endpoint)
  // | 'error' (offline / 5xx).
  const blank = () => ({ status: 'loading', data: null, err: null, path: '' });
  async function req(path) {
    try {
      return { status: 'ok', data: await getJSON(path), err: null, path };
    } catch (e) {
      const missing = e instanceof ApiError && e.status === 404;
      return { status: missing ? 'missing' : 'error', data: null, err: e, path };
    }
  }

  let summary = $state(blank());
  let timeseries = $state(blank());
  let questions = $state(blank());
  let repeats = $state(blank());
  let embeds = $state(blank());
  let health = $state(blank());
  let tools = $state(blank());
  let paths = $state(blank());
  let costDay = $state(blank());
  let costModel = $state(blank());
  let toolOutcomes = $state(blank());
  let llmUsage = $state(blank());
  let diagnosis = $state(blank());
  let actors = $state(blank());
  let intents = $state(blank());
  // Console v2. Both may 404 against a backend that predates them; `Section`
  // renders that as "not served by this backend" rather than as no data.
  let llmCalls = $state(blank());
  let economics = $state(blank());
  let slowTurns = $state(blank());

  let qOffset = $state(0);
  const Q_SIZE = 25;
  const CALLS_SIZE = 50;
  const SLOW_WINDOW = 200;
  let loading = $state(false);

  /**
   * Which of the eighteen feeds each section actually reads.
   *
   * When these were all one page, one `Promise.all` served every tab and the
   * cost was paid once. They are pages now, so a reader walking the rail would
   * have paid it six times over. The map is derived from what each section's
   * markup reads — a section left out of a feed's list simply never draws from
   * it, so the failure mode is a missing panel in review, not a wrong number.
   *
   * BASE is what the FILTER BAR needs. It is fetched on every page regardless
   * of section, because a select whose options are missing looks like a filter
   * with nothing to filter by.
   */
  const BASE_FEEDS = ['summary', 'paths', 'embeds', 'llmUsage', 'costModel', 'actors'];
  const SECTION_FEEDS = {
    overview: ['timeseries', 'health', 'tools', 'toolOutcomes', 'economics', 'llmCalls', 'questions'],
    questions: ['intents', 'repeats', 'questions', 'llmCalls'],
    users: ['questions'],
    embeds: [],
    performance: ['timeseries', 'tools', 'toolOutcomes', 'slowTurns', 'llmCalls', 'questions'],
    cost: ['costDay', 'economics', 'llmCalls', 'questions'],
    cache: ['timeseries', 'repeats', 'economics', 'questions'],
    quality: ['timeseries', 'diagnosis', 'questions'],
    diagnostics: ['diagnosis', 'intents', 'slowTurns', 'tools', 'toolOutcomes', 'questions'],
    health: ['health']
  };
  let needed = $derived(
    new Set([...BASE_FEEDS, ...sections.flatMap((id) => SECTION_FEEDS[id] ?? [])])
  );

  async function loadAll() {
    loading = true;
    const q = qs();
    const rowq = qs({ limit: Q_SIZE, offset: qOffset }, { rows: true });
    const want = needed;

    const FEEDS = {
      summary: () => req('/admin/analytics/summary?' + q),
      timeseries: () => req('/admin/analytics/timeseries?' + qs({ bucket: 'day' })),
      questions: () => req('/admin/analytics/questions?' + rowq),
      repeats: () => req('/admin/analytics/repeats?' + qs({ limit: 25 }, { rows: true })),
      embeds: () => req('/admin/analytics/embeds?' + q),
      health: () => req('/admin/analytics/data-health?' + q),
      tools: () => req('/admin/analytics/tools?' + q),
      paths: () => req('/admin/analytics/paths?' + q),
      costDay: () => req('/admin/analytics/cost?' + qs({ group: 'day' })),
      costModel: () => req('/admin/analytics/cost?' + qs({ group: 'model' })),
      toolOutcomes: () => req('/admin/analytics/tool-outcomes?' + q),
      llmUsage: () => req('/admin/analytics/llm-usage?' + q),
      diagnosis: () => req('/admin/analytics/diagnosis?' + qs({ limit: 50, issue }, { rows: true })),
      actors: () => req('/admin/analytics/actors?' + q),
      intents: () => req('/admin/analytics/intents?' + q),
      // `order=turn` (turn_id DESC, seq ASC), NOT `order=ts`. Sorting by
      // timestamp puts the calls of one turn next to each other only while no
      // other turn's call falls between them — true on a quiet box, false on a
      // busy afternoon. The whole point of this table is reading two calls of
      // the SAME turn against each other, so their adjacency has to be
      // guaranteed by the sort rather than left to traffic.
      llmCalls: () => req('/admin/analytics/llm-calls?' + qs({ limit: CALLS_SIZE, order: 'turn' }, { rows: true })),
      economics: () => req('/admin/analytics/economics?' + q),
      // The slow tail. `/questions` has no declared `sort`, so the ordering is
      // done here over a WIDER page than the log table uses — and the caption
      // says so. Sorting the 25-row log page would have produced a table headed
      // "the slow tail" showing whatever happened to be most recent.
      slowTurns: () => req('/admin/analytics/questions?' + qs({ limit: SLOW_WINDOW, offset: 0 }, { rows: true }))
    };

    const keys = Object.keys(FEEDS).filter((k) => want.has(k));
    const got = await Promise.all(keys.map((k) => FEEDS[k]()));
    const by = Object.fromEntries(keys.map((k, i) => [k, got[i]]));

    // Assigned only when fetched. A feed this page does not read keeps its
    // `blank()` — which every panel already renders as "unknown", never zero.
    if (by.summary) summary = by.summary;
    if (by.timeseries) timeseries = by.timeseries;
    if (by.questions) questions = by.questions;
    if (by.repeats) repeats = by.repeats;
    if (by.embeds) embeds = by.embeds;
    if (by.health) health = by.health;
    if (by.tools) tools = by.tools;
    if (by.paths) paths = by.paths;
    if (by.costDay) costDay = by.costDay;
    if (by.costModel) costModel = by.costModel;
    if (by.toolOutcomes) toolOutcomes = by.toolOutcomes;
    if (by.llmUsage) llmUsage = by.llmUsage;
    if (by.diagnosis) diagnosis = by.diagnosis;
    if (by.actors) actors = by.actors;
    if (by.intents) intents = by.intents;
    if (by.llmCalls) llmCalls = by.llmCalls;
    if (by.economics) economics = by.economics;
    if (by.slowTurns) slowTurns = by.slowTurns;
    loading = false;
  }

  // One reload per change of the query, whatever moved it — a chart click, a
  // select, the back button. Reading the serialised query is what makes the
  // URL the single source of truth rather than a copy of it.
  let queryKey = $derived(qs({ offset: qOffset, issue }, { rows: true }));
  let lastKey = '';
  $effect(() => {
    const k = queryKey;
    // An event page reads none of these endpoints. `lastKey` is deliberately
    // NOT advanced here, so a later chat-turn page still loads.
    if (isActivityView) return;
    if (k === lastKey) return;
    lastKey = k;
    loadAll();
  });

  // Scroll to the section named by `sub` once its group is on screen. Guarded
  // by group+section so re-running the effect for any other reason does not
  // yank the page back up while somebody is reading further down.
  let jumped = '';
  $effect(() => {
    const key = sections.join(',') + ':' + sec;
    if (!sec || key === jumped) return;
    jumped = key;
    tick().then(() => document.getElementById('sec-' + sec)?.scrollIntoView({ block: 'start' }));
  });

  // ---------------------------------------------------------------- derived
  // The list lives under a different key per endpoint — `rows`, `bars`,
  // `buckets`, or the bare array on the older ones. Naming all of them here
  // keeps every call site from having to know which.
  const listOf = (st) =>
    Array.isArray(st.data)
      ? st.data
      : (Array.isArray(st.data?.rows) && st.data.rows) ||
        (Array.isArray(st.data?.bars) && st.data.bars) ||
        (Array.isArray(st.data?.buckets) && st.data.buckets) ||
        [];

  let S = $derived(summary.data ?? {});

  /**
   * The movement block for one metric, read out of whichever shape the backend
   * ships it in — `deltas: {turns: {…}}`, or the metric itself promoted to an
   * object with a `.value`.
   *
   * The return value distinguishes three states and the whole point of the chip
   * is that they stay distinct:
   *
   *   undefined  no movement block at all → NO CHIP. We do not know whether it
   *              moved, and an empty space says exactly that.
   *   {delta:null} there is no prior window → "no prior period".
   *   {delta:n}  it moved by n.
   *
   * Collapsing the first two into "0%" is the failure this is built around: a
   * zero there reads as "measured, and unchanged", which is a claim we cannot
   * make about a window that does not exist.
   */
  function deltaOf(src, key) {
    const d = src?.deltas?.[key] ?? (src?.[key] && typeof src[key] === 'object' ? src[key] : null);
    if (!d || typeof d !== 'object') return null;
    if (!('delta' in d) && !('delta_pct' in d) && !('prev' in d)) return null;
    return {
      value: isNum(d.value) ? d.value : null,
      prev: isNum(d.prev) ? d.prev : null,
      delta: isNum(d.delta) ? d.delta : null,
      delta_pct: isNum(d.delta_pct) ? d.delta_pct : null,
      prev_period: d.prev_period ?? null
    };
  }
  const dSummary = (key) => deltaOf(S, key);

  /**
   * The zone the SERVER used, echoed back — never the one we asked for.
   *
   * FastAPI drops an undeclared query parameter and answers 200, so a backend
   * that has not shipped `tz` yet accepts our request and buckets in UTC
   * anyway. Trusting our own request here would put "GMT+6:30" in the header
   * over UTC-cut days, which is the bug with better manners.
   */
  let tzApplied = $derived(
    timeseries.data?.tz ?? costDay.data?.tz ?? summary.data?.tz ?? null
  );

  let tsRows = $derived(Array.isArray(timeseries.data?.rows) ? timeseries.data.rows : []);
  // The bucket key has been spelled `t`, `day` and `bucket_ts` across versions
  // of this endpoint. Reading all three costs nothing; guessing one wrong makes
  // every x-axis label read "—" while the chart itself looks fine.
  const bucketOf = (r) => r?.t ?? r?.day ?? r?.bucket_ts ?? null;
  // Declared by the endpoint. Absent on a backend predating the key, in which
  // case fall back to "did any bucket carry a rating column at all" — never to
  // "is the value zero", which would hide a real day of no ratings.
  let feedbackAvailable = $derived(
    timeseries.data?.feedback_available === true ||
      (timeseries.data?.feedback_available === undefined &&
        tsRows.some((r) => isNum(r.up) || isNum(r.down)))
  );
  let tsLabels = $derived(tsRows.map((r) => dayLabel(bucketOf(r))));
  const col = (key) => tsRows.map((r) => (isNum(r[key]) ? r[key] : null));

  let turns = $derived(isNum(S.turns) ? S.turns : null);
  let fb = $derived(S.feedback ?? {});
  let upRate = $derived.by(() => {
    const up = fb.up;
    const down = fb.down;
    if (!isNum(up) || !isNum(down) || up + down === 0) return null;
    return (up / (up + down)) * 100;
  });
  let ratedN = $derived(isNum(fb.rated) ? fb.rated : isNum(fb.up) && isNum(fb.down) ? fb.up + fb.down : null);

  // The feedback block declares whether it could obey the filters. `chat_feedback`
  // has no turn_id, so lang/embed/path/actor/cached/rated cannot narrow it — and a
  // number sitting under a chip it ignores is the same lie as a dropped param,
  // just further from the wire. The endpoint says so; every card that reads it
  // says so too, in its own footnote, not only in a banner at the top.
  //
  // The `?? ` fallback covers a backend that predates the flag: infer it from the
  // chips instead of assuming the number is clean.
  const FB_BLIND = ['lang', 'embed', 'path', 'actor', 'cached', 'rated'];
  let fbIgnored = $derived(
    Array.isArray(fb.ignored_filters) ? fb.ignored_filters : FB_BLIND.filter((k) => f[k])
  );
  let fbUnfiltered = $derived(
    fb.filters_applied === false || (fb.filters_applied === undefined && fbIgnored.length > 0)
  );
  // Appended to every footnote on a feedback-derived number.
  let fbNote = $derived(fbUnfiltered ? ` · NOT narrowed by ${fbIgnored.join(', ')}` : '');

  // Cache-hit rate per day. A rate, so it is drawn as a plain line: an area
  // fill under a rate reads as a volume.
  let hitRateSeries = $derived(
    tsRows.map((r) => (isNum(r.turns) && r.turns > 0 && isNum(r.cached) ? (r.cached / r.turns) * 100 : null))
  );
  let missSeries = $derived(
    tsRows.map((r) => (isNum(r.turns) && isNum(r.cached) ? Math.max(0, r.turns - r.cached) : null))
  );

  /**
   * Turns per language. `by_lang` is grouped by (lang, cached), so a language
   * appears in up to two rows and summing is required — reading the first row
   * would silently report only the uncached half. `?` is the code for a turn
   * whose language was never recorded and keeps its own slice.
   */
  let langCounts = $derived.by(() => {
    const rows = S.by_lang ?? [];
    if (!rows.length) return null;
    const by = new Map();
    for (const r of rows) if (isNum(r.n)) by.set(r.lang, (by.get(r.lang) ?? 0) + r.n);
    const total = [...by.values()].reduce((a, b) => a + b, 0);
    return { by, total, my: by.get('my') ?? 0, en: by.get('en') ?? 0 };
  });

  /**
   * `/summary` answers `cache_rate: 0.0` over an empty window rather than null
   * — it predates the null-not-zero rule and was deliberately left alone rather
   * than changed under an existing caller. A 0% hit rate over zero turns is not
   * a measured zero: there was nothing to hit. The guard therefore lives here,
   * at the only place that knows the window was empty.
   *
   * Note this is NOT a guard against `cache_rate === 0` in general — a real day
   * of traffic that cached nothing is a genuine zero and must be drawn as one.
   * The condition is the empty window, never the zero value.
   */
  let cacheRateShown = $derived(turns === 0 ? null : S.cache_rate);

  let burmese = $derived.by(() => {
    const rows = S.by_lang ?? [];
    if (!rows.length) return null;
    const my = rows.filter((r) => r.lang === 'my').reduce((a, r) => a + (isNum(r.n) ? r.n : 0), 0);
    const tot = rows.reduce((a, r) => a + (isNum(r.n) ? r.n : 0), 0);
    return tot > 0 ? (my / tot) * 100 : null;
  });

  /**
   * The Burmese share, preferring the endpoint's own figure over the one summed
   * from `by_lang` here.
   *
   * Both are correct, but they are computed over different things the moment
   * either side changes, and two nearly-equal percentages on one screen is the
   * kind of disagreement nobody notices and nobody can then explain. The
   * endpoint's value arrives as a fraction (0..1); `asPct` normalises it, and a
   * measured 0 — every turn in an `lang=EN` window — survives as 0%, because
   * `asPct` tests for a number rather than for truthiness.
   */
  let burmeseShare = $derived.by(() => {
    const v = dSummary('burmese_share')?.value;
    return isNum(v) ? asPct(v) : burmese;
  });

  let pathRows = $derived(
    listOf(paths).map((r) => ({
      key: r.path == null ? 'none' : String(r.path),
      label: r.path == null ? 'not recorded' : String(r.path),
      turns: isNum(r.turns) ? r.turns : null,
      p50: r.p50_ms,
      cached: r.cached
    }))
  );
  let pathSlices = $derived(
    pathRows.map((r) => ({
      key: r.key,
      label: r.label,
      value: r.turns,
      // Four modes, four fills. `fast_path` and `cache` were both C.ok —
      // measured dE2000 0.0, so the two cheapest paths shared a swatch and a
      // reader could not tell which one the console was crediting.
      color:
        r.key === 'fast_path'
          ? C.ok
          : r.key === 'agent'
            ? C.accent
            : r.key === 'cache'
              ? C.s4
              : C.other
    }))
  );
  let notRecordedPath = $derived(pathRows.find((r) => r.key === 'none')?.turns ?? null);

  let embedRows = $derived(listOf(embeds));
  // The endpoint groups by (embed_id, store_id), so one embed occupies as many
  // rows as it has stores and "no embed id" occupies one row per store. A list
  // keyed on embed_id alone therefore has duplicate keys, which is fatal in
  // Svelte 5 (each_key_duplicate) — and it killed the whole console, not just
  // this list. Roll the stores up here: "turns by embed" means one bar per
  // embed, and the row-per-store detail stays in the table below.
  let embedTotals = $derived.by(() => {
    const by = new Map();
    for (const e of embedRows) {
      const id = e.embed_id ?? null;
      const k = id ?? 'none';
      const cur = by.get(k) ?? { key: k, id, turns: null, rated: null, stores: 0 };
      if (isNum(e.turns)) cur.turns = (cur.turns ?? 0) + e.turns;
      if (isNum(e.rated)) cur.rated = (cur.rated ?? 0) + e.rated;
      cur.stores += 1;
      by.set(k, cur);
    }
    return [...by.values()].sort((a, b) => (b.turns ?? 0) - (a.turns ?? 0));
  });
  let namedEmbeds = $derived(embedTotals.filter((e) => e.id));
  let qRows = $derived(questions.data?.rows ?? []);
  let qTotal = $derived(isNum(questions.data?.total) ? questions.data.total : null);

  let toolRows = $derived(listOf(tools));
  let outcomeRows = $derived(listOf(toolOutcomes));
  let outcomeTotals = $derived(toolOutcomes.data?.totals ?? {});

  let llmRows = $derived(listOf(llmUsage));
  // The endpoint returns its own totals. Summing the rows here instead would
  // silently disagree with them the moment a row is filtered or truncated.
  let llmTotals = $derived(llmUsage.data?.totals ?? {});

  let diagRows = $derived(listOf(diagnosis));
  // `counts` and `problem_rate` are over the whole WINDOW, not the page — which
  // is exactly what a KPI above a paginated list needs. Counting diagRows would
  // have made every diagnostics KPI read "50" the moment the queue got busy.
  let diagCounts = $derived(diagnosis.data?.counts ?? {});
  let diagProblemRate = $derived(diagnosis.data?.problem_rate ?? null);

  // `null` (cannot read the event log) is preserved as null — `listOf` would
  // flatten it to [], which reads as "no ingests happened".
  let ingestDays = $derived(Array.isArray(health.data?.by_day) ? health.data.by_day : null);

  let actorRows = $derived(listOf(actors));
  let actorScopeLimited = $derived(actors.data?.scope_limited === true);
  let A = $derived(actors.data ?? {});

  let intentRows = $derived(listOf(intents));
  // The matrix arrives as a flat cell list; the heatmap wants a grid. Missing
  // pairs stay UNDEFINED rather than becoming 0 — an intent that never touched
  // a tool is a blank cell, not a measured zero.
  let intentGrid = $derived.by(() => {
    const m = intents.data?.matrix;
    if (!m || !Array.isArray(m.intents) || !Array.isArray(m.tools) || !m.intents.length) return null;
    const at = new Map((m.cells ?? []).map((c) => [`${c.intent}§${c.tool}`, c.n]));
    return {
      cols: m.tools.map((t) => ({ key: t, label: t })),
      rows: m.intents.map((i) => ({
        key: i,
        label: i,
        cells: m.tools.map((t) => ({ value: at.get(`${i}§${t}`) ?? null }))
      }))
    };
  });

  let costModelRows = $derived(Array.isArray(costModel.data?.rows) ? costModel.data.rows : []);
  let costDayRows = $derived(Array.isArray(costDay.data?.rows) ? costDay.data.rows : []);

  // `/economics` is the authority on this flag; `economics.data` is read
  // directly rather than through the `E` alias below, because a $derived that
  // reaches forward to a later declaration is a trap waiting for whoever
  // reorders this block.
  let anyCost = $derived(
    isNum(economics.data?.cost_usd) ||
      isNum(llmTotals.cost_usd) ||
      isNum(S.cost_usd) ||
      costModelRows.some((r) => isNum(r.cost_usd))
  );
  // `/economics` returns `cost_usd: null` — never 0.0 — when nothing in the
  // window was priced, so `isNum` is the right test and an unpriced window
  // falls through to the next source rather than pinning the total at zero.
  let costTotal = $derived(
    isNum(economics.data?.cost_usd)
      ? economics.data.cost_usd
      : isNum(llmTotals.cost_usd)
        ? llmTotals.cost_usd
        : isNum(S.cost_usd)
          ? S.cost_usd
          : null
  );
  let costEstimated = $derived(
    economics.data?.cost_is_estimated === true ||
      llmTotals.cost_is_estimated === true ||
      llmRows.some((r) => r.cost_is_estimated)
  );

  // How much of the spend is actually priced. A total over 3 of 400 calls is a
  // different claim from a total over all of them, and the card has to say which.
  let costCoverage = $derived.by(() => {
    if (!llmRows.length) return null;
    let priced = 0;
    let calls = 0;
    for (const r of llmRows) {
      if (isNum(r.priced_calls)) priced += r.priced_calls;
      if (isNum(r.calls)) calls += r.calls;
    }
    return calls > 0 ? { priced, calls } : null;
  });

  let tokensTotal = $derived.by(() => {
    const p = llmTotals.prompt_tokens;
    const c = llmTotals.completion_tokens;
    if (isNum(p) || isNum(c)) return (isNum(p) ? p : 0) + (isNum(c) ? c : 0);
    return isNum(S.tokens?.total) ? S.tokens.total : null;
  });

  let cacheTokens = $derived(
    isNum(llmTotals.cache_read_tokens) || isNum(llmTotals.cache_creation_tokens)
      ? {
          read: isNum(llmTotals.cache_read_tokens) ? llmTotals.cache_read_tokens : null,
          made: isNum(llmTotals.cache_creation_tokens) ? llmTotals.cache_creation_tokens : null
        }
      : null
  );

  // ------------------------------------------------------------- economics
  //
  // Each figure may arrive bare or wrapped with its denominator. §3 says a rate
  // ships with the number it is over, so the wrapped form is preferred and the
  // bare one is accepted rather than refused — but a missing denominator is
  // reported as unknown, never as "over everything".
  //
  // Two spellings on purpose: a magnitude is `{value, n, denominator}` and a
  // proportion is `{rate, n}`. Reading only `.value` would silently return null
  // for every share on the tab — a rate would render as "—" while the endpoint
  // was answering perfectly well.
  //
  // `denominator` is a human STRING naming what `n` counts ("prompt tokens"),
  // not a number. Reading it as one would print "NaN" beside a correct figure.
  const eNum = (v) => (isNum(v) ? v : isNum(v?.value) ? v.value : isNum(v?.rate) ? v.rate : null);
  const eDen = (v) => (isNum(v?.n) ? v.n : null);
  const eDenLabel = (v) => (typeof v?.denominator === 'string' ? v.denominator : null);
  let E = $derived(economics.data ?? {});

  let callRows = $derived(listOf(llmCalls));
  let callsTotal = $derived(isNum(llmCalls.data?.total) ? llmCalls.data.total : null);
  // The endpoint's own count first; the loaded page only as a floor. `0` here
  // would be a claim that no model ran, which is not what "we loaded no rows"
  // means.
  let modelCallCount = $derived(
    isNum(llmTotals.calls) ? llmTotals.calls : isNum(callsTotal) ? callsTotal : callRows.length || null
  );

  const sumBy = (rows, key) => {
    let t = null;
    for (const r of rows) if (isNum(r[key])) t = (t ?? 0) + r[key];
    return t;
  };

  /**
   * Token totals, preferring the per-model endpoint's own totals and falling
   * back to the per-call rows. Never summed from both — two numbers that
   * disagree by a truncated page is worse than one number with a caveat.
   */
  let tok = $derived.by(() => {
    // `/economics` reports the window's token counts directly; prefer them over
    // the per-model totals, and both over a sum of the loaded page — a page is
    // a slice, and a slice summed looks exactly like a total.
    const t = E.tokens ?? llmTotals;
    const ALIAS = { cache_read_tokens: 'cache_read', prompt_tokens: 'prompt', completion_tokens: 'completion', reasoning_tokens: 'reasoning' };
    const from = (k) => {
      const alias = ALIAS[k];
      if (alias && isNum(t?.[alias])) return t[alias];
      return isNum(t?.[k]) ? t[k] : sumBy(callRows, k);
    };
    const prompt = from('prompt_tokens');
    const cacheRead = from('cache_read_tokens');
    const completion = from('completion_tokens');
    const reasoning = from('reasoning_tokens');
    return {
      prompt,
      cacheRead,
      completion,
      reasoning,
      // Prompt tokens INCLUDE the cached ones; the donut must not double-count.
      promptUncached: isNum(prompt) && isNum(cacheRead) ? Math.max(0, prompt - cacheRead) : prompt,
      billed: isNum(t?.total)
        ? t.total
        : isNum(prompt) || isNum(completion) || isNum(reasoning)
          ? (isNum(prompt) ? prompt : 0) + (isNum(completion) ? completion : 0) + (isNum(reasoning) ? reasoning : 0)
          : null
    };
  });

  let cacheReadShare = $derived(
    eNum(E.cache_read_share) ??
      (isNum(tok.cacheRead) && isNum(tok.prompt) && tok.prompt > 0 ? (tok.cacheRead / tok.prompt) * 100 : null)
  );
  let blendedPerM = $derived(
    (isNum(eNum(E.blended_per_1m_usd)) ? `$${eNum(E.blended_per_1m_usd).toFixed(2)}` : null) ??
      perMillion(costTotal, tok.billed)
  );
  let costPerTurn = $derived(
    eNum(E.cost_per_turn_usd) ?? (isNum(costTotal) && isNum(turns) && turns > 0 ? costTotal / turns : null)
  );
  let promptToCompletion = $derived(
    (isNum(eNum(E.prompt_completion_ratio)) ? eNum(E.prompt_completion_ratio).toFixed(1) : null) ??
      ratio(tok.prompt, tok.completion)
  );
  let completionShare = $derived(
    eNum(E.completion_share) ??
      (isNum(tok.completion) && isNum(tok.billed) && tok.billed > 0 ? (tok.completion / tok.billed) * 100 : null)
  );

  // `/economics` is the authority on whether a price was derived or reported,
  // and it counts HOW MANY calls were derived — so "estimated" cannot mean one
  // rounding on a large bill and the whole bill in the same badge.
  let econEstimated = $derived(E.cost_is_estimated === true);
  let estimatedCalls = $derived(isNum(E.estimated_calls) ? E.estimated_calls : null);

  // --------------------------------------------------------- ingest funnel
  //
  // `/data-health` reports current state and a per-day row count; it does not
  // yet report how many files arrived and how many stopped at each stage. The
  // funnel is wired to the shape it will take and renders every stage as `—`
  // until it lands — an em-dash is a true statement about a stage nobody has
  // counted, and the mockup's numbers hardcoded here would not be.
  // True while the filter bar is showing chips that the data-health tab cannot
  // honour — it reads the catalog and the ingest log, not the turn log.
  let healthIgnoring = $derived(FILTER_KEYS.some((k) => f[k]) || !!day);

  let ingestFunnel = $derived(health.data?.funnel ?? health.data?.ingest_funnel ?? null);
  const fStage = (k) => (isNum(ingestFunnel?.[k]) ? ingestFunnel[k] : null);

  /**
   * `funnel_meta`, and the two things about it that change what may be drawn.
   *
   * 1. `set_aside` is a TERMINAL stage, not a narrowing one — it is where the
   *    failed runs ended up, so it can legitimately exceed `loaded`. Drawing it
   *    as the last bar of a descending funnel would assert a containment that
   *    does not hold, and a bar wider than the one above it in a funnel reads as
   *    a rendering fault rather than as a different kind of quantity. It gets
   *    its own outcome card instead.
   * 2. The unit is a pipeline RUN, not a filename. A file that failed on Monday
   *    and loaded on Tuesday is two attempts, and collapsing it to one success
   *    erases exactly the retry somebody opened this page to find. The copy says
   *    "runs" for that reason; `funnel_meta.files` holds the by-filename reading.
   */
  let funnelMeta = $derived(health.data?.funnel_meta ?? null);
  let funnelTerminal = $derived(
    Array.isArray(funnelMeta?.terminal) ? funnelMeta.terminal : ['set_aside']
  );
  let funnelUnit = $derived(funnelMeta?.unit === 'run' ? 'runs' : (funnelMeta?.unit ?? 'runs'));
  let funnelDrops = $derived(funnelMeta?.drops ?? null);
  // The narrowing stages only — whatever the endpoint declares terminal is
  // excluded rather than assumed to be `set_aside`.
  let funnelStages = $derived(
    ['arrived', 'detected', 'checked', 'loaded'].filter((k) => !funnelTerminal.includes(k))
  );

  // ------------------------------------------------------------ slow tail
  //
  // Ordered here, over a wider window than the log table pages through, because
  // `/questions` declares no `sort`. A turn with no recorded latency is dropped
  // rather than sorted as zero — it would take the fastest seat in a table
  // about the slowest turns.
  let slowWindow = $derived(Array.isArray(slowTurns.data?.rows) ? slowTurns.data.rows : []);
  let slowRows = $derived(
    [...slowWindow]
      .filter((r) => isNum(r.latency_ms))
      .sort((a, b) => b.latency_ms - a.latency_ms)
      .slice(0, 15)
  );

  /**
   * Per-turn model-call totals, folded up from the per-call rows.
   *
   * `chat_logs` carries no token or cost column, so this is the only place a
   * turn's spend exists — and only for turns whose calls are inside the loaded
   * window. A turn outside it renders `—`: unknown, not free.
   */
  let perTurnCalls = $derived.by(() => {
    const m = new Map();
    for (const c of callRows) {
      const id = c.turn_id;
      if (id == null) continue;
      const cur = m.get(id) ?? { calls: 0, tokens: null, cost: null, cacheRead: null };
      cur.calls += 1;
      for (const [k, f] of [
        ['tokens', (r) => (isNum(r.prompt_tokens) ? r.prompt_tokens : 0) + (isNum(r.completion_tokens) ? r.completion_tokens : 0)],
        ['cost', (r) => r.cost_usd],
        ['cacheRead', (r) => r.cache_read_tokens]
      ]) {
        const v = f(c);
        if (isNum(v)) cur[k] = (cur[k] ?? 0) + v;
      }
      m.set(id, cur);
    }
    return m;
  });
  // Highlight threshold for the per-call table: the 90th percentile of the
  // costs actually present, not a hardcoded dollar figure that would light up
  // every row on one deployment and none on another.
  let callCostP90 = $derived.by(() => {
    const v = callRows.map((r) => r.cost_usd).filter(isNum).sort((a, b) => a - b);
    if (v.length < 5) return null;
    return v[Math.min(v.length - 1, Math.floor(v.length * 0.9))];
  });

  /**
   * The widest cost spread between two calls INSIDE one turn.
   *
   * This is the finding the per-call table exists to make visible, so the page
   * states it rather than leaving the reader to spot two rows sharing a turn
   * id. Computed, never hardcoded: if the widest spread in the loaded window is
   * unremarkable, the sentence does not appear at all.
   */
  let callSpread = $derived.by(() => {
    const byTurn = new Map();
    for (const c of callRows) {
      if (c.turn_id == null || !isNum(c.cost_usd)) continue;
      const list = byTurn.get(c.turn_id) ?? [];
      list.push(c);
      byTurn.set(c.turn_id, list);
    }
    let best = null;
    for (const [turn, list] of byTurn) {
      if (list.length < 2) continue;
      const sorted = [...list].sort((a, b) => a.cost_usd - b.cost_usd);
      const cheap = sorted[0];
      const dear = sorted[sorted.length - 1];
      // A cheapest call of exactly $0 makes the ratio infinite, which is not a
      // number anyone can act on. Report the pair only when a factor exists.
      if (!(cheap.cost_usd > 0)) continue;
      const factor = dear.cost_usd / cheap.cost_usd;
      if (factor < 2) continue;
      if (!best || factor > best.factor) best = { turn, cheap, dear, factor: Math.round(factor) };
    }
    return best;
  });

  const turnCost = (id) => perTurnCalls.get(id)?.cost ?? null;
  const turnTokens = (id) => perTurnCalls.get(id)?.tokens ?? null;
  const turnCallCount = (id) => (perTurnCalls.has(id) ? perTurnCalls.get(id).calls : null);

  // Mean model calls per turn — the number that makes an agent loop visible.
  let callsPerTurn = $derived.by(() => {
    const calls = isNum(llmTotals.calls) ? llmTotals.calls : callRows.length || null;
    const t = perTurnCalls.size || null;
    return isNum(calls) && t ? { value: calls / t, calls, turns: t } : null;
  });

  // Tool durations. `avg_ms` is null (not 0) when nothing in the group timed a
  // call, so an untimed tool is absent from the chart rather than sitting at
  // the fast end of it.
  let toolDurations = $derived(
    outcomeRows
      .map((r) => ({ key: String(r.name), label: String(r.name), value: r.avg_ms }))
      .filter((r) => isNum(r.value))
      .sort((a, b) => b.value - a.value)
      .map((r) => ({ ...r, valueLabel: ms(r.value), tone: r.value > 1000 ? 'warn' : 'ok' }))
  );

  // Filter dropdown options come from the data itself, so a filter can never
  // offer a value that no turn carries.
  let options = $derived({
    stores: (S.by_store ?? []).map((s) => s.store_id).filter(Boolean).sort(),
    langs: [...new Set((S.by_lang ?? []).map((l) => l.lang).filter((l) => l && l !== '?'))].sort(),
    paths: pathRows.filter((r) => r.key !== 'none').map((r) => r.key),
    embeds: [
      ...[...new Set(embedRows.map((e) => e.embed_id).filter(Boolean))].sort(),
      ...(EMBED_NONE_SUPPORTED ? [{ value: 'none', label: 'unattributed' }] : [])
    ],
    models: [...new Set([...llmRows.map((r) => r.model), ...costModelRows.map((r) => r.key)].filter(Boolean))].sort(),
    actors: [...new Set(actorRows.map((r) => r.actor ?? r.actor_email).filter(Boolean))].sort()
  });

  // ------------------------------------------------------- drill-through
  function pickDay(i) {
    const t = bucketOf(tsRows[i]);
    if (!t) return;
    crossTo('questions', (p) => p.set('day', isoDay(t)));
  }
  function openTurn(id) {
    if (id == null) return;
    setParam('turn', String(id));
  }
  function openTrace(id) {
    crossTo('diagnostics', (p) => {
      p.set('trace', String(id));
      p.delete('turn');
    });
  }
  /**
   * Drill from a chart into the rows behind it.
   *
   * The third argument names the SECTION the reader lands in, and the page that
   * draws it is resolved from one map — see crossTo. It used to name a tab id
   * that no longer existed after the sections were grouped, so every one of
   * these landed on Overview with the filter applied: the numbers changed, so
   * it read as working.
   */
  function drillTo(key, value, toSection = 'questions') {
    crossTo(toSection, (p) => {
      if (value) p.set(key, String(value));
      else p.delete(key);
    });
  }
</script>

<PageHeader {title} {subtitle} {level}>
  {#snippet actions()}
    <!-- The chip reports the zone the ACTIVE half's endpoints echoed back. The
         two halves are bucketed by different endpoints and either one may be
         running a backend that has not declared `tz`, so a single chip drawn
         from one of them would vouch for the other. -->
    <TzChip zone={isActivityView ? ATZ : TZ} applied={isActivityView ? actTz : tzApplied} />
    <button
      onclick={() => (isActivityView ? (actNonce += 1) : loadAll())}
      class="inline-flex min-h-[38px] cursor-pointer items-center gap-2 rounded-panel border border-line px-3 text-body-sm font-medium text-ink hover:bg-surface-2"
    >
      <RefreshCw size="15" class={loading && !isActivityView ? 'animate-spin' : ''} /> Refresh
    </button>
  {/snippet}
</PageHeader>


{#if isActivityView}
  <ActivityFilters
    f={af}
    setParams={actSet}
    onclear={actClear}
    bind:actorInput
    bind:qInput
    ontext={debouncedText}
    locked={lockSource}
  />
{:else}
  <FilterBar
    {f}
    {options}
    {range}
    dayFilter={day ? dayLabel(day) : null}
    onclearday={() => setParam('day', '')}
    onrange={(r) => {
      qOffset = 0;
      setParam('range', r);
    }}
    onset={setF}
    onclear={clearFilters}
  />
{/if}

<!--
  Within-group navigation.

  A group of one draws nothing here — the tab label already named it. A group
  of several draws one chip per section, which jumps AND writes `sub` to the
  URL, so the position inside the group is part of the link. That is what makes
  the /activity redirect able to land on Audit rather than at the top of the
  group that now contains Audit.
-->
{#if sections.length > 1}
  <nav aria-label="Sections on {title}" class="mb-5 flex flex-wrap items-center gap-1.5">
    {#each sections as sid (sid)}
      {@const Icon = SECTION[sid].icon}
      <button
        onclick={() => jumpTo(sid)}
        class="flex min-h-[32px] cursor-pointer items-center gap-1.5 rounded-full border border-line bg-surface px-3 text-meta font-medium text-ink-2 transition-colors hover:border-accent hover:text-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
      >
        <Icon size={13} class="text-ink-3" />
        {SECTION[sid].label}
      </button>
    {/each}
  </nav>
{/if}

{#snippet kpiRow(children)}
  <div class="mt-5 grid grid-cols-[repeat(auto-fit,minmax(172px,1fr))] gap-3">{@render children()}</div>
{/snippet}

<!--
  One section's heading and scroll anchor.

  A group of one gets the anchor and NO heading: the tab is already labelled
  "Overview", and repeating it immediately below is furniture. A group of
  several gets a real heading with a rule above it, because otherwise four
  stacked panels read as one very long panel and the reader cannot tell where
  Questions stops and Users begins.
-->
{#snippet secHead(id)}
  {@const Icon = SECTION[id].icon}
  {@const first = sections[0] === id}
  {#if sections.length > 1}
    <div
      id="sec-{id}"
      class="flex scroll-mt-28 items-center gap-2 {first ? 'mt-6' : 'mt-12 border-t border-line pt-9'}"
    >
      <Icon size={16} class="text-ink-3" />
      <h2 class="text-body font-bold tracking-[-0.01em] text-ink">{SECTION[id].label}</h2>
    </div>
  {:else}
    <div id="sec-{id}" class="scroll-mt-28"></div>
  {/if}
{/snippet}

<!--
  A boundary, not decoration. A single thrown error inside any panel used to
  unmount the whole page: the tab strip stopped responding, the underline froze
  on whatever tab you were on, and every other tab went blank until a reload —
  which reads as "all tabs are broken" rather than "one list is broken".
  One GROUP may now fail on its own — and a group is up to four sections since
  the merge, so the blast radius grew. It is deliberately not one boundary per
  section: a section's own fetch failures are already handled by `Section`
  (which renders "not served by this backend" in place), so what is left for a
  boundary to catch is a render bug, and a render bug in one chart of a group is
  worth showing rather than hiding behind three that still drew.
-->
{#key sections.join(',')}
  <!--
    Keyed on the section list: a boundary that has failed STAYS failed until it
    is reset, and without the key that turned "one broken panel" into "every
    page is dead until you press Try again" — the exact symptom the boundary
    was added to stop. Navigating mounts a fresh boundary instead.
  -->
  <svelte:boundary onerror={(e) => console.error("[analytics] panel failed:", e)}>
<div id="analytics-panel" tabindex="-1">
  <!-- ================================================== OVERVIEW -->
  {#if has('overview')}
    {@render secHead('overview')}
    {#snippet overviewKpis()}
      <!-- `good` is set per metric, never inferred from the sign. Latency going
           up is red; a cache hit rate going up is green; both are "↑". -->
      <Kpi
        label="Questions answered"
        value={turns == null ? null : int(turns)}
        spark={col('turns')}
        delta={dSummary('turns')}
        good="up"
        foot="{tsRows.length} days of history in this range"
        onclick={() => crossTo('questions')}
      />
      <Kpi
        label="Answered from cache"
        value={cacheRateShown == null ? null : pct(cacheRateShown)}
        tone="ok"
        spark={hitRateSeries}
        delta={dSummary('cache_rate')}
        good="up"
        deltaFmt={signedPctOf(dSummary('cache_rate'))}
        foot="target >30% is healthy · {int(S.cache_hits)} of {int(turns)} turns"
        onclick={() => crossTo('cache')}
      />
      <Kpi
        label="Median answer time"
        value={secs(S.p50_ms)}
        unit="s"
        tone="info"
        spark={col('p50_ms')}
        delta={dSummary('p50_ms')}
        good="down"
        deltaFmt={signedSecs}
        foot="target <3s · p95 is {isNum(S.p95_ms) ? ms(S.p95_ms) : 'the one that hurts'}"
        onclick={() => crossTo('performance')}
      />
      <Kpi
        label="Thumbs up rate"
        value={upRate == null ? null : pct(upRate)}
        tone="ok"
        delta={dSummary('up_rate')}
        good="up"
        deltaFmt={signedPctOf(dSummary('up_rate'))}
        unfiltered={fbUnfiltered && `Ratings cannot be narrowed by ${fbIgnored.join(', ')}.`}
        foot={ratedN
          ? `${int(fb.up)} up · ${int(fb.down)} down · only ${int(ratedN)} of ${int(turns)} turns rated${fbNote}`
          : 'no turn has been rated yet'}
        onclick={() => crossTo('quality')}
      />
      <Kpi
        label="Active users"
        value={isNum(A.dau) ? int(A.dau) : null}
        delta={deltaOf(A, 'dau')}
        good="up"
        foot={isNum(A.dau) ? 'distinct actors who asked today' : 'no actor is recorded on a turn — see Users & sessions'}
        onclick={() => crossTo('users')}
      />
      <Kpi
        label="Spend"
        value={anyCost ? usd(costTotal) : null}
        estimated={costEstimated}
        spark={col('cost_usd')}
        delta={dSummary('cost_usd')}
        good="none"
        deltaFmt={signedUsd}
        foot={anyCost
          ? `${costEstimated ? 'estimated' : 'measured, not estimated'} · ${int(modelCallCount)} model calls`
          : 'no model has a configured price — not configured'}
        onclick={() => crossTo('cost')}
      />
    {/snippet}
    {@render kpiRow(overviewKpis)}

    <Section
      title="Question volume"
      hint="Click any point to open that day's turns."
      state={timeseries}
      retry={loadAll}
      what="the timeseries"
    >
      {#snippet children()}
        <LineChart
          labels={tsLabels}
          series={[
            { key: 'turns', label: 'Questions', color: C.accent, values: col('turns'), area: true },
            { key: 'cached', label: 'From cache', color: C.ok, values: col('cached'), area: true }
          ]}
          onpick={pickDay}
          pickLabel={(i) => `Open the turns from ${tsLabels[i]}`}
        />
      {/snippet}
    </Section>

    <Section
      title="Where answers come from"
      hint="fast_path skips the LLM entirely. Click a slice to filter every chart."
      state={paths}
      retry={loadAll}
      what="the path breakdown"
    >
      {#snippet children()}
        <Donut slices={pathSlices} onpick={(s) => drillTo('path', s.key, 'performance')} />
        {#if isNum(notRecordedPath) && notRecordedPath > 0}
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            {int(notRecordedPath)} of {int(turns)} turns predate path instrumentation. They render as
            <b>not recorded</b> and are never folded into a bucket that would flatter the chart.
          </p>
        {/if}
      {/snippet}
    </Section>

    <Section
      title="Busiest stores"
      hint="Ranked, because these are categories. Click a store to scope every tab to it."
      state={summary}
      retry={loadAll}
      what="turns by store"
    >
      {#snippet children()}
        <RankBars
          rows={(S.by_store ?? []).map((r) => ({
            key: r.store_id ?? 'none',
            label: r.store_id ?? 'no store on the turn',
            value: isNum(r.n) ? r.n : null,
            tone: r.store_id ? 'accent' : 'muted'
          }))}
          onpick={(r) => (r.key === 'none' ? null : drillTo('store', r.key, 'questions'))}
          empty="No turn in this range carries a store."
        />
        {#if (S.by_store ?? []).some((r) => !r.store_id)}
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            Turns with no store are console tests — the widget always sends one. They keep their own
            bar and are not clickable: the filter matches a store id, so scoping to "no store" would
            return an empty table under a chip claiming otherwise.
          </p>
        {/if}
      {/snippet}
    </Section>

    <!--
      Health at a glance. Every tile is a real measurement or an em-dash; a tile
      whose number is unknown is drawn muted and says what it needs, rather than
      being dropped — a missing tile reads as a check that passed.
    -->
    <Section title="Health at a glance" hint="Each tile links to the tab that explains it.">
      {#snippet children()}
        {@const cacheP = asPct(S.cache_rate)}
        {@const failed = isNum(outcomeTotals.failed) ? outcomeTotals.failed : null}
        {@const tiles = [
          {
            key: 'cache',
            head: cacheP == null ? 'Cache —' : `Cache ${pct(S.cache_rate, 1)}`,
            body:
              cacheP == null
                ? 'no cache flag recorded on a turn in this range'
                : cacheP < 30
                  ? `below the 30% target · ${int(S.cache_hits)} of ${int(turns)} turns`
                  : `at or above the 30% target · ${int(S.cache_hits)} of ${int(turns)} turns`,
            tone: cacheP == null ? 'muted' : cacheP < 30 ? 'bad' : 'ok',
            section: 'cache'
          },
          {
            key: 'p95',
            head: isNum(S.p95_ms) ? `p95 ${ms(S.p95_ms)}` : 'p95 —',
            body: isNum(S.p95_ms) ? 'the tail users complain about · target <10s' : 'no latency recorded in this range',
            tone: !isNum(S.p95_ms) ? 'muted' : S.p95_ms > 10000 ? 'bad' : 'ok',
            section: 'performance'
          },
          {
            key: 'tools',
            head: failed == null ? 'Tool failures —' : `${int(failed)} tool failures`,
            body:
              failed == null
                ? 'outcome is not recorded on these turns — a name is never assumed to mean success'
                : `${int(outcomeTotals.succeeded)} succeeded, ${int(outcomeTotals.refused)} deliberate refusals`,
            tone: failed == null ? 'muted' : failed > 0 ? 'bad' : 'ok',
            section: 'diagnostics'
          },
          {
            key: 'ingest',
            head: fStage('set_aside') == null ? 'Set aside —' : `${int(fStage('set_aside'))} runs set aside`,
            body:
              fStage('set_aside') == null
                ? 'the ingest event log could not be read'
                : `of ${int(fStage('arrived'))} ingest runs · ${share(fStage('set_aside'), fStage('arrived'))} ended there`,
            tone: fStage('set_aside') == null ? 'muted' : 'warn',
            section: 'health'
          },
          {
            key: 'blended',
            head: blendedPerM ? `${blendedPerM} / 1M tokens` : 'Blended rate —',
            body: blendedPerM
              ? `blended over ${int(tok.billed)} billed tokens · ${costEstimated ? 'estimated' : 'measured'}`
              : 'no model has a configured price — not configured',
            tone: blendedPerM ? 'ok' : 'muted',
            section: 'cost'
          },
          {
            key: 'rated',
            head: isNum(ratedN) ? `${int(ratedN)} turns rated` : 'Turns rated —',
            body: isNum(ratedN) && isNum(turns) ? `${share(ratedN, turns)} of turns · the rest are unjudged` : 'nothing rated yet',
            tone: isNum(ratedN) && isNum(turns) && turns > 0 && ratedN / turns < 0.2 ? 'warn' : 'ok',
            section: 'quality'
          }
        ]}
        <div class="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
          {#each tiles as t (t.key)}
            <button
              onclick={() => crossTo(t.section)}
              class="min-h-[64px] cursor-pointer rounded-card border border-l-[3px] bg-surface px-3.5 py-3 text-left hover:bg-accent-soft
                     {t.tone === 'bad'
                ? 'border-line border-l-danger'
                : t.tone === 'warn'
                  ? 'border-line border-l-warning'
                  : t.tone === 'ok'
                    ? 'border-line border-l-success'
                    : 'border-line border-l-line-2'}"
            >
              <b class="block text-body-sm font-bold tnum {t.tone === 'muted' ? 'text-ink-3' : 'text-ink'}">{t.head}</b>
              <span class="mt-0.5 block text-label leading-snug text-ink-3">{t.body}</span>
            </button>
          {/each}
        </div>
      {/snippet}
    </Section>

    {#if !isNum(A.dau) && actorRows.length === 0}
      <GapCard
        tag="NEEDS MIGRATION"
        title="No user identity on a turn yet"
        body="chat_logs carries store_id, session_id and embed_id. Until actor_email is populated, console turns cannot be attributed to the admin who ran them, and adoption metrics (DAU/WAU/MAU, stickiness, retention) cannot be computed at all — so they show an em-dash rather than a flattering zero."
        sql={'ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS actor_email text;\nALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS actor_role  text;'}
      />
    {/if}
  {/if}

  <!-- ================================================== QUESTIONS -->
  {#if has('questions')}
    {@render secHead('questions')}
    {#snippet questionKpis()}
      <Kpi
        label="Turns"
        value={turns == null ? null : int(turns)}
        spark={col('turns')}
        delta={dSummary('turns')}
        good="up"
        foot="every question asked in this range"
      />
      <Kpi
        label="Distinct questions"
        value={isNum(S.distinct) ? int(S.distinct) : null}
        delta={dSummary('distinct')}
        good="none"
        foot={isNum(S.repeat_rate) ? `of ${int(turns)} turns · ${pct(S.repeat_rate)} are repeats` : `of ${int(turns)} turns`}
      />
      <Kpi
        label="Top intent"
        value={intentRows.length ? String(intentRows[0].bucket ?? intentRows[0].name ?? UNKNOWN) : null}
        foot="keyword buckets, not embeddings — cheap and debuggable"
        onclick={INTENT_FILTER_DECLARED
          ? () => intentRows.length && drillTo('intent', intentRows[0].bucket ?? intentRows[0].name)
          : null}
      />
      <Kpi
        label="Unanswered / fallback"
        value={isNum(S.refusals) ? int(S.refusals) : null}
        tone="bad"
        delta={dSummary('refusals')}
        good="down"
        foot="turns that produced no answer text — the queue worth reading first"
      />
      <Kpi
        label="Burmese share"
        value={burmeseShare == null ? null : pct(burmeseShare)}
        tone="info"
        delta={dSummary('burmese_share')}
        good="none"
        deltaFmt={signedPctOf(dSummary('burmese_share'))}
        foot={langCounts ? `${int(langCounts.my)} of ${int(langCounts.total)} turns · EN ${int(langCounts.en)}` : 'lang column, filled on every turn'}
        onclick={() => drillTo('lang', 'my')}
      />
    {/snippet}
    {@render kpiRow(questionKpis)}

    <Section
      title="What people come here for"
      hint={INTENT_FILTER_DECLARED
        ? 'Keyword buckets. Click a bucket to filter the turn list below.'
        : 'Keyword buckets. Filtering by bucket is switched off — see the note below.'}
      state={intents}
      retry={loadAll}
      what="the intent buckets"
    >
      {#snippet children()}
        <RankBars
          rows={intentRows.map((r) => ({
            key: String(r.bucket ?? r.name),
            label: String(r.label ?? r.bucket ?? r.name),
            value: isNum(r.turns) ? r.turns : isNum(r.n) ? r.n : null,
            tone: (r.bucket ?? r.name) === 'other' ? 'muted' : 'accent'
          }))}
          onpick={INTENT_FILTER_DECLARED ? (r) => drillTo('intent', r.key) : null}
          empty="No intent buckets computed for this range."
        />
        {#if !INTENT_FILTER_DECLARED && intentRows.length}
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            These bars are counts only — clicking one is deliberately switched off. No endpoint
            declares an <code class="font-mono text-meta">intent</code> parameter, and an undeclared
            query param is dropped silently: the turn list would come back unfiltered while the chip
            claimed it was scoped to this bucket. It becomes clickable the moment the parameter is
            declared, with no other change to this page.
          </p>
        {/if}
      {/snippet}
    </Section>

    <Section
      title="Language mix"
      hint="The Burmese share matters: the fallback text and the disclaimer differ per language."
      state={summary}
      retry={loadAll}
      what="the language mix"
    >
      {#snippet children()}
        <Donut
          slices={langCounts
            ? [...langCounts.by.entries()].map(([code, n], i) => ({
                key: code,
                label: langName(code),
                value: n,
                color: code === 'en' ? C.accent : code === 'my' ? C.s2 : C.other,
                pickable: code !== '?'
              }))
            : []}
          onpick={(s) => (s.key === '?' ? null : drillTo('lang', s.key))}
        />
        {#if langCounts?.by.has('?')}
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            {int(langCounts.by.get('?'))} turns have no recorded language. They keep their own slice
            rather than being counted as English, and they are not clickable — the filter matches a
            language code, and there is none to match.
          </p>
        {/if}
      {/snippet}
    </Section>

    <Section
      title="Most repeated questions"
      hint="A repeat that misses cache is a caching bug, not user behaviour."
      state={repeats}
      retry={loadAll}
      what="the repeat list"
    >
      {#snippet children()}
        <Table
          cols={[
            { key: 'q', label: 'Question' },
            { key: 'asked', label: 'Asked', align: 'right' },
            { key: 'cached', label: 'Cached', align: 'right' },
            { key: 'median', label: 'Median', align: 'right' }
          ]}
          rows={listOf(repeats)}
          rowKey={(r, i) => `${i}|${r.question ?? ''}`}
          rowClass={(r) => (r.cached === 0 && r.asked > 1 ? 'bg-danger-soft' : '')}
          onpick={(r) => drillTo('q', r.question)}
          empty="No question was asked twice in this range."
        >
          {#snippet row(r)}
            <td class="bilingual">{clip(r.question, 70)}</td>
            <td class="r tnum">{int(r.asked)}</td>
            <td class="r tnum">{int(r.cached)}</td>
            <td class="r tnum">{ms(r.median_ms)}</td>
          {/snippet}
        </Table>
        {#if listOf(repeats).some((r) => r.cached === 0 && r.asked > 1)}
          <p class="mt-2 text-meta leading-relaxed text-ink-3">
            Highlighted rows were asked more than once and cached zero times. That is a caching
            defect, not user behaviour — each one is worth opening.
          </p>
        {/if}
      {/snippet}
    </Section>

    <Section
      title="Every turn"
      hint="The full log. Click any row for the complete turn. This table honours the day drill-down as well as the filter bar."
      state={questions}
      retry={loadAll}
      what="the turn log"
    >
      {#snippet children()}
        <Table
          cols={[
            { key: 'ts', label: 'When' },
            { key: 'q', label: 'Question' },
            { key: 'store', label: 'Store' },
            { key: 'path', label: 'Path' },
            { key: 'lat', label: 'Latency', align: 'right' },
            { key: 'tokens', label: 'Tokens', align: 'right' },
            { key: 'cost', label: 'Cost', align: 'right' },
            { key: 'cached', label: 'Cached', align: 'right' }
          ]}
          rows={qRows}
          onpick={(r) => openTurn(r.id)}
          empty="No turn matches these filters."
        >
          {#snippet row(r)}
            <td class="tnum whitespace-nowrap">{when(r.ts)}</td>
            <td class="bilingual">{clip(r.question, 60)}</td>
            <td>{r.store_id ?? UNKNOWN}</td>
            <td>
              {#if r.path}<Badge tone={r.path === 'fast_path' ? 'ok' : 'info'}>{r.path}</Badge>
              {:else}<Badge tone="neutral">not recorded</Badge>{/if}
            </td>
            <td class="r tnum">{ms(r.latency_ms)}</td>
            <!-- Tokens and cost live on llm_calls, never on the turn. A turn
                 whose calls are outside the loaded call window shows `—`: not
                 measured here, which is not the same as free. -->
            <td class="r tnum">{int(turnTokens(r.id))}</td>
            <td class="r tnum">{usd(turnCost(r.id)) ?? UNKNOWN}</td>
            <td class="r">{r.cached === true ? 'yes' : r.cached === false ? 'no' : UNKNOWN}</td>
          {/snippet}
        </Table>
        {#if llmCalls.status === 'ok' && qRows.some((r) => turnCost(r.id) == null)}
          <p class="mt-2 text-meta leading-relaxed text-ink-3">
            Token and cost columns are folded up from the per-call rows, which load the most recent
            {CALLS_SIZE} model calls in range. A turn older than that window shows an em-dash — the
            calls exist, this page has not fetched them. Open the turn, or the
            <b>Cost &amp; tokens</b> tab, for the per-call detail.
          </p>
        {/if}

        <div class="mt-3 flex items-center gap-3 text-meta text-ink-3">
          <span>
            {qTotal == null ? UNKNOWN : `${qOffset + 1}–${Math.min(qOffset + Q_SIZE, qTotal)} of ${int(qTotal)}`}
          </span>
          <button
            class="ml-auto inline-flex min-h-[36px] cursor-pointer items-center rounded-panel border border-line px-2 disabled:opacity-40"
            disabled={qOffset === 0}
            onclick={() => (qOffset = Math.max(0, qOffset - Q_SIZE))}
            aria-label="Previous page"><ChevronLeft size="16" /></button
          >
          <button
            class="inline-flex min-h-[36px] cursor-pointer items-center rounded-panel border border-line px-2 disabled:opacity-40"
            disabled={qTotal != null && qOffset + Q_SIZE >= qTotal}
            onclick={() => (qOffset += Q_SIZE)}
            aria-label="Next page"><ChevronRight size="16" /></button
          >
        </div>
      {/snippet}
    </Section>
  {/if}

  <!-- ================================================== USERS & SESSIONS -->
  {#if has('users')}
    {@render secHead('users')}
    {#if actors.status === 'missing' || (actorRows.length === 0 && !isNum(A.dau))}
      <WarnBar>
        {#snippet children()}
          Everything on this tab depends on an actor being recorded against a turn
          (<code class="font-mono text-meta">chat_logs.actor_email</code>). Until turns carry one,
          these cards show an em-dash: the metrics are <b>unknown</b>, which is not the same as zero,
          and drawing them as zero would make adoption look like a product problem rather than an
          instrumentation gap.
        {/snippet}
      </WarnBar>
    {/if}

    {#snippet userKpis()}
      <Kpi
        label="Daily active users"
        value={isNum(A.dau) ? int(A.dau) : null}
        spark={A.dau_series ?? null}
        delta={deltaOf(A, 'dau')}
        good="up"
        foot="DAU · distinct actors asking on a day"
      />
      <Kpi
        label="Stickiness (DAU/MAU)"
        value={isNum(A.stickiness) ? pct(A.stickiness) : null}
        delta={deltaOf(A, 'stickiness')}
        good="up"
        deltaFmt={signedPctOf(deltaOf(A, 'stickiness'))}
        foot="target >20% is healthy"
      />
      <Kpi
        label="Repeat users"
        value={isNum(A.repeat_users) ? int(A.repeat_users) : null}
        delta={deltaOf(A, 'repeat_users')}
        good="up"
        foot="asked on more than one day"
      />
      <!-- Console actors is a DIFFERENT population from the three cards to its
           left: it counts who used the console (app_events), not who asked a
           question (chat_logs). It is real today, which is exactly why it must
           not be read as an answer to "how many users does the agent have". -->
      <Kpi
        label="Console actors"
        value={actorRows.length ? int(actorRows.filter((r) => r.actor ?? r.actor_email).length) : null}
        delta={deltaOf(A, 'actors')}
        good="none"
        foot={actorRows.length
          ? 'people who used the CONSOLE — not the number who asked a question'
          : 'no console action recorded in this range'}
      />
    {/snippet}
    {@render kpiRow(userKpis)}

    <Section
      title="Console activity by actor"
      hint="Who used the console, and what they did most. Click a row to scope every chart to that person."
      state={actors}
      retry={loadAll}
      what="actor activity"
    >
      {#snippet children()}
        <Table
          cols={[
            { key: 'actor', label: 'Actor' },
            { key: 'role', label: 'Role' },
            { key: 'turns', label: 'Turns', align: 'right' },
            { key: 'cached', label: 'Cached', align: 'right' },
            { key: 'p50', label: 'p50', align: 'right' },
            { key: 'events', label: 'Console events', align: 'right' },
            { key: 'last', label: 'Last turn', align: 'right' }
          ]}
          rows={actorRows}
          rowKey={(r, i) => `${i}|${r.actor ?? ''}`}
          onpick={(r) => (r.actor ? drillTo('actor', r.actor, 'questions') : null)}
          empty="No actor has been recorded against a turn or a console action in this range."
        >
          {#snippet row(r)}
            <td>{r.actor ?? `${UNKNOWN} no actor recorded`}</td>
            <td>{#if r.role}<Badge tone="info">{r.role}</Badge>{:else}{UNKNOWN}{/if}</td>
            <td class="r tnum">{int(r.turns)}</td>
            <td class="r tnum">{int(r.cached_turns)}</td>
            <td class="r tnum">{ms(r.p50_ms)}</td>
            <td class="r tnum">{int(r.console_events)}</td>
            <td class="r tnum">{when(r.last_turn)}</td>
          {/snippet}
        </Table>
        {#if actorScopeLimited}
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            Console events read <code class="font-mono text-meta">app_events</code>, which has no
            branch column, so they show as {UNKNOWN} for a branch-scoped account rather than handing
            you a figure counting every branch. The turn columns are scoped to your branch and are real.
          </p>
        {/if}
      {/snippet}
    </Section>

    <Section
      title="Retention by cohort"
      hint="Two categorical axes plus a magnitude → grid, colour is intensity."
      state={actors}
      retry={loadAll}
      what="retention"
    >
      {#snippet children()}
        {#if Array.isArray(A.cohorts) && A.cohorts.length}
          <Heatmap
            cols={(A.cohort_weeks ?? ['Week 0', 'W1', 'W2', 'W3']).map((w, i) => ({ key: `w${i}`, label: w }))}
            rows={A.cohorts.map((c) => ({
              key: c.cohort,
              label: c.cohort,
              cells: (c.cells ?? []).map((v) => ({ value: isNum(v) ? v : null }))
            }))}
            onpick={() => {}}
          />
        {:else}
          <Heatmap
            cols={['Week 0', 'W1', 'W2', 'W3'].map((w, i) => ({ key: `w${i}`, label: w }))}
            rows={['Week 0', 'Week 1', 'Week 2', 'Week 3'].map((r, i) => ({
              key: `r${i}`,
              label: r,
              cells: [null, null, null, null].map(() => ({ value: null }))
            }))}
          />
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            Rendered empty on purpose: with no user identity every cell is unknown, and an unknown
            cell must not be drawn as a zero.
          </p>
        {/if}
      {/snippet}
    </Section>

    <Section title="Adoption funnel" hint="Where people are lost. A stage that is not measured shows an em-dash and no bar.">
      {#snippet children()}
        <Funnel
          stages={[
            { key: 'loaded', label: 'Widget loaded', value: isNum(A.widget_loads) ? A.widget_loads : null, note: 'needs a widget-load event' },
            { key: 'typed', label: 'Question typed', value: isNum(A.questions_typed) ? A.questions_typed : null, note: 'needs a visitor id on the widget' },
            { key: 'answered', label: 'Answer shown', value: turns, note: 'the only stage measured today' },
            {
              key: 'rated',
              label: 'Rated',
              value: ratedN,
              note: isNum(ratedN) && isNum(turns) && turns > 0 ? `${share(ratedN, turns)} of answered turns` : 'no rating recorded'
            }
          ]}
          onpick={(s) => s.key === 'rated' && drillTo('rated', 'any')}
        />
      {/snippet}
    </Section>

    {#if actorRows.length === 0}
      <GapCard
        tag="NEEDS MIGRATION"
        title="Who asked what is unknowable today"
        body="Widget visitors are anonymous by design, which is correct for a public pharmacy chat. Console turns need not be. An actor on a console turn is the one change that makes this whole tab computable; a rotating, disclosed visitor id would add the funnel and retention for widget traffic — but that is a disclosure decision, not an engineering one."
        sql={'ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS actor_email text;\nCREATE INDEX IF NOT EXISTS chat_logs_actor_idx ON chat_logs (actor_email, ts DESC);'}
      />
    {/if}
  {/if}

  <!-- ================================================== EMBEDS -->
  {#if has('embeds')}
    {@render secHead('embeds')}
    {#snippet embedKpis()}
      {@const named = namedEmbeds}
      {@const attributed = named.reduce((a, e) => a + (isNum(e.turns) ? e.turns : 0), 0)}
      {@const unattributed = embedTotals.find((e) => !e.id)?.turns ?? 0}
      <Kpi
        label="Embeds seen"
        value={embeds.status === 'ok' ? int(named.length) : null}
        foot={named.length ? named.map((e) => e.id).slice(0, 3).join(' · ') : 'no embed has produced a turn in this range'}
      />
      <Kpi
        label="Turns attributed"
        value={embeds.status === 'ok' ? int(attributed) : null}
        spark={col('turns')}
        delta={deltaOf(embeds.data, 'attributed')}
        good="up"
        foot="of {int(turns)} turns in range · {share(attributed, turns)}"
      />
      <Kpi
        label="Unattributed turns"
        value={embeds.status === 'ok' ? int(unattributed) : null}
        tone="warn"
        foot="kept separate, never assigned to an embed that did not produce them"
        onclick={EMBED_NONE_SUPPORTED ? () => drillTo('embed', 'none') : null}
      />
      <Kpi
        label="Rated turns from embeds"
        value={embeds.status === 'ok' ? int(named.reduce((a, e) => a + (isNum(e.rated) ? e.rated : 0), 0)) : null}
        foot="feedback carries no embed id of its own — this counts turns, matched back"
      />
    {/snippet}
    {@render kpiRow(embedKpis)}

    <Section
      title="Turns by embed"
      hint="Click an embed to scope every chart on every tab to it."
      state={embeds}
      retry={loadAll}
      what="the embed breakdown"
    >
      {#snippet children()}
        <RankBars
          rows={embedTotals.map((e) => ({
            key: e.key,
            label: e.id ?? 'unattributed',
            value: e.turns,
            tone: e.id ? 'accent' : 'muted'
          }))}
          onpick={(r) => (r.key === 'none' && !EMBED_NONE_SUPPORTED ? null : drillTo('embed', r.key, 'embeds'))}
          empty="No turn in this range carries an embed id."
        />
        {#if !EMBED_NONE_SUPPORTED && embedRows.some((e) => !e.embed_id)}
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            The unattributed bar is not clickable. The endpoint matches
            <code class="font-mono text-meta">embed_id</code> exactly, so scoping to it would
            return an empty table rather than the turns it counts — an empty result that looks like
            a measurement is worse than no drill-through.
          </p>
        {/if}
      {/snippet}
    </Section>

    <Section
      title="Per-embed detail"
      hint="Each embed gets the whole dashboard, scoped to itself."
      state={embeds}
      retry={loadAll}
      what="per-embed detail"
    >
      {#snippet children()}
        <Table
          cols={[
            { key: 'embed', label: 'Embed' },
            { key: 'store', label: 'Store' },
            { key: 'turns', label: 'Turns', align: 'right' },
            { key: 'cache', label: 'Cache', align: 'right' },
            { key: 'p50', label: 'p50', align: 'right' },
            { key: 'rated', label: 'Rated', align: 'right' },
            { key: 'seen', label: 'Last seen', align: 'right' }
          ]}
          rows={embedRows}
          rowKey={(e) => `${e.embed_id ?? 'none'}|${e.store_id ?? 'none'}`}
          rowClass={(e) => (e.embed_id ? '' : 'text-ink-3')}
          onpick={(e) => (e.embed_id ? drillTo('embed', e.embed_id, 'embeds') : null)}
          empty="No embed activity in this range."
        >
          {#snippet row(e)}
            <td><b>{e.embed_id ?? 'unattributed'}</b></td>
            <td>{e.store_id ?? UNKNOWN}</td>
            <td class="r tnum">{int(e.turns)}</td>
            <td class="r tnum">{isNum(e.cache_rate) ? pct(e.cache_rate) : UNKNOWN}</td>
            <td class="r tnum">{ms(e.p50_ms)}</td>
            <td class="r tnum">{int(e.rated)}</td>
            <td class="r tnum">{when(e.last_seen)}</td>
          {/snippet}
        </Table>
        {#if embedRows.some((e) => !e.embed_id)}
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            The unattributed row is turns recorded before <code class="font-mono text-meta">embed_id</code>
            existed. It keeps its own row rather than being folded into an embed that did not produce it.
          </p>
        {/if}
      {/snippet}
    </Section>
  {/if}

  <!-- ================================================== PERFORMANCE -->
  {#if has('performance')}
    {@render secHead('performance')}
    {#snippet perfKpis()}
      {@const ttft = llmRows.map((r) => r.p50_ttft_ms).find(isNum) ?? null}
      {@const slowest = [...pathRows].filter((r) => isNum(r.p50)).sort((a, b) => b.p50 - a.p50)[0] ?? null}
      <Kpi
        label="p50"
        value={secs(S.p50_ms)}
        unit="s"
        tone="info"
        spark={col('p50_ms')}
        delta={dSummary('p50_ms')}
        good="down"
        deltaFmt={signedSecs}
        foot={isNum(S.p50_ms) ? (S.p50_ms > 3000 ? 'target <3s — missed' : 'target <3s — met') : 'no latency recorded in this range'}
      />
      <Kpi
        label="p95"
        value={secs(S.p95_ms)}
        unit="s"
        tone="bad"
        spark={col('p95_ms')}
        delta={dSummary('p95_ms')}
        good="down"
        deltaFmt={signedSecs}
        foot="the tail users complain about · target <10s"
      />
      <Kpi
        label="Time to first token"
        value={ttft == null ? null : ms(ttft)}
        delta={dSummary('p50_ttft_ms')}
        good="down"
        deltaFmt={signedSecs}
        foot={ttft == null
          ? 'ttft is recorded per LLM call — none in this range carries one'
          : `median across ${int(modelCallCount)} model calls`}
      />
      <!-- The number that makes an agent loop visible. One question is often
           two or three model calls, each resending a growing conversation, and
           at turn grain that is a single latency and a single cost. -->
      <Kpi
        label="Model calls per turn"
        value={callsPerTurn ? callsPerTurn.value.toFixed(1) : null}
        good="down"
        foot={callsPerTurn
          ? `${int(callsPerTurn.calls)} model calls over ${int(callsPerTurn.turns)} instrumented turns`
          : 'no turn in this range has per-call rows'}
        onclick={() => crossTo('cost')}
      />
      <Kpi
        label="Slowest path"
        value={slowest?.label ?? null}
        tone="bad"
        foot={slowest ? `${ms(slowest.p50)} median` : 'no path has a recorded latency in this range'}
        onclick={() => slowest && drillTo('path', slowest.key, 'performance')}
      />
    {/snippet}
    {@render kpiRow(perfKpis)}

    <Section
      title="Latency over time"
      hint="These are rates, so they are plain lines — an area fill would imply volume. Click a point for that day's turns."
      state={timeseries}
      retry={loadAll}
      what="latency over time"
    >
      {#snippet children()}
        <LineChart
          labels={tsLabels}
          series={[
            { key: 'p50', label: 'p50', color: C.accent, values: col('p50_ms'), area: false },
            { key: 'p95', label: 'p95', color: C.bad, values: col('p95_ms'), area: false }
          ]}
          fmt={(v) => (v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`)}
          onpick={pickDay}
        />
      {/snippet}
    </Section>

    <Section
      title="Where the time goes"
      hint="Median latency by path. fast_path exists to skip the LLM; a cache hit skips everything."
      state={paths}
      retry={loadAll}
      what="latency by path"
    >
      {#snippet children()}
        <RankBars
          rows={[...pathRows]
            .sort((a, b) => (b.p50 ?? 0) - (a.p50 ?? 0))
            .map((r) => ({
              key: r.key,
              label: r.label,
              value: isNum(r.p50) ? r.p50 : null,
              valueLabel: ms(r.p50),
              tone: r.key === 'agent' ? 'bad' : r.key === 'none' ? 'muted' : 'ok'
            }))}
          onpick={(r) => drillTo('path', r.key === 'none' ? '' : r.key, 'questions')}
          empty="No path has a recorded latency in this range."
        />
      {/snippet}
    </Section>

    <Section
      title="Slowest tools"
      hint="Mean duration per tool. A tool nobody timed is absent rather than fast."
      state={toolOutcomes.status === 'missing' ? tools : toolOutcomes}
      retry={loadAll}
      what="tool durations"
    >
      {#snippet children()}
        <RankBars
          rows={toolDurations}
          onpick={(r) => drillTo('tool', r.key)}
          empty="No tool call in this range recorded a duration."
        />
        {#if toolDurations.length && isNum(S.p50_ms)}
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            The slowest tool takes {ms(toolDurations[0].value)} against a {ms(S.p50_ms)} median turn.
            <b>Tools are not the bottleneck — the model is.</b> Optimising a two-millisecond SQL
            lookup cannot move a number dominated by an LLM round trip.
          </p>
        {/if}
      {/snippet}
    </Section>

    <Section
      title="The slow tail, turn by turn"
      hint="The slowest turns, newest {SLOW_WINDOW} in range. Click one to open the turn; the trace shows every call it made."
      state={slowTurns}
      retry={loadAll}
      what="the slow tail"
    >
      {#snippet children()}
        <Table
          cols={[
            { key: 'ts', label: 'When' },
            { key: 'q', label: 'Question' },
            { key: 'path', label: 'Path' },
            { key: 'calls', label: 'Calls', align: 'right' },
            { key: 'tokens', label: 'Tokens', align: 'right' },
            { key: 'lat', label: 'Latency', align: 'right' }
          ]}
          rows={slowRows}
          rowClass={(r) => (isNum(S.p95_ms) && r.latency_ms >= S.p95_ms ? 'bg-danger-soft' : '')}
          onpick={(r) => openTurn(r.id)}
          empty="No turn in this range has a recorded latency."
        >
          {#snippet row(r)}
            <td class="tnum whitespace-nowrap">{when(r.ts)}</td>
            <td class="bilingual">{clip(r.question, 55)}</td>
            <td>
              {#if r.path}<Badge tone={r.path === 'fast_path' ? 'ok' : 'info'}>{r.path}</Badge>
              {:else}<Badge tone="neutral">not recorded</Badge>{/if}
            </td>
            <td class="r tnum">{int(turnCallCount(r.id))}</td>
            <td class="r tnum">{int(turnTokens(r.id))}</td>
            <td class="r tnum font-semibold">{ms(r.latency_ms)}</td>
          {/snippet}
        </Table>
        <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
          <code class="font-mono text-meta">/questions</code> declares no sort parameter, so this
          ranks the most recent {int(slowWindow.length)} turns in range rather than every turn in it.
          Widening the range widens the window. Turns with no recorded latency are left out entirely —
          sorting them as zero would seat them at the top of a table about the slowest turns.
          {#if isNum(S.p95_ms)}Highlighted rows are at or past p95 ({ms(S.p95_ms)}).{/if}
        </p>
      {/snippet}
    </Section>

    <Section
      title="How answers are distributed"
      hint="The latency histogram. The tail is what people remember, so the buckets are named in the units they feel."
      state={summary}
      retry={loadAll}
      what="the latency histogram"
    >
      {#snippet children()}
        {@const b = S.buckets ?? {}}
        <RankBars
          rows={[
            { key: 'lt100', label: 'under 0.1s (cached)', value: b.lt100, tone: 'ok' },
            { key: 'lt5000', label: 'under 5s', value: b.lt5000, tone: 'ok' },
            { key: 'lt10000', label: '5 – 10s', value: b.lt10000, tone: 'warn' },
            { key: 'lt20000', label: '10 – 20s', value: b.lt20000, tone: 'warn' },
            { key: 'gte20000', label: 'over 20s', value: b.gte20000, tone: 'bad' }
          ]}
          empty="No latency recorded in this range."
        />
      {/snippet}
    </Section>
  {/if}

  <!-- ================================================== COST & TOKENS -->
  {#if has('cost')}
    {@render secHead('cost')}
    {#if !anyCost}
      <WarnBar>
        {#snippet children()}
          No turn in this range has a recorded cost. Every unpriced figure below renders as
          <b>not configured</b> rather than <b>0.00</b> — a zero reads as “free”, and that is how a
          dashboard lies quietly for months.
        {/snippet}
      </WarnBar>
    {:else if costEstimated}
      <WarnBar>
        {#snippet children()}
          {#if isNum(estimatedCalls)}<b>{int(estimatedCalls)} of {int(modelCallCount)} calls</b> in this
            range are{:else}Some rows below are{/if} marked <b>estimated</b>: their cost was derived
          from a configured price rather than returned by the provider. A derived cost must never be
          presented as a measured one, so it carries the flag wherever it appears — and the count is
          shown, because “estimated” over one rounding and over the whole bill must not look alike.
        {/snippet}
      </WarnBar>
    {:else}
      <WarnBar tone="ok">
        {#snippet children()}
          These prices are <b>real</b>: <code class="font-mono text-meta">cost_is_estimated = false</code>
          on every row in range. Where a model has no configured price the figure renders
          <b>not configured</b>, never <b>$0.00</b> — a zero reads as free, and that is how a
          dashboard lies quietly for months.
        {/snippet}
      </WarnBar>
    {/if}

    {#snippet costKpis()}
      <Kpi
        label="Spend"
        value={anyCost ? usd(costTotal) : null}
        estimated={costEstimated}
        spark={col('cost_usd')}
        delta={dSummary('cost_usd')}
        good="none"
        deltaFmt={signedUsd}
        foot={anyCost
          ? costEstimated
            ? 'derived from a configured price — not returned by the provider'
            : 'measured — not an estimate'
          : 'cost_usd is null on every turn in range'}
      />
      <Kpi
        label="Blended"
        value={blendedPerM}
        unit="/1M"
        tone="info"
        delta={deltaOf(E, 'blended_per_1m_usd')}
        good="down"
        deltaFmt={signedUsd}
        foot={blendedPerM
          ? `over ${int(eDen(E.blended_per_1m_usd) ?? tok.billed)} billed tokens`
          : 'needs both a spend and a token count'}
      />
      <Kpi
        label="Cost per turn"
        value={costPerTurn == null ? null : usd(costPerTurn)}
        estimated={costEstimated}
        delta={deltaOf(E, 'cost_per_turn_usd')}
        good="down"
        deltaFmt={signedUsd}
        foot={costPerTurn == null
          ? 'needs a price configured per model'
          : costCoverage
            ? `over ${int(eDen(E.cost_per_turn_usd) ?? turns)} turns · priced on ${int(costCoverage.priced)} of ${int(costCoverage.calls)} calls`
            : `over ${int(eDen(E.cost_per_turn_usd) ?? turns)} turns`}
      />
      <!-- The largest lever available, which is why it is a headline and not a
           column in a table. -->
      <Kpi
        label="Cache-read share"
        value={cacheReadShare == null ? null : pct(cacheReadShare, 1)}
        tone="ok"
        delta={deltaOf(E, 'cache_read_share')}
        good="up"
        deltaFmt={signedPctOf(deltaOf(E, 'cache_read_share'))}
        foot={cacheReadShare == null
          ? 'no call in range recorded a cache split — and it cannot be backfilled'
          : `${int(tok.cacheRead)} of ${int(eDen(E.cache_read_share) ?? tok.prompt)} ${eDenLabel(E.cache_read_share) ?? 'prompt tokens'} read from cache`}
      />
      <Kpi
        label="Prompt : completion"
        value={promptToCompletion}
        unit=":1"
        delta={deltaOf(E, 'prompt_completion_ratio')}
        good="down"
        foot={promptToCompletion
          ? 'you are paying for context, not for output'
          : 'needs both a prompt and a completion token count'}
      />
    {/snippet}
    {@render kpiRow(costKpis)}

    {#if isNum(completionShare)}
      <p class="mt-3 text-meta leading-relaxed text-ink-3">
        <b>Completion is {pct(completionShare, 1)} of tokens.</b> Shortening answers saves close to
        nothing here — the money is in the prompt. The two levers that move this bill are trimming
        what gets resent on every call and raising the share read from cache
        {#if cacheReadShare != null}(currently {pct(cacheReadShare, 1)}){/if}.
      </p>
    {/if}

    <Section
      title="Spend by model"
      hint="A bar, not a line — this is a comparison, not a trend. Click a model to filter every chart."
      state={llmUsage.status === 'missing' ? costModel : llmUsage}
      retry={loadAll}
      what="per-model usage"
    >
      {#snippet children()}
        {@const rows = llmRows.length ? llmRows : costModelRows.map((r) => ({ model: r.key, calls: r.turns, cost_usd: r.cost_usd, prompt_tokens: r.input_tokens, completion_tokens: r.output_tokens }))}
        <RankBars
          rows={rows.map((r) => ({
            key: String(r.model),
            label: String(r.model),
            sub: isNum(r.calls) ? `${int(r.calls)} calls` : undefined,
            value: isNum(r.cost_usd) ? r.cost_usd : isNum(r.calls) ? r.calls : null,
            valueLabel: isNum(r.cost_usd) ? usd(r.cost_usd) : isNum(r.calls) ? `${int(r.calls)} calls` : UNKNOWN,
            tone: isNum(r.cost_usd) ? 'accent' : 'muted'
          }))}
          onpick={(r) => drillTo('model', r.key, 'cost')}
          empty="No model has recorded a call in this range."
        />
        {#if rows.length && !rows.some((r) => isNum(r.cost_usd))}
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            No price is configured for any model, so the bars rank by call volume and no USD figure is
            shown at all. When a price is <i>inferred</i> rather than known, the row carries an
            <b>estimated</b> flag — a derived cost must never be presented as a measured one.
          </p>
        {/if}
      {/snippet}
    </Section>

    <Section
      title="Token breakdown"
      hint="Where the tokens actually go. Cache reads are carved OUT of the prompt total, never added to it."
      state={llmUsage}
      retry={loadAll}
      what="the token breakdown"
    >
      {#snippet children()}
        <Donut
          slices={[
            { key: 'prompt', label: 'Prompt (uncached)', value: tok.promptUncached, color: C.accent },
            { key: 'cache', label: 'Cache read', value: tok.cacheRead, color: C.ok },
            { key: 'completion', label: 'Completion', value: tok.completion, color: C.s2 },
            { key: 'reasoning', label: 'Reasoning', value: tok.reasoning, color: C.warn }
          ]}
        />
        <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
          <b>Prompt tokens already include the cached ones</b>, so the two slices are prompt-minus-cache
          and cache, not prompt and cache. Adding them would inflate the total by the size of the
          lever this chart exists to show.
          {#if tok.reasoning == null}Reasoning tokens are not reported by this provider, so that slice
            is absent rather than drawn as zero.{/if}
        </p>
      {/snippet}
    </Section>

    <Section
      title="Cost drivers"
      hint="Ranked by contribution to the bill. These are token counts, not dollars — one price per model makes them proportional."
      state={llmUsage}
      retry={loadAll}
      what="cost drivers"
    >
      {#snippet children()}
        <RankBars
          rows={[
            { key: 'prompt', label: 'prompt tokens', value: tok.prompt, valueLabel: `${int(tok.prompt)} tok`, tone: 'accent' },
            {
              key: 'cache',
              label: 'cache read',
              sub: 'billed cheaper',
              value: tok.cacheRead,
              valueLabel: `${int(tok.cacheRead)} tok`,
              tone: 'ok'
            },
            { key: 'completion', label: 'completion', value: tok.completion, valueLabel: `${int(tok.completion)} tok`, tone: 'warn' },
            { key: 'reasoning', label: 'reasoning', value: tok.reasoning, valueLabel: `${int(tok.reasoning)} tok`, tone: 'warn' }
          ].filter((r) => isNum(r.value))}
          empty="No call in this range recorded a token count."
        />
        {#if isNum(completionShare)}
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            Completion is {pct(completionShare, 1)} of tokens. Trimming answers saves nothing worth
            measuring; trimming the prompt, or raising the cache-read share, is where the money is.
          </p>
        {/if}
      {/snippet}
    </Section>

    <!--
      The point of this tab. `chat_logs` holds one cost per TURN, and a turn is
      often several calls: #20772 contains a call that read 5,597 tokens from
      cache and cost $0.0026, and another that read none and cost $0.0388 — 15×
      apart, same turn, same model. At turn grain that is a single number and
      the lever is invisible.
    -->
    <Section
      title="Every model call"
      hint="One row per CALL, not per turn. Click a row to open the turn it belongs to."
      state={llmCalls}
      retry={loadAll}
      what="the per-call log"
    >
      {#snippet children()}
        <Table
          cols={[
            { key: 'ts', label: 'When' },
            { key: 'turn', label: 'Turn' },
            { key: 'seq', label: 'Seq', align: 'right' },
            { key: 'model', label: 'Model' },
            { key: 'prompt', label: 'Prompt', align: 'right' },
            { key: 'cr', label: 'Cache read', align: 'right' },
            { key: 'completion', label: 'Completion', align: 'right' },
            { key: 'ttft', label: 'TTFT', align: 'right' },
            { key: 'cost', label: 'Cost', align: 'right' }
          ]}
          rows={callRows}
          rowKey={(r, i) => (r.turn_id != null && r.seq != null ? `${r.turn_id}-${r.seq}` : i)}
          rowClass={(r) => (isNum(r.cost_usd) && isNum(callCostP90) && r.cost_usd >= callCostP90 ? 'bg-danger-soft' : '')}
          onpick={(r) => openTurn(r.turn_id)}
          empty="No model call has been recorded in this range."
        >
          {#snippet row(r)}
            <td class="tnum whitespace-nowrap">{clock(r.ts)}</td>
            <td class="font-mono text-meta">#{r.turn_id ?? UNKNOWN}</td>
            <td class="r tnum">{int(r.seq)}</td>
            <td class="font-mono text-meta">{r.model ?? UNKNOWN}</td>
            <td class="r tnum">{int(r.prompt_tokens)}</td>
            <!-- A measured zero here is the whole story: this call read nothing
                 from cache and paid full price for every token. It must render
                 as 0, and an absent split must render as an em-dash. -->
            <td class="r tnum {r.cache_read_tokens === 0 ? 'text-warning' : ''}">{int(r.cache_read_tokens)}</td>
            <td class="r tnum">{int(r.completion_tokens)}</td>
            <td class="r tnum">{ms(r.ttft_ms)}</td>
            <td class="r tnum">
              {#if isNum(r.cost_usd)}
                {usd(r.cost_usd)}{#if r.cost_is_estimated}<span class="ml-1 rounded border border-warning px-1 text-micro font-semibold text-warning">est</span>{/if}
              {:else}<span class="text-ink-3">not configured</span>{/if}
            </td>
          {/snippet}
        </Table>
        {#if callSpread}
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            Turn <b>#{callSpread.turn}</b> appears more than once: seq {callSpread.cheap.seq} read
            {int(callSpread.cheap.cache_read_tokens)} tokens from cache and cost {usd(callSpread.cheap.cost_usd)};
            seq {callSpread.dear.seq} read {int(callSpread.dear.cache_read_tokens)} and cost
            <b>{usd(callSpread.dear.cost_usd)}</b> — {callSpread.factor}× more, same turn, same model.
            That gap is the entire argument for recording the cache split from day one; at turn grain
            it is one number.
          </p>
        {/if}
        {#if isNum(callsTotal) && callsTotal > callRows.length}
          <p class="mt-2 text-meta text-ink-3">
            Showing the {int(callRows.length)} most recent of {int(callsTotal)} calls in range.
          </p>
        {/if}
      {/snippet}
    </Section>

    <Section
      title="Tokens per day"
      hint="Prompt vs completion as a composition. One agent turn can be many calls, each resending a growing conversation."
      state={costDay}
      retry={loadAll}
      what="tokens over time"
    >
      {#snippet children()}
        <!-- The split comes from /cost?group=day, which returns input_tokens and
             output_tokens per bucket. No new endpoint key was needed — I had
             been about to ask for one that already existed. -->
        {#if costDayRows.length}
          <StackedBars
            labels={costDayRows.map((r) => dayLabel(r.key))}
            series={[
              {
                key: 'prompt',
                label: 'Prompt tokens',
                color: C.accent,
                values: costDayRows.map((r) => (isNum(r.input_tokens) ? r.input_tokens : null))
              },
              {
                key: 'completion',
                label: 'Completion tokens',
                color: C.s2,
                values: costDayRows.map((r) => (isNum(r.output_tokens) ? r.output_tokens : null))
              }
            ]}
            onpick={(i) => {
              const k = costDayRows[i]?.key;
              if (!k) return;
              crossTo('questions', (p) => p.set('day', isoDay(k)));
            }}
          />
          {#if !costDayRows.some((r) => isNum(r.input_tokens) || isNum(r.output_tokens))}
            <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
              Every bucket in this range has an unknown token count, so the chart is empty rather
              than a row of zero-height bars. No turn here recorded tokens.
            </p>
          {/if}
        {:else}
          <p class="rounded-card border border-dashed border-line-2 bg-surface px-4 py-6 text-center text-body-sm text-ink-3">
            {costDay.data?.reason ?? 'No token data in this range.'}
          </p>
        {/if}
      {/snippet}
    </Section>

    <Section
      title="Per-model detail"
      hint="Calls, tokens and the cache split, per model. Click a row to filter."
      state={llmUsage}
      retry={loadAll}
      what="per-model detail"
    >
      {#snippet children()}
        <Table
          cols={[
            { key: 'model', label: 'Model' },
            { key: 'calls', label: 'Calls', align: 'right' },
            { key: 'in', label: 'Prompt', align: 'right' },
            { key: 'out', label: 'Completion', align: 'right' },
            { key: 'cr', label: 'Cache read', align: 'right' },
            { key: 'cc', label: 'Cache new', align: 'right' },
            { key: 'ttft', label: 'p50 ttft', align: 'right' },
            { key: 'cost', label: 'Cost', align: 'right' }
          ]}
          rows={llmRows}
          rowKey={(r, i) => r.model ?? i}
          onpick={(r) => drillTo('model', r.model, 'cost')}
          empty="No LLM call has been recorded in this range."
        >
          {#snippet row(r)}
            <td class="font-mono text-meta">{r.model ?? UNKNOWN}</td>
            <td class="r tnum">{int(r.calls)}</td>
            <td class="r tnum">{int(r.prompt_tokens)}</td>
            <td class="r tnum">{int(r.completion_tokens)}</td>
            <td class="r tnum">{int(r.cache_read_tokens)}</td>
            <td class="r tnum">{int(r.cache_creation_tokens)}</td>
            <td class="r tnum">{ms(r.p50_ttft_ms)}</td>
            <td class="r tnum">
              {#if isNum(r.cost_usd)}
                {usd(r.cost_usd)}{#if r.cost_is_estimated}<span class="ml-1 rounded border border-warning px-1 text-micro font-semibold text-warning">est</span>{/if}
              {:else}<span class="text-ink-3">not configured</span>{/if}
            </td>
          {/snippet}
        </Table>
      {/snippet}
    </Section>
  {/if}

  <!-- ================================================== CACHE -->
  {#if has('cache')}
    {@render secHead('cache')}
    {#snippet cacheKpis()}
      {@const missedRepeats = listOf(repeats).filter((r) => r.cached === 0 && r.asked > 1)}
      {@const cachedP50 = pathRows.find((r) => r.key === 'cache')?.p50 ?? null}
      {@const moneySaved = isNum(S.cache_hits) && isNum(costPerTurn) ? S.cache_hits * costPerTurn : null}
      <Kpi
        label="Hit rate"
        value={cacheRateShown == null ? null : pct(cacheRateShown, 1)}
        tone="ok"
        spark={hitRateSeries}
        delta={dSummary('cache_rate')}
        good="up"
        deltaFmt={signedPctOf(dSummary('cache_rate'))}
        foot="target >30% is healthy · {rateOf(S.cache_rate, turns, 'turns')}"
      />
      <Kpi
        label="Served from cache"
        value={isNum(S.cache_hits) ? int(S.cache_hits) : null}
        tone="ok"
        spark={col('cached')}
        delta={dSummary('cache_hits')}
        good="up"
        foot="turns answered with no model call · of {int(turns)}"
        onclick={() => drillTo('cached', 'true')}
      />
      <Kpi
        label="Time saved"
        value={isNum(S.p50_ms) && isNum(cachedP50) ? secs(S.p50_ms - cachedP50) : null}
        unit="s/hit"
        foot={isNum(cachedP50) ? `median ${ms(S.p50_ms)} overall vs ${ms(cachedP50)} cached` : 'no cache-path latency recorded, so the saving is not computed'}
      />
      <!-- Derived, not measured: hits × the mean cost of a turn. It carries the
           `estimated` flag for the same reason a derived price does — a number
           computed from two other numbers must not sit next to measured ones
           looking identical. -->
      <Kpi
        label="Money saved"
        value={moneySaved == null ? null : usd(moneySaved)}
        estimated={moneySaved != null}
        tone="ok"
        good="up"
        foot={moneySaved == null
          ? 'needs both a hit count and a cost per turn'
          : `${int(S.cache_hits)} hits × ${usd(costPerTurn)} avoided — the mean turn, not these turns`}
      />
      <Kpi
        label="Repeats that missed"
        value={repeats.status === 'ok' ? int(missedRepeats.length) : null}
        tone="bad"
        foot="each one is a caching defect worth reading"
        onclick={() => crossTo('questions')}
      />
    {/snippet}
    {@render kpiRow(cacheKpis)}

    <Section
      title="Hit rate over time"
      hint="A rate, so a plain line. The area fill is reserved for volume."
      state={timeseries}
      retry={loadAll}
      what="the cache hit rate"
    >
      {#snippet children()}
        <!-- The target rides as a second series rather than as a caption: a
             rate is only readable against the line it is supposed to clear. -->
        <LineChart
          labels={tsLabels}
          series={[
            { key: 'hit', label: 'Hit rate %', color: C.ok, values: hitRateSeries, area: false },
            // The target is the line the hit rate is judged against, so it has
            // to be visible: C.muted measured 1.18:1 on this card in dark and
            // 1.14:1 in light. C.other is 3.65:1 / 5.71:1 on the same card.
            { key: 'target', label: 'Target 30%', color: C.other, values: tsRows.map(() => 30), area: false }
          ]}
          fmt={(v) => `${Math.round(v)}%`}
          onpick={pickDay}
        />
      {/snippet}
    </Section>

    <Section
      title="Hit vs miss per day"
      hint="Composition per day → stacked bar. Click a segment for those turns."
      state={timeseries}
      retry={loadAll}
      what="cache composition"
    >
      {#snippet children()}
        <StackedBars
          labels={tsLabels}
          series={[
            { key: 'true', label: 'Cache hit', color: C.ok, values: col('cached') },
            { key: 'false', label: 'Miss', color: C.other, values: missSeries }
          ]}
          onpick={(i, key) => {
            const t = bucketOf(tsRows[i]);
            crossTo('questions', (p) => {
              if (t) p.set('day', isoDay(t));
              p.set('cached', key);
            });
          }}
        />
      {/snippet}
    </Section>

    <Section
      title="Repeats that never cached"
      hint="Sorted by damage: asked often, cached never. Click one to see every time it was asked."
      state={repeats}
      retry={loadAll}
      what="uncached repeats"
    >
      {#snippet children()}
        <RankBars
          rows={listOf(repeats)
            .filter((r) => r.cached === 0 && r.asked > 1)
            .map((r) => ({
              key: r.question,
              label: r.question,
              value: r.asked,
              valueLabel: `${int(r.asked)} asks`,
              tone: 'bad'
            }))}
          onpick={(r) => drillTo('q', r.key)}
          empty="Every repeated question in this range hit the cache at least once."
        />
        <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
          A follow-up is <b>deliberately</b> never cached — its answer depends on conversation
          history the cache key does not contain, and caching it would serve the next person who
          typed those three words an answer about a different drug. Check these are not follow-ups
          before treating them as defects.
        </p>
      {/snippet}
    </Section>
  {/if}

  <!-- ================================================== QUALITY -->
  {#if has('quality')}
    {@render secHead('quality')}
    <!-- Feedback lives in chat_feedback, which carries no lang / embed / path /
         actor / cached column. Those filters cannot narrow these numbers, so if
         one is active the cards below are answering a WIDER question than the
         filter bar claims. Saying so is the only honest option — silently
         showing unnarrowed numbers under an active chip is the failure this
         whole page is built to avoid. -->
    {#if fbUnfiltered}
      <WarnBar>
        {#snippet children()}
          <b>These rating numbers ignore {fbIgnored.join(', ')}.</b>
          <code class="font-mono text-meta">chat_feedback</code> has no
          <code class="font-mono text-meta">turn_id</code>, so a rating cannot be joined back to the
          turn it belongs to and those filters cannot narrow it. The cards below are scoped by date
          and store only. The endpoint reports this itself
          (<code class="font-mono text-meta">filters_applied: false</code>) — it is not inferred
          from the chips on screen, so it stays correct even when the two disagree.
        {/snippet}
      </WarnBar>
    {/if}

    {#snippet qualityKpis()}
      <Kpi
        label="Thumbs up"
        value={upRate == null ? null : pct(upRate)}
        tone="ok"
        delta={dSummary('up_rate')}
        good="up"
        deltaFmt={signedPctOf(dSummary('up_rate'))}
        unfiltered={fbUnfiltered && `Ratings cannot be narrowed by ${fbIgnored.join(', ')}.`}
        foot={ratedN
          ? `${int(fb.up)} up · ${int(fb.down)} down · over only ${int(ratedN)} ratings${fbNote}`
          : 'nothing rated yet — no rate is computed'}
        onclick={() => drillTo('rated', 'up')}
      />
      <Kpi
        label="Turns rated"
        value={isNum(ratedN) ? int(ratedN) : null}
        tone="info"
        delta={dSummary('rated')}
        good="up"
        unfiltered={fbUnfiltered && `Ratings cannot be narrowed by ${fbIgnored.join(', ')}.`}
        foot={isNum(ratedN) && isNum(turns)
          ? `${share(ratedN, turns)} of ${int(turns)} — the rest are unjudged${fbNote}`
          : 'no rating recorded'}
        onclick={() => drillTo('rated', 'any')}
      />
      <Kpi
        label="Corrections"
        value={isNum(fb.corrections) ? int(fb.corrections) : null}
        delta={dSummary('corrections')}
        good="none"
        unfiltered={fbUnfiltered && `Ratings cannot be narrowed by ${fbIgnored.join(', ')}.`}
        foot={`the highest-signal feedback there is${fbNote}`}
        onclick={() => drillTo('rated', 'down')}
      />
      <Kpi label="Judge score" value={null} foot="no LLM judge runs against this product — see below" />
    {/snippet}
    {@render kpiRow(qualityKpis)}

    <p class="mt-3 text-meta leading-relaxed text-ink-3">
      Filtering by rating joins <code class="font-mono text-meta">chat_feedback.turn_id</code>.
      Ratings recorded before that column existed have a NULL turn_id and match nothing, so
      <b>a rated filter looks sparser than the totals above</b> until new feedback accumulates. The
      totals count every rating; the filter counts only the ones that can be traced to their turn.
      Both numbers are honest and they are answering different questions.
    </p>

    <Section
      title="Ratings over time"
      hint="Up and down as a composition, not as two competing rates. Click a segment for that day's turns."
      state={timeseries}
      retry={loadAll}
      what="feedback over time"
    >
      {#snippet children()}
        <!-- Branch on the endpoint's own `feedback_available`, NOT on the values.
             `up === 0` is a day with traffic and no ratings — a measured zero,
             which must be drawn. `null` is the ratings join being unavailable
             entirely. Inferring one from the other is exactly how a real zero
             gets hidden or an unknown gets drawn as a floor. -->
        {#if feedbackAvailable}
          <StackedBars
            labels={tsLabels}
            series={[
              { key: 'up', label: 'Thumbs up', color: C.ok, values: col('up') },
              { key: 'down', label: 'Thumbs down', color: C.bad, values: col('down') }
            ]}
            onpick={(i, key) => {
              const t = bucketOf(tsRows[i]);
              crossTo('questions', (p) => {
                if (t) p.set('day', isoDay(t));
                p.set('rated', key);
              });
            }}
          />
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            These count <b>ratings, not rated turns</b> — two people disliking the same answer is 2
            here and 1 turn on the cards above. A day with traffic and no ratings is a real zero and
            is drawn as one; a day whose ratings could not be read at all is left as a gap.
          </p>
        {:else}
          <RankBars
            rows={[
              { key: 'up', label: 'Thumbs up', value: isNum(fb.up) ? fb.up : null, tone: 'ok' },
              { key: 'down', label: 'Thumbs down', value: isNum(fb.down) ? fb.down : null, tone: 'bad' }
            ]}
            onpick={(r) => drillTo('rated', r.key)}
            empty="No feedback recorded in this range."
          />
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            This database cannot join ratings to their turn
            (<code class="font-mono text-meta">feedback_available: false</code>), so there is no
            per-day shape to draw. The range totals are shown instead of a fabricated daily
            distribution.
          </p>
        {/if}
      {/snippet}
    </Section>

    <Section
      title="Every thumbs-down, with the correction"
      hint="The complaint queue. Click a row to open the full turn."
      state={diagnosis}
      retry={loadAll}
      what="the complaint queue"
    >
      {#snippet children()}
        <Table
          cols={[
            { key: 'ts', label: 'When' },
            { key: 'q', label: 'Question' },
            { key: 'said', label: 'What the user said' },
            { key: 'answer', label: 'What the agent answered' },
            { key: 'store', label: 'Store' }
          ]}
          rows={diagRows.filter((r) => r.negative_feedback === true)}
          rowKey={(r, i) => r.turn_id ?? i}
          onpick={(r) => openTurn(r.turn_id)}
          empty="No negative feedback in this range."
        >
          {#snippet row(r)}
            {@const said = r.correction ?? r.feedback_comment ?? r.comment ?? null}
            <td class="tnum whitespace-nowrap">{when(r.ts)}</td>
            <td>{clip(r.question, 45)}</td>
            <!-- The correction, when the endpoint carries one. A row with none
                 says "not returned by this endpoint", not "the user said
                 nothing" — the two look identical if you print an empty cell. -->
            <td>{#if said}“{clip(said, 45)}”{:else}<span class="text-ink-3">not returned by /diagnosis</span>{/if}</td>
            <td>{r.answer ? clip(r.answer, 45) : UNKNOWN}</td>
            <td>{r.store_id ?? UNKNOWN}</td>
          {/snippet}
        </Table>
        <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
          {#if !diagRows.some((r) => r.correction ?? r.feedback_comment ?? r.comment)}
            <code class="font-mono text-meta">/diagnosis</code> returns the turn, not the
            <code class="font-mono text-meta">chat_feedback</code> text, so the correction column is
            empty here. Open a row to read the full turn.
            {#if isNum(fb.corrections)}{int(fb.corrections)} corrections were written in this range.{/if}
          {/if}
          {#if isNum(upRate) && isNum(ratedN) && ratedN > 0}
            {pct(upRate)} over {int(ratedN)} ratings is fragile — one more complaint moves it to
            {pct(((upRate / 100) * ratedN) / (ratedN + 1) * 100)}. That is why the card says “of
            {int(ratedN)}” rather than a bare percentage.
          {/if}
        </p>
      {/snippet}
    </Section>

    <GapCard
      tag="NEEDS MIGRATION"
      title="No LLM judge, and no “was this judged” flag"
      body="Thumbs cover a fraction of turns. A judge could score the rest — but a judge that fails must not silently default every score to the middle, because a systematic outage then pulls the whole dashboard toward 3/5 and nobody can tell. Store the score AND a separate judged flag, clamp to 1–5, and always keep the reasoning: a score you cannot argue with is a score nobody trusts."
      sql={'CREATE TABLE turn_judgements (\n  turn_id bigint, response_quality int, context_score int,\n  judged boolean NOT NULL, reasoning text, judge_model text);'}
    />
  {/if}

  <!-- ================================================== DIAGNOSTICS -->
  {#if has('diagnostics')}
    {@render secHead('diagnostics')}
    {#snippet diagKpis()}
      <!-- Every count here is the endpoint's own window-wide `counts`, never a
           tally of the rows on screen: the list is paginated, so counting it
           would cap every card at the page size and call it a measurement. -->
      <Kpi
        label="Failed tool calls"
        value={isNum(outcomeTotals.failed) ? int(outcomeTotals.failed) : null}
        tone={outcomeTotals.failed === 0 ? 'ok' : 'bad'}
        delta={deltaOf(toolOutcomes.data, 'failed')}
        good="down"
        foot={isNum(outcomeTotals.failed)
          ? outcomeTotals.failed === 0
            ? 'measured zero, not unknown — outcome is recorded on every call in range'
            : `of ${int(outcomeTotals.calls)} calls`
          : 'outcome is not recorded on these turns'}
        onclick={() => setParam('issue', 'failed_tool')}
      />
      <!-- The distinction the whole tool table rests on. A tool that declines
           on purpose and redirects is NOT a failure; counting the two together
           is what gave a working tool a 56% failure rate in the product this
           instrumentation came from. -->
      <Kpi
        label="Deliberate refusals"
        value={isNum(outcomeTotals.refused) ? int(outcomeTotals.refused) : null}
        tone="warn"
        delta={deltaOf(toolOutcomes.data, 'refused')}
        good="none"
        foot={isNum(outcomeTotals.refused)
          ? 'a tool declined and redirected — NOT a failure, and never counted as one'
          : 'outcome is not recorded on these turns'}
      />
      <Kpi
        label="Failed turns"
        value={isNum(diagCounts.failed_tool) ? int(diagCounts.failed_tool) : null}
        tone="bad"
        foot={diagProblemRate && isNum(diagProblemRate.n)
          ? `turns with a raised tool · ${rateOf(diagProblemRate.rate, diagProblemRate.n, 'turns')} had some problem`
          : 'turns in which a tool raised — not one that declined'}
        onclick={() => setParam('issue', 'failed_tool')}
      />
      <Kpi
        label="Negative feedback"
        value={isNum(diagCounts.negative_feedback) ? int(diagCounts.negative_feedback) : isNum(fb.down) ? int(fb.down) : null}
        tone="bad"
        delta={dSummary('down')}
        good="down"
        unfiltered={fbUnfiltered && `Ratings cannot be narrowed by ${fbIgnored.join(', ')}.`}
        foot={`the user says it failed${fbNote}`}
        onclick={() => drillTo('rated', 'down', 'quality')}
      />
      <Kpi
        label="Both"
        value={isNum(diagCounts.both) ? int(diagCounts.both) : null}
        tone="bad"
        foot="a failure the user also complained about — the highest-value queue"
      />
      <Kpi
        label="“Gave up” answers"
        value={isNum(diagCounts.gave_up) ? int(diagCounts.gave_up) : null}
        tone="warn"
        foot="a turn can succeed while the answer apologises — checked against the text, not a status field"
      />
    {/snippet}
    {@render kpiRow(diagKpis)}

    <Section
      title="Tool calls by outcome"
      hint="Three states. A refusal is NOT a failure — collapsing them is what makes tool dashboards lie."
      state={toolOutcomes.status === 'missing' ? tools : toolOutcomes}
      retry={loadAll}
      what="tool outcomes"
    >
      {#snippet children()}
        {#if outcomeRows.length}
          <RankBars
            rows={outcomeRows.flatMap((r) => [
              { key: `${r.name}-s`, label: `${r.name} · succeeded`, value: r.succeeded, tone: 'ok', tool: r.name },
              { key: `${r.name}-r`, label: `${r.name} · refused`, value: r.refused, tone: 'warn', tool: r.name },
              { key: `${r.name}-f`, label: `${r.name} · failed`, value: r.failed, tone: 'bad', tool: r.name }
            ])}
            onpick={(r) => drillTo('tool', r.tool)}
          />
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            {int(outcomeTotals.calls)} tool calls in range: {int(outcomeTotals.succeeded)} succeeded,
            {int(outcomeTotals.refused)} refused, {int(outcomeTotals.failed)} failed{#if toolOutcomes.data?.success_rate}
              · success {rateOf(toolOutcomes.data.success_rate.rate, toolOutcomes.data.success_rate.n, 'calls')}{/if}.
            A tool that deliberately declines and redirects is <b>refused</b>, in amber, and is not
            counted as a failure anywhere on this page.
          </p>
        {:else}
          <RankBars
            rows={toolRows.map((r) => ({
              key: String(r.tool),
              label: `${r.tool} · calls (outcome not recorded)`,
              value: isNum(r.calls) ? r.calls : null,
              tone: 'muted'
            }))}
            onpick={(r) => drillTo('tool', r.key)}
            empty="No tool call recorded in this range."
          />
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            Only the tool <i>name</i> is available for these turns
            (<code class="font-mono text-meta">tools: ["get_stock"]</code>). Outcome, arguments,
            error text, attempt number and duration are absent, so the three-state chart cannot be
            computed for them — and a name is never assumed to mean success.
          </p>
        {/if}
      {/snippet}
    </Section>

    <Section
      title="Which intents hit which tools"
      hint="Pairwise affinity → heatmap, single-hue ramp. A rainbow would imply category boundaries that do not exist."
      state={intents}
      retry={loadAll}
      what="the intent/tool matrix"
    >
      {#snippet children()}
        {#if intentGrid}
          <Heatmap
            cols={intentGrid.cols}
            rows={intentGrid.rows}
            onpick={INTENT_FILTER_DECLARED
              ? (r, c) =>
                  crossTo('questions', (p) => {
                    p.set('intent', r.key);
                    p.set('tool', c.key);
                  })
              : null}
          />
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            This recovers the mental model users actually have, which is rarely the one in the schema
            docs.
            {#if INTENT_FILTER_DECLARED}
              Click a cell for the turns behind it.
            {:else}
              Cells are not clickable yet: a cell is an intent <i>and</i> a tool, and only
              <code class="font-mono text-meta">tool</code> is a declared parameter. Filtering on
              the tool alone would answer a different question than the cell asks, so the page does
              neither.
            {/if}
          </p>
        {:else}
          <p class="rounded-card border border-dashed border-line-2 bg-surface px-4 py-6 text-center text-body-sm text-ink-3">
            No intent/tool pairs recorded in this range.
          </p>
        {/if}
      {/snippet}
    </Section>

    <Section
      title="The diagnosis queue"
      hint="Failed tool calls, negative feedback and gave-up answers in one list. Click a row for the turn; open its trace below."
      state={diagnosis}
      retry={loadAll}
      what="the diagnosis queue"
    >
      {#snippet children()}
        <div class="mb-3 flex flex-wrap gap-2">
          {#each [['', 'Everything'], ['failed_tool', 'Failed tool'], ['negative_feedback', 'Rated down'], ['gave_up', 'Gave up'], ['both', 'Both']] as [v, label]}
            <button
              onclick={() => setParam('issue', v)}
              class="min-h-[34px] cursor-pointer rounded-full border px-3 text-meta
                     {issue === v ? 'border-accent bg-accent text-on-accent' : 'border-line bg-surface text-ink-2 hover:border-accent hover:text-accent'}"
              aria-pressed={issue === v}>{label}</button
            >
          {/each}
        </div>
        <Table
          cols={[
            { key: 'ts', label: 'When' },
            { key: 'q', label: 'Question' },
            { key: 'issue', label: 'Issue' },
            { key: 'detail', label: 'Detail' },
            { key: 'trace', label: '', align: 'right' }
          ]}
          rows={diagRows}
          rowKey={(r, i) => r.turn_id ?? i}
          onpick={(r) => openTurn(r.turn_id)}
          empty="Nothing failed, nothing was rated down, and no answer gave up in this range."
        >
          {#snippet row(r)}
            <td class="tnum whitespace-nowrap">{when(r.ts)}</td>
            <td>{clip(r.question, 50)}</td>
            <td>
              <!-- `signals` lists every problem the turn has; issue_type is the
                   headline. A turn that both failed AND was rated down must not
                   be filed under only one of them. -->
              <Badge tone={r.issue_type === 'gave_up' ? 'warn' : 'danger'}>
                {String(r.issue_type ?? 'unknown').replaceAll('_', ' ')}
              </Badge>
              {#if Array.isArray(r.signals) && r.signals.length > 1}
                <span class="ml-1 text-label text-ink-3">+{r.signals.length - 1} more</span>
              {/if}
            </td>
            <td>
              {#if r.failed_tool_name}
                <span class="font-mono text-meta">{r.failed_tool_name}</span>
                {#if r.failed_tool_error}· {clip(r.failed_tool_error, 40)}{/if}
              {:else}{UNKNOWN}{/if}
            </td>
            <td class="r">
              <button
                class="cursor-pointer rounded-panel border border-line px-2 py-1 text-meta hover:bg-surface"
                onclick={(e) => {
                  e.stopPropagation();
                  openTrace(r.turn_id);
                }}>trace</button
              >
            </td>
          {/snippet}
        </Table>
      {/snippet}
    </Section>

    <Section
      title="One turn, every call, in order"
      hint="The trace view. Build it early or you debug from summary statistics — a tool row opens its arguments, an llm row opens its prompt."
    >
      {#snippet children()}
        {#if !traceId && slowRows.length}
          <!-- A trace view nobody opens is a trace view nobody trusts. The
               slowest turn is the one worth reading first, so it is one click
               away rather than a row somebody has to go and find. -->
          <div class="mb-3">
            <button
              onclick={() => openTrace(slowRows[0].id)}
              class="inline-flex min-h-[38px] cursor-pointer items-center rounded-panel border border-line px-3 text-meta font-semibold text-ink-2 hover:border-accent hover:text-accent"
            >
              Open the slowest turn in range ({ms(slowRows[0].latency_ms)}) ›
            </button>
          </div>
        {/if}
        <TraceView turnId={traceId} onclose={() => setParam('trace', '')} />
      {/snippet}
    </Section>
  {/if}

  <!-- ================================================== DATA HEALTH -->
  {#if has('health')}
    {@render secHead('health')}
    <!-- Same class of honesty as the feedback flag: /data-health reads catalog,
         inventory and the ingest log — not chat_logs — so it takes none of the
         §4 filters, and `by_day` is estimate-wide because an ingest is a file
         arriving for the whole estate and ingest_events has no branch column.
         The chips stay on screen while this tab ignores them, so it says so. -->
    {#if FILTER_KEYS.some((k) => f[k]) || day}
      <WarnBar>
        {#snippet children()}
          <b>This tab ignores the filter bar.</b> It reads the catalog, the inventory and the ingest
          log rather than the turn log, so date, store and every other chip above leave it unchanged.
          The ingest history is estate-wide by design — a file arrives for every branch at once, and
          a per-branch row count there would be invented.
        {/snippet}
      </WarnBar>
    {/if}

    {#snippet healthKpis()}
      {@const H = health.data ?? {}}
      <!-- Every card on this tab carries the `unfiltered` mark while a chip is
           active, not just the banner above. A reader who scrolled past the
           banner is looking at one number, and that number has to say for
           itself that the chips do not reach it. -->
      {@const ign = healthIgnoring && 'This tab reads the catalog and the ingest log, not the turn log — the filter chips do not narrow it.'}
      <Kpi
        label="Catalog rows"
        value={isNum(H.catalog?.total) ? int(H.catalog.total) : null}
        unfiltered={ign}
        foot={H.freshness?.catalog_at ? `last write ${when(H.freshness.catalog_at)}` : 'no write timestamp recorded'}
      />
      <Kpi
        label="Inventory rows"
        value={isNum(H.inventory?.rows) ? int(H.inventory.rows) : null}
        unfiltered={ign}
        foot={isNum(H.inventory?.sites) ? `across ${int(H.inventory.sites)} stores` : 'store count not recorded'}
      />
      <Kpi
        label="Ingest runs"
        value={fStage('arrived') == null ? null : int(fStage('arrived'))}
        unfiltered={ign}
        good="none"
        foot={fStage('arrived') == null
          ? 'the ingest event log could not be read — unknown, not none'
          : `pipeline attempts, not filenames — a retry counts twice`}
      />
      <Kpi
        label="Set aside"
        value={fStage('set_aside') == null ? null : int(fStage('set_aside'))}
        tone="bad"
        unfiltered={ign}
        good="down"
        foot={fStage('set_aside') == null
          ? 'the ingest event log could not be read'
          : `${share(fStage('set_aside'), fStage('arrived'))} of ${funnelUnit} ended here — an outcome, not a stage`}
      />
      <Kpi
        label="Stub catalog rows"
        value={isNum(H.catalog?.stubs) ? int(H.catalog.stubs) : null}
        tone="warn"
        unfiltered={ign}
        foot={isNum(H.catalog?.stub_ratio)
          ? `${pct(H.catalog.stub_ratio, 1)} of the catalog · a high ratio means the file never loaded`
          : 'stub ratio not computed'}
      />
      <Kpi
        label="Unknown quantities"
        value={isNum(H.inventory?.null_qty) ? int(H.inventory.null_qty) : null}
        unfiltered={ign}
        foot="blank stock is NULL — unknown, not zero. That distinction is the whole reason this card exists."
      />
    {/snippet}
    {@render kpiRow(healthKpis)}

    <!--
      The ingest funnel. Half of everything arriving is never recognised, and
      nothing in this console says so today — which is precisely why a funnel
      belongs here rather than another row count.
    -->
    <Section
      title="Ingest funnel"
      hint="Every stage a file passes, and where they stop."
      state={health}
      retry={loadAll}
      what="the ingest funnel"
    >
      {#snippet children()}
        {@const LABELS = {
          arrived: ['Arrived', 'landed in the SFTP drop'],
          detected: ['Detected', 'recognised as a known layout'],
          checked: ['Checked', 'passed validation'],
          loaded: ['Loaded', 'rows written to inventory']
        }}
        <Funnel
          stages={funnelStages.map((k) => ({
            key: k,
            label: LABELS[k]?.[0] ?? k,
            value: fStage(k),
            note:
              k === 'detected' && fStage('arrived') != null && fStage('detected') != null
                ? `recognised as a known layout — ${int(fStage('arrived') - fStage('detected'))} were not`
                : (LABELS[k]?.[1] ?? '')
          }))}
        />

        <!--
          `set_aside` sits OUTSIDE the funnel, deliberately. It is a terminal
          outcome rather than a narrowing stage, so it can exceed `loaded` — and
          a funnel bar wider than the one above it asserts a containment that
          does not hold. Its own card can state the quantity without implying it.
        -->
        {#if fStage('set_aside') != null || ingestFunnel != null}
          <div class="mt-3 rounded-card border border-l-[3px] border-line border-l-danger bg-surface px-4 py-3">
            <div class="flex flex-wrap items-baseline justify-between gap-2">
              <b class="text-body-sm font-semibold text-ink">Set aside</b>
              <b class="text-body font-bold text-ink tnum">{int(fStage('set_aside'))}</b>
            </div>
            <p class="mt-1 text-label leading-snug text-ink-3">
              Quarantined — needs a human. This is an <b>outcome, not a stage</b>: it collects the
              {funnelUnit} that failed anywhere above, so it can be larger than “Loaded” and is not
              drawn as part of the funnel.
              {#if funnelDrops}
                {#if isNum(funnelDrops.unrecognised)}<br />{int(funnelDrops.unrecognised)} never matched a
                  known layout{/if}{#if isNum(funnelDrops.rejected)} · {int(funnelDrops.rejected)} failed
                  validation{/if}
              {/if}
            </p>
          </div>
        {/if}
        {#if ingestFunnel == null}
          <!-- `funnel: null` means the event log could not be READ. It is not
               the same as a funnel of zeroes, which would say no file has ever
               arrived — indistinguishable, on screen, from a dead SFTP drop. -->
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            Every stage is an em-dash because
            <code class="font-mono text-meta">funnel</code> came back
            <code class="font-mono text-meta">null</code>: the ingest event log could not be read.
            That is <b>unknown, not empty</b> — a row of zeroes here would claim no file has ever
            arrived, which looks exactly like a drop folder that has stopped working. The per-file
            timeline on the SFTP page is the other way to see it.
          </p>
        {:else if fStage('arrived') != null && fStage('detected') != null && fStage('arrived') > 0}
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            The drop from {int(fStage('arrived'))} to {int(fStage('detected'))} is the story on this
            page: <b>{share(fStage('arrived') - fStage('detected'), fStage('arrived'))} of everything
            that arrives is not recognised</b> as a known layout, and no other chart in this console
            shows it. A {funnelUnit === 'runs' ? 'run' : 'file'} that is never detected never fails
            validation either — it simply does not appear in any of the counts below.
            <br />
            These count <b>pipeline {funnelUnit}, not filenames</b>: a file that failed on Monday and
            loaded on Tuesday is two attempts here. Collapsing it to one success would erase exactly
            the retry worth looking at.{#if isNum(funnelMeta?.files)}
              {int(funnelMeta.files)} distinct filenames are behind them.{/if}
          </p>
        {/if}
      {/snippet}
    </Section>

    <Section
      title="What the inventory looks like"
      hint="Ranked, because these are categories, not a trend. Negatives and blanks are stored as sent — neither is silently corrected."
      state={health}
      retry={loadAll}
      what="data health"
    >
      {#snippet children()}
        {@const inv = health.data?.inventory ?? {}}
        <RankBars
          rows={[
            { key: 'rows', label: 'rows in total', value: inv.rows, tone: 'accent' },
            { key: 'zero', label: 'stock exactly 0 (measured)', value: inv.zero, tone: 'muted' },
            { key: 'null', label: 'stock unknown (NULL)', value: inv.null_qty, tone: 'warn' },
            { key: 'negative', label: 'negative stock (stored as sent)', value: inv.negative, tone: 'bad' },
            { key: 'under20', label: 'under 20 units', value: inv.under_20, tone: 'warn' }
          ]}
          empty="No inventory rows."
        />
      {/snippet}
    </Section>

    <Section
      title="Ingest over time"
      hint="Rows landed per day."
      state={health}
      retry={loadAll}
      what="ingest history"
    >
      {#snippet children()}
        <!-- null and [] are DIFFERENT answers and get different screens:
             null  = the event table cannot be read — we do not know.
             []    = no ingest happened in this period — we know, and it is none.
             Collapsing them would report a broken feed as a quiet week. -->
        {#if ingestDays === null}
          <p class="rounded-card border border-dashed border-line-2 bg-surface px-4 py-6 text-center text-body-sm leading-relaxed text-ink-3">
            The ingest event log could not be read
            (<code class="font-mono text-meta">by_day: null</code>), so this is unknown rather than
            empty. Nothing is estimated in its place; the per-file timeline on the SFTP page is the
            other way to see it.
          </p>
        {:else if ingestDays.length === 0}
          <p class="rounded-card border border-dashed border-line-2 bg-surface px-4 py-6 text-center text-body-sm text-ink-3">
            No ingest ran in this period. That is a measured none, not a missing reading.
          </p>
        {:else}
          <StackedBars
            labels={ingestDays.map((r) => dayLabel(r.day))}
            series={[
              {
                key: 'ok',
                label: 'Rows loaded',
                color: C.accent,
                values: ingestDays.map((r) => (isNum(r.rows) ? r.rows : null))
              },
              {
                key: 'rejected',
                label: 'Files rejected',
                color: C.bad,
                values: ingestDays.map((r) => (isNum(r.rejected) ? r.rejected : null))
              }
            ]}
          />
          <p class="mt-2.5 text-meta leading-relaxed text-ink-3">
            Two different units share this chart: <b>rows loaded</b> and <b>files rejected</b>. A
            rejected file has no row count — validation refused it before it loaded — so the red
            segment is a file count and is tiny next to the blue by construction. A day whose row
            count is unknown leaves a gap rather than sitting on the floor.
            {#if ingestDays.some((r) => isNum(r.files))}
              {int(ingestDays.reduce((a, r) => a + (isNum(r.files) ? r.files : 0), 0))} files touched
              in this period.
            {/if}
          </p>
        {/if}
      {/snippet}
    </Section>

    <Section
      title="Freshness"
      hint="When each side of the data last moved."
      state={health}
      retry={loadAll}
      what="freshness"
    >
      {#snippet children()}
        <div class="grid gap-3 sm:grid-cols-2">
          {#each [['Catalog', health.data?.freshness?.catalog_at], ['Inventory', health.data?.freshness?.inventory_at]] as [label, at]}
            <div class="rounded-card border border-line bg-surface px-4 py-3">
              <div class="text-label font-semibold tracking-[0.03em] text-ink-3 uppercase">{label}</div>
              <div class="mt-1 text-title font-bold tnum {at ? 'text-ink' : 'text-ink-3'}">{when(at)}</div>
              <p class="mt-1 text-label text-ink-3">
                {at ? 'last row written' : 'no write timestamp recorded — this is unknown, not “never”'}
              </p>
            </div>
          {/each}
        </div>
      {/snippet}
    </Section>
  {/if}

  <!-- ================================================== FEED -->
  {#if has('feed')}
    {@render secHead('feed')}
    <FeedTab qs={aqs} f={af} tz={ATZ} nonce={actNonce} setParams={actSet} {reportTz} />
  {/if}

  <!-- ================================================== AUDIT -->
  {#if has('audit')}
    {@render secHead('audit')}
    <AuditTab qs={aqs} f={af} tz={ATZ} nonce={actNonce} setParams={actSet} {reportTz} />
  {/if}

  <!-- ================================================== TRENDS -->
  {#if has('trends')}
    {@render secHead('trends')}
    <TrendsTab
      qs={aqs}
      f={af}
      tz={ATZ}
      nonce={actNonce}
      setParams={actSet}
      sp={url.searchParams}
      {reportTz}
    />
  {/if}

  <!-- ================================================== EXPLORE -->
  {#if has('explore')}
    {@render secHead('explore')}
    <ExploreTab
      qs={aqs}
      f={af}
      tz={ATZ}
      nonce={actNonce}
      setParams={actSet}
      sp={url.searchParams}
      {reportTz}
    />
  {/if}
</div>
    {#snippet failed(error, reset)}
    <div class="mt-5 rounded-card border border-danger bg-danger-soft px-4 py-4">
      <div class="text-body-sm font-bold text-ink">This section could not be drawn.</div>
      <p class="mt-1 text-meta leading-relaxed text-ink-2">
        The rest of the console is unaffected — the other tabs still work. The
        error is in the browser console.
      </p>
      <p class="mt-1 font-mono text-label break-all text-ink-3">{String(error)}</p>
      <button
        class="mt-3 min-h-[36px] cursor-pointer rounded-panel border border-line bg-surface px-3 text-body-sm font-semibold"
        onclick={reset}>Try again</button>
    </div>
    {/snippet}
  </svelte:boundary>
{/key}

{#if openTurnId}
  <TurnDrawer turnId={openTurnId} onclose={() => setParam('turn', '')} ontrace={openTrace} />
{/if}
