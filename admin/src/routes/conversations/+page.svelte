<script>
  // Conversations — the questions themselves, and who is asking them.
  import AnalyticsPage from '$lib/analytics/AnalyticsPage.svelte';
</script>

<svelte:head><title>Conversations · CityCare console</title></svelte:head>

<AnalyticsPage
  sections={['questions', 'users']}
  title="Conversations"
  subtitle="Every question put to the assistant, what it answered from, and how long it took. Click any row to read the whole turn."
/>
