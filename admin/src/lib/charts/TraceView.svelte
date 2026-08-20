<script>
  // One turn, every tool_call and llm_call in order.
  //
  // This is the view that makes the rest of the page trustworthy. Without it,
  // a regression is debugged from summary statistics: you can see that p95 went
  // up and that a tool "was used", and nothing about which call was slow, what
  // it was asked, or whether it refused or crashed.
  //
  // Each row expands to its arguments (tool) or its prompt (llm). The three
  // outcome states stay distinct all the way down: `refused` is drawn as its
  // own state, never merged into `failed`.
  import { getJSON, ApiError } from '$lib/api.js';
  import ErrorState from '$lib/ErrorState.svelte';
  import Badge from '$lib/Badge.svelte';
  import { isNum, ms, int, usd, UNKNOWN, whenFull, clip } from './format.js';

  let { turnId = null, onclose = null } = $props();

  let state = $state({ status: 'idle', data: null, err: null });
  let openSeq = $state(new Set());

  async function load(id) {
    state = { status: 'loading', data: null, err: null };
    try {
      state = { status: 'ok', data: await getJSON(`/admin/analytics/trace/${encodeURIComponent(id)}`), err: null };
    } catch (e) {
      state = {
        status: e instanceof ApiError && e.status === 404 ? 'missing' : 'error',
        data: null,
        err: e
      };
    }
  }

  $effect(() => {
    if (turnId != null) load(turnId);
  });

  function toggle(seq) {
    const next = new Set(openSeq);
    if (next.has(seq)) next.delete(seq);
    else next.add(seq);
    openSeq = next;
  }

  let turn = $derived(state.data?.turn ?? null);
  let calls = $derived(Array.isArray(state.data?.calls) ? state.data.calls : []);

  // Offset from the start of the turn. Falls back to a cumulative sum of the
  // per-call durations when the rows carry no timestamp — and says nothing at
  // all when neither is available, rather than printing a made-up 0 ms.
  let rows = $derived.by(() => {
    const t0 = turn?.ts ? new Date(turn.ts).getTime() : null;
    let acc = 0;
    return calls.map((c) => {
      let at = null;
      if (t0 != null && c.ts) {
        const d = new Date(c.ts).getTime();
        if (!Number.isNaN(d)) at = Math.max(0, d - t0);
      }
      if (at == null && isNum(c.duration_ms)) {
        at = acc;
        acc += c.duration_ms;
      }
      return { ...c, at };
    });
  });

  const TONE = { succeeded: 'ok', refused: 'warn', failed: 'danger' };
</script>

{#if turnId == null}
  <p class="rounded-card border border-dashed border-line-2 bg-surface px-4 py-6 text-center text-body-sm text-ink-3">
    Pick a turn — from the diagnosis queue below, the turn list on Questions, or any chart —
    to see every call it made, in order.
  </p>
{:else if state.status === 'loading'}
  <div class="h-40 animate-pulse rounded-card border border-line bg-surface-2"></div>
{:else if state.status === 'missing'}
  <div class="rounded-card border border-dashed border-line-2 bg-surface-2 px-4 py-5 text-body-sm text-ink-2">
    <p class="font-medium text-ink">No trace for turn #{turnId}</p>
    <p class="mt-1">
      <span class="font-mono text-meta">/admin/analytics/trace/{turnId}</span> answered 404. Either
      this backend has no per-call tables yet, or this turn predates them — the 122 turns recorded
      before instrumentation have no calls to show, and none are invented for them.
    </p>
  </div>
{:else if state.status === 'error'}
  <ErrorState error={state.err} retry={() => load(turnId)} what="the trace for this turn" />
{:else}
  <div class="rounded-card border border-line bg-surface p-1.5">
    <div class="flex flex-wrap items-center gap-3 rounded-panel px-3 py-2.5 text-body-sm text-ink-2">
      <span class="w-14 flex-none text-right font-mono text-label text-ink-3">0 ms</span>
      <Badge tone="info">turn start</Badge>
      <span class="min-w-0 truncate">
        {turn?.question ?? UNKNOWN} · {turn?.store_id ?? UNKNOWN} · {turn?.lang ?? UNKNOWN}
      </span>
      <span class="ml-auto font-mono text-label text-ink-3">{whenFull(turn?.ts)}</span>
    </div>

    {#if rows.length === 0}
      <p class="px-3 py-4 text-body-sm text-ink-3">
        This turn recorded no calls. A cache hit runs no agent at all, and a fast-path turn makes one
        phrasing call — an empty trace is a real result, not a missing one.
      </p>
    {/if}

    {#each rows as c (`${c.kind}-${c.seq}`)}
      <div class="border-t border-line first:border-t-0">
        <button
          type="button"
          onclick={() => toggle(`${c.kind}-${c.seq}`)}
          class="flex w-full min-h-[44px] cursor-pointer flex-wrap items-center gap-3 rounded-panel px-3 py-2.5 text-left text-body-sm text-ink-2 hover:bg-accent-soft"
          aria-expanded={openSeq.has(`${c.kind}-${c.seq}`)}
        >
          <span class="w-14 flex-none text-right font-mono text-label text-ink-3">
            {c.at == null ? UNKNOWN : `${int(c.at)} ms`}
          </span>
          <Badge tone={c.kind === 'tool' ? 'neutral' : 'info'}>{c.kind}</Badge>
          {#if c.kind === 'tool'}
            <span class="min-w-0 truncate font-mono text-meta">{c.name}</span>
            <Badge tone={TONE[c.outcome] ?? 'neutral'}>{c.outcome ?? 'outcome not recorded'}</Badge>
            {#if isNum(c.attempt) && c.attempt > 1}<span class="text-ink-3">attempt {c.attempt}</span>{/if}
          {:else}
            <span class="min-w-0 truncate font-mono text-meta">{c.model ?? UNKNOWN}</span>
            <span class="tnum">
              {isNum(c.prompt_tokens) ? int(c.prompt_tokens) : UNKNOWN} prompt +
              {isNum(c.completion_tokens) ? int(c.completion_tokens) : UNKNOWN} completion
            </span>
            {#if isNum(c.ttft_ms)}<span class="text-ink-3">ttft {ms(c.ttft_ms)}</span>{/if}
          {/if}
          <span class="ml-auto tnum text-ink-3">{isNum(c.duration_ms) ? ms(c.duration_ms) : UNKNOWN}</span>
        </button>

        {#if openSeq.has(`${c.kind}-${c.seq}`)}
          <div class="mx-3 mb-3 rounded-panel border border-line bg-surface-2 p-3 text-meta">
            {#if c.kind === 'tool'}
              <p class="mb-1 font-semibold text-ink-3">Arguments</p>
              <pre class="overflow-x-auto font-mono text-label whitespace-pre-wrap text-ink-2">{c.arguments
                  ? JSON.stringify(c.arguments, null, 2)
                  : '— not recorded'}</pre>
              {#if c.error_message}
                <p class="mt-2 mb-1 font-semibold text-danger">Error</p>
                <pre class="overflow-x-auto font-mono text-label whitespace-pre-wrap text-danger">{c.error_message}</pre>
              {/if}
              {#if c.result_preview}
                <p class="mt-2 mb-1 font-semibold text-ink-3">Result</p>
                <pre class="overflow-x-auto font-mono text-label whitespace-pre-wrap text-ink-2">{clip(String(c.result_preview), 2000)}</pre>
              {/if}
            {:else}
              <p class="mb-1 font-semibold text-ink-3">Prompt</p>
              <pre class="overflow-x-auto font-mono text-label whitespace-pre-wrap text-ink-2">{c.prompt
                  ? clip(String(c.prompt), 4000)
                  : '— the prompt text is not stored for this call'}</pre>
              <dl class="mt-2 grid grid-cols-[130px_1fr] gap-x-3 gap-y-1 tnum">
                <dt class="text-ink-3">reasoning tokens</dt>
                <dd>{isNum(c.reasoning_tokens) ? int(c.reasoning_tokens) : UNKNOWN}</dd>
                <dt class="text-ink-3">cache read / created</dt>
                <dd>
                  {isNum(c.cache_read_tokens) ? int(c.cache_read_tokens) : UNKNOWN} /
                  {isNum(c.cache_creation_tokens) ? int(c.cache_creation_tokens) : UNKNOWN}
                </dd>
                <dt class="text-ink-3">cost</dt>
                <dd>
                  {#if isNum(c.cost_usd)}
                    {usd(c.cost_usd)}{#if c.cost_is_estimated}<span class="ml-1.5 rounded border border-warning px-1 py-px text-micro font-semibold text-warning">estimated</span>{/if}
                  {:else}<span class="text-ink-3">{UNKNOWN} pricing not configured</span>{/if}
                </dd>
                <dt class="text-ink-3">finish reason</dt>
                <dd>{c.finish_reason ?? UNKNOWN}</dd>
              </dl>
            {/if}
          </div>
        {/if}
      </div>
    {/each}
  </div>

  <div class="mt-2 flex flex-wrap items-center gap-3">
    <p class="text-meta text-ink-3">
      Showing turn #{turnId}. A tool row opens its arguments and error text; an llm row opens the
      prompt and the per-call token split.
    </p>
    {#if onclose}
      <button
        onclick={onclose}
        class="ml-auto min-h-[36px] cursor-pointer rounded-panel border border-line px-3 text-body-sm text-ink-2 hover:bg-surface-2"
      >Clear</button>
    {/if}
  </div>
{/if}
