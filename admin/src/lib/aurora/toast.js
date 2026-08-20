// Tiny global toast store. Call toast('Saved') from anywhere.
import { writable } from 'svelte/store';

export const toasts = writable([]);
let id = 0;

/** Icon names callers pass when the thing FAILED.
 *
 * These decide two separate things, and both were wrong before.
 *
 * The icon map in ToastHost falls back to a tick for a name it does not know.
 * Callers were passing `alert-triangle` and `shield-alert`, neither of which
 * was in that map — so nine failure messages ("could not save branding",
 * "backend offline — nothing was saved") rendered with a green checkmark
 * beside them. A failure that looks like a success is worse than no toast.
 *
 * They also decide politeness. A confirmation can wait for a gap in speech; a
 * failure cannot, because the next thing the person does depends on it.
 */
export const BAD_ICONS = new Set(['alert', 'alert-triangle', 'shield-alert']);

export function toast(message, icon = 'check') {
  const t = { id: ++id, message, icon, bad: BAD_ICONS.has(icon) };
  toasts.update((list) => [...list, t]);
  setTimeout(() => {
    toasts.update((list) => list.filter((x) => x.id !== t.id));
  }, 2300);
}
