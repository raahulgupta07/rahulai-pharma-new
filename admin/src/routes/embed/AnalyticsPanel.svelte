<script>
  // Per-embed usage, drawn by the shared analytics loader so the numbers and
  // the filter bar are the same ones the rest of the console shows.
  import AnalyticsPage from '$lib/analytics/AnalyticsPage.svelte';
</script>

<AnalyticsPage
  level={2}
  sections={['embeds']}
  title="Embed usage"
  subtitle="Traffic per embed, and what each one was asked. Turns with no embed recorded are shown as “unattributed” rather than folded into a total."
/>
