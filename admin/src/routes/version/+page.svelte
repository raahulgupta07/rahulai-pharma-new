<script>
  import { onMount } from 'svelte';
  import { API_BASE } from '$lib/apiBase.js';
  import { Tag, GitCommitHorizontal, Clock, AlertTriangle } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import Badge from '$lib/Badge.svelte';

  // Reads /admin/releases: the running build plus the parsed CHANGELOG.md. The
  // notes are written for the people who use the console, not for engineers,
  // so this page renders them as-is rather than dressing them up.
  let data = $state(null);
  let failed = $state(false);

  onMount(async () => {
    try {
      const r = await fetch(`${API_BASE}/admin/releases`);
      if (r.ok) data = await r.json();
      else failed = true;
    } catch {
      failed = true;
    }
  });

  // Section colour carries meaning: a security fix should not look like a
  // copy tweak when someone is skimming to decide whether to upgrade.
  const TONE = {
    Security: 'danger',
    Fixed: 'ok',
    Added: 'info',
    Changed: 'neutral',
    Removed: 'warn',
    Deprecated: 'warn'
  };

  const current = $derived(data?.current ?? null);
  const releases = $derived(data?.releases ?? []);
  // False when the running build and the top changelog entry disagree — a
  // rebuild that skipped the notes, or notes edited without a rebuild. Both
  // have to be visible or this page quietly lies about what is deployed.
  const mismatch = $derived(!!data && !data.notes_match_build);

  function fmt(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    return isNaN(d) ? ts : d.toLocaleString();
  }
</script>

<PageHeader
  title="Version"
  subtitle="Which build this console is running, and what changed in each release."
/>

{#if failed}
  <div class="elev rounded-xl border border-line bg-surface p-5 text-[14px] text-ink-2">
    Could not load version information — the backend may be offline, or your session may have
    expired. Try signing in again.
  </div>
{:else if !data}
  <div class="text-[14px] text-ink-3">Loading…</div>
{:else}
  <!-- running build -->
  <div class="elev mb-6 rounded-xl border border-line bg-surface p-5">
    <div class="flex flex-wrap items-center gap-3">
      <Tag size={20} class="text-accent" />
      <span class="page-title text-[24px] leading-none text-ink">v{current.version}</span>
      {#if current.is_release_build}
        <Badge tone="ok">Release build</Badge>
      {:else}
        <Badge tone="warn">Development build</Badge>
      {/if}
    </div>

    <div class="mt-4 flex flex-wrap gap-x-8 gap-y-3 text-[13px]">
      <div class="flex items-center gap-2 text-ink-2">
        <GitCommitHorizontal size={16} class="text-ink-3" />
        <span class="font-mono">{current.git_sha_short || 'unknown'}</span>
      </div>
      {#if current.built_at}
        <div class="flex items-center gap-2 text-ink-2">
          <Clock size={16} class="text-ink-3" />
          <span>Built {fmt(current.built_at)}</span>
        </div>
      {/if}
    </div>

    {#if !current.is_release_build}
      <p class="mt-4 text-[13px] leading-relaxed text-ink-2">
        This build was not produced by the release pipeline, so it carries no commit or build
        time. Do not report its version number in a support ticket — it may not match any
        released code.
      </p>
    {/if}
  </div>

  {#if mismatch}
    <div
      class="mb-6 flex items-start gap-3 rounded-xl border border-line bg-warning-soft p-4 text-[13px] leading-relaxed text-ink"
    >
      <AlertTriangle size={18} class="mt-0.5 flex-shrink-0 text-warning" />
      <div>
        <strong>The release notes below do not describe this build.</strong>
        The running version is <span class="font-mono">v{current.version}</span>, but the newest
        note is for
        <span class="font-mono">v{releases[0]?.version ?? 'nothing'}</span>. Either the image was
        rebuilt without updating the notes, or the notes were updated without a rebuild.
      </div>
    </div>
  {/if}

  <!-- release history -->
  {#if !releases.length}
    <div class="elev rounded-xl border border-line bg-surface p-5 text-[14px] text-ink-2">
      No release notes are available in this build.
    </div>
  {:else}
    <div class="flex flex-col gap-5">
      {#each releases as rel (rel.version)}
        <section class="elev rounded-xl border border-line bg-surface p-5">
          <div class="flex flex-wrap items-baseline gap-3">
            <h2 class="page-title text-[19px] text-ink">v{rel.version}</h2>
            {#if rel.date}<span class="text-[12.5px] text-ink-3">{rel.date}</span>{/if}
            {#if rel.version === current.version}
              <Badge tone="ok">Running now</Badge>
            {/if}
          </div>

          {#each Object.entries(rel.sections) as [name, items] (name)}
            <div class="mt-4">
              <Badge tone={TONE[name] ?? 'neutral'}>{name}</Badge>
              <ul class="mt-2 flex list-disc flex-col gap-1.5 pl-5 text-[13.5px] leading-relaxed text-ink-2">
                {#each items as item}
                  <li>{item}</li>
                {/each}
              </ul>
            </div>
          {/each}
        </section>
      {/each}
    </div>
  {/if}
{/if}
