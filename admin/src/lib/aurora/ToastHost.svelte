<script>
  import { toasts } from './toast.js';
  import { Check, Trash2, Bell, Search, AlertTriangle, ShieldAlert } from '@lucide/svelte';

  // Every name any caller passes must be here. An unknown name falls back to a
  // tick, which is how "could not save branding" came to be announced with a
  // success glyph. `alert-triangle` and `shield-alert` are the two that were
  // missing; see BAD_ICONS in toast.js.
  const icons = {
    check: Check,
    'trash-2': Trash2,
    bell: Bell,
    search: Search,
    alert: AlertTriangle,
    'alert-triangle': AlertTriangle,
    'shield-alert': ShieldAlert
  };

  let good = $derived($toasts.filter((t) => !t.bad));
  let bad = $derived($toasts.filter((t) => t.bad));
</script>

<!--
  TWO permanently-mounted regions, not one.

  Permanently mounted because a live region has to be in the accessibility tree
  BEFORE its content changes — a region created in the same tick as its text is
  treated as initial content and never announced. The `{#each}` is inside each
  container, never around it, so both containers exist from first render.

  Two of them because politeness is not one setting here. Eight files call
  toast() about thirty-four times, and for privileged actions it is the ONLY
  confirmation there is: `toast(`Deleted ${u.email}`)` on success and
  `toast(reason(e, 'delete this user'), 'alert-triangle')` on failure. A polite
  region queues behind whatever is being read; a failure that arrives after the
  user has moved on is a failure they act on too late.
-->
<div class="pointer-events-none fixed bottom-6 left-1/2 z-[100] -translate-x-1/2 flex flex-col items-center gap-2">
  <div role="status" aria-live="polite" class="flex flex-col items-center gap-2">
    {#each good as t (t.id)}
      {@const Icon = icons[t.icon] ?? Check}
      <div
        class="pointer-events-auto flex items-center gap-2.5 rounded-panel bg-ink px-[19px] py-3 text-body-sm font-medium text-page shadow-[var(--shadow-pop)]"
      >
        <Icon size={16} />
        {t.message}
      </div>
    {/each}
  </div>

  <div role="alert" class="flex flex-col items-center gap-2">
    {#each bad as t (t.id)}
      {@const Icon = icons[t.icon] ?? AlertTriangle}
      <div
        class="pointer-events-auto flex items-center gap-2.5 rounded-panel border-l-[3px] border-l-danger bg-ink px-[19px] py-3 text-body-sm font-medium text-page shadow-[var(--shadow-pop)]"
      >
        <Icon size={16} />
        {t.message}
      </div>
    {/each}
  </div>
</div>
