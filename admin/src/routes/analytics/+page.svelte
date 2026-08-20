<script>
  // Health & usage — what this system did, and whether it is keeping up.
  //
  // Four sections over one filter bar. The other ten sections that used to be
  // tabs here are their own pages now (Conversations, Answer quality, Cost &
  // KPIs, the activity feed); every link between them resolves through
  // $lib/analytics/routes.js, so a section can move without breaking them.
  import AnalyticsPage from '$lib/analytics/AnalyticsPage.svelte';
</script>

<svelte:head><title>Health & usage · CityCare console</title></svelte:head>

<AnalyticsPage
  sections={['overview', 'performance', 'cache', 'health']}
  title="Health & usage"
  subtitle="What was asked, what it took, and whether the data behind it is current. Nothing here is estimated: a number that is not recorded shows as an em-dash, never as zero."
/>
