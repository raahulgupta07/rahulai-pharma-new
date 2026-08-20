<script>
  // Explore — the pivot.
  //
  // Ten fixed charts answer ten questions somebody guessed in advance. This
  // tab answers the eleventh: `[measure] by [dimension] (+ subgroup), rolled up
  // [daily|hourly|weekly], top N` drives one chart AND one table, and every
  // panel on the other three tabs links here with its filters already applied.
  //
  // Every control is in the URL, so a pivot somebody built is a link they can
  // send. The server whitelists `measure` and `by` and 400s on anything else
  // (addendum §C) — so an unknown combination surfaces as an honest failure
  // panel here rather than as a chart of something else.
  //
  // The table is where "one spike or a habit?" gets answered: Min / Max / Avg /
  // Sum side by side. A Sum alone cannot tell those apart, and a chart of the
  // Sum makes them look identical.
  import { untrack } from 'svelte';
  import Section from '$lib/charts/Section.svelte';
  import LineChart from '$lib/charts/LineChart.svelte';
  import StackedBars from '$lib/charts/StackedBars.svelte';
  import Table from '$lib/charts/Table.svelte';
  import {
    UNKNOWN,
    isNum,
    int,
    fetchSection,
    loadingSection,
    buildQuery,
    pivotRows,
    memberLabel,
    tzEcho,
    openSection
  } from './shared.js';

  let { qs, f, tz, nonce, setParams, sp, reportTz } = $props();

  // These lists MIRROR the server's whitelist. `explore` 400s on anything
  // outside it, so a menu offering "Spend" against a backend that does not
  // measure spend is not a nice-to-have gap — it is a control that produces an
  // error panel every time somebody picks it. If the backend sends `options`,
  // its lists win, because it owns the whitelist and these two must not drift.
  const MEASURES = [
    { key: 'events', label: 'Event count' },
    { key: 'status', label: 'HTTP status' },
    { key: 'duration_ms', label: 'Duration (ms)' }
  ];
  const DIMENSIONS = [
    { key: 'action', label: 'Action' },
    { key: 'actor', label: 'Actor' },
    { key: 'source', label: 'Source' },
    { key: 'target', label: 'Target' },
    { key: 'ip', label: 'IP' },
    { key: 'status_class', label: 'Status class' }
  ];
  const ROLLUPS = [
    { key: 'hour', label: 'Hourly' },
    { key: 'day', label: 'Daily' },
    { key: 'week', label: 'Weekly' },
    { key: 'month', label: 'Monthly' }
  ];
  const TOPS = [
    { key: '10', label: '10' },
    { key: '25', label: '25' },
    { key: '50', label: '50' }
  ];
  // Measures that are counts of things and therefore add up across a stack. A
  // status code or a duration does not: stacking those would draw a total that
  // means nothing.
  const ADDITIVE = new Set(['events']);

  let sec = $state(loadingSection('/admin/activity/explore'));
  let d = $derived(sec.data ?? {});

  let measures = $derived(
    Array.isArray(d?.options?.measures) && d.options.measures.length ? d.options.measures : MEASURES
  );
  let dimensions = $derived(
    Array.isArray(d?.options?.dimensions) && d.options.dimensions.length
      ? d.options.dimensions
      : DIMENSIONS
  );
  let rollups = $derived(
    Array.isArray(d?.options?.rollups) && d.options.rollups.length ? d.options.rollups : ROLLUPS
  );
  // The subgroup menu is the dimension list minus the one already on `by`:
  // grouping a dimension by itself is one part per bucket and a stacked chart
  // identical to the unstacked one, which the backend refuses with a 400. A
  // request the UI could have prevented is a UI bug, so the option is not
  // offered rather than being offered and rejected.
  let subOptions = $derived([{ key: '', label: 'none' }, ...dimensions.filter((x) => x.key !== by)]);

  let measure = $derived(MEASURES.some((m) => m.key === sp.get('measure')) ? sp.get('measure') : 'events');
  let by = $derived(DIMENSIONS.some((x) => x.key === sp.get('by')) ? sp.get('by') : 'action');
  // A `sub` equal to `by` — reachable by editing the URL, or by changing `by`
  // to whatever `sub` already was — is dropped rather than sent, because the
  // backend refuses that pair and an error panel here would be self-inflicted.
  let sub = $derived.by(() => {
    const v = sp.get('sub') ?? '';
    if (!v || v === by) return '';
    return DIMENSIONS.some((x) => x.key === v) ? v : '';
  });
  let rollup = $derived(ROLLUPS.some((r) => r.key === sp.get('rollup')) ? sp.get('rollup') : 'day');
  let top = $derived(TOPS.some((t) => t.key === sp.get('top')) ? sp.get('top') : '10');

  async function load() {
    sec = loadingSection('/admin/activity/explore');
    sec = await fetchSection(
      '/admin/activity/explore?' + buildQuery(f, { measure, by, sub, rollup, top })
    );
    reportTz?.(tzEcho(sec.data));
  }
  $effect(() => {
    void qs;
    void nonce;
    void measure;
    void by;
    void sub;
    void rollup;
    void top;
    untrack(() => load());
  });

  let measureLabel = $derived(measures.find((m) => m.key === measure)?.label ?? measure);
  let byLabel = $derived(dimensions.find((x) => x.key === by)?.label ?? by);
  let rollupLabel = $derived(ROLLUPS.find((r) => r.key === rollup)?.label?.toLowerCase() ?? rollup);
  let unit = $derived(typeof d?.unit === 'string' ? d.unit : '');

  // `series` arrives long — `[{t, key, value, sub?}]` — and is pivoted here.
  //
  // With a subgroup the bands are the SUBGROUP, summed across the top-N members
  // of `by`; the table below stays grouped by `by`. That is a deliberate split
  // rather than a compromise: stacking key × sub would be 10 × 5 = 50 bands in
  // one chart, which nobody can read, and both views total to the same number
  // per bucket so they cannot disagree. The panel says which is which.
  let subHonoured = $derived(!sub || typeof d?.sub === 'string');
  let splitField = $derived(subHonoured && sub ? 'sub' : 'key');
  let pivot = $derived(pivotRows(d?.series, {}, splitField));
  let labels = $derived(pivot.labels);
  let series = $derived(pivot.series);
  // Several members of one dimension over one bucket ARE a composition of that
  // bucket's whole, so a count stacks. A status code or a duration does not add
  // up, and stacking those would draw a total that means nothing.
  let stacked = $derived(ADDITIVE.has(measure) && series.length > 1);
  let subTruncated = $derived(d?.sub_truncated === true);
  let subTop = $derived(isNum(d?.sub_top) ? d.sub_top : null);
  let subLabel = $derived(subOptions.find((s) => s.key === sub)?.label ?? sub);

  let tableRows = $derived(
    (Array.isArray(d?.table) ? d.table : []).map((r, i) => ({
      id: `${r?.key ?? 'null'}|${i}`,
      label: memberLabel(r?.key),
      key: r?.key == null ? '' : String(r.key),
      buckets: isNum(r?.n) ? r.n : null,
      rows: isNum(r?.rows) ? r.rows : null,
      min: isNum(r?.min) ? r.min : null,
      max: isNum(r?.max) ? r.max : null,
      avg: isNum(r?.avg) ? r.avg : null,
      sum: isNum(r?.sum) ? r.sum : null,
      // `share` is `{rate, n}` — the rate and the total it is a share OF.
      share: isNum(r?.share?.rate) ? r.share.rate : isNum(r?.share) ? r.share : null,
      shareOf: isNum(r?.share?.n) ? r.share.n : null,
      // Per-row truncation: this key had more parts than the chart shows. The
      // marker belongs on the row that actually lost its tail, not only on the
      // page — "something was cut" leaves the reader checking every row.
      subCut: r?.sub_truncated === true,
      subOf: isNum(r?.sub_of) ? r.sub_of : null
    }))
  );
  let shareOf = $derived(tableRows.find((r) => isNum(r.shareOf))?.shareOf ?? null);
  let shareMax = $derived(Math.max(1, ...tableRows.map((r) => (isNum(r.share) ? r.share : 0))));
  let truncated = $derived(d?.truncated === true);

  // "Count" would be the wrong header: `n` counts BUCKETS that had a
  // measurement, not observations. A day on which one actor did forty things is
  // one bucket and forty rows, and calling that column "Count" would make every
  // reader take the smaller number for the bigger one.
  const cols = [
    { key: 'label', label: 'Group' },
    { key: 'rows', label: 'Observations', align: 'right' },
    { key: 'buckets', label: 'Buckets', align: 'right' },
    { key: 'min', label: 'Min / bucket', align: 'right' },
    { key: 'max', label: 'Max / bucket', align: 'right' },
    { key: 'avg', label: 'Avg / bucket', align: 'right' },
    { key: 'sum', label: 'Sum', align: 'right' },
    { key: 'share', label: 'Share', align: 'right' },
    { key: 'bar', label: '', align: 'right' }
  ];

  /**
   * Values are formatted by MEASURE, because $0.0026 and 2,204 rows and 41%
   * are not the same kind of number and rounding them alike hides the small
   * one. A non-number renders `—`, never 0.
   */
  function fmt(v) {
    if (!isNum(v)) return UNKNOWN;
    if (measure === 'duration_ms')
      return v < 1000 ? `${Math.round(v)}ms` : `${(v / 1000).toFixed(1)}s`;
    if (measure === 'status') return String(Math.round(v));
    return int(v) + (unit ? ` ${unit}` : '');
  }
  const axisFmt = (v) => (measure === 'duration_ms' && v >= 1000 ? `${(v / 1000).toFixed(1)}s` : String(Math.round(v)));
  const pctOf = (v) => (isNum(v) ? `${(v > 1 ? v : v * 100).toFixed(0)}%` : UNKNOWN);
</script>

<!-- ------------------------------ pivot bar ------------------------------- -->
<div
  class="elev mb-5 flex flex-wrap items-end gap-3 rounded-panel border border-line bg-surface px-3.5 py-3"
>
  <label class="flex flex-col gap-1 text-label font-bold tracking-[0.05em] text-ink-3 uppercase">
    Measure
    <select
      value={measure}
      onchange={(e) => setParams({ measure: e.currentTarget.value })}
      class="min-h-[40px] min-w-[150px] cursor-pointer rounded-card border border-line bg-surface px-2.5 text-body-sm font-semibold text-ink capitalize"
    >
      {#each measures as m (m.key)}<option value={m.key}>{m.label}</option>{/each}
    </select>
  </label>

  <label class="flex flex-col gap-1 text-label font-bold tracking-[0.05em] text-ink-3 uppercase">
    by
    <select
      value={by}
      onchange={(e) => setParams({ by: e.currentTarget.value })}
      class="min-h-[40px] min-w-[130px] cursor-pointer rounded-card border border-line bg-surface px-2.5 text-body-sm font-semibold text-ink"
    >
      {#each dimensions as x (x.key)}<option value={x.key}>{x.label}</option>{/each}
    </select>
  </label>

  <label class="flex flex-col gap-1 text-label font-bold tracking-[0.05em] text-ink-3 uppercase">
    Subgroup
    <select
      value={sub}
      onchange={(e) => setParams({ sub: e.currentTarget.value })}
      class="min-h-[40px] min-w-[110px] cursor-pointer rounded-card border border-line bg-surface px-2.5 text-body-sm font-semibold text-ink"
    >
      {#each subOptions as s (s.key)}<option value={s.key}>{s.label}</option>{/each}
    </select>
  </label>

  <label class="flex flex-col gap-1 text-label font-bold tracking-[0.05em] text-ink-3 uppercase">
    Rollup
    <select
      value={rollup}
      onchange={(e) => setParams({ rollup: e.currentTarget.value })}
      class="min-h-[40px] min-w-[110px] cursor-pointer rounded-card border border-line bg-surface px-2.5 text-body-sm font-semibold text-ink"
    >
      {#each rollups as r (r.key)}<option value={r.key}>{r.label}</option>{/each}
    </select>
  </label>

  <label class="flex flex-col gap-1 text-label font-bold tracking-[0.05em] text-ink-3 uppercase">
    Top
    <select
      value={top}
      onchange={(e) => setParams({ top: e.currentTarget.value })}
      class="min-h-[40px] min-w-[86px] cursor-pointer rounded-card border border-line bg-surface px-2.5 text-body-sm font-semibold text-ink"
    >
      {#each TOPS as t (t.key)}<option value={t.key}>{t.label}</option>{/each}
    </select>
  </label>

  <span class="ml-auto max-w-[34ch] text-label leading-snug text-ink-3">
    Every filter in the bar above this one is applied too, and the whole pivot is in the URL — copy the
    address to hand somebody the exact view.
  </span>
</div>

<Section
  title="{measureLabel} by {byLabel}{sub ? `, split by ${subLabel}` : ''}, {rollupLabel}"
  hint={sub
    ? `Bands are ${subLabel.toLowerCase()}, summed across the top ${top} ${byLabel.toLowerCase()} — the table below stays grouped by ${byLabel.toLowerCase()}. Both total to the same number per bucket.`
    : 'The chart follows the controls above. Nothing on this tab is a fixed report.'}
  state={sec}
  retry={load}
  what="this pivot"
>
  {#if !subHonoured}
    <p class="mb-2 rounded-panel border border-warning bg-warning-soft px-3 py-2 text-meta text-ink">
      This backend does not implement <b>subgroup</b>: it answered without echoing one, so the chart
      below is <b>{measureLabel} by {byLabel}</b> with no second split. The subgroup is not being quietly
      dropped — it is being reported as ignored.
    </p>
  {/if}
  {#if stacked}
    <StackedBars {labels} {series} fmt={axisFmt} />
  {:else}
    <LineChart {labels} {series} fmt={axisFmt} />
  {/if}
  {#if truncated || subTruncated}
    <p class="mt-2 text-meta leading-relaxed text-ink-3">
      {#if truncated}
        Top {top} {byLabel.toLowerCase()} only.
      {/if}
      {#if subTruncated}
        Each of those shows its top {subTop ?? 'few'}
        {subLabel.toLowerCase()} and had more.
      {/if}
      What falls outside a cut is absent from the chart, not folded into an “other” band that would sit
      in the legend looking like a real member.
    </p>
  {/if}
</Section>

<Section
  title="The same query as a table"
  hint="Min / Max / Avg are PER BUCKET, so they answer “one spike, or a habit?” — a row reading 3 / 40 / 12 did between 3 and 40 things a day. A Sum on its own cannot tell those apart."
  state={sec}
  retry={load}
  what="this pivot"
>
  <Table
    {cols}
    rows={tableRows}
    empty="This combination returned no groups. That is the query answering, not an error."
    rowKey={(r) => r.id}
    onpick={(r) => (r.key && by === 'action' ? setParams(openSection('feed', { action: r.key })) : null)}
  >
    {#snippet row(r)}
      <td class="max-w-[280px]" title={r.label}>
        <span class="block truncate">
          {#if r.key}{r.label}{:else}<span class="text-ink-3 italic">{r.label}</span>{/if}
        </span>
        {#if r.subCut}
          <span class="text-label text-ink-3">
            top {subTop ?? 'few'}{#if r.subOf != null} of {int(r.subOf)}{/if}
            {subLabel.toLowerCase()}
          </span>
        {/if}
      </td>
      <td class="tnum r">{int(r.rows)}</td>
      <td class="tnum r">{int(r.buckets)}</td>
      <td class="tnum r">{fmt(r.min)}</td>
      <td class="tnum r">{fmt(r.max)}</td>
      <td class="tnum r">{fmt(r.avg)}</td>
      <td class="tnum r">{fmt(r.sum)}</td>
      <td class="tnum r">{pctOf(r.share)}</td>
      <td class="r">
        <span
          class="inline-block h-[7px] w-[70px] overflow-hidden rounded-full border border-line bg-surface-2 align-middle"
        >
          {#if isNum(r.share)}
            <span
              class="block h-full rounded-full bg-accent"
              style="width:{Math.min(100, ((r.share > 1 ? r.share : r.share * 100) / (shareMax > 1 ? shareMax : shareMax * 100)) * 100).toFixed(1)}%"
            ></span>
          {/if}
        </span>
      </td>
    {/snippet}
  </Table>

  <p class="mt-2 text-meta leading-relaxed text-ink-3">
    <b>Observations</b> counts rows; <b>Buckets</b> counts the {rollupLabel} periods that had any — so a
    group with 240 observations across 6 buckets was busy on six days, not on sixty. Share is of
    {#if shareOf != null}<b class="tnum">{int(shareOf)}</b> observations in this range{:else}the range
      total{/if}. A blank cell is a value this backend does not record for
    <b>{measureLabel.toLowerCase()}</b>; it is not a zero. A group shown as
    <span class="italic">not recorded</span> is a real band — the events whose {byLabel.toLowerCase()} was
    NULL — and it is kept so the shares still sum to the whole.
  </p>
</Section>
