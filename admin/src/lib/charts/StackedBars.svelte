<script>
  // Composition per bucket → stacked bar. Hit vs miss, up vs down, prompt vs
  // completion: two parts of one whole per bucket, which a pair of lines would
  // render as two competing quantities instead of one composition.
  //
  // PIXEL MODE. The viewBox is built at the container's MEASURED width, so one
  // view unit is one CSS pixel: 11px type is 11px on a 1440px monitor and 11px
  // on a 1920px one. The old fixed 680×170 viewBox was scaled by the browser to
  // the container width — measured on the running console, that put a label
  // declared at 10.5px on screen at 17.4px and 24.8px respectively. Height is a
  // PROP, never derived from width, for the same reason.
  //
  // The readout is an HTML tooltip over the whole column, not an SVG <title> per
  // rect: <title> costs ~1s of browser delay, is unstyled, shows one segment at
  // a time, and is dead on touch. A column readout shows every segment plus the
  // total, which is the number a stacked bar is actually claiming.
  import { plotBox, niceScale, columnsIn, yIn, slotAt, labelEveryPx } from '$lib/charts/geom.js';
  import { isNum, UNKNOWN } from './format.js';

  let {
    labels = [],
    series = [],
    fmt = (v) => String(Math.round(v)),
    onpick = null,
    height = 200
  } = $props();

  let cw = $state(0); // measured container width — the whole drawing is built from it
  let svgEl = $state(null);
  let hover = $state(-1);

  let n = $derived(labels.length);
  let box = $derived(plotBox(cw, height));
  let col = $derived(columnsIn(n, box));
  let step = $derived(labelEveryPx(n, box));

  // The bar height is the SUM of the stack, so the axis is scaled to the tallest
  // TOTAL, not to the tallest single series. A missing value is not summed as a
  // zero: it contributes nothing and is reported as unknown in the readout.
  let totals = $derived(
    labels.map((_, i) => {
      let sum = 0;
      let known = false;
      for (const s of series) {
        const v = s.values?.[i];
        if (isNum(v)) {
          sum += Math.max(0, v);
          known = true;
        }
      }
      return { sum, known };
    })
  );
  let peak = $derived(totals.length ? Math.max(...totals.map((t) => t.sum)) : 0);
  let scale = $derived(niceScale(peak));

  let stacks = $derived(
    labels.map((label, i) => {
      let acc = 0;
      const parts = series.map((s) => {
        const v = s.values?.[i];
        const known = isNum(v);
        const value = known ? Math.max(0, v) : 0;
        const yTop = yIn(acc + value, scale.top, box);
        const yBase = yIn(acc, scale.top, box);
        if (known) acc += value;
        return {
          key: s.key,
          label: s.label,
          color: s.color,
          v,
          known,
          y: yTop ?? box.y1,
          h: known ? Math.max(0, (yBase ?? box.y1) - (yTop ?? box.y1)) : 0
        };
      });
      // A boundary can only be drawn where it does not cost more than it buys.
      // The stroke straddles the edge, so it takes SEPARATOR_WIDTH/2 off THIS
      // segment and off the one it touches — a segment is separable only if it
      // and both of its drawn neighbours can afford that.
      const drawn = parts.filter((p) => p.known && p.h > 0);
      const at = new Map(drawn.map((p, k) => [p.key, k]));
      for (const p of parts) {
        const k = at.get(p.key);
        p.sep =
          k !== undefined &&
          p.h >= SEPARABLE &&
          (k === 0 || drawn[k - 1].h >= SEPARABLE) &&
          (k === drawn.length - 1 || drawn[k + 1].h >= SEPARABLE);
      }
      return { i, label, x: col.xOf(i), cx: col.cxOf(i), slotX: box.x0 + col.slot * i, parts };
    })
  );

  let active = $derived(hover >= 0 && hover < n ? stacks[hover] : null);
  // Flip the tooltip to the left of the column once the column sits in the right
  // third, or it renders off the card.
  let flip = $derived(active ? active.cx > box.W * 0.62 : false);
  let tipStyle = $derived(
    active
      ? flip
        ? `right:${Math.max(4, box.W - active.cx + 10)}px;top:${box.y0}px;`
        : `left:${Math.min(box.W - 8, active.cx + 10)}px;top:${box.y0}px;`
      : ''
  );

  // Pointer events are what browsers fire; mouse events are what a headless
  // driver dispatches. A readout that cannot be driven in a test is how this
  // silently regresses, so both are wired.
  /**
   * Keyboard path to the same readout the pointer gets.
   *
   * The old chart put the only interaction on 3px <circle> elements, which no
   * keyboard could reach — and the a11y warnings the pointer handlers raise are
   * only honestly silenced once there IS another way in. Arrow keys walk the
   * buckets, Home/End jump to the ends, Enter/Space drills through exactly as a
   * click does.
   */
  function onKeys(e) {
    if (!n) return;
    const cur = hover < 0 ? 0 : hover;
    let next = null;
    if (e.key === 'ArrowRight') next = Math.min(n - 1, cur + 1);
    else if (e.key === 'ArrowLeft') next = Math.max(0, cur - 1);
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = n - 1;
    else if ((e.key === 'Enter' || e.key === ' ') && onpick && cur >= 0) {
      e.preventDefault();
      onpick(cur);
      return;
    } else return;
    e.preventDefault();
    hover = next;
  }

  function track(e) {
    if (!n || !svgEl) return;
    hover = slotAt(e.clientX, svgEl, n, box);
  }
  function leave() {
    hover = -1;
  }

  /** Which segment is the pointer over vertically? `null` above the stack. */
  function segmentAt(e, st) {
    if (!svgEl) return null;
    const r = svgEl.getBoundingClientRect();
    if (!r.height) return null;
    const py = (e.clientY - r.top) * (box.H / r.height);
    for (const p of st.parts) if (p.known && p.h > 0 && py >= p.y && py <= p.y + p.h) return p;
    return null;
  }

  function pick(e) {
    if (!onpick || !n || !svgEl) return;
    const i = slotAt(e.clientX, svgEl, n, box);
    hover = i;
    // Drill-through carries a series key, so a click on empty space above the
    // stack has no honest answer and does nothing.
    const p = segmentAt(e, stacks[i]);
    if (p) onpick(i, p.key);
  }

  const valueOf = (p) => (p.known ? fmt(p.v) : UNKNOWN);

  /**
   * ADJACENT SEGMENTS ARE SEPARATED BY THE SURFACE, NOT BY THEIR OWN CONTRAST.
   *
   * Two segments that touch need to be told apart at the boundary, and WCAG's
   * 3:1 is a pure LUMINANCE ratio, so a palette cannot supply it: k mutually
   * 3:1 colours need L(i+1) >= 3*(L(i)+0.05)-0.05, which from L=0 reaches
   * 1.300 on the fourth step — past white. Three is the ceiling for the whole
   * sRGB gamut and TWO once every fill must also clear 3:1 against a near-black
   * page. Measured on the running console before this stroke existed: the
   * danger segment on series-1 was 1.01:1, series-2 on series-1 1.06:1, and
   * warning on danger 1.29:1.
   *
   * So the boundary is drawn instead of implied. Every fill in the series
   * palette already clears 3:1 against --color-surface, which makes a
   * surface-coloured hairline >= 3:1 against BOTH neighbours by construction —
   * an achievable constraint replacing an impossible one.
   *
   * THE WIDTH IS SET BY MEASUREMENT, NOT BY TASTE. Segment heights were read
   * off the running console (viewBox `0 0 1292 200`, one view unit = one CSS
   * pixel, so these ARE pixels):
   *
   *   /activity "Events by Action, daily"  13 segments, min 0.54, median 3.24
   *   /cost     "Tokens per day"            4 segments, min 0.23, median 6.12
   *
   * The first draft of this used a 1.5px stroke skipped below 3px of segment
   * height. On the real data that skipped SIX of the thirteen boundaries — the
   * crowded end of the chart, which is the only end that needs them — and where
   * it did fire it took 0.75px off each side. A stroke is centred on the edge,
   * so it eats half its width from each neighbour: 1px is the narrowest line
   * that still renders, and it costs each side 0.5px.
   *
   * SEPARABLE is therefore 2px and it is checked on BOTH NEIGHBOURS, not just
   * on the segment itself. A 147px bar sitting on a 0.23px one — which /cost
   * really draws — would otherwise stroke its own edge straight over the thin
   * band and delete it. Below the threshold nothing is drawn and the segments
   * touch: at under 2px a band cannot be identified by colour whatever we do,
   * and the honest place to read it is the column readout, which lists every
   * segment by name and value.
   */
  const SEPARATOR = 'var(--color-surface)';
  const SEPARATOR_WIDTH = 1;
  const SEPARABLE = 2;
</script>

{#if n === 0}
  <p class="rounded-card border border-dashed border-line-2 bg-surface px-4 py-6 text-center text-body-sm text-ink-3">
    No buckets in this range yet.
  </p>
{:else}
  <div class="mb-2 flex flex-wrap gap-4">
    {#each series as s (s.key)}
      <span class="inline-flex items-center gap-1.5 text-meta font-medium text-ink-2">
        <i class="inline-block h-2.5 w-2.5 rounded-xs" style="background:{s.color}"></i>{s.label}
      </span>
    {/each}
  </div>

  <!-- The FOCUSABLE thing is this group, not the svg: an <svg role="img">
       with a positive tabindex is a contradiction (a graphic that is also a
       widget). The group owns the keyboard, the svg stays a labelled image. -->
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
  <div
    class="relative outline-offset-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
    bind:clientWidth={cw}
    role="group"
    tabindex="0"
    aria-label="{series.map((s) => s.label).join(' and ')} per bucket. Arrow keys read each bucket."
    onkeydown={onKeys}
    onfocus={() => (hover = hover < 0 ? 0 : hover)}
    onblur={leave}
  >
    <!-- Suppressed, not ignored: the svg carries pointer handlers AND a keyboard
         path (onKeys) plus tabindex, so the interaction the rule is protecting
         against actually exists. -->
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <svg
      bind:this={svgEl}
      viewBox="0 0 {box.W} {box.H}"
      width={box.W}
      height={box.H}
      class="block w-full rounded-card border border-line bg-surface {onpick ? 'cursor-pointer' : ''}"
      role="img"
      aria-label="{series.map((s) => s.label).join(' and ')} per bucket"
      onpointermove={track}
      onmousemove={track}
      onpointerdown={track}
      onclick={pick}
      onpointerleave={leave}
      onmouseleave={leave}
    >
      {#if active}
        <rect
          class="band"
          x={active.slotX}
          y={box.y0}
          width={col.slot}
          height={box.y1 - box.y0}
          fill="var(--color-surface-2)"
        />
      {/if}

      {#each scale.rows as v}
        {@const gy = yIn(v, scale.top, box)}
        <line x1={box.x0} y1={gy} x2={box.x1} y2={gy} stroke="var(--color-line)" stroke-width="1" />
        <text x={box.x0 - 6} y={gy + 4} text-anchor="end" class="fill-ink-3 font-mono text-label">
          {fmt(v)}
        </text>
      {/each}

      {#each stacks as st (st.i)}
        {#each st.parts as p (p.key)}
          {#if p.known && p.h > 0}
            <!-- An SVG shape carrying role="button" IS interactive; the
                 compiler's heuristic only recognises HTML elements. The rect
                 keeps keyboard access; the pointer path is handled on the svg so
                 one move updates the whole column at once. -->
            <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
            <rect
              x={st.x}
              y={p.y}
              width={col.w}
              height={p.h}
              fill={p.color}
              stroke={p.sep ? SEPARATOR : 'none'}
              stroke-width={p.sep ? SEPARATOR_WIDTH : 0}
              shape-rendering="crispEdges"
              role={onpick ? 'button' : undefined}
              tabindex={onpick ? 0 : undefined}
              aria-label={onpick ? `${st.label} ${p.label}: ${valueOf(p)}` : undefined}
              onfocus={onpick ? () => (hover = st.i) : undefined}
              onblur={onpick ? leave : undefined}
              onkeydown={onpick
                ? (e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), onpick(st.i, p.key))
                : undefined}
            />
          {/if}
        {/each}
        {#if st.i % step === 0 || st.i === n - 1}
          <text x={st.cx} y={box.H - 8} text-anchor="middle" class="fill-ink-3 font-mono text-label"
            >{st.label}</text
          >
        {/if}
      {/each}
    </svg>

    {#if active}
      <div
        class="tip pointer-events-none absolute z-10 min-w-[9rem] rounded-panel border border-line bg-surface px-2.5 py-2 text-meta shadow-lg"
        style={tipStyle}
      >
        <div class="mb-1 font-semibold text-ink">{active.label}</div>
        {#each active.parts as p (p.key)}
          <div class="flex items-center gap-2 leading-5">
            <i class="inline-block h-2 w-2 shrink-0 rounded-xs" style="background:{p.color}"></i>
            <span class="text-ink-2">{p.label}</span>
            <span class="ml-auto font-mono text-ink">{valueOf(p)}</span>
          </div>
        {/each}
        <div class="mt-1 flex items-center gap-2 border-t border-line pt-1 leading-5">
          <span class="text-ink-2">Total</span>
          <span class="ml-auto font-mono font-semibold text-ink"
            >{totals[active.i].known ? fmt(totals[active.i].sum) : UNKNOWN}</span
          >
        </div>
      </div>
    {/if}
  </div>
{/if}

<style>
  .band {
    transition: x 90ms linear;
  }
  .tip {
    transition: left 90ms linear, right 90ms linear;
  }
  @media (prefers-reduced-motion: reduce) {
    .band,
    .tip {
      transition: none;
    }
  }
</style>
