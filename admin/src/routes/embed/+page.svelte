<script>
  import { Copy, Check, Play } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';

  const base = 'http://localhost:8088';

  const widgetSnippet = `<script src="${base}/api/embed/widget.js"
  data-embed-id="web"
  data-public-key="web"
  data-title="CityCare Agent"
  data-greeting="Ask about stock, prices, or substitutes."
  data-accent="#006869"
  data-stream="true" async><\/script>`;

  const scopedSnippet = `<script src="${base}/api/embed/widget.js"
  data-embed-id="{{ $embedId }}"
  data-public-key="{{ $publicKey }}"
  data-user='@json($user)'
  data-user-sig="{{ $signature }}"
  data-title="CityCare Agent"
  data-stream="true" async><\/script>`;

  const phpSnippet = `$user = [
  'id'       => (string) $currentUser->id,
  'store_id' => (string) $currentUser->branch,   // e.g. "20060-CCBHSC"
];
ksort($user);
$canonical = json_encode($user, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
$signature = hash_hmac('sha256', $canonical, config('services.cityagent.secret_key'));`;

  let copied = $state('');
  function copy(key, text) {
    navigator.clipboard.writeText(text);
    copied = key;
    setTimeout(() => (copied = ''), 1500);
  }

  let previewLoaded = $state(false);
  function loadPreview() {
    if (previewLoaded) return;
    const s = document.createElement('script');
    s.src = `${base}/api/embed/widget.js`;
    s.setAttribute('data-embed-id', 'web');
    s.setAttribute('data-public-key', 'web');
    s.setAttribute('data-title', 'CityCare Agent (preview)');
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
      disabled={previewLoaded}
      class="inline-flex items-center gap-2 rounded-lg bg-accent px-3.5 py-2 text-[13px] font-medium text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
    >
      <Play size={15} />
      {previewLoaded ? 'Preview loaded' : 'Load live preview'}
    </button>
  {/snippet}
</PageHeader>

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
    <pre class="overflow-x-auto rounded-[14px] border border-line bg-surface-2 p-4 text-[12.5px] leading-relaxed text-ink"><code>{code}</code></pre>
  </section>
{/snippet}

{@render block('public', 'Public widget (all stores)', widgetSnippet, 'Paste into any HTML page. A floating chat bubble appears bottom-right.')}

{@render block('scoped', 'Store-scoped widget (data by store)', scopedSnippet, 'Sign the user server-side; the signed store_id limits every answer to that branch.')}

{@render block('php', 'Server-side signing (Laravel / PHP)', phpSnippet, 'CITYAGENT_SECRET_KEY must equal the backend SECRET_KEY. Canonical = sorted keys, no spaces, unescaped.')}

<div class="rounded-[14px] border border-line bg-surface p-4 text-[13px] text-ink-2">
  <span class="font-medium text-ink">Endpoints:</span>
  <span class="font-mono">{base}/api/embed/widget.js</span> ·
  <span class="font-mono">/session/create</span> ·
  <span class="font-mono">/chat</span> ·
  <span class="font-mono">/chat/stream</span>
  <div class="mt-1 text-ink-3">Full guide in INTEGRATION.md. Click “Load live preview” to try the bubble on this page.</div>
</div>
