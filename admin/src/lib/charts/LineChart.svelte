<script>
  // Line chart. `area` is a per-series flag and it carries meaning:
  //
  //   volume over time  → line + area fill
  //   a RATE over time  → plain line, no fill
  //
  // An area fill under a rate reads as a quantity. That is how a hit-rate or a
  // latency chart quietly misleads, so the flag is set by the caller for each
  // series rather than being a chart-wide default.
  //
  // PIXEL MODE. The viewBox is built at the container's MEASURED width, so one
  // view unit is one CSS pixel: 11px text is 11px on screen. The old fixed
  // 680×170 viewBox stretched to `width:100%` scaled the entire drawing, TYPE
  // INCLUDED — measured on the running console, a 10.5px axis label landed at
  // 17.4px in a 1440px window and 24.8px at 1920px. Height is a PROP and does
  // not follow the width; that coupling is what made these 400px tall.
  //
  // The readout is a crosshair + HTML tooltip covering every series at once.
  // What it replaces was an SVG <title> per dot: ~1s browser delay, a 3px hit
  // target, one series at a time, and nothing at all on touch.
  import {
    plotBox,
    niceScale,
    xIn,
    yIn,
    maxOf,
    polySegments,
    linePathPx,
    areaPathPx,
    labelEveryPx,
    bucketAt
  } from '$lib/charts/geom.js';
  import { isNum, UNKNOWN } from './format.js';

  let {
    labels = [],
    series = [],
    fmt = (v) => String(Math.round(v)),
    onpick = null,
    pickLabel = (i) => `open ${labels[i]}`,
    height = 200
  } = $props();

  let measured = $state(0); // container width in CSS px (bind:clientWidth)
  let svgEl = $state(null);
  let hover = $state(null); // hovered bucket index, or null

  let n = $derived(labels.length);
  let box = $derived(plotBox(measured, height));
  let max = $derived(maxOf(...series.map((s) => s.values)));
  let scale = $derived(niceScale(max));
  let step = $derived(labelEveryPx(n, box));

  // Above 20 buckets the per-point circles stop being data and become a dotted
  // rule — 62 dots on a 31-day two-series chart, nearly all on the baseline.
  // The hover knob is the affordance instead; every bucket stays clickable
  // through a full-height hit rect, which is a far larger target than a dot.
  let showDots = $derived(n > 0 && n <= 20);

  let plotted = $derived(
    series.map((s) => {
      const pts = (s.values ?? []).map((v, i) => ({
        i,
        v,
        x: xIn(i, n, box),
        y: yIn(v, scale.top, box)
      }));
      return { ...s, pts, segs: polySegments(pts), dots: pts.filter((p) => p.y != null) };
    })
  );
  let unknownCount = $derived(
    plotted.length ? plotted[0].pts.filter((p) => p.y == null).length : 0
  );

  let hoverX = $derived(hover == null ? 0 : xIn(hover, n, box));
  // Flip the tooltip to the left of the crosshair before it can run off the
  // right edge. Anchoring by `right` means the flipped box needs no measured
  // width of its own to stay inside.
  let flip = $derived(hoverX > box.W * 0.58);

  const bucketOf = (clientX) => (svgEl && n ? bucketAt(clientX, svgEl, n, box) : null);

  // BOTH families are bound on purpose. Browsers fire pointer events; a headless
  // driver dispatches mouse events. A readout that cannot be driven in a test is
  // how a readout silently regresses back to nothing.
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
    const cur = hover ?? 0;
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

  function onMove(e) {
    const i = bucketOf(e.clientX);
    if (i != null) hover = i;
  }
  function onLeave() {
    hover = null;
  }
  // A mouse click fires pointerdown AND click, so a naive handler on both picks
  // the bucket twice — and `onpick` here toggles a page-wide filter. Both are
  // still bound (a headless driver may dispatch only one of them); the second
  // one through for the same bucket is dropped.
  let lastPick = { i: -1, t: 0 };
  function onPress(e) {
    const i = bucketOf(e.clientX);
    if (i == null) return;
    hover = i;
    if (!onpick) return;
    const now = Date.now();
    if (lastPick.i === i && now - lastPick.t < 500) return;
    lastPick = { i, t: now };
    onpick(i);
  }
  function onKey(e, i) {
    if (!onpick) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onpick(i);
    }
  }

  const valueAt = (s, i) => {
    const v = (s.values ?? [])[i];
    return isNum(v) ? fmt(v) : UNKNOWN;
  };
</script>

{#if n === 0}
  <p class="rounded-card border border-dashed border-line-2 bg-surface px-4 py-6 text-center text-body-sm text-ink-3">
    No days in this range yet.
  </p>
{:else}
  <div class="mb-2 flex flex-wrap gap-4">
    {#each series as s (s.key)}
      <span class="inline-flex items-center gap-1.5 text-meta font-medium text-ink-2">
        <i class="inline-block h-2.5 w-2.5 rounded-xs" style="background:{s.color}"></i>{s.label}
      </span>
    {/each}
  </div>

  <div class="rounded-card border border-line bg-surface px-2.5 py-3">
    <!-- The measured element carries no padding of its own: clientWidth would
         include it, and the svg would then be wider than the space it sits in. -->
    <!-- The FOCUSABLE thing is this group, not the svg: an <svg role="img">
         with a positive tabindex is a contradiction (a graphic that is also a
         widget), and Svelte is right to say so. The group owns the keyboard,
         the svg stays a labelled image. -->
    <!-- Suppressed deliberately, and only after building the thing the rule is
         protecting against the absence of: this group IS keyboard-operable
         (onKeys walks the buckets with the arrow keys, Home/End jump, Enter
         drills through) and it shows a visible focus ring. Svelte cannot see
         that a role="group" was made operable on purpose; the alternative,
         role="application", tells a screen reader to stop interpreting keys for
         the whole subtree, which is a bigger lie than this comment. -->
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div
      class="relative outline-offset-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
      bind:clientWidth={measured}
      role="group"
      tabindex="0"
      aria-label="{series.map((s) => s.label).join(' and ')} by bucket. Arrow keys read each bucket."
      onkeydown={onKeys}
      onfocus={() => (hover = hover ?? 0)}
      onblur={onLeave}
    >
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <svg
        bind:this={svgEl}
        viewBox="0 0 {box.W} {box.H}"
        width={box.W}
        height={box.H}
        class="block w-full"
        role="img"
        aria-label="{series.map((s) => s.label).join(' and ')} over {n} buckets"
        onpointermove={onMove}
        onmousemove={onMove}
        onpointerleave={onLeave}
        onmouseleave={onLeave}
        onpointerdown={onPress}
        onclick={onPress}
      >
        {#each scale.rows as v}
          {@const gy = yIn(v, scale.top, box)}
          <line x1={box.x0} y1={gy} x2={box.x1} y2={gy} stroke="var(--color-line)" stroke-width="1" />
          <text x={box.x0 - 6} y={gy + 3.5} text-anchor="end" class="fill-ink-3 font-mono text-label">
            {fmt(v)}
          </text>
        {/each}

        {#each plotted as s (s.key)}
          {#if s.area}
            {#each s.segs as seg}
              <path d={areaPathPx(seg, box)} fill={s.color} opacity="0.09" />
            {/each}
          {/if}
          {#each s.segs as seg}
            <path
              d={linePathPx(seg)}
              fill="none"
              stroke={s.color}
              stroke-width="2"
              stroke-linejoin="round"
              stroke-linecap="round"
            />
          {/each}
          {#if showDots}
            {#each s.dots as p}
              <circle cx={p.x} cy={p.y} r="3" fill={s.color} />
            {/each}
          {/if}
        {/each}

        {#if hover != null}
          <line
            class="crosshair"
            x1={hoverX}
            y1={box.y0}
            x2={hoverX}
            y2={box.y1}
            stroke="var(--color-line-2)"
            stroke-width="1"
            stroke-dasharray="3 3"
          />
          {#each plotted as s (s.key)}
            {@const p = s.pts[hover]}
            {#if p && p.y != null}
              <circle
                cx={p.x}
                cy={p.y}
                r="4.5"
                fill={s.color}
                stroke="var(--color-surface)"
                stroke-width="2"
              />
            {/if}
          {/each}
        {/if}

        <!-- One focusable full-height target per bucket. Keyboard reach is kept
             (it was one stop per dot before, so this is fewer stops, not fewer
             routes) and the pointer target is a whole column, not a 3px dot. -->
        {#if onpick}
          {#each labels as _l, i}
            {@const cx = xIn(i, n, box)}
            {@const w = Math.max(6, (box.x1 - box.x0) / Math.max(1, n - 1))}
            <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
            <rect
              x={cx - w / 2}
              y={box.y0}
              width={w}
              height={Math.max(0, box.y1 - box.y0)}
              fill="transparent"
              class="cursor-pointer"
              role="button"
              tabindex="0"
              aria-label={pickLabel(i)}
              onfocus={() => (hover = i)}
              onblur={() => (hover = null)}
              onkeydown={(e) => onKey(e, i)}
            />
          {/each}
        {/if}

        <!-- The LAST label is anchored `end`, not `middle`. The last point sits
             ON the right edge of the plot, so a centred label puts half its
             width outside the viewBox and the SVG clips it — "18 Aug" shipped
             as "18 Au", which reads as a truncated date rather than as a label
             that ran out of room. -->
        {#each labels as l, i}
          {#if i % step === 0 || i === n - 1}
            <text
              x={xIn(i, n, box)}
              y={box.y1 + 17}
              text-anchor={i === n - 1 ? 'end' : 'middle'}
              class="fill-ink-3 font-mono text-label">{l}</text
            >
          {/if}
        {/each}
      </svg>

      {#if hover != null}
        <div
          class="tip absolute z-10 rounded-panel border border-line bg-surface px-2.5 py-1.5 text-meta shadow-lg"
          style="top:{box.y0}px; {flip
            ? `right:${Math.round(box.W - hoverX + 10)}px`
            : `left:${Math.round(hoverX + 10)}px`}"
        >
          <div class="mb-1 font-mono text-label text-ink-3">{labels[hover]}</div>
          {#each series as s (s.key)}
            <div class="flex items-center gap-1.5 whitespace-nowrap text-ink-2">
              <i class="inline-block h-2 w-2 shrink-0 rounded-xs" style="background:{s.color}"></i>
              <span>{s.label}</span>
              <span class="ml-auto pl-2 font-mono font-semibold text-ink">{valueAt(s, hover)}</span>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  </div>

  {#if unknownCount > 0}
    <p class="mt-2 text-meta text-ink-3">
      {unknownCount} of {n} buckets have no measurement. They are left as gaps in the line — a gap is
      not a zero.
    </p>
  {/if}
{/if}

<style>
  /* The tooltip is a readout, never a pointer target: it sits over the plot and
     must not steal the pointermove that keeps it alive. */
  .tip {
    pointer-events: none;
    transition: opacity 120ms ease;
  }
  .crosshair {
    transition: opacity 120ms ease;
  }
  @media (prefers-reduced-motion: reduce) {
    .tip,
    .crosshair {
      transition: none;
    }
  }
</style>
