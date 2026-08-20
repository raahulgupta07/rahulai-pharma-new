<script>
  /* THE GOVERNANCE PROPOSAL, AND WHICH OF ITS CLAIMS ARE TRUE HERE.

     The redesign's Governance screen describes a process: five steps from
     intake to operate, each with an owner and an SLA; a decision log with an
     owner per decision; and four support tiers with response times.

     Nothing in this repository or this deployment establishes any of it. No
     document names an AI working group, an exec sponsor, an ops owner or an
     on-call rota, and no code enforces a response time. The five steps, the
     owners and every SLA below come from the design proposal and nowhere else.

     Two ways to get this page wrong, and the second is worse:

       Leave it empty. Then the proposal — which is good, and which somebody
       thought about — is invisible, and the next person invents a different
       one.

       Print it as though it were operating. Then this console tells a branch
       pharmacist that Platform is on call and that a data gap gets picked up
       within one business day. Nobody is on call. That is not a presentation
       problem, it is somebody's Saturday night.

     So the proposal is drawn in full and marked as a proposal, and every claim
     the console can actually check is checked — from the running system, on
     load. Where the proposal and the measurement disagree, the disagreement is
     the point of the page. */

  import { onMount } from 'svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import ErrorState from '$lib/ErrorState.svelte';
  import { base as appBase } from '$app/paths';
  import { getJSON } from '$lib/api.js';
  import { UNKNOWN } from '$lib/charts/format.js';
  import { Inbox, ListChecks, Gavel, Hammer, LifeBuoy } from '@lucide/svelte';

  /** The proposal's intake-to-operate flow, verbatim from the redesign.
   *  `who` and `sla` are the proposal's — not this system's. */
  const STEPS = [
    {
      step: 1,
      title: 'Intake',
      who: 'Anyone',
      artifact: 'One page: problem, who feels it, what it replaces',
      sla: null,
      icon: Inbox
    },
    {
      step: 2,
      title: 'Screen',
      who: 'AI working group, fortnightly',
      artifact: 'Score on value, data readiness, risk',
      sla: '2 weeks',
      icon: ListChecks
    },
    {
      step: 3,
      title: 'Approve & fund',
      who: 'CMHL exec sponsor',
      artifact: 'Budget, owner, success measure',
      sla: '1 month',
      icon: Gavel
    },
    {
      step: 4,
      title: 'Build & pilot',
      who: 'Platform + a named business owner',
      artifact: 'Pilot with one branch, KPI baseline',
      sla: '1 quarter',
      icon: Hammer
    },
    {
      step: 5,
      title: 'Operate',
      who: 'Platform on-call + ops owner',
      artifact: 'Runbook, alerts, monthly review',
      sla: 'ongoing',
      icon: LifeBuoy
    }
  ];

  /** The proposal's support tiers, verbatim. The fourth is the only one this
   *  system can confirm, and it confirms it by having nothing that could page
   *  anybody. */
  const TIERS = [
    { tier: 'Branch staff', who: 'Ask their branch manager', covers: 'Wrong or missing stock answer', sla: 'same day' },
    { tier: 'Ops owner', who: 'Pharmacy ops, named per branch group', covers: 'Data gaps, missing branches, file drops', sla: '1 business day' },
    { tier: 'Platform', who: 'Two engineers, business hours', covers: 'Outage, deploy, model or database fault', sla: '4 hours, business hours only' },
    { tier: 'Out of hours', who: 'Nobody on call', covers: 'Nothing is paged overnight', sla: 'next business day', confirmed: 'alerting' }
  ];

  /** Decisions this repository actually records, with where each is written
   *  down. The proposal's own log carries owners and dates that no document
   *  here supports, so the owner column below says what is known — which for
   *  the open ones is a real name, because the customer is on the document. */
  const DECISIONS = [
    {
      when: '2026-08-03',
      what: 'A blank stock count means “not recorded”. Zero means none. Negatives are shown as sent.',
      why: 'Blank used to be stored as 0, so “we have no record of this” and “there are none left” became the same answer.',
      who: 'Recorded in the field-feedback response to CMHL',
      state: 'shipped',
      check: 'blank_is_not_zero'
    },
    {
      when: '2026-08-03',
      what: 'An upload always replaces. Merge mode is gone.',
      why: 'The upload page offered a “merge instead of replace” choice that reported success and did nothing at all.',
      who: 'Recorded in CLAUDE.md and in the response to CMHL',
      state: 'shipped'
    },
    {
      when: '2026-08-03',
      what: 'The safety line is decided by a rule, not asked of the model.',
      why: '“Consult a pharmacist” appeared on a price question and was sometimes missing from a dosage answer.',
      who: 'Recorded in CLAUDE.md',
      state: 'shipped'
    },
    {
      when: '2026-08-03',
      what: 'A long answer says when it truncated. The limit is 250 rows.',
      why: 'Results were being cut short with no indication — worse than a summary, because it looked complete.',
      who: 'Set as a judgement call; CMHL asked for their own number and has not given one',
      state: 'shipped'
    },
    {
      when: null,
      what: 'The assistant is read-only. It will never write inventory.',
      why: 'Every tool it is given reads. Nothing it can call changes stock.',
      who: 'Enforced by the tool list, not by the prompt',
      state: 'standing',
      check: 'read_only'
    },
    {
      when: null,
      what: 'One model vendor, reached through one gateway.',
      why: 'A second vendor would be a second set of prices, a second failure mode and a second thing to audit.',
      who: 'Standing instruction; no document here records who set it',
      state: 'standing',
      check: 'one_vendor'
    },
    {
      when: '2026-08-03',
      what: 'Should the agent tell a pharmacist what other branches hold?',
      why: 'Forms 4 and 9 asked it to refuse. It answers today, and answering was deliberate — a pharmacist who can say where stock is keeps the customer. Three options were put to CMHL: answer freely, say only that stock exists somewhere, or refuse and say so.',
      who: 'CMHL — asked 2026-08-03, no answer recorded',
      state: 'open'
    },
    {
      when: '2026-08-03',
      what: 'How much belongs in an answer?',
      why: 'Form 5 asked for “Found” or “Not Found”. Form 10 asked for code, brand, quantity, dosage and indication. Both are reasonable and they cannot both be the default.',
      who: 'CMHL — asked 2026-08-03, no answer recorded',
      state: 'open'
    },
    {
      when: '2026-08-03',
      what: 'When there is no stock, a suggestion or a plain “no”?',
      why: 'Suggesting an alternative is a bigger step than reporting a number — it is close to advice, and who may see it matters.',
      who: 'CMHL — asked 2026-08-03, no answer recorded',
      state: 'open'
    },
    {
      when: null,
      what: 'Accuracy is graded before the next rollout wave.',
      why: 'An eval set exists in the repository. No result from it is stored anywhere, on this deployment or any other.',
      who: 'Nobody is recorded as owning this',
      state: 'overdue',
      check: 'accuracy_graded'
    }
  ];

  const STATE = {
    shipped: { label: 'Shipped', cls: 'bg-success-soft text-success' },
    standing: { label: 'Standing', cls: 'bg-accent-soft text-accent-hover' },
    open: { label: 'Open · needs CMHL', cls: 'bg-warning-soft text-warning' },
    overdue: { label: 'Overdue', cls: 'bg-danger-soft text-danger' }
  };
  const CHECK = {
    in_place: { label: 'Checked here · true', cls: 'bg-success-soft text-success' },
    absent: { label: 'Checked here · not true', cls: 'bg-danger-soft text-danger' },
    unknown: { label: 'Could not be checked', cls: 'bg-surface-2 text-ink-3' }
  };
  const HOW = { probed: 'asked just now', observed: 'read from what it left behind', not_checked: 'not checked' };

  let data = $state(null);
  let error = $state(null);
  let loading = $state(true);

  async function load() {
    loading = true;
    error = null;
    try {
      data = await getJSON('/admin/governance');
    } catch (e) {
      // Keep the error OBJECT — its status is the difference between "sign in
      // again" and "the server is down".
      error = e;
      data = null;
    } finally {
      loading = false;
    }
  }
  onMount(load);

  /** A claim by id, or null when the endpoint could not be read. A missing
   *  check must never read as a passing one. */
  const checkOf = (id) => data?.checks?.find((c) => c.id === id) ?? null;

  let absent = $derived(data?.checks?.filter((c) => c.state === 'absent') ?? []);
  let open = $derived(DECISIONS.filter((d) => d.state === 'open'));
  let overdue = $derived(DECISIONS.filter((d) => d.state === 'overdue'));
</script>

<PageHeader
  title="Governance"
  subtitle="How an idea becomes something that runs, what has been decided, and who picks up the phone when it stops working."
/>

<section class="mb-6 rounded-panel border border-warning bg-warning-soft p-5">
  <h2 class="text-body font-semibold text-ink">This process is proposed, not agreed</h2>
  <p class="mt-1.5 max-w-3xl text-body-sm leading-relaxed text-ink-2">
    The flow, the owners and every response time on this page come from the console redesign and from nowhere
    else. No document in this system names an AI working group, an exec sponsor, an ops owner or an on-call rota,
    and nothing enforces a response time. They are drawn here because a proposal nobody can see gets reinvented —
    not because anybody has agreed to them.
  </p>
  <p class="mt-2 max-w-3xl text-body-sm leading-relaxed text-ink-2">
    Where this console can check a claim, it checks it below and says so. <strong
      >Nobody is on call, and nothing pages anybody</strong
    >; that one is measured, not assumed.
  </p>
</section>

{#if error}
  <ErrorState {error} retry={load} what="the governance checks" />
{/if}

<section class="mb-6 rounded-panel border border-line bg-surface p-5">
  <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
    <h2 class="text-title font-semibold text-ink">Intake to operate</h2>
    <span class="text-meta text-ink-3">{STEPS.length} steps · proposed</span>
  </div>
  <p class="mt-2 max-w-3xl text-body-sm leading-relaxed text-ink-2">
    Who decides at each step, and what they hand on. Every owner and every duration below is the proposal's.
  </p>

  <ol class="mt-4 space-y-3">
    {#each STEPS as s (s.step)}
      {@const Icon = s.icon}
      <li class="flex items-start gap-3.5 rounded-card border border-line bg-page p-3.5">
        <span
          class="mt-0.5 flex h-8 w-8 flex-none items-center justify-center rounded-card bg-accent-soft text-accent"
        >
          <Icon size={16} />
        </span>
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-baseline gap-x-2.5">
            <span class="font-mono text-label text-ink-3">step {s.step}</span>
            <h3 class="text-body font-semibold text-ink">{s.title}</h3>
            <span class="text-meta text-ink-3">{s.who}</span>
          </div>
          <p class="mt-1 text-body-sm leading-relaxed text-ink-2">{s.artifact}</p>
          {#if s.step === 5}
            {@const alerting = checkOf('alerting')}
            <p class="mt-2 rounded-card bg-danger-soft px-3 py-2 text-body-sm leading-relaxed text-danger">
              This step says “Runbook, alerts, monthly review”. There are no alerts.
              {#if alerting}
                {alerting.detail}.
              {:else}
                That could not be checked just now, so it is unknown rather than fine.
              {/if}
              The fourth support tier below says nobody is on call, and these two cannot both be true.
            </p>
          {/if}
        </div>
        <span class="ml-auto flex-none text-meta tnum text-ink-3">{s.sla ?? UNKNOWN}</span>
      </li>
    {/each}
  </ol>
</section>

<section class="mb-6 rounded-panel border border-line bg-surface p-5">
  <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
    <h2 class="text-title font-semibold text-ink">Decision log</h2>
    <span class="text-meta text-ink-3">
      {DECISIONS.length} recorded · {open.length} waiting on CMHL{overdue.length
        ? ` · ${overdue.length} overdue`
        : ''}
    </span>
  </div>
  <p class="mt-2 max-w-3xl text-body-sm leading-relaxed text-ink-2">
    So nobody re-argues them. Each one below is traceable to something written down in this repository or to the
    field-feedback response sent to CMHL on 3 August 2026 — the owner column says where, and says so plainly when
    nobody is recorded as owning it. Where this console can check that a decision is still being kept, it does.
  </p>

  {#if open.length}
    <p class="mt-3 rounded-card bg-warning-soft px-3 py-2 text-body-sm leading-relaxed text-warning">
      {open.length} decisions have been with CMHL since 3 August 2026 with no answer recorded. They are product
      questions, not engineering ones — the system can be built either way, and until one is chosen it keeps doing
      whatever it does today, which for the first of them is the opposite of what the branch forms asked for.
    </p>
  {/if}

  <p class="mt-2 max-w-3xl text-body-sm leading-relaxed text-ink-3">
    “Standing” means it is settled policy. Reopening one is itself a decision that belongs in this log.
  </p>

  <div class="mt-4 overflow-x-auto">
    <table class="tbl">
      <thead>
        <tr>
          <th>Decided</th>
          <th>What</th>
          <th>Who</th>
          <th>State</th>
          <th>Still true?</th>
        </tr>
      </thead>
      <tbody>
        {#each DECISIONS as d (d.what)}
          {@const c = d.check ? checkOf(d.check) : null}
          <tr>
            <td class="whitespace-nowrap tnum text-ink-3">{d.when ?? UNKNOWN}</td>
            <td>
              <div class="bilingual text-ink">{d.what}</div>
              <div class="mt-1 max-w-xl text-meta leading-relaxed text-ink-3">{d.why}</div>
            </td>
            <td class="max-w-[220px] text-meta leading-relaxed text-ink-3">{d.who}</td>
            <td>
              <span class="whitespace-nowrap rounded-xs px-1.5 py-0.5 text-label font-medium {STATE[d.state].cls}"
                >{STATE[d.state].label}</span
              >
            </td>
            <td>
              {#if !d.check}
                <span class="text-meta text-ink-3">not checkable from here</span>
              {:else if loading}
                <span class="text-meta text-ink-3">checking…</span>
              {:else if c}
                <span class="whitespace-nowrap rounded-xs px-1.5 py-0.5 text-label font-medium {CHECK[c.state].cls}"
                  >{CHECK[c.state].label}</span
                >
                <div class="mt-1 max-w-xs text-meta leading-relaxed text-ink-3">{c.detail}</div>
              {:else}
                <span class="whitespace-nowrap rounded-xs bg-surface-2 px-1.5 py-0.5 text-label text-ink-3"
                  >{CHECK.unknown.label}</span
                >
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
</section>

<section class="rounded-panel border border-line bg-surface p-5">
  <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
    <h2 class="text-title font-semibold text-ink">Who supports it</h2>
    <span class="text-meta text-ink-3">{TIERS.length} tiers · proposed</span>
  </div>
  <p class="mt-2 max-w-3xl text-body-sm leading-relaxed text-ink-2">
    Every name and every response time here is the proposal's. Nothing in this system holds a rota, and nothing
    measures a response time, so three of these four rows are an intention. The fourth is the one this console can
    confirm.
  </p>

  <div class="mt-4 overflow-x-auto">
    <table class="tbl">
      <thead>
        <tr>
          <th>Tier</th>
          <th>Who</th>
          <th>Covers</th>
          <th>Response</th>
          <th>Established?</th>
        </tr>
      </thead>
      <tbody>
        {#each TIERS as t (t.tier)}
          {@const c = t.confirmed ? checkOf(t.confirmed) : null}
          <tr>
            <td class="whitespace-nowrap font-medium text-ink">{t.tier}</td>
            <td class="text-ink-2">{t.who}</td>
            <td class="text-ink-2">{t.covers}</td>
            <td class="whitespace-nowrap text-ink-3">{t.sla}</td>
            <td>
              {#if !t.confirmed}
                <span class="whitespace-nowrap rounded-xs bg-warning-soft px-1.5 py-0.5 text-label font-medium text-warning"
                  >Proposed only</span
                >
              {:else if loading}
                <span class="text-meta text-ink-3">checking…</span>
              {:else if c?.state === 'absent'}
                <span class="whitespace-nowrap rounded-xs bg-danger-soft px-1.5 py-0.5 text-label font-medium text-danger"
                  >Confirmed</span
                >
                <div class="mt-1 max-w-xs text-meta leading-relaxed text-ink-3">{c.detail}</div>
              {:else}
                <span class="whitespace-nowrap rounded-xs bg-surface-2 px-1.5 py-0.5 text-label text-ink-3"
                  >Could not be checked</span
                >
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>

  <div class="mt-4 rounded-card border border-warning bg-warning-soft p-4">
    <h3 class="text-body font-semibold text-ink">Nothing is paged out of hours</h3>
    <p class="mt-1 max-w-3xl text-body-sm leading-relaxed text-ink-2">
      A failure overnight is discovered by whoever opens this console in the morning. Before a wider rollout, that
      is the gap to close first.
    </p>
    <a
      href={appBase + '/architecture'}
      class="mt-3 inline-flex items-center gap-1.5 rounded-control border border-line bg-surface px-3 py-1.5 text-body-sm font-medium text-ink transition-colors hover:bg-surface-2"
      >See observability gaps</a
    >
  </div>

  {#if !loading && data}
    <p class="mt-4 max-w-3xl text-body-sm leading-relaxed text-ink-3">
      {data.counts.in_place} of {data.checks.length} claims on this page were checked against the running system and
      hold; {data.counts.absent} were checked and do not
      {#if absent.length}({absent.map((a) => a.claim.toLowerCase()).join('; ')}){/if}.
      Each carries how it was established — {HOW.probed} or {HOW.observed} — because a gap found by looking and a gap
      assumed from silence are different claims.
    </p>
  {/if}
</section>
