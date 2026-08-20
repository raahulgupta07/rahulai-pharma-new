<script>
  import { dialog } from '$lib/aurora/dialog.js';
  // The full turn. Every table row on every tab opens this.
  //
  // The field list is fixed and complete on purpose: a drawer that shows only
  // the fields that happen to be populated teaches the reader that the missing
  // ones do not exist. Every field is always listed, and one that is not
  // recorded says so — "— not recorded", "— pricing not configured". None of
  // them ever renders as 0.
  //
  // Two requests feed it, independently: the turn row itself, and the trace
  // (per-call rows). If the trace endpoint is not deployed the drawer still
  // opens; the per-call fields say they are not recorded.
  import { getJSON, ApiError } from '$lib/api.js';
  import Badge from '$lib/Badge.svelte';
  import { X } from '@lucide/svelte';
  import { UNKNOWN, isNum, int, ms, usd, whenFull, langName, clip } from './format.js';

  let { turnId = null, onclose, ontrace = null } = $props();

  let turn = $state(null);
  let trace = $state(null);
  let err = $state(null);
  let loading = $state(false);
  let closeBtn = $state(null);

  /**
   * `/trace/{id}` returns the full turn row AND its interleaved calls, so one
   * request fills the whole drawer. `/question/{id}` is kept only as a fallback
   * for a backend that predates the trace route — without it the drawer would
   * go blank on an older deployment rather than degrading to the turn alone.
   *
   * A 404 from trace is ambiguous by design: unknown turn and out-of-scope turn
   * answer identically, so a pinned admin cannot probe for the existence of a
   * sibling branch's turns by reading status codes. The fallback therefore also
   * has to be allowed to fail, and the drawer says only that it could not load.
   */
  async function load(id) {
    loading = true;
    turn = null;
    trace = null;
    err = null;
    try {
      const t = await getJSON(`/admin/analytics/trace/${encodeURIComponent(id)}`);
      trace = t;
      turn = t?.turn ?? null;
    } catch (e) {
      trace = e instanceof ApiError && e.status === 404 ? { missing: true } : { failed: true };
      try {
        turn = await getJSON(`/admin/analytics/question/${encodeURIComponent(id)}`);
      } catch (e2) {
        err = e2;
      }
    }
    loading = false;
  }

  $effect(() => {
    if (turnId != null) load(turnId);
  });

  // Focus-in and Escape used to live here. They were correct as far as they
  // went, but there was no Tab trap: two Tabs past Close landed on the skip
  // link and then walked the nav rail, with the drawer still open and the user
  // invisible under the scrim. Closing left them in the rail rather than back
  // at the row they came from. `use:dialog` on the drawer element does all
  // four, so this hook is gone rather than half-doing two of them.

  // ---- derived fields ------------------------------------------------------
  let calls = $derived(Array.isArray(trace?.calls) ? trace.calls : []);
  let llm = $derived(calls.filter((c) => c.kind === 'llm'));
  let toolCalls = $derived(calls.filter((c) => c.kind === 'tool'));

  /** Sum that stays null when NOTHING contributed a number. */
  function sumOrNull(list, key) {
    let seen = false;
    let acc = 0;
    for (const c of list) {
      if (isNum(c?.[key])) {
        seen = true;
        acc += c[key];
      }
    }
    return seen ? acc : null;
  }

  let promptTok = $derived(sumOrNull(llm, 'prompt_tokens'));
  let complTok = $derived(sumOrNull(llm, 'completion_tokens'));
  let cacheRead = $derived(sumOrNull(llm, 'cache_read_tokens'));
  let cacheNew = $derived(sumOrNull(llm, 'cache_creation_tokens'));
  let cost = $derived(sumOrNull(llm, 'cost_usd'));
  let costEstimated = $derived(llm.some((c) => c.cost_is_estimated));
  let ttft = $derived(llm.map((c) => c.ttft_ms).find(isNum) ?? null);

  let toolsText = $derived.by(() => {
    if (toolCalls.length) {
      return toolCalls
        .map((c) => `${c.name} → ${c.outcome}${isNum(c.duration_ms) ? ` · ${ms(c.duration_ms)}` : ''}`)
        .join('\n');
    }
    const names = Array.isArray(turn?.tools) ? turn.tools : [];
    if (!names.length) return null;
    // Names only — the pre-instrumentation shape. Saying so is the point:
    // a name with no outcome cannot tell a refusal from a crash.
    return `${names.join(', ')} — outcome not recorded`;
  });
</script>

<!-- Scrim. Clicking outside closes, same as Escape. -->
<div
  class="scrim fixed inset-0 z-40 bg-[rgba(6,8,14,0.34)]"
  role="presentation"
  onclick={onclose}
></div>

<div
  use:dialog={{ onclose }}
  class="drawer fixed inset-y-0 right-0 z-50 w-[min(470px,100%)] overflow-y-auto border-l border-line bg-surface p-5 outline-none"
  role="dialog"
  aria-modal="true"
  aria-label="Turn detail"
  tabindex="-1"
>
  <button
    bind:this={closeBtn}
    onclick={onclose}
    class="absolute top-4 right-4 flex h-8 w-8 cursor-pointer items-center justify-center rounded-panel border border-line text-ink-2 hover:bg-surface-2"
    aria-label="Close"
  >
    <X size="16" />
  </button>

  {#if loading}
    <div class="mt-8 space-y-2">
      {#each [1, 2, 3, 4, 5, 6] as i}<div class="h-4 animate-pulse rounded bg-surface-2"></div>{/each}
    </div>
  {:else if err}
    <p class="mt-8 text-body-sm text-ink-2">
      This turn could not be loaded ({err.status || 'no response'}). It may have been outside your
      store scope, or removed.
    </p>
  {:else if turn}
    <h4 class="bilingual page-title mt-1 pr-10 text-body font-extrabold text-ink">{turn.question || UNKNOWN}</h4>
    <div class="mt-0.5 text-meta text-ink-3">{whenFull(turn.ts)} · turn #{turn.id}</div>

    <dl class="mt-4 grid grid-cols-[112px_1fr] gap-x-3 gap-y-1.5 text-body-sm text-ink-2">
      <dt class="text-meta font-semibold text-ink-3">Answer</dt>
      <dd class="bilingual whitespace-pre-wrap">
        {#if turn.answer == null}<span class="text-ink-3">{UNKNOWN} not recorded</span>
        {:else if String(turn.answer).trim() === ''}<span class="text-danger">empty answer — the turn produced no text</span>
        {:else}{turn.answer}{/if}
      </dd>

      <dt class="text-meta font-semibold text-ink-3">Store</dt>
      <dd>{turn.store_id ?? UNKNOWN}</dd>

      <dt class="text-meta font-semibold text-ink-3">Language</dt>
      <dd>{langName(turn.lang)}</dd>

      <dt class="text-meta font-semibold text-ink-3">Path</dt>
      <dd>
        {#if turn.path}<Badge tone={turn.path === 'fast_path' ? 'ok' : 'info'}>{turn.path}</Badge>
        {:else}<Badge tone="neutral">not recorded</Badge>{/if}
      </dd>

      <dt class="text-meta font-semibold text-ink-3">Model</dt>
      <dd class="font-mono text-meta">{turn.model ?? UNKNOWN}</dd>

      <dt class="text-meta font-semibold text-ink-3">Tools</dt>
      <dd class="font-mono text-meta whitespace-pre-line">
        {#if toolsText}{toolsText}{:else}<span class="text-ink-3">{UNKNOWN} none recorded</span>{/if}
      </dd>

      <dt class="text-meta font-semibold text-ink-3">Tokens</dt>
      <dd class="tnum">
        {#if promptTok != null || complTok != null}
          {int(promptTok)} prompt + {int(complTok)} completion
          {#if promptTok != null && complTok != null}= {int(promptTok + complTok)}{/if}
        {:else}<span class="text-ink-3">{UNKNOWN} not recorded on this turn</span>{/if}
      </dd>

      <dt class="text-meta font-semibold text-ink-3">Cache split</dt>
      <dd class="tnum">
        {#if cacheRead != null || cacheNew != null}
          {int(cacheRead)} read · {int(cacheNew)} created
        {:else}<span class="text-ink-3">{UNKNOWN} not recorded — cannot be backfilled</span>{/if}
      </dd>

      <dt class="text-meta font-semibold text-ink-3">Cost</dt>
      <dd class="tnum">
        {#if cost != null}
          {usd(cost)}{#if costEstimated}<span class="ml-1.5 rounded border border-warning px-1 py-px text-micro font-semibold text-warning">estimated</span>{/if}
        {:else}<span class="text-ink-3">{UNKNOWN} pricing not configured</span>{/if}
      </dd>

      <dt class="text-meta font-semibold text-ink-3">Latency</dt>
      <dd class="tnum">
        {ms(turn.latency_ms)}{#if ttft != null} · ttft {ms(ttft)}{:else} · <span class="text-ink-3">ttft {UNKNOWN}</span>{/if}
      </dd>

      <dt class="text-meta font-semibold text-ink-3">Cached</dt>
      <dd>
        {#if turn.cached === true}yes — served from cache
        {:else if turn.cached === false}no
        {:else}<span class="text-ink-3">{UNKNOWN}</span>{/if}
      </dd>

      <dt class="text-meta font-semibold text-ink-3">Embed</dt>
      <dd>{turn.embed_id ?? `${UNKNOWN} unattributed`}</dd>

      <dt class="text-meta font-semibold text-ink-3">Session</dt>
      <dd class="font-mono text-meta">{turn.session_id ? clip(turn.session_id, 22) : UNKNOWN}</dd>

      <dt class="text-meta font-semibold text-ink-3">Asked by</dt>
      <dd>
        {#if turn.actor_email}
          {turn.actor_email}{#if turn.actor_role} · {turn.actor_role}{/if}
        {:else}<span class="text-ink-3">{UNKNOWN} no actor recorded</span>{/if}
      </dd>

      <dt class="text-meta font-semibold text-ink-3">Feedback</dt>
      <dd>
        {#if turn.feedback === 'up' || turn.rating === 1}👍 up
        {:else if turn.feedback === 'down' || turn.rating === -1}👎 down{#if turn.correction} — “{turn.correction}”{/if}
        {:else}<span class="text-ink-3">{UNKNOWN} not rated</span>{/if}
      </dd>
    </dl>

    {#if trace?.instrumented === false}
      <p class="mt-3 rounded-panel border border-line bg-surface-2 px-3 py-2 text-meta leading-relaxed text-ink-3">
        This turn predates per-call instrumentation, so it has no tool or LLM rows. The fields above
        that say “not recorded” are unknown for that reason — they are not zeros, and nothing has
        been backfilled for them.
      </p>
    {/if}

    {#if ontrace}
      <button
        onclick={() => ontrace(turn.id)}
        class="mt-4 min-h-[38px] w-full cursor-pointer rounded-panel border border-line px-3 py-2 text-body-sm font-medium text-ink hover:bg-surface-2"
      >
        Open the full trace for this turn
      </button>
    {/if}

    <p class="mt-3 text-meta leading-relaxed text-ink-3">
      Every field that is not recorded says so. None of them render as 0.
    </p>
  {/if}
</div>

<style>
  .drawer {
    box-shadow: -16px 0 40px rgba(0, 0, 0, 0.14);
    animation: slide 0.22s ease-out;
  }
  .scrim {
    animation: fade 0.22s ease-out;
  }
  @keyframes slide {
    from {
      transform: translateX(100%);
    }
  }
  @keyframes fade {
    from {
      opacity: 0;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .drawer,
    .scrim {
      animation: none;
    }
  }
</style>
