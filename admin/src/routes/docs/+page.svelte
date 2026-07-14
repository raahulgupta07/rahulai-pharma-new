<script>
  import { onMount } from 'svelte';
  import { base as appBase } from '$app/paths';
  import { API_BASE } from '$lib/apiBase.js';
  import { Copy, Check, ExternalLink, Globe, KeyRound, Store } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import Badge from '$lib/Badge.svelte';

  // A living guide: it shows the operator's REAL backend URL + registered
  // credential + store count, so every snippet here is copy-paste-ready rather
  // than a placeholder an engineer has to hand-edit.
  const PUBLIC_BASE_KEY = 'embed_public_base';

  let base = $state(API_BASE);
  let creds = $state([]);
  let outletCount = $state(0);

  onMount(async () => {
    const saved = localStorage.getItem(PUBLIC_BASE_KEY);
    if (saved) base = saved;
    try {
      const r = await fetch(`${API_BASE}/admin/credentials`);
      if (r.ok) creds = await r.json();
    } catch {}
    try {
      const r = await fetch(`${API_BASE}/admin/embed/outlets`);
      if (r.ok) outletCount = (await r.json()).length;
    } catch {}
  });

  const cleanBase = $derived((base || '').trim().replace(/\/+$/, ''));
  const realCred = $derived(creds.find((c) => !(c.embed_id === 'web' && c.public_key === 'web')) ?? null);
  const embedId = $derived(realCred?.embed_id ?? creds[0]?.embed_id ?? 'YOUR_EMBED_ID');
  const publicKey = $derived(realCred?.public_key ?? creds[0]?.public_key ?? 'YOUR_PUBLIC_KEY');
  const onlyDevCred = $derived(creds.length > 0 && !realCred);

  const publicSnippet = $derived(`<script src="${cleanBase}/api/embed/widget.js"
        data-embed-id="${embedId}"
        data-public-key="${publicKey}"
        data-title="Pharmacy assistant"
        data-accent="#006869"
        data-stream="true"
        async><\/script>`);

  const tabs = [
    { id: 'overview', label: 'Overview' },
    { id: 'outlet', label: 'Outlet developer' },
    { id: 'operator', label: 'Operator (you)' },
    { id: 'devops', label: 'DevOps (AWS)' },
    { id: 'trouble', label: 'Troubleshooting' }
  ];
  let tab = $state('overview');

  let copied = $state('');
  function copy(key, text) {
    navigator.clipboard.writeText(text);
    copied = key;
    setTimeout(() => (copied = ''), 1500);
  }
</script>

<PageHeader
  title="Integration guide"
  subtitle="How to embed the pharmacy assistant — for outlet developers, operators, and DevOps. Snippets below use this deployment's real values."
/>

<!-- tab bar -->
<div class="mb-5 flex flex-wrap gap-1.5">
  {#each tabs as t (t.id)}
    <button
      onclick={() => (tab = t.id)}
      class="rounded-lg border px-3 py-1.5 text-[13px] font-medium transition-colors {tab === t.id
        ? 'border-accent bg-accent-soft text-accent-hover'
        : 'border-line text-ink-2 hover:bg-surface-2'}"
    >
      {t.label}
    </button>
  {/each}
</div>

{#snippet codeblock(key, code)}
  <div class="relative">
    <button
      onclick={() => copy(key, code)}
      class="absolute right-2 top-2 inline-flex items-center gap-1.5 rounded-lg border border-line bg-surface px-2.5 py-1 text-[12px] text-ink-2 transition-colors hover:bg-surface-2"
    >
      {#if copied === key}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy{/if}
    </button>
    <pre
      class="overflow-x-auto rounded-[14px] border border-line bg-surface-2 p-4 pr-20 text-[12.5px] leading-relaxed text-ink"><code
        >{code}</code
      ></pre>
  </div>
{/snippet}

{#snippet card(title, children)}
  <section class="mb-5 rounded-[14px] border border-line bg-surface p-5">
    <h3 class="mb-3 text-[15px] font-semibold text-ink">{title}</h3>
    {@render children()}
  </section>
{/snippet}

<!-- ============================ OVERVIEW ============================ -->
{#if tab === 'overview'}
  {#snippet overviewBody()}
    <p class="mb-4 text-[13.5px] leading-relaxed text-ink-2">
      Embedding is gated by <span class="font-medium text-ink">three independent layers</span>. All
      three must pass for the widget to answer. They are often confused — they are not the same thing.
    </p>
    <div class="grid gap-3 sm:grid-cols-3">
      <div class="rounded-[12px] border border-line bg-surface-2 p-4">
        <div class="mb-2 flex items-center gap-2"><KeyRound size={16} class="text-accent" /><span class="font-medium text-ink">Credential</span></div>
        <p class="text-[12.5px] leading-relaxed text-ink-2">
          <span class="font-mono">embed_id</span> + <span class="font-mono">public_key</span>. Proves
          the request is your widget. <span class="font-medium text-ink">One shared</span> credential
          for every outlet — not per store.
        </p>
      </div>
      <div class="rounded-[12px] border border-line bg-surface-2 p-4">
        <div class="mb-2 flex items-center gap-2"><Store size={16} class="text-accent" /><span class="font-medium text-ink">Signed store_id</span></div>
        <p class="text-[12.5px] leading-relaxed text-ink-2">
          Locks answers to one branch. <span class="font-medium text-ink">Automatic per outlet</span> —
          baked + HMAC-signed into each downloaded snippet. This is the per-store part.
        </p>
      </div>
      <div class="rounded-[12px] border border-line bg-surface-2 p-4">
        <div class="mb-2 flex items-center gap-2"><Globe size={16} class="text-accent" /><span class="font-medium text-ink">CORS origin</span></div>
        <p class="text-[12.5px] leading-relaxed text-ink-2">
          Which <span class="font-medium text-ink">website</span> may load the widget. Add each
          customer site's exact origin. Separate from the credential.
        </p>
      </div>
    </div>
    <p class="mt-4 text-[12.5px] text-ink-3">
      So for {outletCount || 'N'} outlets you register <span class="font-medium text-ink">one</span>
      credential, generate {outletCount || 'N'} snippets (each auto-signed for its store), and add each
      site's origin to CORS.
    </p>
  {/snippet}
  {@render card('The three layers', overviewBody)}
{/if}

<!-- ======================= OUTLET DEVELOPER ======================= -->
{#if tab === 'outlet'}
  {#snippet outletA()}
    <p class="mb-3 text-[13.5px] leading-relaxed text-ink-2">
      The simplest path. Your outlet was handed a file (<span class="font-mono">outlet-STORE.html</span>)
      or a folder from a ZIP. It is already signed for your store — no keys, no signing on your end.
    </p>
    <ol class="ml-4 list-decimal space-y-2 text-[13.5px] text-ink-2">
      <li>Open the <span class="font-mono">.html</span> file in a browser to see it working.</li>
      <li>Copy the <span class="font-mono">&lt;script&gt;</span> tag from it (or from <span class="font-mono">snippet.txt</span>) onto any page of your site — before <span class="font-mono">&lt;/body&gt;</span>.</li>
      <li>Send us the <span class="font-medium text-ink">exact origin</span> your site runs on (e.g. <span class="font-mono">https://your-shop.com</span>) so we can allow it. Until then the browser blocks the widget.</li>
    </ol>
    <p class="mt-3 text-[12.5px] text-ink-3">A floating chat bubble appears bottom-right. It only ever answers for your store.</p>
  {/snippet}
  {@render card('Method A — download your outlet file (recommended)', outletA)}

  {#snippet outletB()}
    <p class="mb-3 text-[13.5px] leading-relaxed text-ink-2">
      If your site has a login and a PHP backend, sign each logged-in user server-side so their branch
      flows in automatically — one snippet serves all your outlets. Full drop-in PHP is on the
      <a href={appBase + '/embed'} class="text-accent hover:underline">Embed widget</a> page.
    </p>
    <p class="text-[12.5px] text-ink-3">
      Keep <span class="font-mono">CITYAGENT_SECRET_KEY</span> (server env) equal to the backend's
      <span class="font-mono">SECRET_KEY</span>, or every signed session is rejected.
    </p>
  {/snippet}
  {@render card('Method B — dynamic per-user (PHP login)', outletB)}

  {#snippet outletC()}
    <p class="mb-3 text-[13.5px] leading-relaxed text-ink-2">
      No store scope — the widget answers across all stores. For a public catalog page.
    </p>
    {@render codeblock('pub', publicSnippet)}
    {#if onlyDevCred}
      <p class="mt-2 text-[12.5px] text-warning">
        Only the dev credential <span class="font-mono">web</span>/<span class="font-mono">web</span>
        is registered — it is rejected in production. The operator must mint a real one.
      </p>
    {/if}
  {/snippet}
  {@render card('Method C — public widget (all stores)', outletC)}
{/if}

<!-- ========================== OPERATOR =========================== -->
{#if tab === 'operator'}
  {#snippet opBody()}
    <ol class="ml-4 list-decimal space-y-3 text-[13.5px] text-ink-2">
      <li>
        <span class="font-medium text-ink">Mint one production credential.</span> On
        <a href={appBase + '/tenants'} class="text-accent hover:underline">Tenants</a>, add an
        <span class="font-mono">embed_id</span> + a random <span class="font-mono">public_key</span>.
        Do not ship <span class="font-mono">web</span>/<span class="font-mono">web</span> — it 403s in
        production. One credential covers all outlets.
      </li>
      <li>
        <span class="font-medium text-ink">Set the public backend URL.</span> On
        <a href={appBase + '/embed'} class="text-accent hover:underline">Embed widget</a>, enter the
        https address customers reach (not localhost).
      </li>
      <li>
        <span class="font-medium text-ink">Generate per-outlet snippets.</span> On the same page →
        <em>Per-outlet embeds</em>: pick a store for one <span class="font-mono">.html</span>, or
        <em>Download all {outletCount || 'N'} (.zip)</em> for a folder per store. Hand each outlet its folder.
      </li>
      <li>
        <span class="font-medium text-ink">Allow each site's origin.</span> As outlets report their
        domains, add them on <a href={appBase + '/tenants'} class="text-accent hover:underline">Tenants → Allowed origins (CORS)</a>.
        Live within seconds, no restart.
      </li>
      <li>
        <span class="font-medium text-ink">Answer length (optional).</span> On
        <a href={appBase + '/settings'} class="text-accent hover:underline">Answer behaviour</a>, pick
        Crisp / Standard / Detailed. Applies to every embed.
      </li>
    </ol>
  {/snippet}
  {@render card('Rollout steps', opBody)}

  {#snippet reloadBody()}
    <p class="text-[13.5px] leading-relaxed text-ink-2">
      Answers are cached (up to 10 min). After anything that changes what the model should say — a
      new data upload, an answer-length change, or a backend deploy — flush the cache with
      <span class="font-mono">POST /api/embed/reload</span>, or the old answer keeps serving.
    </p>
  {/snippet}
  {@render card('Reload after a change', reloadBody)}
{/if}

<!-- =========================== DEVOPS ============================ -->
{#if tab === 'devops'}
  {#snippet awsBody()}
    <ol class="ml-4 list-decimal space-y-2.5 text-[13.5px] text-ink-2">
      <li><span class="font-medium text-ink">HTTPS in front.</span> Terminate TLS at an ALB (ACM cert) or nginx (Let's Encrypt). An https customer page cannot call an http backend — the browser blocks it.</li>
      <li><span class="font-medium text-ink">Persistent Redis + Postgres.</span> Embed credentials, CORS origins, and answer style live in Redis. Ephemeral Redis wipes them on restart → every embed 403s. Use ElastiCache/RDS or volume-backed containers.</li>
      <li><span class="font-medium text-ink">SECRET_KEY match.</span> The backend's <span class="font-mono">SECRET_KEY</span> must equal the value used to sign users (the PHP <span class="font-mono">CITYAGENT_SECRET_KEY</span>). Mismatch → every signed session 403.</li>
      <li><span class="font-medium text-ink">Open admin on the AWS host</span> and copy fresh snippets — the public base auto-fills the deployment URL.</li>
      <li><span class="font-medium text-ink">Add customer origins</span> to CORS, then <span class="font-medium text-ink">reload</span> (<span class="font-mono">POST /api/embed/reload</span>) after the first data load.</li>
    </ol>
  {/snippet}
  {@render card('AWS deploy checklist', awsBody)}

  {#snippet endpointsBody()}
    <ul class="space-y-1 text-[12.5px] text-ink-2">
      <li><span class="font-mono">{cleanBase}/api/embed/widget.js</span> — the widget script</li>
      <li><span class="font-mono">POST /api/embed/session/create</span> — verify signature → mint session</li>
      <li><span class="font-mono">POST /api/embed/chat</span> · <span class="font-mono">/chat/stream</span> — ask (scoped to the token's store)</li>
      <li><span class="font-mono">POST /api/embed/reload</span> — flush the answer cache</li>
    </ul>
  {/snippet}
  {@render card('Embed endpoints', endpointsBody)}
{/if}

<!-- ======================== TROUBLESHOOTING ===================== -->
{#if tab === 'trouble'}
  {#snippet tbody()}
    <div class="space-y-4">
      <div>
        <p class="mb-1 flex items-center gap-2 text-[14px] font-medium text-ink"><Badge tone="danger">CORS</Badge> "blocked by CORS policy" / "Failed to fetch"</p>
        <p class="text-[13px] text-ink-2">The site's origin is not allowed. Add its exact origin (scheme + host + port, no path) on <a href={appBase + '/tenants'} class="text-accent hover:underline">Tenants → Allowed origins</a>. Takes effect in seconds.</p>
      </div>
      <div>
        <p class="mb-1 flex items-center gap-2 text-[14px] font-medium text-ink"><Badge tone="danger">403</Badge> "invalid embed credentials"</p>
        <p class="text-[13px] text-ink-2">The <span class="font-mono">embed_id</span>/<span class="font-mono">public_key</span> in the snippet is not registered (or is the dev-only <span class="font-mono">web</span>/<span class="font-mono">web</span> in production). Register a real credential on Tenants and regenerate the snippet.</p>
      </div>
      <div>
        <p class="mb-1 flex items-center gap-2 text-[14px] font-medium text-ink"><Badge tone="danger">401</Badge> "bad user signature"</p>
        <p class="text-[13px] text-ink-2">The signing secret does not match the backend, or the signed user object was altered. Confirm <span class="font-mono">SECRET_KEY</span> equals the signer's key. Downloaded outlet snippets are pre-signed — do not edit the <span class="font-mono">data-user</span> / <span class="font-mono">data-user-sig</span> values.</p>
      </div>
      <div>
        <p class="mb-1 flex items-center gap-2 text-[14px] font-medium text-ink"><Badge tone="warn">stale</Badge> answer is out of date</p>
        <p class="text-[13px] text-ink-2">The answer cache (up to 10 min) held an old reply. Call <span class="font-mono">POST /api/embed/reload</span> after a data change or deploy.</p>
      </div>
      <div>
        <p class="mb-1 flex items-center gap-2 text-[14px] font-medium text-ink"><Badge tone="warn">blank</Badge> no bubble appears</p>
        <p class="text-[13px] text-ink-2">Check the browser console. Usually a CORS block (above) or the widget script 404'd — confirm <span class="font-mono">{cleanBase}/api/embed/widget.js</span> loads.</p>
      </div>
    </div>
  {/snippet}
  {@render card('Common failures & fixes', tbody)}
{/if}
