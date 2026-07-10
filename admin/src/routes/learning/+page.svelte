<script>
  import { API_BASE } from '$lib/apiBase.js';
  import { onMount } from 'svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import {
    Brain,
    Users,
    ThumbsUp,
    ThumbsDown,
    MessageSquare,
    Trash2,
    Database
  } from '@lucide/svelte';
  import { toast } from '$lib/aurora/toast.js';

  const base = API_BASE;

  let view = $state('memories'); // 'memories' | 'feedback'

  // ---- stats ----
  let lStats = $state(null);
  let fStats = $state(null);

  // ---- memories ----
  let mRows = $state([]);
  let mLoading = $state(true);
  let mError = $state(null);

  // ---- feedback ----
  let fRows = $state([]);
  let fLoading = $state(true);
  let fError = $state(null);
  let fFilter = $state(''); // '' | 'up' | 'down'

  const fmt = (v) => (v == null ? '–' : Number(v).toLocaleString());

  // GET helper; throws on non-OK so callers can show a friendly message.
  async function getJSON(path) {
    const r = await fetch(base + path);
    if (!r.ok) {
      throw new Error(
        r.status === 401 ? 'session expired — sign in again' : `couldn't load (${r.status})`
      );
    }
    return r.json();
  }

  function truncate(s, n = 90) {
    if (!s) return '';
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  // short relative-ish date: "3h ago", "2d ago", else a short date.
  function relTime(iso) {
    if (!iso) return '–';
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return '–';
    const diff = Date.now() - t;
    const m = Math.round(diff / 60000);
    if (m < 1) return 'just now';
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h}h ago`;
    const d = Math.round(h / 24);
    if (d < 30) return `${d}d ago`;
    return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }
  function shortWhen(iso) {
    if (!iso) return '–';
    const t = new Date(iso);
    if (Number.isNaN(t.getTime())) return '–';
    return t.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    });
  }

  // colored badge per learning_type
  function typeBadge(t) {
    const k = (t ?? '').toLowerCase();
    if (k === 'session_context')
      return { cls: 'bg-info-soft text-info', txt: 'Session context' };
    if (k === 'user_memory') return { cls: 'bg-warning-soft text-warning', txt: 'User memory' };
    if (k === 'decision_log') return { cls: 'bg-surface-2 text-ink-2', txt: 'Decision log' };
    // unknown -> slate
    return {
      cls: 'bg-surface-2 text-ink-2',
      txt: (t ?? 'Unknown').replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
    };
  }

  async function loadStats() {
    try {
      lStats = await getJSON('/admin/learning/stats');
    } catch {
      lStats = null;
    }
    try {
      fStats = await getJSON('/admin/feedback/stats');
    } catch {
      fStats = null;
    }
  }

  async function loadMemories() {
    mLoading = true;
    mError = null;
    try {
      const data = await getJSON('/admin/learning?limit=200');
      mRows = Array.isArray(data) ? data : [];
    } catch (e) {
      mError = e.message || "couldn't load";
      mRows = [];
    } finally {
      mLoading = false;
    }
  }

  async function loadFeedback() {
    fLoading = true;
    fError = null;
    try {
      const data = await getJSON('/admin/feedback?limit=100');
      fRows = Array.isArray(data) ? data : [];
    } catch (e) {
      fError = e.message || "couldn't load";
      fRows = [];
    } finally {
      fLoading = false;
    }
  }

  // optimistic delete with rollback on failure
  async function forget(id) {
    const idx = mRows.findIndex((r) => r.id === id);
    if (idx === -1) return;
    const removed = mRows[idx];
    mRows = mRows.filter((r) => r.id !== id);
    try {
      const r = await fetch(`${base}/admin/learning/${encodeURIComponent(id)}`, {
        method: 'DELETE'
      });
      if (!r.ok) throw new Error(String(r.status));
      // keep KPI honest
      if (lStats && typeof lStats.total === 'number') lStats.total = Math.max(0, lStats.total - 1);
      toast('Memory forgotten');
    } catch {
      // rollback
      mRows = [...mRows.slice(0, idx), removed, ...mRows.slice(idx)];
      toast('Could not forget — try again', 'alert');
    }
  }

  let fFiltered = $derived(fFilter ? fRows.filter((r) => r.verdict === fFilter) : fRows);

  onMount(() => {
    loadStats();
    loadMemories();
    loadFeedback();
  });
</script>

<PageHeader
  title="Self-learning"
  subtitle="What the assistant has learned from conversations — user preferences and session context only. Clinical facts are never learned; they always come from live data. Review, audit, and forget memories here."
/>

<!-- KPI strip -->
<div class="my-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
  {#each [['Total memories', fmt(lStats?.total), Brain], ['Users learned about', fmt(lStats?.users), Users], ['Positive feedback', fmt(fStats?.up), ThumbsUp], ['Negative feedback', fmt(fStats?.down), ThumbsDown]] as [k, v, Icon]}
    <div class="elev rounded-[14px] border-[0.5px] border-line bg-surface p-3.5">
      <div class="page-title text-[23px] tnum text-ink">{v}</div>
      <div class="mt-0.5 flex items-center gap-1.5 text-[12px] text-ink-2"><Icon size={13} /> {k}</div>
    </div>
  {/each}
</div>

<!-- segmented -->
<div class="flex items-center gap-3">
  <div class="inline-flex rounded-[11px] bg-surface-2 p-[3px]">
    {#each [['memories', 'Memories', Brain], ['feedback', 'Feedback', MessageSquare]] as [id, label, Icon]}
      <button
        onclick={() => (view = id)}
        class="flex cursor-pointer items-center gap-1.5 rounded-[9px] px-4 py-1.5 text-[13.5px] font-semibold transition-colors
          {view === id ? 'bg-surface text-ink shadow-[var(--shadow-card)]' : 'text-ink-2'}"
      >
        <Icon size={15} /> {label}
      </button>
    {/each}
  </div>
  <span class="text-[12.5px] text-ink-3">
    {view === 'memories'
      ? `${fmt(lStats?.total ?? mRows.length)} memories`
      : `${fmt(fStats?.total ?? fRows.length)} responses rated`}
  </span>
</div>

{#if view === 'memories'}
  <!-- MEMORIES table -->
  <div class="my-3.5 elev overflow-hidden rounded-[18px] border-[0.5px] border-line bg-surface">
    <div class="max-h-[calc(100vh-360px)] overflow-auto">
      <table class="tbl">
        <thead>
          <tr>
            <th>Type</th>
            <th>User</th>
            <th>What it learned</th>
            <th>Updated</th>
            <th style="width:60px"></th>
          </tr>
        </thead>
        <tbody>
          {#if mLoading}
            {#each Array(6) as _}
              <tr><td colspan="5"><div class="skel" style="height:16px"></div></td></tr>
            {/each}
          {:else if mError}
            <tr><td colspan="5" class="py-10 text-center text-ink-3">{mError}</td></tr>
          {:else if mRows.length === 0}
            <tr><td colspan="5" class="py-10 text-center text-ink-3">No learned memories yet — chat with the assistant to build memory.</td></tr>
          {:else}
            {#each mRows as r (r.id)}
              {@const tb = typeBadge(r.learning_type)}
              <tr>
                <td><span class="rounded-md px-2 py-0.5 text-[10.5px] font-semibold {tb.cls}">{tb.txt}</span></td>
                <td class="font-mono text-[12px] text-ink-2">{r.user_id ?? '—'}</td>
                <td class="text-ink" title={r.summary}>{truncate(r.summary)}</td>
                <td class="text-[12.5px] text-ink-2" title={r.updated_at}>{relTime(r.updated_at)}</td>
                <td>
                  <button
                    onclick={() => forget(r.id)}
                    aria-label="Forget this memory"
                    title="Forget"
                    class="flex h-8 w-8 cursor-pointer items-center justify-center rounded-lg text-ink-3 transition-colors hover:bg-danger-soft hover:text-danger"
                  >
                    <Trash2 size={15} />
                  </button>
                </td>
              </tr>
            {/each}
          {/if}
        </tbody>
      </table>
    </div>
  </div>
{:else}
  <!-- FEEDBACK filter chips -->
  <div class="my-3.5 flex flex-wrap items-center gap-2">
    {#each [['', 'All', null], ['up', 'Positive', ThumbsUp], ['down', 'Negative', ThumbsDown]] as [val, label, Icon]}
      <button
        onclick={() => (fFilter = val)}
        class="flex items-center gap-1.5 rounded-[9px] border px-3 py-1.5 text-[12.5px] font-medium transition-colors
          {fFilter === val
          ? 'border-transparent bg-accent-soft text-accent'
          : 'border-line bg-surface text-ink-2 hover:border-accent hover:text-accent'}"
      >
        {#if Icon}<Icon size={13} />{/if}
        {label}
      </button>
    {/each}
  </div>

  <!-- FEEDBACK table -->
  <div class="elev overflow-hidden rounded-[18px] border-[0.5px] border-line bg-surface">
    <div class="max-h-[calc(100vh-360px)] overflow-auto">
      <table class="tbl">
        <thead>
          <tr>
            <th>When</th>
            <th>Verdict</th>
            <th>Model</th>
            <th>Question</th>
            <th>Correction</th>
            <th>Store</th>
          </tr>
        </thead>
        <tbody>
          {#if fLoading}
            {#each Array(6) as _}
              <tr><td colspan="6"><div class="skel" style="height:16px"></div></td></tr>
            {/each}
          {:else if fError}
            <tr><td colspan="6" class="py-10 text-center text-ink-3">{fError}</td></tr>
          {:else if fFiltered.length === 0}
            <tr><td colspan="6" class="py-10 text-center text-ink-3">No feedback yet — ratings from the chat appear here.</td></tr>
          {:else}
            {#each fFiltered as r (r.id)}
              <tr>
                <td class="text-[12.5px] text-ink-2" title={r.ts}>{shortWhen(r.ts)}</td>
                <td>
                  {#if r.verdict === 'up'}
                    <span class="inline-flex items-center gap-1 rounded-md bg-success-soft px-2 py-0.5 text-[10.5px] font-semibold text-success"><ThumbsUp size={11} /> Up</span>
                  {:else}
                    <span class="inline-flex items-center gap-1 rounded-md bg-danger-soft px-2 py-0.5 text-[10.5px] font-semibold text-danger"><ThumbsDown size={11} /> Down</span>
                  {/if}
                </td>
                <td class="font-mono text-[11.5px] text-ink-2">{r.model ?? '—'}</td>
                <td class="text-ink" title={r.question}>{truncate(r.question)}</td>
                <td>
                  {#if r.correction}
                    <span class="italic text-ink-2" title={r.correction}>{truncate(r.correction, 70)}</span>
                  {:else}
                    <span class="text-ink-3">—</span>
                  {/if}
                </td>
                <td class="font-mono text-[12px] text-ink-2">{r.store_id ?? '—'}</td>
              </tr>
            {/each}
          {/if}
        </tbody>
      </table>
    </div>
  </div>
{/if}
