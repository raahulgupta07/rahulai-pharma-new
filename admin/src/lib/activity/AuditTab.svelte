<script>
  // Audit — who signed in, who could not, and who was refused.
  //
  // The distinction the whole tab rests on: a LOCK-OUT is not a failed attempt.
  // It is the consequence of several, and folding the two into one "sign-in
  // problems" series would both double-count the failures and hide the moment
  // the threshold started firing. They are separate series for the same reason
  // the tool-call contract has three outcomes and not two.
  //
  // A 403 is likewise its own fact: the account was known and refused. That is
  // a role or scope question, not a credentials question, and it is worth
  // reading every one — which is why they are ranked rather than summed.
  import { untrack } from 'svelte';
  import Kpi from '$lib/charts/Kpi.svelte';
  import Section from '$lib/charts/Section.svelte';
  import StackedBars from '$lib/charts/StackedBars.svelte';
  import RankBars from '$lib/charts/RankBars.svelte';
  import Table from '$lib/charts/Table.svelte';
  import Badge from '$lib/Badge.svelte';
  import {
    UNKNOWN,
    isNum,
    int,
    kpi,
    deltaOf,
    unfilteredOf,
    tzEcho,
    fetchSection,
    loadingSection,
    buildQuery,
    fromRows,
    memberLabel,
    COLOR,
    openSection
  } from './shared.js';

  let { qs, f, tz, nonce, setParams, reportTz } = $props();

  const EVENT_TONE = {
    login_ok: 'ok',
    sso_ok: 'ok',
    login_fail: 'warn',
    sso_fail: 'warn',
    login_locked: 'danger',
    login_blocked: 'danger'
  };
  const EVENT_LABEL = {
    login_ok: 'Login OK',
    login_fail: 'Login failed',
    login_locked: 'Locked out',
    login_blocked: 'Blocked by IP',
    sso_ok: 'SSO OK',
    sso_fail: 'SSO failed'
  };
  const OUTCOME_COLOR = {
    signed_in: COLOR.ok,
    ok: COLOR.ok,
    failed: COLOR.bad,
    fail: COLOR.bad,
    locked: COLOR.warn,
    blocked: COLOR.muted
  };

  // Two endpoints. `/audit` owns the per-bucket outcomes, the 403s and the event
  // list; the sign-in KPIs with their period deltas live on `/summary` under
  // `signins`, so the KPI row reads them from there rather than summing the
  // buckets — a sum would give the value and lose the movement.
  let sec = $state(loadingSection('/admin/activity/audit'));
  let sum = $state(loadingSection('/admin/activity/summary'));
  let d = $derived(sec.data ?? {});

  let limit = $state(50);
  let offset = $state(0);

  async function load() {
    sec = loadingSection('/admin/activity/audit');
    sum = loadingSection('/admin/activity/summary');
    const [a, s] = await Promise.all([
      fetchSection('/admin/activity/audit?' + buildQuery(f, { limit, offset })),
      fetchSection('/admin/activity/summary?' + buildQuery(f))
    ]);
    sec = a;
    sum = s;
    reportTz?.(tzEcho(sec.data));
  }
  // One fetch per change: a filter change rewinds the pager, and the load hangs
  // off a key that includes it, so the two never race into two requests.
  let loadKey = $derived(`${qs}|${nonce}|${offset}|${limit}`);
  $effect(() => {
    void qs;
    void nonce;
    untrack(() => {
      offset = 0;
    });
  });
  $effect(() => {
    void loadKey;
    untrack(() => load());
  });

  let raw = $derived(sum.data?.signins ?? {});
  let K = $derived({
    ok: kpi(raw.login_ok),
    fail: kpi(raw.login_fail),
    locked: kpi(raw.login_locked),
    blocked: kpi(raw.login_blocked)
  });
  let attempts = $derived(kpi(raw.attempts));
  // A rate with its denominator, from the backend's own pair (§3).
  let successFoot = $derived.by(() => {
    const r = raw?.success_rate;
    if (!r || !isNum(r.rate)) return 'success rate not reported';
    const pct = (r.rate > 1 ? r.rate : r.rate * 100).toFixed(0);
    return isNum(r.n) ? `${pct}% of ${int(r.n)} attempts` : `${pct}% of an unreported number of attempts`;
  });

  // `signins` is flat rows: one per bucket, a column per outcome. They stay
  // SEPARATE series — a lock-out is the consequence of several failures, so
  // adding it to them would count one incident twice.
  const OUTCOME_COLS = [
    { key: 'login_ok', label: 'Signed in', color: COLOR.ok },
    { key: 'sso_ok', label: 'SSO signed in', color: 'var(--color-series-2)' },
    { key: 'login_fail', label: 'Failed', color: COLOR.bad },
    { key: 'sso_fail', label: 'SSO failed', color: 'var(--color-danger)' },
    { key: 'login_locked', label: 'Locked out', color: COLOR.warn },
    { key: 'login_blocked', label: 'Blocked by IP', color: COLOR.muted }
  ];
  let signinRows = $derived(Array.isArray(d?.signins) ? d.signins : []);
  // A column that is zero in every bucket adds a legend entry and no
  // information. It is dropped from the CHART only — never from the KPI row,
  // where "0 lock-outs" is a fact worth stating.
  let outcomeCols = $derived(
    OUTCOME_COLS.filter((c) => signinRows.some((r) => isNum(r?.[c.key]) && r[c.key] > 0))
  );
  let overTime = $derived(fromRows(signinRows, outcomeCols.length ? outcomeCols : OUTCOME_COLS.slice(0, 3)));

  let denied = $derived(
    (Array.isArray(d?.forbidden) ? d.forbidden : []).map((r, i) => ({
      key: r?.action == null ? '' : String(r.action),
      label: memberLabel(r?.action),
      // One account hitting a wall forty times and forty accounts hitting it
      // once are different incidents behind the same count, so the account
      // spread rides alongside the bar rather than being averaged away.
      sub: isNum(r?.actors) ? `${int(r.actors)} account${r.actors === 1 ? '' : 's'}` : '',
      value: isNum(r?.n) ? r.n : null,
      tone: 'warn'
    }))
  );

  let rows = $derived(
    (Array.isArray(d?.events) ? d.events : []).map((r, i) => ({
      id: `${r?.ts}|${i}`,
      ts: r?.ts ?? null,
      event: r?.event ?? r?.action ?? null,
      account: r?.email ?? r?.target ?? null,
      actor: r?.actor ?? null,
      ip: r?.ip ?? null,
      detail: r?.detail ?? null
    }))
  );
  let total = $derived(isNum(d?.total) ? d.total : null);
  let hasNext = $derived(total == null ? rows.length === limit : offset + limit < total);

  const cols = [
    { key: 'ts', label: 'When' },
    { key: 'event', label: 'Event' },
    { key: 'account', label: 'Account' },
    { key: 'ip', label: 'IP' },
    { key: 'detail', label: 'Detail' }
  ];

  function when(ts) {
    if (!ts) return UNKNOWN;
    const dt = new Date(ts);
    if (Number.isNaN(dt.getTime())) return String(ts);
    return dt.toLocaleString(undefined, {
      day: 'numeric',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit'
    });
  }
  function detailText(v) {
    if (v == null) return null;
    if (typeof v === 'string') return v.trim() || null;
    if (typeof v === 'object') {
      const inner = v.detail ?? v.reason ?? v.message;
      if (typeof inner === 'string' && inner.trim()) return inner.trim();
      try {
        return JSON.stringify(v);
      } catch {
        return null;
      }
    }
    return String(v);
  }
  function pickDay(i) {
    const day = String(overTime.keys[i] ?? '').slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return;
    setParams({ from: day, to: day });
  }
</script>

<Section
  title="Sign-in outcomes"
  hint="Four separate facts. A lock-out is the consequence of several failed attempts, so it is counted apart from them rather than added to them."
  state={sum}
  retry={load}
  what="the sign-in summary"
>
  <div class="grid gap-3 [grid-template-columns:repeat(auto-fit,minmax(212px,1fr))]">
    <!-- Only the first of these is an improvement when it rises. Lock-outs and
         blocks going up is the threshold doing its job against somebody, not a
         win, so each card declares its own `good` direction. -->
    <Kpi
      label="Successful sign-ins"
      value={isNum(K.ok.value) ? int(K.ok.value) : null}
      spark={K.ok.spark}
      tone="ok"
      delta={deltaOf(raw.login_ok)}
      good="up"
      unfiltered={unfilteredOf(raw)}
      foot={successFoot}
    />
    <Kpi
      label="Failed sign-ins"
      value={isNum(K.fail.value) ? int(K.fail.value) : null}
      spark={K.fail.spark}
      tone="bad"
      delta={deltaOf(raw.login_fail)}
      good="down"
      unfiltered={unfilteredOf(raw)}
      foot={isNum(attempts.value)
        ? `wrong password or unknown account, of ${int(attempts.value)} attempts`
        : 'wrong password or unknown account'}
    />
    <Kpi
      label="Lock-outs"
      value={isNum(K.locked.value) ? int(K.locked.value) : null}
      spark={K.locked.spark}
      tone="warn"
      delta={deltaOf(raw.login_locked)}
      good="down"
      unfiltered={unfilteredOf(raw)}
      foot="LOGIN_MAX_FAIL / LOGIN_LOCK_MINUTES — the consequence of several failures, counted apart from them"
    />
    <Kpi
      label="Blocked by IP"
      value={isNum(K.blocked.value) ? int(K.blocked.value) : null}
      spark={K.blocked.spark}
      tone="warn"
      delta={deltaOf(raw.login_blocked)}
      good="down"
      unfiltered={unfilteredOf(raw)}
      foot="LOGIN_IP_MAX_FAIL reached"
    />
  </div>
</Section>

<Section
  title="Sign-in outcomes over time"
  hint="Signed in, failed and locked out as three series — the shape of the mix is what tells you whether somebody is guessing."
  state={sec}
  retry={load}
  what="sign-in outcomes over time"
>
  <StackedBars labels={overTime.labels} series={overTime.series} onpick={(i) => pickDay(i)} />
  {#if sec.status === 'ok' && outcomeCols.length && outcomeCols.length < OUTCOME_COLS.length}
    <p class="mt-2 text-meta text-ink-3">
      Outcomes with no occurrence anywhere in this range are left off the legend:
      {OUTCOME_COLS.filter((c) => !outcomeCols.includes(c))
        .map((c) => c.label.toLowerCase())
        .join(', ')}. They are still counted above, where a measured zero is worth stating.
    </p>
  {/if}
</Section>

<Section
  title="Denied by permission"
  hint="A 403 means the account was known and refused — a role or scope question, not a credentials one. Worth reading every one."
  state={sec}
  retry={load}
  what="the 403 ranking"
>
  <RankBars
    rows={denied}
    onpick={(r) => (r.key ? setParams(openSection('feed', { action: r.key })) : null)}
    empty="No request was refused on permissions in this range."
  />
  {#if denied.length}
    <p class="mt-2 text-meta leading-relaxed text-ink-3">
      A scoped account reaching repeatedly for a super-admin route is either a misconfigured role or
      somebody exploring. Click one to see the attempts themselves in the feed.
    </p>
  {/if}
</Section>

<Section
  title="Every security event"
  hint="The full audit trail behind the numbers above — sign-ins, failures, lock-outs and IP blocks, newest first."
  state={sec}
  retry={load}
  what="the security event list"
>
  <Table
    {cols}
    {rows}
    rowKey={(r) => r.id}
    empty="No security events in this range. The table answered — it is not that logging is off."
  >
    {#snippet row(r)}
      <td class="tnum font-mono text-label whitespace-nowrap">{when(r.ts)}</td>
      <td>
        {#if r.event}
          <Badge tone={EVENT_TONE[String(r.event)] ?? 'neutral'}>
            {EVENT_LABEL[String(r.event)] ?? r.event}
          </Badge>
        {:else}
          <span class="text-ink-3">{UNKNOWN}</span>
        {/if}
      </td>
      <td class="max-w-[240px] truncate">
        {#if r.account}{r.account}{:else}<span class="text-ink-3">{UNKNOWN}</span>{/if}
      </td>
      <td class="font-mono text-label whitespace-nowrap">
        {#if r.ip}{r.ip}{:else}<span class="text-ink-3">{UNKNOWN}</span>{/if}
      </td>
      <td class="max-w-[280px] truncate">
        {#if detailText(r.detail)}{detailText(r.detail)}{:else}<span class="text-ink-3 italic"
            >nothing stored</span
          >{/if}
      </td>
    {/snippet}
  </Table>

  <div class="mt-3 flex flex-wrap items-center gap-3 text-meta text-ink-3">
    <span>
      Showing <b class="tnum text-ink">{rows.length ? offset + 1 : 0}–{offset + rows.length}</b>
      {#if total != null}
        of <b class="tnum text-ink">{int(total)}</b>
      {:else}
        <span class="italic"> — the API did not report a total</span>
      {/if}
    </span>
    <div class="ml-auto flex items-center gap-1.5">
      <button
        onclick={() => (offset = Math.max(0, offset - limit))}
        disabled={offset === 0}
        class="min-h-[36px] cursor-pointer rounded-panel border border-line px-3 text-ink-2 hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-40"
      >
        Previous
      </button>
      <button
        onclick={() => (offset += limit)}
        disabled={!hasNext}
        class="min-h-[36px] cursor-pointer rounded-panel border border-line px-3 text-ink-2 hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-40"
      >
        Next
      </button>
    </div>
  </div>
</Section>
