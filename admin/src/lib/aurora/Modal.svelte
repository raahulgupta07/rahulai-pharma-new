<script>
  // Styled confirmation/content modal. Replaces browser confirm().
  import { X } from '@lucide/svelte';
  import { dialog } from './dialog.js';
  let {
    open = $bindable(false),
    title = '',
    confirmLabel = 'Confirm',
    tone = 'accent', // 'accent' | 'danger'
    onconfirm,
    children
  } = $props();

  function close() {
    open = false;
  }
  function confirm() {
    onconfirm?.();
    open = false;
  }
</script>

{#if open}
  <div
    class="fixed inset-0 z-[90] flex items-center justify-center bg-black/50 backdrop-blur-[3px]"
    onclick={(e) => e.target === e.currentTarget && close()}
    role="presentation"
  >
    <!--
      `use:dialog` moves focus in, traps Tab, closes on Escape from anywhere and
      hands focus back to the trigger. Before it, this component had none of
      those: the Escape handler on the overlay below could not see a keystroke
      while focus was still out in the page, which is where focus stayed. Both
      delete confirmations in the console are this component.
    -->
    <div
      use:dialog={{ onclose: close }}
      tabindex="-1"
      class="w-[430px] max-w-[calc(100vw-32px)] rounded-hero border border-line bg-surface p-[22px] shadow-[var(--shadow-pop)] outline-none"
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div class="mb-4 flex items-center gap-2">
        <b class="text-body font-semibold text-ink">{title}</b>
        <button
          onclick={close}
          aria-label="Close"
          class="ml-auto flex h-8 w-8 items-center justify-center rounded-panel text-ink-3 hover:bg-surface-2"
        >
          <X size={18} />
        </button>
      </div>

      <div class="text-body-sm leading-relaxed text-ink-2">{@render children?.()}</div>

      <div class="mt-5 flex justify-end gap-2">
        <button
          onclick={close}
          class="rounded-card border border-line bg-surface px-4 py-2 text-body-sm font-medium text-ink hover:bg-surface-2"
        >
          Cancel
        </button>
        <button
          onclick={confirm}
          class="rounded-card px-4 py-2 text-body-sm font-semibold text-on-accent"
          style="background:{tone === 'danger' ? 'var(--color-danger)' : 'var(--color-accent)'}"
        >
          {confirmLabel}
        </button>
      </div>
    </div>
  </div>
{/if}
