<script>
  // The auth slice of the activity feed, on its own URL.
  //
  // `lockSource` pins it rather than defaulting it: if the source were a
  // default the reader could clear it from the filter bar and be left on a page
  // headed "Security log" listing ingest runs.
  import AnalyticsPage from '$lib/analytics/AnalyticsPage.svelte';
</script>

<svelte:head><title>Security log · CityCare console</title></svelte:head>

<AnalyticsPage
  sections={['audit']}
  lockSource="auth"
  title="Security log"
  subtitle="Sign-ins, sign-in failures, role changes and account approvals. Pinned to authentication events — the rest of the event feed is under Activity."
/>
