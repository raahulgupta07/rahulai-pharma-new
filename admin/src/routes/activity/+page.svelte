<script>
  // The event feed — what happened, rather than what was asked.
  //
  // Reads /admin/activity/*, which carries its own filter names (source,
  // actor, action) and therefore its own filter bar. That is why Trends and
  // Explore live here rather than on Health & usage: they read these same
  // endpoints, and a page never carries two bars.
  //
  // Not a rail row. It is reached from "Full feed" on Today and from the
  // Security log, which is this page's auth slice.
  import AnalyticsPage from '$lib/analytics/AnalyticsPage.svelte';
</script>

<svelte:head><title>Activity · CityCare console</title></svelte:head>

<AnalyticsPage
  sections={['feed', 'audit', 'trends', 'explore']}
  title="Activity"
  subtitle="Sign-ins, ingest runs and console actions, as they were recorded. Every row is an event this system wrote at the time, not a reconstruction."
/>
