<script>
  // Quality — the two pages that answer "is the assistant any good?", merged:
  //
  //   Answers     how answers were rated, and the queue of turns that look
  //               wrong (was two tabs of Analytics)
  //   Evaluation  the graded question set (was /eval)
  //   Learning    what it has remembered and how people rated it (was /learning)
  //
  // The panels are the former pages, moved unchanged. Three rules hold here and
  // in the other merged shells (/settings, /embed):
  //
  //  1. THE TAB IS IN THE URL (`?tab=`), so a tab is linkable and survives a
  //     refresh. Navigation PUSHES rather than replaces, so Back returns to the
  //     tab you came from rather than leaving the page.
  //  2. A PANEL IS MOUNTED ONCE AND KEPT. Switching away hides it; it is not
  //     destroyed. A half-filled form or a finished eval run is still there when
  //     you come back, and switching tabs does not re-fetch.
  //  3. A PANEL IS NOT LOADED UNTIL IT IS OPENED, so landing on Evaluation does
  //     not fire Learning's three requests.
  import { tick } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import TabStrip from '$lib/TabStrip.svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import { CircleCheckBig, ClipboardCheck, Brain } from '@lucide/svelte';
  import AnswersPanel from './AnswersPanel.svelte';
  import EvalPanel from './EvalPanel.svelte';
  import LearningPanel from './LearningPanel.svelte';

  const TABS = [
    { id: 'answers', label: 'Answers', icon: CircleCheckBig, panel: AnswersPanel },
    { id: 'eval', label: 'Evaluation', icon: ClipboardCheck, panel: EvalPanel },
    { id: 'learning', label: 'Learning', icon: Brain, panel: LearningPanel }
  ];
  const TAB_IDS = TABS.map((t) => t.id);

  let tab = $derived.by(() => {
    const t = $page.url.searchParams.get('tab');
    return TAB_IDS.includes(t) ? t : 'answers';
  });

  function setTab(id) {
    if (id === tab) return;
    const u = new URL($page.url);
    u.searchParams.set('tab', id);
    goto(u.pathname + u.search, { noScroll: true, keepFocus: true });
  }

  // Which panels have ever been opened. Seeded from the tab the page landed on.
  let visited = $state({});
  $effect(() => {
    if (!visited[tab]) visited = { ...visited, [tab]: true };
  });

</script>

<!-- The page has ONE name, and it is the rail row's. It used to be whichever
     panel was open — which read correctly here only by luck, because the first
     tab happened to share the row's wording.

     pb-6 is not decoration: the sticky strip below pulls itself up 24px over
     main's top padding, and without that space it covers this header's last
     line. -->
<div class="pb-6">
  <PageHeader
    title="Answer quality"
    subtitle="Whether the answers were any good: what people rated, how the eval set scores, and what the assistant has taught itself since."
  />
</div>

<TabStrip tabs={TABS} value={tab} onchange={setTab} gap="gap-x-5" sticky label="Quality sections" />

{#each TABS as t (t.id)}
  {#if visited[t.id]}
    {@const Panel = t.panel}
    <div role="tabpanel" id={'panel-' + t.id} aria-labelledby={'tab-' + t.id} hidden={tab !== t.id}>
      <Panel />
    </div>
  {/if}
{/each}
