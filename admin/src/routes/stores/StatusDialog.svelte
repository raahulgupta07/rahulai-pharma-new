<!--
  Confirming "hide this branch from customers" / "show it again", in TWO steps.

  The two paths are deliberately ASYMMETRIC and must stay that way. Hiding is
  consequences + a reason, then TYPE THE BRANCH CODE. Showing is consequences,
  then a plain confirm. Making them symmetric is the failure mode this design
  exists to avoid: a second step that is the same shape as the first trains
  people to click through both without reading either, and then the typed code
  in front of the dangerous one stops being a check and becomes a toll.

  They are asymmetric because the acts are. Hiding silently changes what
  customers are told — a branch holding stock starts being answered as if it
  does not exist, and nothing on a customer's screen says so. Showing is
  recoverable and visible.

  ONE dialog element, with a `step` inside it — not two chained dialogs. That is
  a focus decision, not a structural preference: `use:dialog` captures
  `document.activeElement` when it mounts and hands focus back there when it
  tears down, so a step-2 dialog that replaces a step-1 dialog would capture a
  Continue button that is being removed in the same breath, and Cancel from step
  2 would drop focus on <body>. With one element the trap is continuous, Escape
  works from either step, the scroll lock is taken and released exactly once,
  and focus returns to the switch that opened it.

  There is no ✕ in the corner on purpose. `use:dialog` focuses the first
  focusable child, and with an ✕ first that is a Close button — so the reason
  field, the only thing anybody opens step 1 to type into, would need a Tab.
  Cancel and Escape are both still here.

  On the copy: the second consequence is the one that surprises people. Hiding a
  branch does not only remove it from answers, it removes its stock from every
  company-wide total on the console — so the numbers on the Overview move the
  moment this is confirmed. That is said here, with this branch's own figures,
  before the button is pressed. Finding out afterwards is what that paragraph
  exists to prevent.
-->
<script>
  import { tick } from 'svelte';
  import { dialog } from '$lib/aurora/dialog.js';
  import { int } from '$lib/charts/format.js';
  import { hasStock } from './status.js';
  import { estateShare } from './detail.js';

  let {
    open = false,
    /** The registry row being changed. May carry the detail payload's extras. */
    row = null,
    /** 'disabled' to hide it, 'active' to bring it back. */
    next = 'disabled',
    busy = false,
    /** Called with the typed reason (may be an empty string). */
    onconfirm,
    onclose
  } = $props();

  let step = $state(1);
  let note = $state('');
  let confirmCode = $state('');
  let codeField = $state(null);
  let backBtn = $state(null);

  // Reset everything the moment the dialog is opened, so a reason typed for one
  // branch — or worse, a branch code already typed into step 2 — cannot be
  // submitted against the next one.
  $effect(() => {
    if (open) {
      step = 1;
      note = '';
      confirmCode = '';
    }
  });

  let code = $derived(row?.site_code ?? '');
  let name = $derived(row?.site_name?.trim() || code || 'this branch');
  let hiding = $derived(next === 'disabled');
  let known = $derived(hasStock(row));

  /**
   * Case-insensitive, and trimmed.
   *
   * The check is "did you mean THIS branch", not "can you type". A pasted code
   * carrying a trailing space, or one typed in lower case, is a person who has
   * read the code and meant it; refusing those would only teach them to
   * copy-paste faster, which is precisely the reflex this step is trying not to
   * build.
   */
  let matches = $derived(confirmCode.trim().toUpperCase() === code.toUpperCase() && code !== '');

  async function goStep2() {
    step = 2;
    await tick();
    // Focus lands where the work is: the code field when there is one, and the
    // Back button otherwise — never on the confirm button, which would make
    // Enter-Enter walk both steps of a destructive change without reading it.
    (codeField ?? backBtn)?.focus();
  }

  // Back needs no focus move: the button the user just pressed stays mounted
  // and only changes its label from "Back" to "Cancel", so focus is already on
  // a real control. Re-focusing it would be a no-op that re-announces it.
  const goStep1 = () => (step = 1);

  function submit() {
    if (hiding && !matches) return;
    onconfirm?.(note.trim());
  }
</script>

{#if open && row}
  <div
    class="fixed inset-0 z-[90] flex items-center justify-center bg-black/50 backdrop-blur-[3px]"
    onclick={(e) => e.target === e.currentTarget && onclose?.()}
    role="presentation"
  >
    <div
      use:dialog={{ onclose: () => onclose?.() }}
      tabindex="-1"
      class="w-[500px] max-w-[calc(100vw-32px)] rounded-hero border border-line bg-surface
             shadow-[var(--shadow-pop)] outline-none"
      role="dialog"
      aria-modal="true"
      aria-labelledby="branch-status-title"
    >
      <div class="p-[22px]">
        <span class="block text-micro font-semibold uppercase tracking-[0.07em] text-ink-3">
          Step {step} of 2
        </span>

        <b id="branch-status-title" class="mt-1.5 block text-body font-semibold text-ink">
          {#if step === 1}
            {hiding ? `Hide ${name} from customers?` : `Show ${name} to customers again?`}
          {:else}
            {hiding ? 'Type the branch code to confirm' : 'Confirm'}
          {/if}
        </b>

        <div class="mt-3 space-y-3 text-body-sm leading-relaxed text-ink-2">
          {#if step === 1 && hiding}
            <p>
              Customers will be answered as if this branch does not exist. Ask where to find a
              medicine and {code} will never come back as one of the places that has it — even
              when it is holding the stock.
            </p>
            <p class="rounded-card border border-warning bg-warning-soft px-3.5 py-2.5 text-ink-2">
              <b class="text-ink">Company-wide totals drop as well.</b>
              {#if known}
                The
                <span class="tnum">{int(row.units)}</span>
                units and
                <span class="tnum">{int(row.value)}</span>
                MMK this branch holds come out of every total on the console — that is
                <span class="tnum">{estateShare(row.pct_of_value)}</span>
                of everything.
              {:else}
                Whatever stock this branch is holding comes out of every total on the console,
                so the figures on Overview will fall today.
              {/if}
            </p>
            <p>
              Nothing is deleted. The branch stays on this page, keeps its stock, and you can
              switch it back on whenever you like.
            </p>

            <label class="block">
              <span class="mb-1 block text-meta text-ink-3">
                Why are you hiding it? <span class="text-ink-3">(shown on this page)</span>
              </span>
              <!-- No `.field-shell`: that pair is for a bordered BOX wrapping a
                   borderless input, where the global focus ring would draw a
                   second edge inside the box. This textarea is the bordered
                   control itself, so the global ring is the right ring. -->
              <textarea
                bind:value={note}
                rows="2"
                maxlength="200"
                placeholder="branch closed"
                class="w-full rounded-card border border-line bg-page px-3 py-2
                       text-body-sm text-ink placeholder:text-ink-3 focus:border-accent"
              ></textarea>
            </label>
          {:else if step === 2 && hiding}
            <p>
              This changes what customers are told. Type
              <span class="font-mono text-meta font-semibold text-ink">{code}</span>
              to be sure it is the branch you mean.
            </p>
            <label class="block">
              <span class="mb-1 block text-meta text-ink-3">Branch code</span>
              <input
                bind:this={codeField}
                bind:value={confirmCode}
                type="text"
                autocomplete="off"
                autocapitalize="off"
                spellcheck="false"
                aria-describedby="branch-code-hint"
                class="w-full rounded-card border border-line bg-page px-3 py-2 font-mono
                       text-body-sm text-ink placeholder:text-ink-3 focus:border-accent"
              />
            </label>
            <!-- The confirm button is disabled until the code matches, and a
                 disabled button gives a screen reader no reason WHY. This line
                 is that reason, and it is polite-live so the change from "does
                 not match yet" to "ready" is announced without stealing the
                 caret out of the field. -->
            <p id="branch-code-hint" aria-live="polite" class="text-meta text-ink-3">
              {matches
                ? `Matches ${code}. “Hide from customers” is now available.`
                : `“Hide from customers” stays unavailable until this matches ${code}.`}
            </p>
          {:else if step === 1}
            <p>
              The assistant will start offering this branch as a place that has stock, and its
              {#if known}<span class="tnum">{int(row.units)}</span> units come{:else}stock
                comes{/if}
              back into every total on the console.
            </p>
            {#if row.note}
              <p class="text-ink-3">It was hidden with the reason “{row.note}”.</p>
            {/if}
          {:else}
            <p>
              {code} goes back into customer answers straight away. Saved answers are cleared, so
              nobody is told yesterday's figures.
            </p>
          {/if}
        </div>
      </div>

      <div
        class="flex justify-end gap-2 rounded-b-hero border-t border-line bg-surface-2 px-[22px] py-3"
      >
        <button
          type="button"
          bind:this={backBtn}
          onclick={() => (step === 1 ? onclose?.() : goStep1())}
          class="cursor-pointer rounded-card border border-line bg-surface px-4 py-2 text-body-sm
                 font-medium text-ink hover:bg-surface-2"
        >
          {step === 1 ? 'Cancel' : 'Back'}
        </button>

        {#if step === 1}
          <button
            type="button"
            onclick={goStep2}
            class="cursor-pointer rounded-card px-4 py-2 text-body-sm font-semibold text-on-accent"
            style="background:{hiding ? 'var(--color-danger)' : 'var(--color-accent)'}"
          >
            Continue
          </button>
        {:else}
          <button
            type="button"
            disabled={busy || (hiding && !matches)}
            onclick={submit}
            class="cursor-pointer rounded-card px-4 py-2 text-body-sm font-semibold text-on-accent
                   disabled:cursor-default disabled:opacity-60"
            style="background:{hiding ? 'var(--color-danger)' : 'var(--color-accent)'}"
          >
            {busy ? 'Saving…' : hiding ? 'Hide from customers' : 'Show to customers'}
          </button>
        {/if}
      </div>
    </div>
  </div>
{/if}
