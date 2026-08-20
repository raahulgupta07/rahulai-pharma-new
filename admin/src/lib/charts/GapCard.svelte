<script>
  // "This cannot be computed yet, and here is exactly what it needs."
  //
  // It is deliberately a card and not a tooltip: a metric that is missing for a
  // schema reason looks identical to a metric that is zero unless the page says
  // which it is. Each of these is rendered only while the data is genuinely
  // absent — once the column or table lands and rows arrive, the card goes and
  // the real chart takes its place.
  let { title, body, sql = '', tag = 'NEEDS DATA' } = $props();
</script>

<div class="mt-5 rounded-card border border-dashed border-line-2 bg-surface-2 p-4">
  <div class="mb-1.5 flex items-center gap-2">
    <span class="rounded bg-accent px-1.5 py-0.5 text-micro font-extrabold tracking-[0.07em] text-on-accent">{tag}</span>
    <b class="page-title text-body text-ink">{title}</b>
  </div>
  <p class="mb-2 text-body-sm leading-relaxed text-ink-2">{body}</p>
  {#if sql}
    <code class="block overflow-x-auto rounded-panel border border-line bg-surface px-3 py-2.5 font-mono text-label whitespace-pre text-ink-2">{sql}</code>
  {/if}
</div>
