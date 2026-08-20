<script>
  // Which midnight are you looking at.
  //
  // The database runs Etc/UTC and buckets with date_trunc('day', ts), while
  // this console labels those buckets in the reader's local time. In Yangon
  // (GMT+6:30) that means every "day" on every chart runs 06:30 → 06:30 local
  // and the first six and a half hours of each morning are filed under the
  // previous day. The fix is server-side (`tz` on every bucketing endpoint);
  // this chip is how the reader can tell which of the two they are being shown.
  //
  // It is deliberately not a tooltip. A chart whose day boundary is six hours
  // off still looks completely plausible, so the boundary has to be on screen.
  //
  // `applied` is the zone the ENDPOINT echoed back, not the one we asked for.
  // If a backend has not declared the parameter yet, FastAPI drops it silently
  // and answers 200 with UTC buckets — so trusting our own request here would
  // print the local zone over UTC data, which is the original bug with better
  // manners. No echo → the chip says UTC and says why.
  import { Clock } from '@lucide/svelte';

  let { zone = 'UTC', applied = null } = $props();

  /** "+6:30" / "−5" / "±0" for an IANA zone name, using the browser's own tables. */
  function offsetOf(tz) {
    try {
      const s = new Intl.DateTimeFormat('en-GB', { timeZone: tz, timeZoneName: 'shortOffset' })
        .formatToParts(new Date())
        .find((p) => p.type === 'timeZoneName')?.value;
      if (!s) return null;
      return s.replace(/^(GMT|UTC)/, '') || '±0';
    } catch {
      return null;
    }
  }

  let honoured = $derived(typeof applied === 'string' && applied.length > 0);
  let shown = $derived(honoured ? applied : 'UTC');
  let off = $derived(offsetOf(shown));
</script>

<span
  class="inline-flex min-h-[36px] items-center gap-1.5 rounded-panel border border-line bg-surface px-2.5 text-meta font-semibold
         {honoured ? 'text-ink-2' : 'text-warning'}"
  title={honoured
    ? `Days are cut at midnight in ${shown}.`
    : `This backend did not echo a tz, so its buckets are cut at UTC midnight — not at midnight in ${zone}.`}
>
  <Clock size="14" />
  <span class="tnum">{off ? `GMT${off}` : shown}</span>
  {#if !honoured}<span class="font-normal">· buckets are UTC</span>{/if}
</span>
