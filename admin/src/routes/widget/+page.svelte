<script>
  import { onMount } from 'svelte';
  import { base } from '$app/paths';
  import { getJSON } from '$lib/api.js';
  import PageHeader from '$lib/PageHeader.svelte';
  import { ShieldCheck, RefreshCw, ExternalLink, Table2, Languages, ListChecks,
    Quote, Store, CircleDashed, Lock } from '@lucide/svelte';

  /** What a branch gets — every line checked against `app/static/widget.js` and
   *  against the widget running in the frame above before it was written here.
   *  A claim on this page is a claim about the product, so an unchecked one is
   *  worse than a missing one. */
  const DOES = [
    {
      icon: Table2,
      title: 'Counts arrive as a table',
      body: 'A substitutes or price answer renders as a real bordered table with the article code in it, not as rows of raw pipe characters. A wide one scrolls inside itself rather than stretching the page.'
    },
    {
      icon: Languages,
      title: 'The answer speaks the language of the question',
      body: 'Burmese in, Burmese out — the quantity, the branch code and the price all come back in the language the pharmacist typed.'
    },
    {
      icon: ListChecks,
      title: 'The lookups are named while they run',
      body: 'Each step says what it is doing to what — “Searching for ALAXAN”, “Checking stock of 1000000015837” — so a slow answer shows its work instead of a spinner.'
    },
    {
      icon: Quote,
      title: 'What it checked stays with the answer',
      body: 'The steps are replaced by the answer, and a citation takes their place: “Checked against our product catalogue and stock levels across 1 branch.” The source outlives the progress.'
    },
    {
      icon: Store,
      title: 'The branch is signed in, not chosen',
      body: 'The store id is HMAC-signed into the session server-side and every tool filters on it. Nothing the visitor types can widen it, and the assistant cannot write a stock row at all.'
    }
  ];

  /** The design's card list claims three more things than the widget does.
   *  They are listed as gaps rather than dropped: an operator reading this page
   *  is deciding what to tell a branch, and "we do not do that yet" is an
   *  answer they need. */
  const NOT_YET = [
    {
      title: 'The panel does not name the branch',
      body: 'Its header shows the title you set and “Online · real stock & price data”. A pharmacist covering two shops cannot tell from the panel whose stock they are reading — only the answer text names the branch.'
    },
    {
      title: 'The follow-up chips are always English',
      body: 'The three suggestions under an answer are fixed English strings, so a Burmese answer is followed by English prompts. The answer language is right; the chips have not caught up.'
    },
    {
      title: 'A blank quantity has never been tested',
      body: 'A blank cell in the stock file means UNKNOWN and must never read as zero. Nothing in the widget handles that case, and none of the 111,654 stock rows currently holds a blank — so the rule is a rule, not a measured behaviour.'
    }
  ];

  /** The real widget, on a real host page, pinned to a real branch.
   *
   *  Not a mock of the widget and not a second implementation of it: this
   *  iframes `GET /embed/preview`, which serves the SAME demo page the snippet
   *  generator produces and loads the SAME `widget.js` a customer site loads.
   *  A hand-built imitation on this page would drift from the shipped widget
   *  silently, and the one screen whose job is "what does a branch actually
   *  see" would be the screen most likely to be wrong.
   */
  let status = $state('loading'); // loading | ready | blocked | error
  let previewUrl = $state(null);

  /** The host the preview really loads from, for the frame's address bar.
   *
   * Derived from the signed URL rather than written down, so a deployment on a
   * different origin shows its own address. The query string is dropped: it
   * carries the signing token, and a token belongs nowhere near a screenshot. */
  let previewHost = $derived.by(() => {
    if (!previewUrl) return '';
    try {
      const u = new URL(previewUrl, location.origin);
      return u.host + u.pathname;
    } catch {
      return '';
    }
  });
  let store = $state(null);
  let embedId = $state(null);
  let expiresAt = $state(null);
  let why = $state(null);

  const ORIGIN = typeof window === 'undefined' ? '' : window.location.origin;

  onMount(load);

  async function load() {
    status = 'loading';
    why = null;
    previewUrl = null;
    try {
      // `/admin/stores` is scoped by the caller's own row, so a branch-pinned
      // admin previews their branch and cannot mint a link for anyone else's.
      // It is also `require_admin` rather than super_admin, unlike
      // `/embed/outlets`, so the branch choice works for the people most likely
      // to want this page.
      const [creds, stores] = await Promise.all([
        getJSON('/admin/credentials'),
        getJSON('/admin/stores')
      ]);

      const cred = Array.isArray(creds) ? creds[0] : null;
      if (!cred) {
        // Not an error: nothing is registered, so there is no embed to preview.
        // Worth stating plainly — with an empty credential store the embed API
        // accepts ANY credential, which is a thing an operator should know.
        status = 'blocked';
        why = 'no-credential';
        return;
      }
      const site = Array.isArray(stores) ? stores[0]?.site_code : null;
      if (!site) {
        status = 'blocked';
        why = 'no-store';
        return;
      }

      store = site;
      embedId = cred.embed_id;
      // getJSON forwards `init` to fetch, and the layout's wrapper adds the
      // Bearer token; api.js exports no post helper.
      const r = await getJSON('/admin/embed/preview-link', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          store_id: site,
          embed_id: cred.embed_id,
          public_key: cred.public_key,
          base_url: ORIGIN,
          title: 'Stock assistant'
        })
      });
      previewUrl = r?.url ?? null;
      expiresAt = typeof r?.expires_in === 'number' ? Date.now() + r.expires_in * 1000 : null;
      status = previewUrl ? 'ready' : 'error';
    } catch (e) {
      status = 'error';
      why = e;
    }
  }

  const mins = (ms) => Math.max(0, Math.round(ms / 60000));
</script>

<PageHeader
  title="Branch assistant"
  subtitle="What a branch sees when it asks about stock — the same widget your staff use, pinned to one branch and read-only."
/>

<div class="grid items-start gap-[18px] lg:grid-cols-[minmax(0,1fr)_300px]">
  <div class="min-w-0">
    <div class="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1.5">
      <h2 class="text-body font-semibold text-ink">What a branch sees</h2>
      {#if status === 'ready'}
        <span
          class="inline-flex items-center gap-1.5 rounded-control bg-surface-2 px-2 py-[3px] font-mono text-label text-ink-2"
        >
          <span class="size-1.5 rounded-full bg-accent-2"></span>
          {store} · read-only
        </span>
        <span class="font-mono text-label text-ink-3">via {embedId}</span>
      {/if}
      <button
        onclick={load}
        class="ml-auto inline-flex items-center gap-1.5 rounded-control border border-line bg-surface px-3 py-1.5 text-meta font-semibold text-ink-2 hover:border-accent hover:text-accent"
      >
        <RefreshCw size={13} /> New preview
      </button>
      {#if previewUrl}
        <a
          href={previewUrl}
          target="_blank"
          rel="noopener"
          class="inline-flex items-center gap-1.5 text-meta font-semibold text-accent hover:underline"
        >
          Open in a tab <ExternalLink size={13} />
        </a>
      {/if}
    </div>

    {#if status === 'loading'}
      <div class="skel" style="height:620px"></div>
    {:else if status === 'ready'}
      <!-- Same origin, so `allow-same-origin` is not a privilege the page does
           not already have; the widget needs it to reach its own API the way it
           does on a customer site. -->
      <!-- Browser chrome, not decoration. Bare, the iframe is a white rectangle
           with a heading in it, which reads as a page that failed to load —
           the panel beside it had to say "the panel on the left is not a
           picture of the widget" precisely because nothing on the panel said
           so. The frame makes "this is the customer's site" the first thing
           you read, and the address is the real signed preview host rather
           than a made-up domain. -->
      <div class="overflow-hidden rounded-card border border-line bg-surface">
        <div class="flex items-center gap-2.5 border-b border-line bg-surface-2 px-3.5 py-2.5">
          <span class="flex flex-none gap-1.5" aria-hidden="true">
            <span class="size-2.5 rounded-full bg-line"></span>
            <span class="size-2.5 rounded-full bg-line"></span>
            <span class="size-2.5 rounded-full bg-line"></span>
          </span>
          <span
            class="flex h-[26px] min-w-0 flex-1 items-center gap-1.5 rounded-full border border-line bg-surface px-3 font-mono text-label text-ink-3"
          >
            <Lock size={10} class="flex-none" />
            <span class="truncate">{previewHost}</span>
          </span>
        </div>
        <!-- Same origin, so `allow-same-origin` is not a privilege the page does
             not already have; the widget needs it to reach its own API the way
             it does on a customer site. -->
        <iframe
          src={previewUrl}
          title="The branch assistant as a branch sees it"
          sandbox="allow-scripts allow-same-origin allow-forms"
          class="block h-[620px] w-full border-0 bg-white"
        ></iframe>
      </div>
      {#if expiresAt}
        <p class="mt-2 text-meta text-ink-3">
          This preview link is signed and expires in {mins(expiresAt - Date.now())} minutes. It is
          shareable while it lasts — anyone with the URL sees this branch's stock, and nothing else.
        </p>
      {/if}
    {:else if status === 'blocked' && why === 'no-credential'}
      <div class="rounded-panel border border-line bg-surface p-[15px]">
        <p class="text-body font-semibold text-ink">No embed credential is registered.</p>
        <p class="mt-1.5 text-body-sm leading-[1.5] text-ink-2">
          A preview is a real widget session, so it needs a real credential to run under, and there
          is none. Worth knowing while you are here: with the credential store empty the embed API
          accepts <em>any</em> embed id and key, so this is not only a preview that cannot run.
        </p>
        <a
          href={base + '/embed'}
          class="mt-3 inline-block text-meta font-semibold text-accent hover:underline"
          >Register one on Embed &amp; integration →</a
        >
      </div>
    {:else if status === 'blocked'}
      <div class="rounded-panel border border-line bg-surface p-[15px]">
        <p class="text-body font-semibold text-ink">No branch has stock recorded.</p>
        <p class="mt-1.5 text-body-sm leading-[1.5] text-ink-2">
          The widget answers from the stock table, and there is nothing in it to answer from, so
          there is nothing to show a branch.
        </p>
      </div>
    {:else}
      <div class="rounded-panel border border-line bg-surface p-[15px]">
        <p class="text-body font-semibold text-ink">The preview could not be started.</p>
        <p class="mt-1.5 text-body-sm leading-[1.5] text-ink-2">
          {#if why?.status === 403}
            Minting a preview link needs a super admin. The widget itself is unaffected — this is
            about who may create a shareable link to it.
          {:else}
            The console could not mint a preview link{why?.status
              ? ` (HTTP ${why.status})`
              : ''}. Nothing about the widget your branches use has changed; only this page could
            not open a copy of it.
          {/if}
        </p>
      </div>
    {/if}

    <!-- WHAT A BRANCH GETS -->
    <section class="mt-7" aria-labelledby="does-heading">
      <h2 id="does-heading" class="mb-3 text-title font-semibold tracking-[-0.014em] text-ink">
        What a branch gets
      </h2>
      <div class="grid gap-3 md:grid-cols-2">
        {#each DOES as d (d.title)}
          {@const Icon = d.icon}
          <article class="elev rounded-panel border border-line bg-surface p-[15px]">
            <div class="flex items-center gap-2">
              <Icon size={16} class="text-accent" />
              <h3 class="text-body font-semibold leading-[1.35] text-ink">{d.title}</h3>
            </div>
            <p class="mt-1.5 text-body-sm leading-[1.5] text-ink-2">{d.body}</p>
          </article>
        {/each}
      </div>

      <h2 class="mb-2.5 mt-6 text-title font-semibold tracking-[-0.014em] text-ink">Not yet</h2>
      <p class="mb-3 max-w-[660px] text-body-sm leading-[1.5] text-ink-2">
        Three things the branch assistant is often described as doing, which it does not do today.
        They are here because deciding what to tell a branch needs both halves.
      </p>
      <ul class="divide-y divide-line-2 rounded-panel border border-line bg-surface">
        {#each NOT_YET as n (n.title)}
          <li class="flex gap-2.5 px-[15px] py-3">
            <CircleDashed size={15} class="mt-0.5 shrink-0 text-ink-3" />
            <div class="min-w-0">
              <p class="text-body-sm font-semibold text-ink">{n.title}</p>
              <p class="mt-1 text-body-sm leading-[1.5] text-ink-2">{n.body}</p>
            </div>
          </li>
        {/each}
      </ul>
    </section>
  </div>

  <aside class="flex flex-col gap-3">
    <section
      class="rounded-panel border border-[color-mix(in_srgb,var(--color-accent)_24%,transparent)] bg-accent-soft p-[15px]"
      aria-labelledby="scope-heading"
    >
      <div class="flex items-center gap-2">
        <ShieldCheck size={16} class="text-accent" />
        <h2 id="scope-heading" class="text-body font-semibold text-accent">One branch, read-only</h2>
      </div>
      <p class="mt-2 text-body-sm leading-[1.55] text-accent">
        The branch is not something the visitor picks or the page asks for — it is signed into the
        session by the server and cannot be changed from the browser. The widget answers for that
        branch and nothing else, and it has no way to write a stock row.
      </p>
    </section>

    <section class="rounded-panel border border-line bg-surface p-[15px]" aria-labelledby="real-heading">
      <h2 id="real-heading" class="text-body font-semibold text-ink">This is the real thing</h2>
      <p class="mt-2 text-body-sm leading-[1.55] text-ink-2">
        The panel on the left is not a picture of the widget. It is
        <span class="font-mono text-label">/embed/preview</span> loading the same
        <span class="font-mono text-label">widget.js</span> a customer site loads, over the same
        embed API, against live stock. Ask it something — the answer you get is the answer a
        pharmacist gets.
      </p>
    </section>
  </aside>
</div>
