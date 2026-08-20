<script>
  import { dialog } from '$lib/aurora/dialog.js';
  // Feed — what happened, newest first.
  //
  // The live feed at the bottom is the page this tab grew out of, and its
  // behaviour is deliberately unchanged: the same `/admin/activity` endpoint,
  // the same pager, the same record drawer, the same `?source=auth` deep link
  // that the rail and the retired /security-log page both point at. What is new
  // is everything above it — the KPI row, the composition over time, what is
  // being done, and where it came from — all from `/admin/activity/summary`.
  //
  // The two halves fail independently. A backend with no `/summary` endpoint
  // still shows a working feed, and a feed that errors does not blank the
  // charts.
  import { tick, untrack } from 'svelte';
  import Kpi from '$lib/charts/Kpi.svelte';
  import Section from '$lib/charts/Section.svelte';
  import StackedBars from '$lib/charts/StackedBars.svelte';
  import RankBars from '$lib/charts/RankBars.svelte';
  import Donut from '$lib/charts/Donut.svelte';
  import ErrorState from '$lib/ErrorState.svelte';
  import Badge from '$lib/Badge.svelte';
  import { getJSON, ApiError } from '$lib/api.js';
  import {
    Activity,
    ChevronLeft,
    ChevronRight,
    X,
    Copy,
    Search,
    ListFilter,
    Info,
    TriangleAlert,
    Play
  } from '@lucide/svelte';
  import {
    UNKNOWN,
    isNum,
    int,
    kpi,
    deltaOf,
    unfilteredOf,
    tzEcho,
    fetchSection,
    loadingSection,
    buildQuery,
    fromRows,
    memberLabel,
    NOT_RECORDED,
    COLOR,
    openSection
  } from './shared.js';

  let { qs, f, tz, nonce, setParams, reportTz } = $props();

  // The five auth events the security-log page named, with its labels. An event
  // outside this list keeps its raw name rather than being relabelled.
  const AUTH_EVENTS = [
    { key: 'login_ok', label: 'Login OK', tone: 'ok' },
    { key: 'login_fail', label: 'Login failed', tone: 'warn' },
    { key: 'login_locked', label: 'Locked out', tone: 'danger' },
    { key: 'sso_ok', label: 'SSO OK', tone: 'ok' },
    { key: 'sso_fail', label: 'SSO failed', tone: 'warn' }
  ];
  const AUTH_EVENT_MAP = Object.fromEntries(AUTH_EVENTS.map((e) => [e.key, e]));

  // ------------------------------------------------------------- summary
  //
  // Four aggregate endpoints back this tab, each its own Section:
  //
  //   /activity/summary                       the KPI row
  //   /activity/trends?rollup=day             composition over time
  //   /activity/explore?by=action|status_class|ip   the three ranked panels
  //
  // The ranked panels come from `explore` rather than from a bespoke key on
  // `summary` because that is precisely what `explore` is: measure × dimension,
  // whitelisted server-side and already tested. Duplicating those aggregates
  // onto `summary` would be a second implementation of the same SQL, and the
  // two would drift.
  let sum = $state(loadingSection('/admin/activity/summary'));
  let trend = $state(loadingSection('/admin/activity/trends'));
  let acts = $state(loadingSection('/admin/activity/explore'));
  let stat = $state(loadingSection('/admin/activity/explore'));
  let ips = $state(loadingSection('/admin/activity/explore'));
  let d = $derived(sum.data ?? {});

  const rank = (by) => `/admin/activity/explore?${buildQuery(f, { measure: 'events', by, rollup: 'day', top: 10 })}`;

  async function loadSummary() {
    sum = loadingSection('/admin/activity/summary');
    trend = loadingSection('/admin/activity/trends');
    acts = loadingSection('/admin/activity/explore');
    stat = loadingSection('/admin/activity/explore');
    ips = loadingSection('/admin/activity/explore');
    // Independent requests, so they go out together rather than in series.
    const [s, t, a, st, ip] = await Promise.all([
      fetchSection('/admin/activity/summary?' + qs),
      fetchSection(`/admin/activity/trends?${buildQuery(f, { rollup: 'day' })}`),
      fetchSection(rank('action')),
      fetchSection(rank('status_class')),
      fetchSection(rank('ip'))
    ]);
    sum = s;
    trend = t;
    acts = a;
    stat = st;
    ips = ip;
    // The header chip belongs to this tab's bucketing endpoint. `summary` echoes
    // the zone its own query used, so that is the one the reader is looking at.
    reportTz?.(tzEcho(sum.data));
  }

  // The KPIs sit at the top level or under `clients` / `failures` / `signins`,
  // not under a `kpis` object — this reads the backend's shape rather than
  // asking it to reshape a tested payload.
  let raw = $derived({
    events: d?.events,
    actors: d?.distinct_actors,
    browser: d?.clients?.browser,
    testclient: d?.clients?.testclient,
    failed: d?.failures?.failed,
    signinFail: d?.signins?.login_fail,
    setAside: d?.files_set_aside
  });
  let K = $derived({
    events: kpi(raw.events),
    browser: kpi(raw.browser),
    testclient: kpi(raw.testclient),
    failed: kpi(raw.failed),
    signin: kpi(raw.signinFail),
    setAside: kpi(raw.setAside),
    actors: kpi(raw.actors)
  });

  // "53×400 · 18×403 · 5×401" — the shape of the failures, not just the count.
  let failFoot = $derived.by(() => {
    const by = d?.failures?.by_status;
    if (!by || typeof by !== 'object') return '';
    return Object.entries(by)
      .filter(([, v]) => isNum(v) && v > 0)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 4)
      .map(([k, v]) => `${int(v)}×${k}`)
      .join(' · ');
  });
  // Lock-outs and blocks are listed beside the failures, never added to them: a
  // lock-out is the consequence of several failed attempts, so summing would
  // count the same incident twice.
  let signinFoot = $derived.by(() => {
    const s = d?.signins;
    if (!s || typeof s !== 'object') return '';
    const n = (k) => (isNum(s[k]?.value) ? s[k].value : isNum(s[k]) ? s[k] : null);
    return [
      isNum(n('login_fail')) ? `${int(n('login_fail'))} failed` : null,
      isNum(n('login_locked')) ? `${int(n('login_locked'))} locked out` : null,
      isNum(n('login_blocked')) ? `${int(n('login_blocked'))} IP-blocked` : null,
      isNum(n('sso_fail')) ? `${int(n('sso_fail'))} SSO failed` : null
    ]
      .filter(Boolean)
      .join(' · ');
  });
  // The denominator turns a bare count into a rate somebody can act on, and it
  // is printed only when the backend sends it: "190 of 573" is a claim, and
  // "190 of ?" would be a rate missing half of itself. `of_arrived` is
  // `{rate, n}` — n is pipeline ATTEMPTS, not distinct filenames, so the same
  // file retried twice counts twice and the wording says "arrivals".
  let setAsideFoot = $derived.by(() => {
    const r = raw.setAside?.of_arrived;
    const base = 'refused by validation and kept for inspection';
    if (!r || !isNum(r.n)) return base;
    return `${base} — of ${int(r.n)} arrivals`;
  });
  let browserFoot = $derived.by(() => {
    const t = K.testclient.value;
    const n = isNum(d?.clients?.n) ? d.clients.n : K.events.value;
    if (!isNum(t)) return 'client addresses not reported';
    return isNum(n) ? `${int(t)} of ${int(n)} came from ip=testclient` : `${int(t)} from ip=testclient`;
  });

  // `/trends` rows are flat: one row per bucket with a column per source.
  const SOURCE_COLS = [
    { key: 'app', label: 'Admin actions', color: COLOR.app },
    { key: 'auth', label: 'Auth', color: COLOR.auth },
    { key: 'ingest', label: 'Ingest', color: COLOR.ingest }
  ];
  let overTime = $derived(fromRows(trend.data?.series, SOURCE_COLS));

  // `explore`'s table summarises the SERIES: `n` is buckets that had a
  // measurement, `rows` is the raw observation count. A "what is being done"
  // ranking wants the observations, so it reads `rows` and falls back to the
  // sum of the buckets — never to `n`, which would rank by how many days a
  // thing happened on rather than by how often it happened.
  const observations = (r) => (isNum(r?.rows) ? r.rows : isNum(r?.sum) ? r.sum : null);
  const tableOf = (sec) => (Array.isArray(sec.data?.table) ? sec.data.table : []);

  let actionRows = $derived(
    tableOf(acts).map((r) => ({
      key: r?.key == null ? '' : String(r.key),
      label: memberLabel(r?.key),
      value: observations(r),
      tone: 'accent'
    }))
  );
  let actionsTruncated = $derived(acts.data?.truncated === true);

  // Five classes, five fills. Two pairs used to share a colour in the SAME
  // donut — `4xx`/`5xx` were both COLOR.bad and `3xx`/not-recorded were both
  // COLOR.muted — so a reader could see there were failures but not whose.
  // The split is also the honest one: a 4xx is the caller's mistake and a 5xx
  // is ours, which is the difference between "someone is probing us" and "we
  // are broken". Tightest pair measures dE 33.9 light / 43.0 dark.
  const STATUS_COLOR = {
    '2xx': COLOR.ok,
    '3xx': COLOR.auth,
    '4xx': COLOR.warn,
    '5xx': COLOR.bad,
    [NOT_RECORDED]: COLOR.muted
  };
  let statusSlices = $derived(
    tableOf(stat).map((r, i) => ({
      key: r?.key == null ? 'null' : String(r.key),
      label: memberLabel(r?.key),
      value: observations(r),
      color: STATUS_COLOR[memberLabel(r?.key)] ?? COLOR.muted
    }))
  );
  let ipRows = $derived(
    tableOf(ips).map((r) => ({
      key: r?.key == null ? '' : String(r.key),
      label: memberLabel(r?.key),
      // `testclient` is the literal address FastAPI's TestClient reports, so
      // this is reading the value, not guessing at it. Anything else gets no
      // qualifier rather than an assumed one.
      sub: String(r?.key ?? '') === 'testclient' ? 'pytest' : '',
      value: observations(r),
      tone: String(r?.key ?? '') === 'testclient' ? 'bad' : 'ok'
    }))
  );

  // --------------------------------------------------------------- feed
  const PAGE_SIZES = [25, 50, 100];
  let size = $state(50);
  let offset = $state(0);
  let rows = $state([]);
  let total = $state(null); // null = the API did not say. Never assume 0.
  let unavailable = $state([]);
  let feedStatus = $state('loading'); // loading | ok | missing | error
  let feedErr = $state(null);
  let feedBusy = $state(false);
  let failuresOnly = $state(false);
  let freshKeys = $state(new Set());

  function feedQuery() {
    return buildQuery(f, { limit: size, offset });
  }

  async function loadFeed({ quiet = false } = {}) {
    if (!quiet) feedBusy = true;
    try {
      const j = await getJSON('/admin/activity?' + feedQuery());
      const next = Array.isArray(j?.rows) ? j.rows : [];
      if (quiet) {
        // Only rows that were not on screen a moment ago are marked fresh; the
        // rest must not flash, or every poll repaints the whole feed and the
        // reader loses their place.
        const seen = new Set(rows.map(rowKey));
        freshKeys = new Set(next.map(rowKey).filter((k) => !seen.has(k)));
      } else {
        freshKeys = new Set();
      }
      rows = next;
      total = isNum(j?.total) ? j.total : null;
      unavailable = Array.isArray(j?.sources?.unavailable)
        ? j.sources.unavailable
        : Array.isArray(j?.unavailable_sources)
          ? j.unavailable_sources
          : [];
      feedStatus = 'ok';
    } catch (e) {
      if (quiet) return; // a failed poll must not tear down a working feed
      rows = [];
      total = null;
      unavailable = [];
      if (e instanceof ApiError && e.status === 404) {
        feedStatus = 'missing';
        return;
      }
      feedStatus = 'error';
      feedErr = e;
    } finally {
      feedBusy = false;
    }
  }

  const rowKey = (r) => `${r?.ts}|${r?.source}|${r?.action}|${r?.target}`;

  // Two effects, one fetch each. `loadFeed` is untracked so the dozen pieces of
  // state it reads do not become dependencies and re-fire it on every
  // keystroke, and the feed's key includes the pager so a page change reloads
  // without a second effect racing the first.
  let feedKey = $derived(`${qs}|${offset}|${size}|${nonce}`);
  $effect(() => {
    void qs;
    void nonce;
    untrack(() => {
      offset = 0;
      loadSummary();
    });
  });
  $effect(() => {
    void feedKey;
    untrack(() => loadFeed());
  });

  // ---- live -------------------------------------------------------------
  // Polling, not a socket: this backend has no event stream, and a UI that
  // animates a "live" dot over a feed that never updates is a lie about
  // freshness. It only runs on the first page — prepending onto page 3 would
  // silently shift what is under the reader's cursor.
  let live = $state(false);
  let reduceMotion = $state(false);
  $effect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    reduceMotion = mq.matches;
    const on = () => (reduceMotion = mq.matches);
    mq.addEventListener?.('change', on);
    return () => mq.removeEventListener?.('change', on);
  });
  $effect(() => {
    if (!live || offset !== 0) return;
    const id = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return;
      untrack(() => loadFeed({ quiet: true }));
    }, 15000);
    return () => clearInterval(id);
  });

  let shown = $derived(
    failuresOnly
      ? rows.filter((r) => {
          const s = statusTone(r.status);
          return s === 'danger' || /fail|denied|locked|blocked|error|set_aside|refused/i.test(String(r.action ?? ''));
        })
      : rows
  );
  let hasNext = $derived(total == null ? rows.length === size : offset + size < total);
  let pageNo = $derived(Math.floor(offset / size) + 1);
  let pageCount = $derived(total == null ? null : Math.max(1, Math.ceil(total / size)));

  // -------------------------------------------------------------- format
  function when(ts) {
    if (!ts) return UNKNOWN;
    const dt = new Date(ts);
    if (Number.isNaN(dt.getTime())) return String(ts);
    return dt.toLocaleString(undefined, {
      day: 'numeric',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit'
    });
  }
  function statusTone(s) {
    const v = String(s ?? '').toLowerCase();
    if (!v) return 'neutral';
    if (/(ok|success|succeeded|done|completed|granted|allowed|200)/.test(v)) return 'ok';
    if (/(fail|error|denied|refused|locked|rejected|invalid|5\d\d|4\d\d)/.test(v)) return 'danger';
    if (/(warn|partial|retry|pending|queued|skipped)/.test(v)) return 'warn';
    return 'neutral';
  }
  function pretty(v) {
    if (v == null) return null;
    if (typeof v === 'string') {
      const s = v.trim();
      if (!s) return null;
      try {
        return JSON.stringify(JSON.parse(s), null, 2);
      } catch {
        return s;
      }
    }
    try {
      return JSON.stringify(v, null, 2);
    } catch {
      return String(v);
    }
  }

  // -------------------------------------------------------------- drawer
  let openRow = $state(null);

  function openDetail(row) {
    openRow = row;
  }
  function closeDrawer() {
    openRow = null;
  }
  function rowEnter(e, fn) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      fn(e);
    }
  }

  /** A bucket key is a day only if it looks like one — otherwise the click
      would write a nonsense date range into the shared filter. */
  function pickBucket(i, source) {
    const day = String(overTime.keys[i] ?? '').slice(0, 10);
    const patch = { source };
    if (/^\d{4}-\d{2}-\d{2}$/.test(day)) {
      patch.from = day;
      patch.to = day;
    }
    setParams(patch);
  }

  // Every panel here can hand its filter to Explore.
  function exploreHref(extra = {}) {
    return '?' + buildQuery(f, openSection('explore', extra));
  }
</script>

{#snippet cell(v, mono)}
  {#if v == null || v === ''}
    <span class="text-meta text-ink-3">{UNKNOWN}</span>
  {:else}
    <span class="text-meta text-ink {mono ? 'font-mono text-label text-ink-2' : ''}">{v}</span>
  {/if}
{/snippet}

{#snippet actionCell(row)}
  {@const known = row?.source === 'auth' ? AUTH_EVENT_MAP[String(row?.action ?? '')] : null}
  {#if known}
    <Badge tone={known.tone}>{known.label}</Badge>
  {:else}
    {@render cell(row?.action, true)}
  {/if}
{/snippet}

<!-- --------------------------------- KPIs --------------------------------- -->
<Section
  title="This period at a glance"
  hint="Every figure carries its movement against the immediately preceding window of the same length. No prior window prints “no prior period”, never 0%."
  state={sum}
  retry={loadSummary}
  what="the activity summary"
>
  <div class="grid gap-3 [grid-template-columns:repeat(auto-fit,minmax(212px,1fr))]">
    <!-- `good` is set per metric, never per direction: more events is neither
         good nor bad, more failures is bad, more real-browser traffic is good.
         A green arrow on this row always means "this got better". -->
    <Kpi
      label="Events"
      value={isNum(K.events.value) ? int(K.events.value) : null}
      spark={K.events.spark}
      delta={deltaOf(raw.events)}
      good="none"
      unfiltered={unfilteredOf(d)}
      foot="app + auth + ingest"
      onclick={() => setParams(openSection('trends', { metric: 'events' }))}
    />
    <Kpi
      label="From a real browser"
      value={isNum(K.browser.value) ? int(K.browser.value) : null}
      spark={K.browser.spark}
      tone="info"
      delta={deltaOf(raw.browser)}
      good="up"
      unfiltered={unfilteredOf(d?.clients)}
      foot={browserFoot}
    />
    <Kpi
      label="Failed requests"
      value={isNum(K.failed.value) ? int(K.failed.value) : null}
      spark={K.failed.spark}
      tone="bad"
      delta={deltaOf(raw.failed)}
      good="down"
      unfiltered={unfilteredOf(d?.failures)}
      foot={failFoot}
    />
    <Kpi
      label="Sign-in failures"
      value={isNum(K.signin.value) ? int(K.signin.value) : null}
      spark={K.signin.spark}
      tone="warn"
      delta={deltaOf(raw.signinFail)}
      good="down"
      unfiltered={unfilteredOf(d?.signins)}
      foot={signinFoot}
      onclick={() => setParams(openSection('audit'))}
    />
    <Kpi
      label="Files set aside"
      value={isNum(K.setAside.value) ? int(K.setAside.value) : null}
      spark={K.setAside.spark}
      tone="warn"
      delta={deltaOf(raw.setAside)}
      good="down"
      unfiltered={unfilteredOf(raw.setAside)}
      foot={setAsideFoot}
    />
    <Kpi
      label="Distinct actors"
      value={isNum(K.actors.value) ? int(K.actors.value) : null}
      spark={K.actors.spark}
      tone="info"
      delta={deltaOf(raw.actors)}
      good="none"
      unfiltered={unfilteredOf(raw.actors)}
      foot="people and test runners that produced at least one event"
    />
  </div>
</Section>

<!-- ---------------------------- over time --------------------------------- -->
<Section
  title="Activity over time"
  hint="Composition per bucket — the mix is the story, which is why this is one stacked bar per day and not three competing lines."
  state={trend}
  retry={loadSummary}
  what="activity over time"
>
  <div class="mb-2 flex justify-end">
    <a
      href={exploreHref({ measure: 'events', by: 'action', sub: 'source' })}
      class="cursor-pointer text-meta font-semibold text-accent hover:underline">Explore ›</a
    >
  </div>
  <StackedBars
    labels={overTime.labels}
    series={overTime.series}
    onpick={(i, key) => pickBucket(i, key)}
  />
</Section>

<!-- ------------------------------ actions --------------------------------- -->
<Section
  title="What is being done"
  hint="Ranked by how OFTEN each action happened, not by how many days it happened on. Click one to filter every panel on every tab."
  state={acts}
  retry={loadSummary}
  what="the action ranking"
>
  <RankBars
    rows={actionRows}
    onpick={(r) => (r.key ? setParams({ action: r.key, offset: null }) : null)}
    empty="No actions recorded in this range."
  />
  {#if actionsTruncated}
    <p class="mt-2 text-meta text-ink-3">
      Top {actionRows.length} only — there are more actions below this cut.
      <a href={exploreHref({ measure: 'events', by: 'action', top: 50 })} class="text-accent hover:underline"
        >See the full ranking in Explore ›</a
      >
    </p>
  {/if}
</Section>

<div class="grid gap-5 lg:grid-cols-2">
  <Section
    title="Response status"
    hint="By class. Auth and ingest events carry no HTTP status at all, so they group as “not recorded” rather than being dropped — otherwise the slices stop summing to the whole."
    state={stat}
    retry={loadSummary}
    what="the status split"
  >
    <Donut slices={statusSlices} />
    {#if failFoot}
      <p class="mt-2 text-meta text-ink-3">Within the failures: {failFoot}.</p>
    {/if}
  </Section>

  <Section
    title="Where it came from"
    hint="`testclient` is the address FastAPI's own test client reports — the pytest suite, not a person. It is kept visible rather than filtered out, because it is most of the traffic."
    state={ips}
    retry={loadSummary}
    what="the source addresses"
  >
    <RankBars rows={ipRows} empty="No addresses recorded in this range." />
  </Section>
</div>

<!-- ------------------------------ live feed ------------------------------- -->
<Section
  title="Live feed"
  hint="Newest first. Every row is a record the backend stored — click one for the whole of it."
>
  {#if unavailable.length}
    <div class="mb-3 flex items-start gap-3 rounded-panel border border-warning bg-warning-soft px-4 py-3.5">
      <TriangleAlert size={17} class="mt-0.5 flex-shrink-0 text-warning" />
      <div>
        <div class="text-body-sm font-semibold text-ink">This is a partial history</div>
        <p class="mt-1 text-meta leading-relaxed text-ink-2">
          The backend could not read
          <span class="font-mono text-meta">{unavailable.join(', ')}</span>, so nothing from
          {unavailable.length === 1 ? 'that feed' : 'those feeds'} appears below. What you see is every other
          source, not the whole story.
        </p>
      </div>
    </div>
  {/if}

  {#if feedStatus === 'missing'}
    <div class="flex items-start gap-3 rounded-panel border border-warning bg-warning-soft px-4 py-3.5">
      <TriangleAlert size={17} class="mt-0.5 flex-shrink-0 text-warning" />
      <div>
        <div class="text-body-sm font-semibold text-ink">This backend has no activity feed yet</div>
        <p class="mt-1 max-w-[70ch] text-meta leading-relaxed text-ink-2">
          <span class="font-mono text-meta">/admin/activity</span> is not served by the API this console is
          talking to, so there is nothing to read here. That is different from an empty log: events may well
          have been recorded, but this build cannot expose them.
        </p>
      </div>
    </div>
  {:else if feedStatus === 'error'}
    <ErrorState error={feedErr} retry={loadFeed} what="the activity feed" />
  {:else}
    <div class="elev overflow-hidden rounded-panel border border-line bg-surface">
      <div class="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5">
        <Activity size={15} class="text-ink-3" />
        <span class="text-body-sm font-semibold text-ink">Event feed</span>
        <button
          onclick={() => (live = !live)}
          aria-pressed={live}
          disabled={offset !== 0}
          title={offset !== 0 ? 'Live only runs on the first page' : 'Re-reads the feed every 15 seconds'}
          class="ml-2 flex min-h-[32px] cursor-pointer items-center gap-1.5 rounded-full border px-2.5 text-meta font-medium disabled:cursor-not-allowed disabled:opacity-50
                 {live ? 'border-success bg-success-soft text-success' : 'border-line bg-surface text-ink-2 hover:bg-surface-2'}"
        >
          {#if live}
            <span class="livedot" class:still={reduceMotion}></span> Live
          {:else}
            <Play size={12} /> Go live
          {/if}
        </button>
        <button
          onclick={() => (failuresOnly = !failuresOnly)}
          aria-pressed={failuresOnly}
          class="flex min-h-[32px] cursor-pointer items-center gap-1.5 rounded-full border px-2.5 text-meta font-medium
                 {failuresOnly ? 'border-danger bg-danger-soft text-danger' : 'border-line bg-surface text-ink-2 hover:bg-surface-2'}"
        >
          Failures only
        </button>
        <span class="ml-auto text-meta text-ink-3">
          {#if failuresOnly}
            filtered in the browser, over this page only
          {:else}
            click any row for the full record
          {/if}
        </span>
      </div>

      {#if f.source === 'auth'}
        <!-- The five quick filters the security-log page offered, listed from a
             constant so an event with no rows in view is still selectable. -->
        <div class="flex flex-wrap items-center gap-1.5 border-b border-line px-4 py-2">
          {#each AUTH_EVENTS as e (e.key)}
            <button
              onclick={() => setParams({ action: f.action === e.key ? null : e.key, offset: null })}
              aria-pressed={f.action === e.key}
              class="min-h-[32px] cursor-pointer rounded-full border px-2.5 text-meta font-medium transition-colors
                     {f.action === e.key
                ? 'border-accent bg-accent text-on-accent'
                : 'border-line bg-surface text-ink-2 hover:bg-surface-2'}"
            >
              {e.label}
            </button>
          {/each}
        </div>
      {/if}

      <div class="max-h-[calc(100vh-330px)] overflow-auto">
        <table class="tbl">
          <thead>
            <tr>
              <th>When</th>
              <th>Source</th>
              <th>Actor</th>
              <th>Action</th>
              <th>Target</th>
              <th>Status</th>
              <th>IP</th>
            </tr>
          </thead>
          <tbody>
            {#if feedStatus === 'loading'}
              {#each Array(8) as _, i (i)}
                <tr><td colspan="7"><div class="skel" style="height:15px"></div></td></tr>
              {/each}
            {:else if shown.length === 0}
              <tr>
                <td colspan="7" class="py-14 text-center">
                  <div class="text-body-sm font-semibold text-ink">
                    {failuresOnly
                      ? 'Nothing failed on this page'
                      : 'No events match these filters'}
                  </div>
                  <p class="mx-auto mt-1.5 max-w-[46ch] text-meta leading-relaxed text-ink-3">
                    The feed answered; it simply has nothing for this combination. That is not the same as
                    nothing having happened.
                  </p>
                </td>
              </tr>
            {:else}
              {#each shown as r, i (rowKey(r) + '|' + i)}
                <tr
                  role="button"
                  tabindex="0"
                  class:fresh={freshKeys.has(rowKey(r)) && !reduceMotion}
                  onclick={() => openDetail(r)}
                  onkeydown={(e) => rowEnter(e, () => openDetail(r))}
                >
                  <td class="tnum whitespace-nowrap font-mono text-label text-ink-2">{when(r.ts)}</td>
                  <td>
                    <span
                      class="inline-flex items-center gap-1 rounded-panel px-2 py-0.5 text-label font-medium {r.source
                        ? 'bg-accent-soft text-accent'
                        : 'bg-surface-2 text-ink-2'}"
                    >
                      {r.source ?? 'not recorded'}
                    </span>
                  </td>
                  <td class="max-w-[180px] truncate">{@render cell(r.actor)}</td>
                  <td class="max-w-[180px] truncate">{@render actionCell(r)}</td>
                  <td class="max-w-[240px] truncate">{@render cell(r.target)}</td>
                  <td>
                    {#if r.status == null || r.status === ''}
                      <span class="text-meta text-ink-3">{UNKNOWN}</span>
                    {:else}
                      <Badge tone={statusTone(r.status)}>{r.status}</Badge>
                    {/if}
                  </td>
                  <td class="whitespace-nowrap">{@render cell(r.ip, true)}</td>
                </tr>
              {/each}
            {/if}
          </tbody>
        </table>
      </div>

      <div
        class="flex flex-wrap items-center gap-3 border-t border-line px-4 py-2.5 text-meta text-ink-3"
      >
        <span>
          Showing <b class="tnum text-ink">{rows.length ? offset + 1 : 0}–{offset + rows.length}</b>
          {#if total != null}
            of <b class="tnum text-ink">{int(total)}</b>
          {:else}
            <span class="italic"> — the API did not report a total</span>
          {/if}
        </span>
        <select
          bind:value={size}
          aria-label="Page size"
          class="min-h-[32px] cursor-pointer rounded-panel border border-line bg-surface px-2 text-meta text-ink"
        >
          {#each PAGE_SIZES as n (n)}<option value={n}>{n} / page</option>{/each}
        </select>
        <div class="ml-auto flex items-center gap-1.5">
          <button
            onclick={() => (offset = Math.max(0, offset - size))}
            disabled={offset === 0 || feedBusy}
            aria-label="Previous page"
            class="flex h-9 w-9 cursor-pointer items-center justify-center rounded-panel border border-line text-ink-2 hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ChevronLeft size={15} />
          </button>
          <span class="tnum text-ink">
            page {pageNo}{#if pageCount != null} of {pageCount}{/if}
          </span>
          <button
            onclick={() => (offset += size)}
            disabled={!hasNext || feedBusy}
            aria-label="Next page"
            class="flex h-9 w-9 cursor-pointer items-center justify-center rounded-panel border border-line text-ink-2 hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ChevronRight size={15} />
          </button>
        </div>
      </div>
    </div>

    <div class="mt-3 flex items-start gap-2 px-1 text-label leading-relaxed text-ink-3">
      <Info size={13} class="mt-0.5 flex-none" />
      <span>
        A field the backend never captured shows as <span class="font-mono">—</span> rather than a
        plausible-looking blank or a zero, and a source it could not read is named above rather than left
        out silently.
      </span>
    </div>
  {/if}
</Section>

<!-- -------------------------------- drawer -------------------------------- -->
{#if openRow}
  <!-- Pointer affordance only. Escape is the keyboard route out and the
       drawer's use:dialog owns it. -->
  <div class="fixed inset-0 z-40 cursor-default bg-black/35" onclick={closeDrawer} aria-hidden="true"></div>
  <div
    use:dialog={{ onclose: closeDrawer }}
    role="dialog"
    aria-modal="true"
    aria-label="Activity record"
    tabindex="-1"
    class="fixed top-0 right-0 bottom-0 z-50 flex w-[560px] max-w-[94vw] flex-col border-l border-line bg-surface shadow-[var(--shadow-pop)]"
  >
    <div class="flex items-center gap-3 border-b border-line px-5 py-4">
      <div class="min-w-0 flex-1">
        <p class="text-micro font-bold tracking-wider text-ink-3 uppercase">Activity record</p>
        <h2 class="mt-0.5 truncate text-body font-semibold text-ink">{openRow.action ?? 'Event'}</h2>
      </div>
      <button
        onclick={closeDrawer}
        aria-label="Close"
        class="flex h-9 w-9 cursor-pointer items-center justify-center rounded-panel text-ink-3 hover:bg-surface-2 hover:text-ink"
      >
        <X size={18} />
      </button>
    </div>

    <div class="flex-1 overflow-y-auto px-5 py-4">
      <div class="mb-4">
        <div class="mb-1.5 text-micro font-bold tracking-[0.05em] text-ink-3 uppercase">Record</div>
        <dl class="grid grid-cols-[112px_1fr] gap-x-3 gap-y-2 text-meta">
          <dt class="text-ink-3">When</dt>
          <dd class="tnum font-mono text-ink">{when(openRow.ts)}</dd>
          <dt class="text-ink-3">Source</dt>
          <dd>{@render cell(openRow.source)}</dd>
          <dt class="text-ink-3">Actor</dt>
          <dd>{@render cell(openRow.actor)}</dd>
          <dt class="text-ink-3">Action</dt>
          <dd class="flex items-center gap-2">
            {@render actionCell(openRow)}
            {#if openRow.source === 'auth' && AUTH_EVENT_MAP[String(openRow.action ?? '')]}
              <span class="font-mono text-label text-ink-3">{openRow.action}</span>
            {/if}
          </dd>
          <dt class="text-ink-3">Target</dt>
          <dd>{@render cell(openRow.target)}</dd>
          <dt class="text-ink-3">Status</dt>
          <dd>
            {#if openRow.status == null || openRow.status === ''}
              <span class="text-meta text-ink-3">{UNKNOWN}</span>
            {:else}
              <Badge tone={statusTone(openRow.status)}>{openRow.status}</Badge>
            {/if}
          </dd>
          <dt class="text-ink-3">IP</dt>
          <dd>{@render cell(openRow.ip, true)}</dd>
        </dl>
      </div>

      <div class="mb-4">
        <div class="mb-1.5 text-micro font-bold tracking-[0.05em] text-ink-3 uppercase">Detail</div>
        {#if pretty(openRow.detail) == null}
          <p class="rounded-card bg-surface-2 px-3 py-2.5 text-body-sm text-ink-3 italic">
            No detail was stored with this event.
          </p>
        {:else}
          <pre
            class="max-h-[46vh] overflow-auto rounded-card border border-line bg-surface-2 px-3 py-2.5 font-mono text-label leading-relaxed whitespace-pre-wrap text-ink">{pretty(
              openRow.detail
            )}</pre>
        {/if}
      </div>

      <div>
        <div class="mb-2 text-micro font-bold tracking-[0.05em] text-ink-3 uppercase">Actions</div>
        <div class="flex flex-wrap gap-2">
          <button
            onclick={() => navigator.clipboard?.writeText(JSON.stringify(openRow, null, 2))}
            class="flex min-h-[36px] cursor-pointer items-center gap-1.5 rounded-card border border-line bg-surface px-3 text-meta font-medium text-ink-2 hover:border-accent hover:text-accent"
          >
            <Copy size={14} /> Copy record
          </button>
          {#if openRow.actor}
            <button
              onclick={() => {
                const a = String(openRow.actor);
                closeDrawer();
                setParams({ actor: a, offset: null });
              }}
              class="flex min-h-[36px] cursor-pointer items-center gap-1.5 rounded-card border border-line bg-surface px-3 text-meta font-medium text-ink-2 hover:border-accent hover:text-accent"
            >
              <Search size={14} /> Filter to this actor
            </button>
          {/if}
          {#if openRow.action}
            <a
              href={exploreHref({ measure: 'events', by: 'action', action: String(openRow.action) })}
              class="flex min-h-[36px] cursor-pointer items-center gap-1.5 rounded-card border border-line bg-surface px-3 text-meta font-medium text-ink-2 hover:border-accent hover:text-accent"
            >
              <ListFilter size={14} /> Explore this action
            </a>
          {/if}
        </div>
      </div>
    </div>
  </div>
{/if}

<style>
  /* The live dot and the new-row flash are the only motion on this tab, and
     both stop under prefers-reduced-motion: a feed that pulses at somebody who
     asked for stillness is worse than one that does not say it is live. */
  .livedot {
    width: 7px;
    height: 7px;
    border-radius: 999px;
    background: var(--color-success);
    animation: pulse 1.6s ease-in-out infinite;
  }
  .livedot.still {
    animation: none;
  }
  @keyframes pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.25;
    }
  }
  tr.fresh {
    animation: flash 1.4s ease-out 1;
  }
  @keyframes flash {
    from {
      background: var(--color-accent-soft);
    }
    to {
      background: transparent;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .livedot,
    tr.fresh {
      animation: none;
    }
  }
</style>
