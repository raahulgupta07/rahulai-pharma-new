<script>
  // Embed — everything about putting the assistant on someone else's site:
  //
  //   Widget     the snippet generator and credentials (was /embed)
  //   Test on a customer domain  the probe that drives a real embed session
  //                              (was /embed-test, and before that "Live test" —
  //                              which read as a twin of the Branch assistant
  //                              preview. One shows what a branch sees; this one
  //                              answers whether it will work on their domain.)
  //   Usage      traffic per embed (was a tab of Analytics)
  //   Guide      the integration guide (was /docs)
  //
  // The panels are the former pages, moved unchanged. See /quality's shell for
  // the three rules these merged pages share (URL tab, mount-once, load-late).
  import { tick } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import TabStrip from '$lib/TabStrip.svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import { Code2, FlaskConical, BookOpen, ChartColumn } from '@lucide/svelte';
  import WidgetPanel from './WidgetPanel.svelte';
  import TestPanel from './TestPanel.svelte';
  import GuidePanel from './GuidePanel.svelte';
  import AnalyticsPanel from './AnalyticsPanel.svelte';

  const TABS = [
    { id: 'widget', label: 'Widget', icon: Code2, panel: WidgetPanel },
    { id: 'analytics', label: 'Usage', icon: ChartColumn, panel: AnalyticsPanel },
    { id: 'test', label: 'Test on a customer domain', icon: FlaskConical, panel: TestPanel },
    { id: 'guide', label: 'Guide', icon: BookOpen, panel: GuidePanel }
  ];
  const TAB_IDS = TABS.map((t) => t.id);

  let tab = $derived.by(() => {
    const t = $page.url.searchParams.get('tab');
    return TAB_IDS.includes(t) ? t : 'widget';
  });

  function setTab(id) {
    if (id === tab) return;
    const u = new URL($page.url);
    u.searchParams.set('tab', id);
    goto(u.pathname + u.search, { noScroll: true, keepFocus: true });
  }

  let visited = $state({});
  $effect(() => {
    if (!visited[tab]) visited = { ...visited, [tab]: true };
  });

</script>

<!-- The page has ONE name, and it is the one on the rail row that got you
     here. It used to be whichever panel was open, so clicking a tab renamed
     the page. Each panel keeps its own heading, one level down. -->
<!-- pb-6 is not decoration. The sticky tab strip below pulls itself up 24px
     over main's top padding, and without that space it climbs over this
     header's last line — which is exactly what it did the first time. -->
<div class="pb-6">
  <PageHeader
    title="Embed & integration"
    subtitle="Everything about putting this assistant on somebody else's site: the snippet and its credentials, a live test against a real page, who is actually using it, and the guide their developers need."
  />
</div>

<TabStrip tabs={TABS} value={tab} onchange={setTab} gap="gap-x-5" sticky label="Embed sections" />

{#each TABS as t (t.id)}
  {#if visited[t.id]}
    {@const Panel = t.panel}
    <div role="tabpanel" id={'panel-' + t.id} aria-labelledby={'tab-' + t.id} hidden={tab !== t.id}>
      <Panel />
    </div>
  {/if}
{/each}
