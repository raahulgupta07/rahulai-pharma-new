<script>
  /**
   * A replayed terminal session — what a partner would actually type, typing
   * itself.
   *
   * WHY THIS AND NOT A VIDEO FILE. An mp4 would be dead weight in the image, it
   * could not be searched, copied or read by a screen reader, and — the part
   * that matters — it would show whatever hostname was true on the day somebody
   * recorded it. This renders from the SAME resolved values the handover block
   * uses, so it cannot demonstrate an address that does not exist. When the
   * address is unknown the caller passes `blocked`, and the cast refuses to run
   * rather than teaching a command that would fail.
   *
   * It is a REPLAY, not a live session, and it says so on screen. The output
   * lines are the ones these commands really print; nothing here is invented to
   * look successful, and nothing is executed.
   *
   * @typedef {{kind: 'cmd'|'out'|'note', text: string}} Line
   */
  import { Play, RotateCcw, SkipForward, Copy, Check, Terminal } from '@lucide/svelte';
  import { onDestroy } from 'svelte';

  let {
    title = 'Session',
    /** @type {Line[]} */ lines = [],
    /** Disables playback and says why — an unusable command must not be taught. */
    blocked = false,
    blockedReason = ''
  } = $props();

  // Typing speed. Commands type; output does not — output is not something the
  // user produces, and animating it only makes the wait longer.
  const CHAR_MS = 22;
  const AFTER_CMD_MS = 320;
  const AFTER_OUT_MS = 120;

  let idx = $state(0); // how many lines are fully rendered
  let partial = $state(''); // the line currently being typed
  let playing = $state(false);
  let done = $state(false);
  let copied = $state(false);

  // Respect the OS setting. `matchMedia` is read once and then LIVE — someone
  // turning reduced motion on mid-session should not have to reload to stop a
  // moving thing they just asked to stop.
  let reduced = $state(false);
  $effect(() => {
    if (typeof window === 'undefined') return;
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    const sync = () => (reduced = mq.matches);
    sync();
    mq.addEventListener('change', sync);
    return () => mq.removeEventListener('change', sync);
  });

  let timer = null;
  function clear() {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  }
  onDestroy(clear);

  function finish() {
    clear();
    idx = lines.length;
    partial = '';
    playing = false;
    done = true;
  }

  function step() {
    if (idx >= lines.length) return finish();
    const line = lines[idx];
    if (line.kind !== 'cmd') {
      idx += 1;
      partial = '';
      timer = setTimeout(step, AFTER_OUT_MS);
      return;
    }
    if (partial.length < line.text.length) {
      partial = line.text.slice(0, partial.length + 1);
      timer = setTimeout(step, CHAR_MS);
      return;
    }
    idx += 1;
    partial = '';
    timer = setTimeout(step, AFTER_CMD_MS);
  }

  function play() {
    if (blocked) return;
    clear();
    // Reduced motion gets the whole transcript at once. Not a degraded version
    // — the same content, without the movement.
    if (reduced) return finish();
    idx = 0;
    partial = '';
    done = false;
    playing = true;
    step();
  }

  function transcript() {
    return lines.map((l) => (l.kind === 'cmd' ? '$ ' + l.text : l.text)).join('\n');
  }

  async function copyAll() {
    try {
      await navigator.clipboard.writeText(transcript());
      copied = true;
      setTimeout(() => (copied = false), 1600);
    } catch {
      /* clipboard blocked — the text is on screen and selectable anyway */
    }
  }

  // What is on screen right now. Before the first play the whole transcript is
  // shown, NOT an empty box: somebody who never presses play must still be able
  // to read and copy every command.
  const shown = $derived(playing || done ? lines.slice(0, idx) : lines);
  const typing = $derived(playing && partial !== '' ? partial : null);
</script>

<div class="overflow-hidden rounded-panel border border-line bg-surface">
  <div class="flex items-center gap-2 border-b border-line bg-surface-2 px-3 py-2">
    <Terminal size={14} class="flex-none text-ink-3" />
    <span class="text-label font-semibold text-ink-2">{title}</span>
    <span class="rounded-full bg-surface px-2 py-0.5 text-micro text-ink-3">replay — nothing runs</span>
    <div class="ml-auto flex items-center gap-1">
      <button
        onclick={play}
        disabled={blocked}
        class="flex items-center gap-1.5 rounded-control px-2 py-1 text-meta text-ink-2 transition-colors hover:bg-surface hover:text-ink disabled:cursor-default disabled:opacity-50"
      >
        {#if done}<RotateCcw size={13} />Replay{:else}<Play size={13} />Play{/if}
      </button>
      {#if playing}
        <button
          onclick={finish}
          class="flex items-center gap-1.5 rounded-control px-2 py-1 text-meta text-ink-2 transition-colors hover:bg-surface hover:text-ink"
        >
          <SkipForward size={13} />Skip
        </button>
      {/if}
      <button
        onclick={copyAll}
        class="flex items-center gap-1.5 rounded-control px-2 py-1 text-meta text-ink-2 transition-colors hover:bg-surface hover:text-ink"
      >
        {#if copied}<Check size={13} />Copied{:else}<Copy size={13} />Copy{/if}
      </button>
    </div>
  </div>

  {#if blocked}
    <p class="px-3.5 py-3 text-body-sm text-ink-3">{blockedReason}</p>
  {:else}
    <!-- aria-live is off on purpose: announcing every typed character would make
         this unusable with a screen reader. The full transcript is in the DOM
         and readable at any time, which is the accessible path. -->
    <pre
      class="max-h-[320px] overflow-auto px-3.5 py-3 text-meta leading-relaxed"
      aria-live="off">{#each shown as l}<span
          class={l.kind === 'cmd'
            ? 'text-ink'
            : l.kind === 'note'
              ? 'text-ink-3'
              : 'text-ink-2'}>{l.kind === 'cmd' ? '$ ' : ''}{l.text}</span
        >{'\n'}{/each}{#if typing}<span class="text-ink">$ {typing}</span><span
          class="cursor-blink text-accent">▋</span>{/if}</pre>
  {/if}
</div>

<style>
  /* The one animation here. Killed outright under reduced motion — the global
     rule in app.css only shortens durations, which for an infinite blink means
     it blinks FASTER rather than stopping. */
  .cursor-blink {
    animation: blink 1.05s step-end infinite;
  }
  @keyframes blink {
    50% {
      opacity: 0;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .cursor-blink {
      animation: none;
    }
  }
</style>
