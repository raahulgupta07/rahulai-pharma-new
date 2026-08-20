<script>
  // The measured half of Answer quality: how answers were rated, and the queue
  // of turns that look wrong.
  //
  // A thin wrapper rather than the sections inlined here, because these two
  // sections read the same eighteen-endpoint loader and the same chat-turn
  // filter bar as the rest of the analytics pages — and every link that points
  // at them resolves through $lib/analytics/routes.js.
  import AnalyticsPage from '$lib/analytics/AnalyticsPage.svelte';
</script>

<AnalyticsPage
  level={2}
  sections={['quality', 'diagnostics']}
  title="Answer quality"
  subtitle="What people rated, and the turns worth reading. A rate always carries its denominator — “92% of 25 rated”, never a bare 92%."
/>
