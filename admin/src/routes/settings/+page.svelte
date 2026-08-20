<script>
  // Settings — the four configuration pages, merged:
  //
  //   Behaviour  answer length and the per-deployment switches (was /settings)
  //   Auth       sign-in methods, SSO, LDAP, security (was /auth)
  //   Branding   names, logos, parent org, preview (was /branding)
  //   Agent      the system prompt and the runtime it reads (was /agent)
  //
  // The panels are the former pages, moved unchanged. See /quality's shell for
  // the three rules these merged pages share (URL tab, mount-once, load-late).
  //
  // Auth and Branding carry tab bars of their OWN. Those inner tabs were moved
  // onto `?sub=` so the two bars do not fight over `?tab=`.
  import { tick } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import TabStrip from '$lib/TabStrip.svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import { SlidersHorizontal, ShieldCheck, Palette, Bot } from '@lucide/svelte';
  import BehaviourPanel from './BehaviourPanel.svelte';
  import AuthPanel from './AuthPanel.svelte';
  import BrandingPanel from './BrandingPanel.svelte';
  import AgentPanel from './AgentPanel.svelte';

  const TABS = [
    { id: 'behaviour', label: 'Behaviour', icon: SlidersHorizontal, panel: BehaviourPanel },
    { id: 'auth', label: 'Auth', icon: ShieldCheck, panel: AuthPanel },
    { id: 'branding', label: 'Branding', icon: Palette, panel: BrandingPanel },
    { id: 'agent', label: 'Agent', icon: Bot, panel: AgentPanel }
  ];
  const TAB_IDS = TABS.map((t) => t.id);

  let tab = $derived.by(() => {
    const t = $page.url.searchParams.get('tab');
    return TAB_IDS.includes(t) ? t : 'behaviour';
  });

  function setTab(id) {
    if (id === tab) return;
    const u = new URL($page.url);
    u.searchParams.set('tab', id);
    // A panel's inner tab does not belong to the panel you are switching to.
    u.searchParams.delete('sub');
    goto(u.pathname + u.search, { noScroll: true, keepFocus: true });
  }

  let visited = $state({});
  $effect(() => {
    if (!visited[tab]) visited = { ...visited, [tab]: true };
  });

</script>

<!-- The page has ONE name, and it is the rail row's. It used to be whichever
     panel was open, so this screen called itself "Answer behaviour" — the first
     tab — to anyone who clicked "Configuration".

     pb-6 is not decoration: the sticky strip below pulls itself up 24px over
     main's top padding, and without that space it covers this header's last
     line. -->
<div class="pb-6">
  <PageHeader
    title="Configuration"
    subtitle="How this deployment behaves: what the assistant is allowed to say, who may sign in, whose name and marks it wears, and which model answers."
  />
</div>

<TabStrip tabs={TABS} value={tab} onchange={setTab} gap="gap-x-5" sticky label="Settings sections" />

{#each TABS as t (t.id)}
  {#if visited[t.id]}
    {@const Panel = t.panel}
    <div role="tabpanel" id={'panel-' + t.id} aria-labelledby={'tab-' + t.id} hidden={tab !== t.id}>
      <Panel />
    </div>
  {/if}
{/each}
