<script>
  // The Activity filter bar, lifted out of the retired /activity page when
  // Activity was folded into Analytics.
  //
  // It stays a SEPARATE bar from the Analytics FilterBar rather than being
  // merged into it. The two bars filter different tables with different
  // columns: Analytics narrows chat turns (store, language, model, embed),
  // Activity narrows events (source, actor, action). A single bar would have to
  // show controls that do nothing on the tab you are looking at, and a control
  // that silently does nothing is the defect this console keeps finding.
  //
  // Every control writes to the URL, so a filtered view is a link — including
  // the `?source=auth` link the Auth settings panel and the retired
  // /security-log page both point at.
  import { Search, X, Calendar, Server, KeyRound, Upload } from '@lucide/svelte';
  import { SOURCES } from './shared.js';

  // `locked` is set when the PAGE is one source — the security log is the auth
  // slice of this feed. The source buttons are then not drawn at all rather
  // than drawn inert: a row of source chips that cannot be pressed, above a
  // list that only ever shows one source, reads as a broken filter.
  let {
    f,
    setParams,
    onclear,
    actorInput = $bindable(''),
    qInput = $bindable(''),
    ontext,
    locked = null
  } = $props();

  const SOURCE_ICON = { app: Server, auth: KeyRound, ingest: Upload };
  let anyFilter = $derived(!!(f.source || f.actor || f.action || f.from || f.to || f.q));
</script>

<div
  class="elev mb-5 flex flex-wrap items-center gap-2 rounded-panel border border-line bg-surface px-3 py-2.5"
>
{#if !locked}
  <div class="flex flex-wrap items-center gap-1">
    <button
      onclick={() => setParams({ source: null, offset: null })}
      aria-pressed={f.source === ''}
      class="min-h-[36px] cursor-pointer rounded-card px-2.5 text-meta font-medium {f.source === ''
        ? 'bg-accent text-on-accent'
        : 'border border-line bg-surface text-ink-2 hover:border-accent hover:text-accent'}"
    >
      All sources
    </button>
    {#each SOURCES as s (s.key)}
      {@const Icon = SOURCE_ICON[s.key]}
      <button
        onclick={() => setParams({ source: f.source === s.key ? null : s.key, offset: null })}
        aria-pressed={f.source === s.key}
        class="flex min-h-[36px] cursor-pointer items-center gap-1.5 rounded-card px-2.5 text-meta font-medium {f.source ===
        s.key
          ? 'bg-accent text-on-accent'
          : 'border border-line bg-surface text-ink-2 hover:border-accent hover:text-accent'}"
      >
        <Icon size={13} />
        {s.label}
      </button>
    {/each}
  </div>
  {/if}

  <div
    class="flex min-h-[36px] min-w-[160px] items-center gap-2 rounded-card border border-line bg-surface-2 px-2.5"
  >
    <Search size={14} class="flex-none text-ink-3" />
    <input
      bind:value={actorInput}
      oninput={ontext}
      aria-label="Filter by actor"
      placeholder="Actor…"
      class="w-full min-w-0 border-0 bg-transparent text-meta text-ink outline-none placeholder:text-ink-3"
    />
  </div>

  <label
    class="flex min-h-[36px] items-center gap-1.5 rounded-card border border-line bg-surface px-2.5 text-meta text-ink-2"
  >
    <Calendar size={14} class="text-ink-3" />
    <span class="sr-only">From date</span>
    <input
      type="date"
      value={f.from}
      onchange={(e) => setParams({ from: e.currentTarget.value, offset: null })}
      aria-label="From date"
      class="cursor-pointer border-0 bg-transparent text-meta text-ink outline-none"
    />
    <span class="text-ink-3">→</span>
    <span class="sr-only">To date</span>
    <input
      type="date"
      value={f.to}
      onchange={(e) => setParams({ to: e.currentTarget.value, offset: null })}
      aria-label="To date"
      class="cursor-pointer border-0 bg-transparent text-meta text-ink outline-none"
    />
  </label>

  <div
    class="flex min-h-[36px] min-w-[190px] flex-1 items-center gap-2 rounded-card border border-line bg-surface-2 px-2.5"
  >
    <Search size={14} class="flex-none text-ink-3" />
    <input
      bind:value={qInput}
      oninput={ontext}
      aria-label="Search every field"
      placeholder="Search target, detail, anything…"
      class="w-full min-w-0 border-0 bg-transparent text-meta text-ink outline-none placeholder:text-ink-3"
    />
  </div>

  {#if f.action}
    <span
      class="flex min-h-[36px] items-center gap-1.5 rounded-card border border-accent bg-accent-soft px-2.5 text-meta font-medium text-accent"
    >
      <span class="max-w-[220px] truncate font-mono text-label">{f.action}</span>
      <button
        onclick={() => setParams({ action: null, offset: null })}
        aria-label="Clear the action filter"
        class="cursor-pointer"><X size={13} /></button
      >
    </span>
  {/if}

  {#if anyFilter}
    <button
      onclick={onclear}
      class="flex min-h-[36px] cursor-pointer items-center gap-1.5 rounded-card border border-line bg-surface px-2.5 text-meta font-medium text-ink-2 hover:border-accent hover:text-accent"
    >
      <X size={14} /> Clear
    </button>
  {/if}
</div>
