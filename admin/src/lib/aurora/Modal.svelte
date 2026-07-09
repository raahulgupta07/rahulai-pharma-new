<script>
  // Styled confirmation/content modal. Replaces browser confirm().
  import { X } from '@lucide/svelte';
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
    onkeydown={(e) => e.key === 'Escape' && close()}
    role="presentation"
  >
    <div
      class="w-[430px] max-w-[calc(100vw-32px)] rounded-[18px] border-[0.5px] border-line bg-surface p-[22px] shadow-[var(--shadow-pop)]"
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div class="mb-4 flex items-center gap-2">
        <b class="text-[16px] font-semibold text-ink">{title}</b>
        <button
          onclick={close}
          aria-label="Close"
          class="ml-auto flex h-8 w-8 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2"
        >
          <X size={18} />
        </button>
      </div>

      <div class="text-[13.5px] leading-relaxed text-ink-2">{@render children?.()}</div>

      <div class="mt-5 flex justify-end gap-2">
        <button
          onclick={close}
          class="rounded-[11px] border-[0.5px] border-line bg-surface px-4 py-2 text-[13px] font-medium text-ink hover:bg-surface-2"
        >
          Cancel
        </button>
        <button
          onclick={confirm}
          class="rounded-[11px] px-4 py-2 text-[13px] font-semibold text-white"
          style="background:{tone === 'danger' ? 'var(--color-danger)' : 'var(--color-accent)'}"
        >
          {confirmLabel}
        </button>
      </div>
    </div>
  </div>
{/if}
