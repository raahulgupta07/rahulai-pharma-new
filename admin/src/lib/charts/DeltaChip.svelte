<script>
  // The movement chip: "↑ +42 · 45% vs prev period".
  //
  // Three rules, and each is here because breaking it makes the chip lie:
  //
  //  1. ABSENT IS NOT "NO CHANGE". `delta = null` (the payload carried no
  //     movement block at all) draws NOTHING — we do not know whether it moved.
  //     A block that IS present but whose `delta` is null means "there is no
  //     prior window", and prints exactly that. Printing `0%` for either would
  //     claim a measurement of no change, which is a third, different thing.
  //  2. GOOD IS PER-METRIC, NOT PER-DIRECTION. Latency going up is bad; cache
  //     hit rate going up is good. The colour comes from `good`, never from the
  //     sign, so a green arrow always means "this got better".
  //  3. A PERCENTAGE SHIPS WITH ITS ABSOLUTE (contract §B). 1 → 4 is +3; calling
  //     that 300% and stopping there is noise. When only one of the two is
  //     known, the one we have is shown alone rather than the other invented.
  import { isNum, int, UNKNOWN } from './format.js';

  let {
    /** {value, prev, delta, delta_pct, prev_period} from the API, or null. */
    delta = null,
    /** Which way is an improvement: 'up' | 'down' | 'none'. */
    good = 'up',
    /** Formats the absolute movement. Gets the raw signed number. */
    fmt = (v) => (v > 0 ? `+${int(v)}` : int(v)),
    /** Shown after the numbers. */
    note = 'vs prev period'
  } = $props();

  let d = $derived(isNum(delta?.delta) ? delta.delta : null);
  let p = $derived(isNum(delta?.delta_pct) ? delta.delta_pct : null);

  // A block with neither number is a real answer — "there was no previous
  // window to compare against" — and it is not the same as no block.
  let noPrior = $derived(delta != null && d === null && p === null);

  let dir = $derived(d !== null ? Math.sign(d) : p !== null ? Math.sign(p) : 0);
  let tone = $derived(
    dir === 0 || good === 'none' ? 'flat' : (dir > 0) === (good === 'up') ? 'up' : 'down'
  );

  // A percentage off a zero base is unbounded and reads as noise at four
  // digits. The absolute beside it is the number that means something.
  let pctText = $derived(p === null ? null : Math.abs(p) > 999 ? '>999%' : `${Math.abs(p).toFixed(0)}%`);
  let absText = $derived(d === null ? null : fmt(d));
</script>

{#if delta != null}
  <div class="mt-0.5 flex flex-wrap items-baseline gap-1.5">
    {#if noPrior}
      <span class="text-meta font-semibold text-ink-3">no prior period</span>
    {:else}
      <span
        class="text-meta font-bold {tone === 'up'
          ? 'text-success'
          : tone === 'down'
            ? 'text-danger'
            : 'text-ink-3'}"
      >
        {dir > 0 ? '↑' : dir < 0 ? '↓' : '→'}
        {#if absText && pctText}{absText} · {pctText}{:else}{absText ?? pctText ?? UNKNOWN}{/if}
      </span>
      <span class="text-label text-ink-3">{note}</span>
    {/if}
  </div>
{/if}
