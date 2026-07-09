<script>
  import { toasts } from './toast.js';
  import { Check, Trash2, Bell, Search, AlertTriangle } from '@lucide/svelte';
  const icons = { check: Check, 'trash-2': Trash2, bell: Bell, search: Search, alert: AlertTriangle };
</script>

<div class="pointer-events-none fixed bottom-6 left-1/2 z-[100] -translate-x-1/2 flex flex-col items-center gap-2">
  {#each $toasts as t (t.id)}
    <div
      class="pointer-events-auto flex items-center gap-2.5 rounded-[13px] bg-ink px-[19px] py-3 text-[13px] font-medium text-page shadow-[var(--shadow-pop)]"
    >
      <svelte:component this={icons[t.icon] ?? Check} size={16} />
      {t.message}
    </div>
  {/each}
</div>
