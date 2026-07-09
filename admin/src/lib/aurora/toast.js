// Tiny global toast store. Call toast('Saved') from anywhere.
import { writable } from 'svelte/store';

export const toasts = writable([]);
let id = 0;

export function toast(message, icon = 'check') {
  const t = { id: ++id, message, icon };
  toasts.update((list) => [...list, t]);
  setTimeout(() => {
    toasts.update((list) => list.filter((x) => x.id !== t.id));
  }, 2300);
}
