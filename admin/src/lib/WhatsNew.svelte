<script>
  /**
   * What's new — the console's release notes, as a modal.
   *
   * This replaced four surfaces that all said the same thing: a
   * `Version & releases` rail row and page, a "release notes" link in the rail
   * foot pointing at that page, and a bell popover that carried the build stamp
   * AND the latest release AND a link to the page. Release notes are a "what
   * changed since I last looked" glance, not a destination, and a rail row for
   * them claimed the same weight as the pages people actually work in.
   *
   * The build stamp lives at the bottom of this sheet rather than beside the
   * version in the rail. A `DEV` badge next to the number was noise on every
   * screen for a fact that matters once — but it is a real fact (a dev build
   * may not match any released code), so it is stated here rather than dropped.
   */
  import { X } from '@lucide/svelte';
  import { dialog } from '$lib/aurora/dialog.js';
  import { getJSON } from '$lib/api.js';

  let { open = $bindable(false), build = null } = $props();

  let releases = $state([]);
  let tried = $state(false);
  let failed = $state(false);

  /** Lazily, and only once: the changelog is admin-only and a few KB. */
  async function load() {
    if (tried) return;
    tried = true;
    try {
      const d = await getJSON('/admin/releases');
      releases = Array.isArray(d?.releases) ? d.releases : [];
    } catch {
      // Offline, or not permitted on an older backend. The sheet then says so
      // rather than rendering an empty timeline that reads as "no releases".
      failed = true;
    }
  }

  $effect(() => {
    if (open) load();
  });

  function close() {
    open = false;
  }

  /** How many individual notes a release carries, for the collapsed row. */
  const countOf = (r) =>
    Object.values(r?.sections || {}).reduce((n, items) => n + (items?.length ?? 0), 0);

  /** "20 Aug 2026" from an ISO day. An unparseable date is shown as written. */
  function when(d) {
    if (!d) return '';
    const t = new Date(d + 'T00:00:00');
    if (Number.isNaN(t.getTime())) return String(d);
    return t.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
  }
</script>

{#if open}
  <!-- Pointer affordance only: aria-hidden, no tabindex. Escape is the keyboard
       route out and `use:dialog` owns it. -->
  <div class="fixed inset-0 z-[90] bg-black/45 backdrop-blur-[3px]" onclick={close} aria-hidden="true"></div>

  <div class="pointer-events-none fixed inset-0 z-[91] grid place-items-center p-6">
    <div
      use:dialog={{ onclose: close }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="whatsnew-title"
      tabindex="-1"
      class="pointer-events-auto flex max-h-[82vh] w-[660px] max-w-full flex-col rounded-hero border border-line bg-surface outline-none"
      style="box-shadow:var(--shadow-pop)"
    >
      <header class="flex items-start gap-3 border-b border-line px-[22px] py-[19px]">
        <div class="min-w-0">
          <h2 id="whatsnew-title" class="page-title text-body font-semibold text-ink">What's new</h2>
          <p class="mt-[3px] text-body-sm text-ink-2">What changed in this console.</p>
        </div>
        {#if build?.version}
          <span
            class="ml-auto flex-none rounded-full bg-accent-soft px-[9px] py-1 text-meta font-semibold tnum text-accent"
            >v{build.version}</span
          >
        {/if}
        <button
          onclick={close}
          aria-label="Close"
          class="flex h-[30px] w-[30px] flex-none items-center justify-center rounded-panel text-ink-3 hover:bg-surface-2 hover:text-ink"
        >
          <X size={16} />
        </button>
      </header>

      <div class="overflow-y-auto px-[22px] pb-[18px] pt-1.5">
        {#if failed}
          <p class="py-6 text-body-sm text-ink-2">
            The release notes could not be loaded. This does not say anything about which
            version is running — that is the number above.
          </p>
        {:else if !releases.length}
          <div class="space-y-2 py-6">
            {#each Array(3) as _}<div class="skel" style="height:14px"></div>{/each}
          </div>
        {:else}
          {#each releases as rel, i (rel.version)}
            <details
              open={i === 0}
              class="relative ml-1.5 border-l-2 border-line pl-[18px]
                before:absolute before:-left-[6px] before:top-[15px] before:h-[9px] before:w-[9px]
                before:rounded-full before:border-2 before:border-surface before:content-['']
                {i === 0 ? 'before:bg-accent' : 'before:bg-line'}"
            >
              <summary
                class="flex cursor-pointer list-none items-center gap-2.5 py-3 pb-1.5 [&::-webkit-details-marker]:hidden"
              >
                <span class="font-mono text-body-sm font-semibold text-ink">v{rel.version}</span>
                {#if i === 0}
                  <span
                    class="rounded-xs bg-accent-soft px-1.5 py-px text-micro font-bold uppercase tracking-[0.07em] text-accent"
                    >Latest</span
                  >
                {/if}
                <span class="text-label text-ink-3">{when(rel.date)}</span>
                <span class="ml-auto text-label text-ink-3">
                  {countOf(rel)}
                  {countOf(rel) === 1 ? 'change' : 'changes'}
                </span>
              </summary>

              {#each Object.entries(rel.sections || {}) as [name, items] (name)}
                <div
                  class="mb-[3px] mt-3 text-micro font-bold uppercase tracking-[0.08em] text-ink-3"
                >
                  {name}
                </div>
                <ul class="m-0 list-none p-0">
                  {#each items || [] as text, k (k)}
                    <li
                      class="relative py-1 pl-[15px] text-body-sm leading-relaxed text-ink-2
                        before:absolute before:left-[2px] before:top-[11px] before:h-1 before:w-1
                        before:rounded-full before:bg-ink-3 before:content-['']"
                    >
                      {text}
                    </li>
                  {/each}
                </ul>
              {/each}
            </details>
          {/each}
        {/if}
      </div>

      <!-- The build stamp. It used to sit in the rail as a `DEV` chip beside the
           number; it is a fact worth one line where somebody is already asking
           "what am I running", and noise everywhere else. -->
      {#if build}
        <div class="border-t border-line px-[22px] py-3 text-meta text-ink-3">
          {build.is_release_build ? 'Release build' : 'Development build'}
          {#if build.git_sha_short && build.git_sha_short !== 'dev'}
            · commit <span class="font-mono">{build.git_sha_short}</span>
          {/if}
          {#if build.built_at}· built {build.built_at}{/if}
          {#if !build.is_release_build}
            <span class="block pt-1"
              >Not produced by the release pipeline — it may not match any released code.</span
            >
          {/if}
        </div>
      {/if}

      <footer class="flex justify-end border-t border-line px-[22px] py-3">
        <button
          onclick={close}
          class="rounded-card border border-line bg-surface px-[15px] py-[7px] text-body-sm font-medium text-ink hover:bg-surface-2"
        >
          Close
        </button>
      </footer>
    </div>
  </div>
{/if}
