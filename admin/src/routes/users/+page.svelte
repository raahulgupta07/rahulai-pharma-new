<script>
  // People & access — the two pages that answer "who can use this, and what can
  // they see?", merged:
  //
  //   People    console accounts, roles and pending approvals (was /users)
  //   Tenants   the store scopes an account can be limited to (was /tenants)
  //
  // The panels are the former pages, moved unchanged. See /quality's shell for
  // the three rules these merged pages share (URL tab, mount-once, load-late).
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import TabStrip from '$lib/TabStrip.svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import { Users, Building2 } from '@lucide/svelte';
  import PeoplePanel from './PeoplePanel.svelte';
  import TenantsPanel from './TenantsPanel.svelte';

  const TABS = [
    { id: 'people', label: 'People', icon: Users, panel: PeoplePanel },
    { id: 'tenants', label: 'Tenants', icon: Building2, panel: TenantsPanel }
  ];
  const TAB_IDS = TABS.map((t) => t.id);

  let tab = $derived.by(() => {
    const t = $page.url.searchParams.get('tab');
    return TAB_IDS.includes(t) ? t : 'people';
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

<svelte:head><title>People & access · CityCare console</title></svelte:head>

<!-- The page has ONE name, and it is the rail row's. It used to be whichever
     panel was open, so this screen called itself "Users" — the first tab — to
     anyone who clicked "People & access".

     pb-6 is not decoration: the sticky strip below pulls itself up 24px over
     main's top padding, and without that space it covers this header's last
     line. -->
<div class="pb-6">
  <PageHeader
    title="People & access"
    subtitle="Who can open this console, and which client sites may embed the assistant. Both are accounts of a kind — one for a person, one for a site."
  />
</div>

<TabStrip tabs={TABS} value={tab} onchange={setTab} gap="gap-x-5" sticky label="People & access sections" />

{#each TABS as t (t.id)}
  {#if visited[t.id]}
    {@const Panel = t.panel}
    <div role="tabpanel" id={'panel-' + t.id} aria-labelledby={'tab-' + t.id} hidden={tab !== t.id}>
      <Panel />
    </div>
  {/if}
{/each}
