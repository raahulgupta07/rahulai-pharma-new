<script>
  // Trends — how this period compares with the one before it.
  //
  // Two rules from the contract are visible in the drawing itself:
  //
  //   · The current period is a VOLUME, so it gets a line and an area fill. The
  //     comparison line is PLAIN — an area under it would read as a second
  //     quantity stacked against the first rather than as a reference.
  //   · A movement is printed absolute-first. "+3" is the fact; "300%" is the
  //     decoration, and on a base of 1 it is noise (addendum §B).
  //
  // The heatmap carries the timezone caveat, but only when the data actually
  // came back UTC-bucketed. Once `tz` is honoured the warning disappears —
  // a banner that never goes away is a banner nobody reads.
  import { untrack } from 'svelte';
  import Section from '$lib/charts/Section.svelte';
  import LineChart from '$lib/charts/LineChart.svelte';
  import Heatmap from '$lib/charts/Heatmap.svelte';
  import WarnBar from '$lib/charts/WarnBar.svelte';
  import DeltaChip from '$lib/charts/DeltaChip.svelte';
  import { sparkPath } from '$lib/charts/geom.js';
  import {
    UNKNOWN,
    isNum,
    int,
    bucketLabel,
    fetchSection,
    loadingSection,
    buildQuery,
    fromRows,
    shouldWarnTz,
    tzEcho,
    COLOR,
    openSection
  } from './shared.js';
  import GapCard from '$lib/charts/GapCard.svelte';

  let { qs, f, tz, nonce, setParams, sp, reportTz } = $props();

  // The measures are the COLUMNS `/activity/trends` actually returns on each
  // bucket row. A menu offering "Spend" or "Questions" against a backend that
  // does not serve them would be a control that silently does nothing — the
  // failure mode this codebase already has two recorded instances of.
  const MEASURES = [
    { key: 'events', label: 'Events', color: COLOR.accent },
    { key: 'failed', label: 'Failures', color: COLOR.bad },
    { key: 'app', label: 'Admin actions', color: COLOR.app },
    { key: 'auth', label: 'Auth', color: COLOR.auth },
    { key: 'ingest', label: 'Ingest', color: COLOR.ingest }
  ];
  // Only one comparison window is served today. A second button that returns
  // the same series would be a lie about what was compared.
  const COMPARES = [{ key: 'prev', label: 'vs previous period' }];

  // Both controls live in the URL, so a trend somebody found is a link they can
  // send. Anything unrecognised falls back rather than 400-ing the page.
  let metric = $derived(MEASURES.some((m) => m.key === sp.get('metric')) ? sp.get('metric') : 'events');
  let compare = $derived(COMPARES.some((c) => c.key === sp.get('compare')) ? sp.get('compare') : 'prev');

  let sec = $state(loadingSection('/admin/activity/trends'));
  let d = $derived(sec.data ?? {});

  async function load() {
    sec = loadingSection('/admin/activity/trends');
    sec = await fetchSection(
      '/admin/activity/trends?' + buildQuery(f, { measure: metric, compare })
    );
    reportTz?.(tzEcho(sec.data));
  }
  $effect(() => {
    void qs;
    void nonce;
    void metric;
    void compare;
    untrack(() => load());
  });

  let measureLabel = $derived(MEASURES.find((m) => m.key === metric)?.label ?? metric);
  let compareLabel = $derived(COMPARES.find((c) => c.key === compare)?.label ?? 'vs previous period');

  // ---- over time -------------------------------------------------------
  //
  // `/activity/trends` returns flat rows — one per bucket, a column per series
  // — so the selected measure is a column, plotted as a volume with its fill.
  let col = $derived(MEASURES.find((m) => m.key === metric) ?? MEASURES[0]);
  let plot = $derived(
    fromRows(d?.series, [{ key: col.key, label: col.label, color: col.color, area: true }])
  );
  let labels = $derived(plot.labels);
  let bucketKeys = $derived(plot.keys);

  /**
   * The ghost line is drawn ONLY when it belongs to the measure on screen.
   *
   * The backend now cuts `previous` for the selected measure and labels it with
   * its own `measure`, so this is no longer a workaround — it is a standing
   * assertion that the two agree. Keeping it costs one comparison and catches a
   * rewiring mistake that would otherwise draw a plausible-looking chart of two
   * different quantities, which is the worst kind of chart bug: nothing about
   * it looks wrong.
   */
  let prevMeasure = $derived(
    typeof d?.previous?.measure === 'string'
      ? d.previous.measure
      : typeof d?.measure === 'string'
        ? d.measure
        : 'events'
  );
  let prevMatches = $derived(prevMeasure === metric);
  let prevSeries = $derived.by(() => {
    const p = d?.previous;
    const values = Array.isArray(p?.values) ? p.values.map((v) => (isNum(v) ? v : null)) : [];
    // An empty array is "there was no prior window" — not a row of zeroes, which
    // would draw a flat line reading as "nothing happened before this".
    if (!values.length || !prevMatches) return [];
    return [
      {
        key: 'previous',
        label: String(p?.label ?? compareLabel),
        color: COLOR.muted,
        area: false, // a reference line is not a second volume
        values
      }
    ];
  });
  let overTime = $derived([...plot.series, ...prevSeries]);

  // ---- success rate ----------------------------------------------------
  //
  // Computed from two counts measured in the SAME bucket — arithmetic on the
  // server's own numbers, not a re-bucketing — and only where both are known.
  // A bucket missing either side stays a gap.
  let rate = $derived.by(() => {
    const rows = Array.isArray(d?.series) ? d.series : [];
    let denom = 0;
    const values = rows.map((r) => {
      const n = isNum(r?.events) ? r.events : null;
      const bad = isNum(r?.failed) ? r.failed : null;
      if (n === null || bad === null || n === 0) return null;
      denom += n;
      return ((n - bad) / n) * 100;
    });
    return {
      labels: rows.map((r, i) => plot.labels[i] ?? ''),
      values,
      denom: denom > 0 ? denom : null,
      computable: values.some((v) => v !== null)
    };
  });

  // ---- movers ----------------------------------------------------------
  // The mover rows carry their own movement block straight to DeltaChip, which
  // owns the absolute-first rule and the >999% clamp. `good` is 'none' here on
  // purpose: this list mixes "questions answered" with "lock-outs", and the
  // server does not say which direction is an improvement for a given key, so
  // colouring by sign would call a rise in lock-outs good.
  let movers = $derived(
    (Array.isArray(d?.movers) ? d.movers : []).map((m, i) => {
      // `spark` arrives as `[{t, n}]`, not as bare numbers.
      const spark = (Array.isArray(m?.spark) ? m.spark : [])
        .map((p) => (isNum(p) ? p : isNum(p?.n) ? p.n : null))
        .filter(isNum);
      const unit = typeof m?.unit === 'string' ? m.unit : '';
      return {
        key: String(m?.key ?? i),
        label: String(m?.label ?? m?.key ?? UNKNOWN),
        block: {
          value: isNum(m?.value) ? m.value : null,
          prev: isNum(m?.prev) ? m.prev : null,
          delta: isNum(m?.delta) ? m.delta : null,
          delta_pct: isNum(m?.delta_pct) ? m.delta_pct : null
        },
        fmt: (v) => `${v > 0 ? '+' : v < 0 ? '−' : '±'}${Math.abs(v).toLocaleString()}${unit}`,
        up: isNum(m?.delta) ? m.delta > 0 : null,
        path: sparkPath(spark)
      };
    })
  );

  // With a single bucket of history there is no line to draw and no earlier
  // window to compare against, so every row reads "no series" and "no prior
  // period". That is the honest answer, but a column of identical grey text
  // reads as a failed load, so the list says which it is underneath.
  let moversThin = $derived(
    movers.length > 0 && movers.every((m) => !m.path || !isNum(m.block.delta))
  );

  // ---- heatmap ---------------------------------------------------------
  //
  // Drawn EXACTLY as sent: `cols` across the top, `rows` down the side,
  // `cells[i]` under `cols[i]`. Nothing is transposed, summed or re-ordered
  // here, which is what lets this survive the axes being swapped upstream — the
  // matrix has been both hours × days and bands × days inside one afternoon,
  // and a renderer that assumed either one would draw a grid that looks
  // completely plausible with both axes wrong.
  //
  // What the panel DOES have to know is which axis carries the dates, because
  // that decides what a click means and how the caption reads. It is detected
  // from the key format rather than assumed, since a `pickDay` pointed at the
  // wrong axis silently does nothing at all.
  const DAY_KEY = /^\d{4}-\d{2}-\d{2}/;
  const dayish = (k) => DAY_KEY.test(String(k ?? ''));
  // A date-shaped key is formatted by the page's own bucket formatter, so
  // "17 Aug" reads the same here as on the axis above; anything else keeps the
  // label the backend gave it ("00-06" is not a date and must never reach a
  // date formatter).
  const axisLabel = (item, i) => {
    const key = String(item?.key ?? i);
    return dayish(key) ? bucketLabel(key) : String(item?.label ?? key);
  };

  let heatCols = $derived(
    (Array.isArray(d?.heatmap?.cols) ? d.heatmap.cols : []).map((c, i) => ({
      key: String(c?.key ?? i),
      label: axisLabel(c, i)
    }))
  );
  let heatRows = $derived(
    (Array.isArray(d?.heatmap?.rows) ? d.heatmap.rows : []).map((r, i) => ({
      key: String(r?.key ?? i),
      label: axisLabel(r, i),
      cells: (Array.isArray(r?.cells) ? r.cells : []).map((c) => ({
        value: isNum(c) ? c : isNum(c?.value) ? c.value : null
      }))
    }))
  );
  let heatUsable = $derived(heatCols.length > 0 && heatRows.length > 0);
  let colsAreDays = $derived(heatCols.length > 0 && heatCols.every((c) => dayish(c.key)));
  let rowsAreDays = $derived(heatRows.length > 0 && heatRows.every((r) => dayish(r.key)));
  /** Filter to the day the reader clicked, whichever axis is carrying days. */
  function pickCell(r, c) {
    if (colsAreDays) pickDay(c?.key);
    else if (rowsAreDays) pickDay(r?.key);
  }
  // Roughly where the fixed-width cells stop fitting the content area at 1280px.
  // Past it the grid scrolls inside its own container — the page never does —
  // and a scroll nobody knows about is the same as missing data.
  let heatScrolls = $derived(heatCols.length > 12);
  let colNoun = $derived(colsAreDays ? 'days' : rowsAreDays ? 'hours' : 'columns');
  // Read from the ECHO. An endpoint that never declared `tz` drops it silently
  // and answers 200 with UTC buckets, so "we sent it" proves nothing.
  let echo = $derived(tzEcho(d));
  // The caveat belongs to every bucket on the tab, not only to the heatmap, and
  // it goes ABOVE the charts: a caveat below the fold is a caveat nobody reads.
  let warnTz = $derived(sec.status === 'ok' && plot.labels.length > 0 && shouldWarnTz(d, tz));
  let localMidnightOffset = $derived(
    new Date(Date.UTC(2000, 0, 1)).toLocaleTimeString(undefined, {
      hour: '2-digit',
      minute: '2-digit'
    })
  );

  /**
   * A column key is a day only if it looks like one. A heatmap whose columns
   * are something else (a store, a model) must not silently write a nonsense
   * date range into the shared filter.
   */
  function pickDay(key) {
    const day = String(key ?? '').slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return;
    setParams({ from: day, to: day });
  }

  function exploreHref(extra = {}) {
    return '?' + buildQuery(f, openSection('explore', extra));
  }
</script>

<!-- ------------------------------ controls -------------------------------- -->
<div class="mb-5 flex flex-wrap items-center gap-x-4 gap-y-2">
  <span class="text-label font-bold tracking-[0.05em] text-ink-3 uppercase">Measure</span>
  <div class="flex flex-wrap gap-1" role="group" aria-label="Measure">
    {#each MEASURES as m (m.key)}
      <button
        onclick={() => setParams({ metric: m.key })}
        aria-pressed={metric === m.key}
        class="min-h-[36px] cursor-pointer rounded-card px-3 text-meta font-medium {metric === m.key
          ? 'bg-accent text-on-accent'
          : 'border border-line bg-surface text-ink-2 hover:border-accent hover:text-accent'}"
      >
        {m.label}
      </button>
    {/each}
  </div>

  <span class="text-label font-bold tracking-[0.05em] text-ink-3 uppercase">Compare</span>
  <div class="flex flex-wrap gap-1" role="group" aria-label="Comparison window">
    {#each COMPARES as c (c.key)}
      <button
        onclick={() => setParams({ compare: c.key })}
        aria-pressed={compare === c.key}
        class="min-h-[36px] cursor-pointer rounded-card px-3 text-meta font-medium {compare === c.key
          ? 'bg-accent text-on-accent'
          : 'border border-line bg-surface text-ink-2 hover:border-accent hover:text-accent'}"
      >
        {c.label}
      </button>
    {/each}
  </div>
</div>

{#if warnTz}
  <WarnBar>
    <b>
      {#if echo}
        These buckets were cut in {echo}, not in {tz}.
      {:else}
        This backend echoed no timezone, so these buckets are cut on UTC midnight while every label on
        this tab reads local.
      {/if}
    </b>
    Every “day” below therefore starts at your local
    <span class="tnum">{localMidnightOffset}</span>, not at midnight, and that first slice of each
    morning sits in the previous day’s column. Every request from this page already sends
    <span class="font-mono text-meta">tz={tz}</span> — an endpoint that has not declared the parameter
    drops it silently and still answers 200, which is why this reads the response rather than the
    request. It clears itself the moment the zone comes back echoed.
  </WarnBar>
{/if}

<Section
  title="{measureLabel} over time, {compareLabel}"
  hint="The current period is a volume, so it carries an area fill. The comparison is a plain line — a fill under a reference would read as a second quantity."
  state={sec}
  retry={load}
  what="the trend series"
>
  <div class="mb-2 flex justify-end">
    <a
      href={exploreHref({ measure: metric, by: 'action', rollup: 'day' })}
      class="cursor-pointer text-meta font-semibold text-accent hover:underline">Explore ›</a
    >
  </div>
  <LineChart
    {labels}
    series={overTime}
    onpick={(i) => pickDay(bucketKeys[i])}
    pickLabel={(i) => `Filter every panel to ${labels[i]}`}
  />
  {#if !prevMatches && sec.status === 'ok'}
    <p class="mt-2 text-meta text-ink-3">
      No comparison line: this backend computes the previous period for
      <b>{MEASURES.find((m) => m.key === prevMeasure)?.label ?? prevMeasure}</b>, and drawing that under
      <b>{measureLabel}</b> would put two different quantities on one chart.
    </p>
  {/if}
</Section>

<div class="grid gap-5 lg:grid-cols-2">
  <Section
    title="Movers"
    hint="Ranked by how much they changed, not by how big they are — and it includes things that stopped happening, which a top-N over this period alone cannot show."
    state={sec}
    retry={load}
    what="the movers list"
  >
    {#if movers.length === 0}
      <p
        class="rounded-card border border-dashed border-line-2 bg-surface px-4 py-6 text-center text-body-sm text-ink-3"
      >
        Nothing moved enough to rank, or there is no prior window to compare against.
      </p>
    {:else}
      <div class="rounded-card border border-line bg-surface px-2 py-1.5">
        {#each movers as m (m.key)}
          <a
            href={exploreHref({ measure: metric, by: 'action', action: m.key })}
            class="grid min-h-[44px] w-full grid-cols-[minmax(110px,1fr)_92px_auto] items-center gap-3 rounded-panel px-2 py-1.5 text-left hover:bg-accent-soft
                   focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            <span class="truncate text-body-sm text-ink-2" title={m.label}>{m.label}</span>
            {#if m.path}
              <svg class="h-[26px] w-[92px]" viewBox="0 0 150 30" preserveAspectRatio="none" aria-hidden="true">
                <path
                  d={m.path.line}
                  fill="none"
                  stroke={m.up === null ? 'var(--color-line-2)' : m.up ? 'var(--color-accent)' : 'var(--color-ink-3)'}
                  stroke-width="1.5"
                  vector-effect="non-scaling-stroke"
                />
              </svg>
            {:else}
              <span class="text-label text-ink-3" title="Fewer than two buckets to draw a line from"
                >no series</span
              >
            {/if}
            <span class="tnum flex justify-end whitespace-nowrap">
              <DeltaChip delta={m.block} good="none" fmt={m.fmt} note="" />
            </span>
          </a>
        {/each}
      </div>
      {#if moversThin}
        <p class="mt-2 text-meta leading-relaxed text-ink-3">
          <b>This list loaded correctly.</b> A sparkline needs at least two buckets to draw a line
          between, and “no prior period” means there was no earlier window of the same length to compare
          against — this range holds one. Both fill in on their own once there is more history; neither
          is a failed request, and the rows are still selectable.
        </p>
      {:else}
        <p class="mt-2 text-meta text-ink-3">
          Direction is an arrow, not a verdict: a rise in lock-outs and a rise in questions answered are
          both “up”, this list mixes the two, and only the label tells you which one you wanted.
        </p>
      {/if}
    {/if}
  </Section>

  <Section
    title="Success rate"
    hint={rate.denom != null
      ? `Events that did not fail, as a share of the ${int(rate.denom)} events in this range — computed per bucket from the two counts the backend measured in it.`
      : 'Events that did not fail, per bucket.'}
    state={sec}
    retry={load}
    what="the success rate"
  >
    {#if rate.computable}
      <LineChart
        labels={rate.labels}
        series={[
          {
            key: 'success',
            label: 'not-failed share',
            color: COLOR.ok,
            area: false, // a RATE never gets an area fill
            values: rate.values
          }
        ]}
        fmt={(v) => `${Math.round(v)}%`}
      />
      <p class="mt-2 text-meta leading-relaxed text-ink-3">
        A bucket with no events has no rate and is left as a gap — 0 events is not a 0% success rate,
        and drawing it on the floor would invent the worst possible day.
      </p>
    {:else}
      <p
        class="rounded-card border border-dashed border-line-2 bg-surface px-4 py-6 text-center text-body-sm text-ink-3"
      >
        No bucket in this range reported both an event count and a failure count, so there is nothing to
        take a share of.
      </p>
    {/if}
  </Section>
</div>

<Section
  title="When {measureLabel.toLowerCase()} happen"
  hint="Hour of day × day, single-hue ramp. A rainbow ramp would imply category boundaries between the colours, and a magnitude has none. It follows the measure control above."
  state={sec}
  retry={load}
  what="the hour-by-day heatmap"
>
  {#if heatUsable}
    {#if heatScrolls}
      <!-- Said ABOVE the grid, not under it. macOS hides scrollbars until you
           scroll, so the only affordance a wide grid has by default is one the
           reader cannot see — and a column they never knew was there is
           indistinguishable from data that is missing. -->
      <p class="mb-2 text-meta font-medium text-ink-2">
        ← → {heatCols.length}
        {colNoun}: wider than the card, so the grid scrolls sideways. Nothing is cut.
      </p>
    {/if}
    <!-- A measured zero PRINTS as 0. Blanking it would make "the query looked
         and found nothing" identical to "no cell here at all", which is the one
         distinction this grid exists to keep. -->
    <Heatmap cols={heatCols} rows={heatRows} cellLabel={(v) => String(v)} onpick={pickCell} />
    <p class="mt-2 text-meta leading-relaxed text-ink-3">
      {#if colsAreDays}
        Time of day down the side, days across the top.
      {:else if rowsAreDays}
        Days down the side, time of day across the top.
      {/if}
      Both axes are fixed rather than derived from what happened, so a quiet period still has its cell
      and a silent day still has its own row or column — which is what keeps two days comparable at the
      same position. A cell reading <b>0</b> is a measured zero, the query ran and found nothing; a blank
      cell is a period this window does not cover. Those are different facts and they do not look alike
      here.
    </p>
  {:else if sec.status === 'ok'}
    <!-- Not "no data" — the endpoint does not serve this block at all yet, and
         those two must never look the same. -->
    <GapCard
      title="The hour-by-day matrix is not served yet"
      body="/admin/activity/trends answers with per-bucket rows but no `heatmap` block, so there is nothing to draw. This is deliberately not derived in the browser from the hourly rollup: getting “hour of day” right depends on how the backend serialises its bucket timestamps, and guessing at that is the same timezone defect one layer further out."
      sql={'GET /admin/activity/trends → heatmap: {\n  cols: [{key: "2026-08-17", label: "17 Aug"}],   // days\n  rows: [{key: "00-06", label: "00-06",           // six-hour bands\n          cells: [{value: 6}]}]                    // one per col, in order\n}\nbucketed with date_trunc(\'hour\', ts AT TIME ZONE $tz)'}
    />
  {/if}
</Section>
