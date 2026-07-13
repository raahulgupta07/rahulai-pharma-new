<script>
  import { onMount } from 'svelte';
  import { base as appBase } from '$app/paths';
  import { API_BASE } from '$lib/apiBase.js';
  import { Copy, Check, Play, TriangleAlert, ExternalLink } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import Badge from '$lib/Badge.svelte';

  // The admin SPA is served BY the backend, so API_BASE is whatever origin the
  // operator happens to be browsing (often http://localhost:8091). A snippet is
  // pasted into a CUSTOMER site, where that URL is meaningless — so the public
  // base is a separate, editable value, persisted per browser.
  const PUBLIC_BASE_KEY = 'embed_public_base';
  const DEFAULT_ACCENT = '#006869';

  let publicBase = $state(API_BASE);
  let baseTouched = $state(false);

  let creds = $state([]);
  let credsLoading = $state(true);
  let credsError = $state(null);
  let selected = $state('');

  onMount(() => {
    const saved = localStorage.getItem(PUBLIC_BASE_KEY);
    if (saved) {
      publicBase = saved;
      baseTouched = true;
    }
    loadCreds();
  });

  async function loadCreds() {
    credsLoading = true;
    credsError = null;
    try {
      const res = await fetch(`${API_BASE}/admin/credentials`);
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      const data = await res.json();
      creds = Array.isArray(data) ? data : [];
      if (creds.length && !creds.some((c) => c.embed_id === selected)) {
        selected = creds[0].embed_id;
      }
    } catch (e) {
      credsError = e.message || 'backend offline';
    } finally {
      credsLoading = false;
    }
  }

  function saveBase(v) {
    publicBase = v;
    baseTouched = true;
    localStorage.setItem(PUBLIC_BASE_KEY, v);
  }

  // Trailing slash in the base would produce '…//api/embed/widget.js'.
  const cleanBase = $derived((publicBase || '').trim().replace(/\/+$/, ''));
  const isLocal = $derived(/^https?:\/\/(localhost|127\.|0\.0\.0\.0|\[::1\])/i.test(cleanBase));

  const cred = $derived(creds.find((c) => c.embed_id === selected) ?? null);
  const hasCred = $derived(cred !== null);
  // The dev credential is auto-seeded only in development (config-gated) and does
  // NOT exist in a production deployment — a snippet carrying it 403s there.
  const isDevCred = $derived(cred?.embed_id === 'web' && cred?.public_key === 'web');

  // With no registered credential there is nothing real to emit. Placeholders are
  // shouted, never a plausible-looking 'web'/'web' that would 403 in production.
  const embedId = $derived(cred?.embed_id ?? 'YOUR_EMBED_ID');
  const publicKey = $derived(cred?.public_key ?? 'YOUR_PUBLIC_KEY');

  const widgetSnippet = $derived(`<script src="${cleanBase}/api/embed/widget.js"
  data-embed-id="${embedId}"
  data-public-key="${publicKey}"
  data-title="CityCare Agent"
  data-greeting="Ask about stock, prices, or substitutes."
  data-accent="${DEFAULT_ACCENT}"
  data-stream="true" async><\/script>`);

  const scopedSnippet = $derived(`<script src="${cleanBase}/api/embed/widget.js"
  data-embed-id="${embedId}"
  data-public-key="${publicKey}"
  data-user='@json($user)'
  data-user-sig="{{ $signature }}"
  data-title="CityCare Agent"
  data-accent="${DEFAULT_ACCENT}"
  data-stream="true" async><\/script>`);

  const phpSnippet = $derived(`// config/services.php
'cityagent' => [
  'base_url'   => env('CITYAGENT_BASE_URL'),   // ${cleanBase}
  'secret_key' => env('CITYAGENT_SECRET_KEY'), // == backend SECRET_KEY
],

// Controller — sign the user with their store
$user = [
  'id'       => (string) $currentUser->id,
  'store_id' => (string) $currentUser->branch,   // e.g. "20060-CCBHSC"
];
ksort($user);
$canonical = json_encode($user, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
$signature = hash_hmac('sha256', $canonical, config('services.cityagent.secret_key'));`);

  let copied = $state('');
  function copy(key, text) {
    navigator.clipboard.writeText(text);
    copied = key;
    setTimeout(() => (copied = ''), 1500);
  }

  let previewLoaded = $state(false);
  function loadPreview() {
    if (previewLoaded || !cred) return;
    const s = document.createElement('script');
    // Preview must hit the backend serving THIS page, not the public base — the
    // public host may not be reachable from the operator's network yet.
    s.src = `${API_BASE}/api/embed/widget.js`;
    s.setAttribute('data-embed-id', cred.embed_id);
    s.setAttribute('data-public-key', cred.public_key);
    s.setAttribute('data-title', 'CityCare Agent (preview)');
    s.setAttribute('data-accent', DEFAULT_ACCENT);
    s.setAttribute('data-stream', 'true');
    s.async = true;
    document.body.appendChild(s);
    previewLoaded = true;
  }
</script>

<PageHeader
  title="Embed widget"
  subtitle="Drop the chat widget on any site. With a signed store_id, answers are locked to that branch — data by store, enforced server-side."
>
  {#snippet actions()}
    <button
      onclick={loadPreview}
      disabled={previewLoaded || !hasCred}
      title={hasCred ? '' : 'Register a credential first'}
      class="inline-flex items-center gap-2 rounded-lg bg-accent px-3.5 py-2 text-[13px] font-medium text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
    >
      <Play size={15} />
      {previewLoaded ? 'Preview loaded' : 'Load live preview'}
    </button>
  {/snippet}
</PageHeader>

<!-- 1. Public backend URL -->
<section class="mb-6 rounded-[14px] border border-line bg-surface p-4">
  <div class="mb-1 flex items-center gap-2">
    <span class="text-[14px] font-medium text-ink">Public backend URL</span>
    {#if isLocal}<Badge tone="warn">not reachable from a customer site</Badge>{/if}
  </div>
  <p class="mb-3 text-[13px] text-ink-2">
    The URL a <em>customer's browser</em> will load the widget from — the public address of this
    backend (https, real domain). It is not necessarily the address you are using right now.
  </p>
  <input
    type="text"
    value={publicBase}
    oninput={(e) => saveBase(e.currentTarget.value)}
    spellcheck="false"
    placeholder="https://agent.example.com"
    class="w-full rounded-lg border border-line bg-surface-2 px-3 py-2 font-mono text-[13px] text-ink outline-none focus:border-accent"
  />
  {#if isLocal}
    <p class="mt-2 flex items-start gap-1.5 text-[12.5px] text-warning">
      <TriangleAlert size={14} class="mt-0.5 shrink-0" />
      <span>
        <span class="font-mono">{cleanBase}</span> only resolves on this machine. Snippets copied
        below will not work on a customer site until you replace it with the public URL.
      </span>
    </p>
  {:else if !baseTouched}
    <p class="mt-2 text-[12.5px] text-ink-3">
      Defaulted to the origin you are browsing. Confirm it is the public URL before handing the
      snippet over.
    </p>
  {/if}
</section>

<!-- 2. Credentials -->
<section class="mb-6 rounded-[14px] border border-line bg-surface p-4">
  <div class="mb-1 flex items-center gap-2">
    <span class="text-[14px] font-medium text-ink">Embed credential</span>
    {#if isDevCred}<Badge tone="warn">development only</Badge>{/if}
  </div>

  {#if credsLoading}
    <p class="text-[13px] text-ink-3">Loading credentials…</p>
  {:else if credsError}
    <p class="text-[13px] text-danger">Could not load credentials: {credsError}</p>
  {:else if creds.length === 0}
    <p class="mb-3 flex items-start gap-1.5 text-[13px] text-warning">
      <TriangleAlert size={14} class="mt-0.5 shrink-0" />
      <span>
        <span class="font-medium">No embed credentials are registered.</span> The snippets below
        carry placeholders, not working values. Credential checks are
        <span class="font-medium">fail-closed</span>: an unregistered
        <span class="font-mono">(embed_id, public_key)</span> pair is rejected with
        <span class="font-mono">403 invalid embed credentials</span>.
      </span>
    </p>
    <a
      href={appBase + '/tenants'}
      class="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-[13px] font-medium text-on-accent transition-colors hover:bg-accent-hover"
    >
      Mint a credential on Tenants <ExternalLink size={13} />
    </a>
  {:else}
    <p class="mb-3 text-[13px] text-ink-2">
      Snippets below are filled with the real credential you pick. Manage the list on
      <a href={appBase + '/tenants'} class="text-accent hover:underline">Tenants</a>.
    </p>
    <div class="flex flex-wrap items-center gap-2">
      {#each creds as c (c.embed_id)}
        <button
          onclick={() => (selected = c.embed_id)}
          class="rounded-lg border px-3 py-1.5 font-mono text-[12.5px] transition-colors {selected ===
          c.embed_id
            ? 'border-accent bg-accent-soft text-accent-hover'
            : 'border-line text-ink-2 hover:bg-surface-2'}"
        >
          {c.embed_id}
        </button>
      {/each}
    </div>
    {#if isDevCred}
      <p class="mt-3 flex items-start gap-1.5 text-[12.5px] text-warning">
        <TriangleAlert size={14} class="mt-0.5 shrink-0" />
        <span>
          <span class="font-mono">web</span> / <span class="font-mono">web</span> is the
          auto-seeded development credential. It is <span class="font-medium">not</span> seeded in
          production — a snippet carrying it gets
          <span class="font-mono">403 invalid embed credentials</span> there. Register a real
          credential on Tenants for anything you hand to a customer.
        </span>
      </p>
    {/if}
  {/if}
</section>

{#snippet block(key, label, code, note)}
  <section class="mb-6">
    <div class="mb-2 flex items-center">
      <span class="text-[14px] font-medium text-ink">{label}</span>
      <button
        onclick={() => copy(key, code)}
        class="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1 text-[12px] text-ink-2 transition-colors hover:bg-surface-2"
      >
        {#if copied === key}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy{/if}
      </button>
    </div>
    {#if note}<p class="mb-2 text-[13px] text-ink-2">{note}</p>{/if}
    <pre
      class="overflow-x-auto rounded-[14px] border border-line bg-surface-2 p-4 text-[12.5px] leading-relaxed text-ink"><code
        >{code}</code
      ></pre>
  </section>
{/snippet}

{@render block(
  'public',
  'Public widget (all stores)',
  widgetSnippet,
  'Paste into any HTML page. A floating chat bubble appears bottom-right.'
)}

{@render block(
  'scoped',
  'Store-scoped widget (data by store)',
  scopedSnippet,
  'Blade template: sign the user server-side; the signed store_id limits every answer to that branch.'
)}

{@render block(
  'php',
  'Server-side signing (Laravel / PHP)',
  phpSnippet,
  'CITYAGENT_SECRET_KEY must equal the backend SECRET_KEY. Canonical = sorted keys, no spaces, unescaped.'
)}

<div class="rounded-[14px] border border-line bg-surface p-4 text-[13px] text-ink-2">
  <span class="font-medium text-ink">Endpoints:</span>
  <span class="font-mono">{cleanBase}/api/embed/widget.js</span> ·
  <span class="font-mono">/session/create</span> ·
  <span class="font-mono">/chat</span> ·
  <span class="font-mono">/chat/stream</span>
  <div class="mt-1 text-ink-3">
    Full guide in INTEGRATION.md. “Load live preview” runs the bubble against this backend
    ({API_BASE}) using the selected credential.
  </div>
</div>
