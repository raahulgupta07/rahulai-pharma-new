<script>
  // Stage drop-off → funnel. It answers one question: where are people lost.
  //
  // A stage that is near zero keeps a visible stub rather than vanishing —
  // a stage that disappears looks like a stage that does not exist, and the
  // whole point of the chart is that the reader can see the gap.
  //
  // A stage that is NOT measured shows `—` and no bar. It is never drawn as 0,
  // and the drop from an unmeasured stage is not computed at all.
  import { isNum, UNKNOWN, int } from './format.js';

  let { stages = [], onpick = null } = $props();

  // A stage may carry its own tone: the last stage of the ingest funnel is
  // "set aside", and a third of everything arriving ending there is not a
  // neutral fact about the shape of the funnel.
  const TONE = { accent: 'bg-accent', ok: 'bg-success', bad: 'bg-danger', warn: 'bg-warning' };

  let top = $derived(Math.max(1, ...stages.map((s) => (isNum(s.value) ? s.value : 0))));
  let rows = $derived(
    stages.map((s, i) => {
      const prev = stages[i - 1];
      const drop =
        i > 0 && isNum(s.value) && isNum(prev?.value) ? prev.value - s.value : null;
      return {
        ...s,
        w: isNum(s.value) ? Math.max(s.value > 0 ? 4 : 0, (s.value / top) * 100) : 0,
        drop
      };
    })
  );
</script>

<div class="space-y-2">
  {#each rows as s (s.key)}
    <svelte:element
      this={onpick ? 'button' : 'div'}
      type={onpick ? 'button' : undefined}
      role={onpick ? 'button' : undefined}
      onclick={onpick ? () => onpick(s) : undefined}
      class="elev block w-full rounded-card border border-line bg-surface px-3.5 py-3 text-left
             {onpick ? 'cursor-pointer hover:border-accent' : ''}"
    >
      <div class="flex justify-between text-body-sm font-semibold text-ink">
        <span>{s.label}</span>
        <b class="tnum {isNum(s.value) ? '' : 'text-ink-3'}">{isNum(s.value) ? int(s.value) : UNKNOWN}</b>
      </div>
      <div class="my-1.5 h-[9px] overflow-hidden rounded-full border border-line bg-surface-2">
        {#if isNum(s.value)}
          <span class="block h-full {TONE[s.tone] ?? TONE.accent}" style="width:{s.w.toFixed(1)}%"></span>
        {/if}
      </div>
      <div class="text-label text-ink-3">
        {s.note}
        {#if s.drop !== null && s.drop > 0}
          <span class="ml-1.5 text-danger">−{int(s.drop)} from above</span>
        {/if}
      </div>
    </svelte:element>
  {/each}
</div>
