<script>
  // Cost & KPIs — what the answers cost to produce.
  //
  // The KPI half of this page (targets, owners, effort saved) is not built:
  // it needs figures that exist nowhere in this system. Until they do, this
  // page is the measured half only, and says so rather than showing a target
  // of zero.
  import AnalyticsPage from '$lib/analytics/AnalyticsPage.svelte';
</script>

<svelte:head><title>Cost & KPIs · CityCare console</title></svelte:head>

<AnalyticsPage
  sections={['cost']}
  title="Cost & KPIs"
  subtitle="Spend per model, per day and per answer. A model with no configured price reads “not configured”, never $0.00 — a zero would read as free."
/>
