<script>
  import { dialog } from '$lib/aurora/dialog.js';
  // Branding — the white-label surface. Four tabs over one config document:
  //
  //   Identity    the words: product name, short name, tagline, subtitle, promise
  //   Logos       the four images, with client-side pre-checks and dark handling
  //   Parent org  the owning organisation: name, legal footer, hold-screen title
  //   Preview     the login screen and the rail, live, light AND dark
  //
  // Rules that run through the whole file and are easy to break:
  //
  //  1. AN UNCONFIGURED INSTALL MUST LOOK EXACTLY AS IT DOES TODAY. Every field
  //     has a server-side default equal to what the UI hardcoded before this
  //     page existed. An empty field here means "use the default", never "render
  //     nothing" — so a cleared field is sent as an empty string and the server
  //     falls back. Nothing on this page ever invents a placeholder value.
  //  2. SVG IS NOT ACCEPTED. An SVG is a document that can carry <script>, and
  //     these images are served from our own origin to every console and every
  //     customer page the widget sits on. PNG/JPEG only. The check here only
  //     saves a round trip; the server re-checks and is the real gate.
  //  3. THE PREVIEW IS THE POINT. The operator has to see the result before
  //     saving, in both themes, because the dark palette is derived and a logo
  //     with a solid white background is a bright tile on a dark rail. That is
  //     what the chip/variant choice is for.
  //  4. A MISSING ENDPOINT IS NOT A CRASH. This page may be opened against a
  //     backend that predates the branding API. A 404 says so plainly and the
  //     rest of the console keeps working.
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import TabStrip from '$lib/TabStrip.svelte';
  import { API_BASE } from '$lib/apiBase.js';
  import PageHeader from '$lib/PageHeader.svelte';
  import ErrorState from '$lib/ErrorState.svelte';
  import { getJSON, ApiError } from '$lib/api.js';
  import { toast } from '$lib/aurora/toast.js';
  import {
    Palette,
    Type,
    Image as ImageIcon,
    Building2,
    Eye,
    Upload,
    Trash2,
    RefreshCw,
    Save,
    Loader2,
    TriangleAlert,
    CircleAlert,
    CircleCheck,
    RotateCcw,
    Pill,
    Sun,
    Moon,
    X
  } from '@lucide/svelte';

  const BASE = API_BASE;

  // ------------------------------------------------------------------ tabs
  const TABS = [
    { id: 'identity', label: 'Identity', icon: Type },
    { id: 'logos', label: 'Logos', icon: ImageIcon },
    { id: 'parent', label: 'Parent org', icon: Building2 },
    { id: 'preview', label: 'Preview', icon: Eye }
  ];
  const TAB_IDS = TABS.map((t) => t.id);

  // The tab lives in the URL so it is linkable and survives a refresh. As a
  // panel inside /settings, whose own tab bar owns `?tab=`, the inner tab rides
  // `?sub=` — two tab bars sharing one parameter would fight over it.
  let tab = $derived.by(() => {
    const t = $page.url.searchParams.get('sub');
    return TAB_IDS.includes(t) ? t : 'identity';
  });
  function setTab(id) {
    const u = new URL($page.url);
    u.searchParams.set('sub', id);
    goto(u.pathname + u.search, { replaceState: true, noScroll: true, keepFocus: true });
  }

  // ------------------------------------------------------------- the fields
  // `where` is the whole reason this list is data and not markup: an operator
  // renaming the product needs to know which screen each string lands on before
  // they type, not after they save. `limit` matches the server's cap.
  const IDENTITY_FIELDS = [
    {
      key: 'product_name',
      label: 'Product name',
      limit: 24,
      where: 'Browser tab, login headline, login lockup, widget default title'
    },
    {
      key: 'short_name',
      label: 'Short name',
      limit: 12,
      where: 'Rail lockup, beside the square icon — keep it narrow'
    },
    {
      key: 'tagline',
      label: 'Tagline',
      limit: 40,
      where: 'Small caps under the product name on the login screen'
    },
    {
      key: 'console_subtitle',
      label: 'Console subtitle',
      limit: 24,
      where: 'Small caps under the product name in the rail'
    },
    {
      key: 'login_promise',
      label: 'Login promise',
      limit: 160,
      rows: 3,
      where: 'The paragraph directly under the sign-in headline'
    }
  ];
  const PARENT_FIELDS = [
    {
      key: 'parent_name',
      label: 'Parent organisation',
      limit: 24,
      where: 'Hold-screen wording and the alt text of the parent logo'
    },
    {
      key: 'legal_footer',
      label: 'Legal footer',
      limit: 120,
      where: 'Bottom-left of the login screen, beside the parent logo'
    },
    {
      key: 'pending_title',
      label: 'Hold-screen title',
      limit: 40,
      // The only field with no shipped wording of its own: blank is a real
      // choice, so the copy has to say what blank produces.
      where:
        'Optional heading a not-yet-approved account sees — blank reads “Thanks for signing in to ‹product name›”'
    }
  ];
  const TEXT_KEYS = [...IDENTITY_FIELDS, ...PARENT_FIELDS].map((f) => f.key);

  const ASSETS = [
    {
      key: 'icon',
      title: 'Square icon',
      where: 'Rail, browser favicon, widget launcher',
      empty: 'not set — falls back to the built-in pill mark'
    },
    {
      key: 'lockup',
      title: 'Horizontal lockup',
      where: 'Login screen',
      empty: 'not set — falls back to icon + product name'
    },
    {
      key: 'lockup_dark',
      title: 'Dark-mode lockup',
      where: 'Login screen in dark mode (optional)',
      empty: 'not set — the light lockup is used, in a chip if “Use chip” is selected'
    },
    {
      key: 'parent',
      title: 'Parent logo',
      where: 'Login footer and rail footer',
      empty: 'not set — the footers show text only'
    }
  ];

  const MAX_BYTES = 1024 * 1024;
  const MAX_PX = 1024;
  const OK_MIME = ['image/png', 'image/jpeg'];

  // ------------------------------------------------------------------ state
  let loading = $state(true);
  let error = $state(null);
  let unsupported = $state(false);
  let saving = $state(false);
  let resetting = $state(false);
  let confirmReset = $state(false);

  let form = $state({
    product_name: '',
    short_name: '',
    tagline: '',
    console_subtitle: '',
    login_promise: '',
    legal_footer: '',
    pending_title: '',
    parent_name: '',
    dark_logo_mode: 'chip'
  });
  let saved = $state(null); // snapshot of the last server state, for `dirty`
  let assets = $state({}); // key -> {url, mime, width, height, size_bytes, updated_at}
  let version = $state('');
  // per-asset transient status: {state:'busy'|'ok'|'err', msg}
  let assetStatus = $state({});

  let dirty = $derived(
    !!saved && Object.keys(form).some((k) => String(form[k] ?? '') !== String(saved[k] ?? ''))
  );
  let overLimit = $derived(
    [...IDENTITY_FIELDS, ...PARENT_FIELDS].some((f) => (form[f.key] || '').length > f.limit)
  );

  // The admin payload may carry a per-asset object or a bare URL. Normalise to
  // one shape so the rest of the page never has to ask which it got. A relative
  // path is made absolute against the API base — never a remote origin.
  function normalizeAssets(raw) {
    const out = {};
    for (const { key } of ASSETS) {
      const a = raw?.[key];
      if (!a) continue;
      if (typeof a === 'string') out[key] = { url: absolute(a) };
      else if (typeof a === 'object') {
        const u = a.url || a.href || `/brand/asset/${key}`;
        out[key] = { ...a, url: absolute(u) };
      }
    }
    return out;
  }
  function absolute(u) {
    if (!u) return '';
    return /^https?:\/\//i.test(u) ? u : BASE + (u.startsWith('/') ? '' : '/') + u;
  }
  function asset(key) {
    return assets[key] || null;
  }

  async function load() {
    loading = true;
    error = null;
    unsupported = false;
    try {
      // A 404 keeps its own dedicated panel below (this backend predates the
      // branding API — a different fact from a failure), so it is caught by
      // status rather than folded into the generic error.
      const d = await getJSON('/admin/branding');
      for (const k of TEXT_KEYS) form[k] = d[k] ?? '';
      form.dark_logo_mode = d.dark_logo_mode === 'variant' ? 'variant' : 'chip';
      saved = { ...form };
      assets = normalizeAssets(d.assets);
      version = d.version || '';
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        unsupported = true;
      } else {
        error = e;
      }
    } finally {
      loading = false;
    }
  }

  async function save() {
    if (saving || overLimit) return;
    saving = true;
    try {
      // Partial by contract: only what changed is sent, so a field this console
      // does not know about is never overwritten with a blank.
      const body = {};
      for (const k of Object.keys(form)) {
        if (String(form[k] ?? '') !== String(saved?.[k] ?? '')) body[k] = form[k];
      }
      const r = await fetch(BASE + '/admin/branding', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        toast(d.detail || 'could not save branding', 'alert-triangle');
        return;
      }
      await load();
      toast('Branding saved');
    } catch {
      toast('backend offline — nothing was saved', 'alert-triangle');
    } finally {
      saving = false;
    }
  }

  async function doReset() {
    resetting = true;
    try {
      const r = await fetch(BASE + '/admin/branding/reset', { method: 'POST' });
      if (!r.ok) {
        toast('could not reset branding', 'alert-triangle');
        return;
      }
      confirmReset = false;
      await load();
      toast('Branding reset to defaults');
    } catch {
      toast('backend offline — nothing was reset', 'alert-triangle');
    } finally {
      resetting = false;
    }
  }

  // ------------------------------------------------------------- asset upload
  function prettyBytes(n) {
    if (!Number.isFinite(n)) return '—';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(0) + ' KB';
    return (n / (1024 * 1024)).toFixed(2) + ' MB';
  }

  function dimensions(file) {
    return new Promise((res) => {
      const url = URL.createObjectURL(file);
      const im = new Image();
      im.onload = () => {
        res({ w: im.naturalWidth, h: im.naturalHeight });
        URL.revokeObjectURL(url);
      };
      im.onerror = () => {
        res(null);
        URL.revokeObjectURL(url);
      };
      im.src = url;
    });
  }

  async function pick(key, ev) {
    const file = ev.currentTarget.files?.[0];
    ev.currentTarget.value = ''; // so re-picking the same file fires again
    if (!file) return;

    // --- client pre-checks. The server re-checks all three; this only saves a
    // --- round trip and gives a specific message instead of a 400.
    if (file.type === 'image/svg+xml' || /\.svg$/i.test(file.name)) {
      assetStatus[key] = {
        state: 'err',
        msg: 'SVG is not accepted — an SVG can carry script, and this file is served to every console and customer page. Export a PNG instead.'
      };
      return;
    }
    if (!OK_MIME.includes(file.type)) {
      assetStatus[key] = { state: 'err', msg: `PNG or JPEG only — this file is ${file.type || 'an unknown type'}.` };
      return;
    }
    if (file.size > MAX_BYTES) {
      assetStatus[key] = { state: 'err', msg: `Too large: ${prettyBytes(file.size)}. The limit is 1 MB.` };
      return;
    }
    const d = await dimensions(file);
    if (!d) {
      assetStatus[key] = { state: 'err', msg: 'This file could not be read as an image.' };
      return;
    }
    if (d.w > MAX_PX || d.h > MAX_PX) {
      assetStatus[key] = { state: 'err', msg: `Too big: ${d.w}×${d.h}px. The limit is 1024px on the longest side.` };
      return;
    }

    assetStatus[key] = { state: 'busy', msg: `Uploading ${file.name}…` };
    try {
      const fd = new FormData();
      fd.append('file', file, file.name);
      const r = await fetch(BASE + `/admin/branding/asset/${key}`, { method: 'POST', body: fd });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        assetStatus[key] = { state: 'err', msg: j.detail || `Upload refused (HTTP ${r.status}).` };
        return;
      }
      await load();
      assetStatus[key] = { state: 'ok', msg: `${file.name} uploaded · ${d.w}×${d.h}px` };
    } catch {
      assetStatus[key] = { state: 'err', msg: 'Backend offline — nothing was uploaded.' };
    }
  }

  async function removeAsset(key) {
    assetStatus[key] = { state: 'busy', msg: 'Removing…' };
    try {
      const r = await fetch(BASE + `/admin/branding/asset/${key}`, { method: 'DELETE' });
      if (!r.ok) {
        assetStatus[key] = { state: 'err', msg: `Could not remove (HTTP ${r.status}).` };
        return;
      }
      await load();
      assetStatus[key] = { state: 'ok', msg: 'Removed — the documented fallback applies now.' };
    } catch {
      assetStatus[key] = { state: 'err', msg: 'Backend offline — nothing was removed.' };
    }
  }

  // ---------------------------------------------------------------- preview
  // What the login screen and the rail resolve to, given the current form and
  // the uploaded assets. Mirrors the chain in +layout.svelte exactly:
  //   lockup (dark variant when asked for and present) -> icon + product name
  //   -> built-in pill mark + product name
  function lockupFor(darkMode) {
    if (darkMode && form.dark_logo_mode === 'variant' && asset('lockup_dark'))
      return { url: asset('lockup_dark').url, chip: false };
    const l = asset('lockup');
    if (l) return { url: l.url, chip: darkMode && form.dark_logo_mode === 'chip' };
    return null;
  }
  function iconFor(darkMode) {
    const i = asset('icon');
    if (!i) return null;
    return { url: i.url, chip: darkMode && form.dark_logo_mode === 'chip' };
  }

  const PV_SCALE = 0.54;
  const PV_W = 720;
  const PV_H = 430;
  const RAIL_H = 300;

  const inputCls =
    'w-full rounded-panel border border-line bg-page px-3 py-2 text-body-sm text-ink outline-none placeholder:text-ink-3 focus:border-accent';
  const btnCls =
    'inline-flex items-center gap-2 rounded-panel border border-line px-3 py-1.5 text-body-sm font-medium text-ink hover:bg-surface-2 disabled:opacity-60';
  const primaryCls =
    'inline-flex items-center gap-2 rounded-panel bg-accent px-4 py-1.5 text-body-sm font-semibold text-on-accent hover:bg-accent-hover disabled:opacity-60';

  // The Escape listener and the focus-in effect that used to live here were
  // both correct, and both incomplete: there was no Tab trap, so two Tabs past
  // the two buttons landed on the skip link and then the nav rail with the
  // dialog still open — invisible behind a 45%-black backdrop, on a dialog
  // guarding an irreversible action ("all four uploaded images are deleted").
  // Escape also left focus in the rail rather than on the trigger.
  // `use:dialog` below owns all of it.

  onMount(load);
</script>


<PageHeader
  level={2}
  title="Branding"
  subtitle="The product name, the marks and the legal line, everywhere they appear. Leave a field empty to keep the shipped default."
>
  {#snippet actions()}
    <button type="button" onclick={load} class={btnCls}>
      <RefreshCw size={15} /> Reload
    </button>
    <button type="button" onclick={() => (confirmReset = true)} class={btnCls} disabled={unsupported}>
      <RotateCcw size={15} /> Reset to defaults
    </button>
    <button type="button" onclick={save} disabled={saving || !dirty || overLimit} class={primaryCls}>
      {#if saving}<Loader2 size={15} class="animate-spin" />{:else}<Save size={15} />{/if}
      Save text
    </button>
  {/snippet}
  {#snippet meta()}
    {#if dirty}
      <span
        class="inline-flex items-center gap-1.5 rounded-full bg-warning-soft px-2.5 py-0.5 text-label font-medium text-warning"
      >
        <CircleAlert size={12} /> Unsaved text changes
      </span>
    {:else if saved}
      <span class="inline-flex items-center gap-1.5 text-label text-ink-3">
        <CircleCheck size={12} /> Text saved
      </span>
    {/if}
    {#if version}
      <span class="font-mono text-label text-ink-3">v{version}</span>
    {/if}
    <span class="text-label text-ink-3">Images upload immediately; text needs Save.</span>
  {/snippet}
</PageHeader>

<!-- tabs -->
<TabStrip tabs={TABS} value={tab} onchange={setTab} gap="gap-x-5" label="Branding sections" />

{#if loading}
  <p class="mt-5 text-body-sm text-ink-2">Loading…</p>
{:else if unsupported}
  <div class="mt-5 rounded-card border border-line bg-surface px-5 py-6 text-body-sm text-ink-2">
    <p class="flex items-center gap-2 font-medium text-ink">
      <TriangleAlert size={16} class="text-warning" /> This backend has no branding API yet
    </p>
    <p class="mt-1.5 leading-relaxed">
      <span class="font-mono text-body-sm text-ink">GET /admin/branding</span> answered 404. The console
      keeps running on the shipped defaults — nothing is broken, and nothing on this page can be saved
      until the server is updated.
    </p>
  </div>
{:else if error}
  <div class="mt-5">
    <ErrorState {error} retry={load} what="the branding settings" />
  </div>
{:else}
  <!-- ------------------------------------------------------ shared snippets -->
  {#snippet textField(f, multiline)}
    {@const val = form[f.key] || ''}
    {@const over = val.length > f.limit}
    <div>
      <div class="mb-1.5 flex items-baseline gap-2">
        <label for={'f-' + f.key} class="text-body-sm font-medium text-ink">{f.label}</label>
        <span
          class="tnum ml-auto text-label {over ? 'font-semibold text-danger' : val.length > f.limit * 0.8 ? 'text-warning' : 'text-ink-3'}"
        >
          {val.length}/{f.limit}
        </span>
      </div>
      {#if multiline}
        <textarea
          id={'f-' + f.key}
          bind:value={form[f.key]}
          rows={f.rows || 3}
          class="{inputCls} resize-y leading-relaxed"
          placeholder="Leave empty for the shipped default"
        ></textarea>
      {:else}
        <input
          id={'f-' + f.key}
          type="text"
          bind:value={form[f.key]}
          class={inputCls}
          placeholder="Leave empty for the shipped default"
        />
      {/if}
      <p class="mt-1.5 text-label leading-relaxed text-ink-3">{f.where}</p>
      {#if over}
        <p class="mt-1 text-label font-medium text-danger">
          Over the {f.limit}-character limit — the server will refuse this.
        </p>
      {/if}
    </div>
  {/snippet}

  {#snippet assetCard(a)}
    {@const cur = asset(a.key)}
    {@const st = assetStatus[a.key]}
    <div class="rounded-panel border border-line bg-surface p-4">
      <div class="flex items-baseline gap-2">
        <h3 class="text-body-sm font-semibold text-ink">{a.title}</h3>
        <span class="ml-auto text-label text-ink-3">{a.where}</span>
      </div>

      <!-- Checkerboard so a transparent PNG reads as transparent and not as
           "white background", which is the single most common upload mistake
           and the one the dark-mode chip exists to survive. -->
      <div class="checker mt-3 flex h-[104px] items-center justify-center rounded-card border border-line p-2">
        {#if cur}
          <img src={cur.url} alt={a.title + ' preview'} class="max-h-full max-w-full object-contain" />
        {:else}
          <span class="px-3 text-center text-meta leading-relaxed text-ink-3">{a.empty}</span>
        {/if}
      </div>

      <p class="mt-2 text-label text-ink-3">
        {#if cur}
          <span class="font-mono">{cur.mime || 'image'}</span> ·
          {prettyBytes(cur.size_bytes)} ·
          {#if Number.isFinite(cur.width) && Number.isFinite(cur.height)}
            <span class="tnum">{cur.width}×{cur.height}</span>
          {:else}
            size not reported
          {/if}
          {#if cur.updated_at}<span class="text-ink-3"> · {cur.updated_at}</span>{/if}
        {:else}
          No file stored.
        {/if}
      </p>

      <div class="mt-3 flex flex-wrap items-center gap-2">
        <label for={'up-' + a.key} class="text-meta font-medium text-ink-2">
          {cur ? 'Replace' : 'Upload'}
        </label>
        <input
          id={'up-' + a.key}
          type="file"
          accept="image/png,image/jpeg"
          onchange={(e) => pick(a.key, e)}
          class="min-w-0 flex-1 rounded-panel border border-line bg-page px-2 py-1.5 text-meta text-ink-2 file:mr-2 file:rounded-control file:border-0 file:bg-surface-2 file:px-2 file:py-1 file:text-meta file:text-ink"
        />
        {#if cur}
          <button
            type="button"
            onclick={() => removeAsset(a.key)}
            aria-label={'Remove ' + a.title}
            class={btnCls}
          >
            <Trash2 size={14} /> Remove
          </button>
        {/if}
      </div>

      <div role="status" aria-live="polite" class="mt-2 min-h-[18px] text-label leading-relaxed">
        {#if !st}
          <span class="text-ink-3">PNG or JPEG · 1 MB max · 1024px max.</span>
        {:else if st.state === 'busy'}
          <span class="inline-flex items-center gap-1.5 text-ink-2"><Loader2 size={12} class="animate-spin" /> {st.msg}</span>
        {:else if st.state === 'ok'}
          <span class="inline-flex items-start gap-1.5 text-success"><CircleCheck size={12} class="mt-0.5 shrink-0" /> {st.msg}</span>
        {:else}
          <span class="inline-flex items-start gap-1.5 text-danger"><CircleAlert size={12} class="mt-0.5 shrink-0" /> {st.msg}</span>
        {/if}
      </div>
    </div>
  {/snippet}

  <!-- The login mock and the rail mock, rendered from the live form. Both take
       a `d` flag and are placed inside .pv-light / .pv-dark, which redefine the
       --c-* runtime vars: every Tailwind colour class inside resolves through
       them, so one piece of markup renders truthfully in both themes. -->
  {#snippet markBlock(d, size)}
    {@const lk = lockupFor(d)}
    {@const ic = iconFor(d)}
    {#if lk}
      <span class={lk.chip ? 'inline-flex rounded-card bg-surface p-1.5' : 'inline-flex'}>
        <img src={lk.url} alt={(form.product_name || 'Product') + ' logo'} style={`height:${size}px`} class="w-auto object-contain" />
      </span>
    {:else}
      <span class="flex items-center gap-2.5">
        {#if ic}
          <span
            class="flex flex-shrink-0 items-center justify-center rounded-card {ic.chip ? 'bg-surface' : ''}"
            style={`height:${size + 8}px;width:${size + 8}px`}
          >
            <img src={ic.url} alt="" style={`height:${size}px;width:${size}px`} class="object-contain" />
          </span>
        {:else}
          <span
            class="flex flex-shrink-0 items-center justify-center rounded-card bg-accent text-on-accent"
            style={`height:${size + 8}px;width:${size + 8}px`}
          >
            <Pill size={Math.round(size * 0.62)} />
          </span>
        {/if}
        <span class="leading-tight">
          <span class="page-title block text-body text-ink">{form.product_name || 'City Care Agent'}</span>
          <span class="block text-micro uppercase tracking-[0.14em] text-ink-3">{form.tagline || 'Stock Intelligence'}</span>
        </span>
      </span>
    {/if}
  {/snippet}

  {#snippet loginMock(d)}
    <div
      class="flex flex-col justify-between bg-page px-8 py-7"
      style={`width:${PV_W}px;height:${PV_H}px`}
    >
      {@render markBlock(d, 22)}
      <div class="w-[330px]">
        <div class="page-title text-display leading-[1.18] text-ink">
          Good morning,<br />sign in to <span class="text-accent">{form.product_name || 'City Care Agent'}</span>
        </div>
        <p class="mt-3 text-body-sm leading-relaxed text-ink-2">
          {form.login_promise || 'Ask about stock in plain words — English or Burmese. Read-only by design.'}
        </p>
        <div class="mt-5 rounded-card border border-line bg-surface px-3.5 py-3 text-body-sm text-ink-3">you@example.com</div>
        <div class="mt-3 rounded-card bg-accent px-4 py-3 text-center text-body-sm font-semibold text-on-accent">
          Continue with email
        </div>
      </div>
      <div class="flex items-center gap-2.5 text-label text-ink-3">
        {#if asset('parent')}
          <img src={asset('parent').url} alt={(form.parent_name || 'Parent organisation') + ' logo'} class="h-4 w-auto object-contain" />
        {/if}
        <span>{form.legal_footer || '© 2026 City Mart Holding Co., Ltd. · Read-only'}</span>
      </div>
    </div>
  {/snippet}

  {#snippet railMock(d)}
    {@const ic = iconFor(d)}
    <div class="flex flex-col bg-surface" style={`width:${PV_W}px;height:${RAIL_H}px`}>
      <div class="flex items-center gap-2.5 border-b border-line px-4 py-3">
        {#if ic}
          <span class="flex h-[34px] w-[34px] flex-shrink-0 items-center justify-center rounded-card {ic.chip ? 'bg-surface' : ''}">
            <img src={ic.url} alt="" class="h-[26px] w-[26px] object-contain" />
          </span>
        {:else}
          <span class="flex h-[34px] w-[34px] flex-shrink-0 items-center justify-center rounded-card bg-accent text-on-accent">
            <Pill size={19} />
          </span>
        {/if}
        <div class="leading-[1.15]">
          <div class="page-title text-body text-ink">{form.short_name || form.product_name || 'City Care'}</div>
          <div class="text-micro font-semibold uppercase tracking-[0.14em] text-ink-3">
            {form.console_subtitle || 'Admin console'}
          </div>
        </div>
      </div>
      <div class="flex flex-1 flex-col gap-1 px-2.5 py-3">
        <div class="px-2.5 pb-1 text-micro font-bold uppercase tracking-[0.1em] text-ink-3">Overview</div>
        <div class="rounded-card bg-accent-soft px-2.5 py-2 text-body-sm font-semibold text-accent">Overview</div>
        <div class="rounded-card px-2.5 py-2 text-body-sm font-medium text-ink-2">Analytics</div>
        <div class="mt-auto flex items-center gap-2 border-t border-line px-2.5 pt-2.5 text-label text-ink-3">
          {#if asset('parent')}
            <img src={asset('parent').url} alt={(form.parent_name || 'Parent organisation') + ' logo'} class="h-3.5 w-auto object-contain" />
          {:else}
            <span>{form.parent_name || 'CMHL'}</span>
          {/if}
          <span class="ml-auto font-semibold">v0.0.0</span>
        </div>
      </div>
    </div>
  {/snippet}

  {#snippet previewFrame(label, d, h, body)}
    <div>
      <div class="mb-1.5 flex items-center gap-1.5 text-label font-medium text-ink-2">
        {#if d}<Moon size={12} />{:else}<Sun size={12} />{/if}
        {label}
      </div>
      <div
        class="overflow-hidden rounded-card border border-line"
        style={`width:${Math.round(PV_W * PV_SCALE)}px;height:${Math.round(h * PV_SCALE)}px`}
      >
        <div style={`transform:scale(${PV_SCALE});transform-origin:top left`} class={d ? 'pv-dark' : 'pv-light'}>
          {@render body(d)}
        </div>
      </div>
    </div>
  {/snippet}

  <!-- ============================================================== IDENTITY -->
  {#if tab === 'identity'}
    <div id="panel-identity" role="tabpanel" aria-labelledby="tab-identity" tabindex="-1" class="mt-5">
      <div class="grid gap-4 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
        <div class="rounded-panel border border-line bg-surface p-5">
          <div class="flex items-center gap-2">
            <Palette size={16} class="text-ink-2" />
            <h2 class="text-body font-semibold text-ink">The words</h2>
          </div>
          <p class="mt-2 text-meta leading-relaxed text-ink-2">
            An empty field keeps the shipped default. Nothing here is ever shown as a placeholder — a
            blank value renders the default text, not an empty space.
          </p>
          <div class="mt-4 space-y-4">
            {#each IDENTITY_FIELDS as f (f.key)}
              {@render textField(f, f.key === 'login_promise')}
            {/each}
          </div>
        </div>

        <div class="rounded-panel border border-line bg-surface p-5">
          <div class="flex items-center gap-2">
            <Eye size={16} class="text-ink-2" />
            <h2 class="text-body font-semibold text-ink">While you type</h2>
          </div>
          <p class="mt-2 text-meta text-ink-2">
            The two surfaces these words land on, in light. The dark pair — where a logo's own
            background starts to matter — is on the Preview tab.
          </p>
          <div class="mt-4 flex flex-wrap gap-4">
            {@render previewFrame('Login', false, PV_H, loginMock)}
            {@render previewFrame('Rail', false, RAIL_H, railMock)}
          </div>
        </div>
      </div>
    </div>
  {/if}

  <!-- ================================================================= LOGOS -->
  {#if tab === 'logos'}
    <div id="panel-logos" role="tabpanel" aria-labelledby="tab-logos" tabindex="-1" class="mt-5 space-y-4">
      <div class="flex items-start gap-2 rounded-card border border-line bg-surface px-4 py-3 text-meta leading-relaxed text-ink-2">
        <TriangleAlert size={14} class="mt-0.5 shrink-0 text-warning" />
        <span>
          <span class="font-medium text-ink">PNG or JPEG only — SVG is not accepted</span>, because an
          SVG is a document that can carry script and these files are served from our own origin into
          every console and every customer page the widget sits on. Maximum 1 MB and 1024px on the
          longest side. The server checks all three again; the check here only saves a round trip.
        </span>
      </div>

      <div class="grid gap-4 md:grid-cols-2">
        {#each ASSETS as a (a.key)}
          {@render assetCard(a)}
        {/each}
      </div>

      <div class="rounded-panel border border-line bg-surface p-5">
        <h3 class="text-body-sm font-semibold text-ink">Dark mode</h3>
        <p class="mt-1.5 max-w-2xl text-meta leading-relaxed text-ink-2">
          Most supplied logos have a solid light background, which sits on a dark rail as a bright
          tile. Containing it in a rounded chip fixes that without a second file. Upload a dark
          variant only if you have artwork made for dark surfaces.
        </p>
        <div class="mt-3 space-y-2">
          <label class="flex cursor-pointer items-start gap-2.5 rounded-card border border-line p-3">
            <input
              type="radio"
              name="dark_logo_mode"
              value="chip"
              bind:group={form.dark_logo_mode}
              class="mt-0.5 h-4 w-4 accent-[var(--c-accent)]"
            />
            <span class="text-body-sm leading-relaxed text-ink">
              <span class="font-medium">Use chip</span>
              <span class="block text-meta text-ink-2">
                The light logo is drawn inside a rounded chip with its own background. Correct when the
                logo has a solid background.
              </span>
            </span>
          </label>
          <label
            class="flex items-start gap-2.5 rounded-card border border-line p-3 {asset('lockup_dark') ? 'cursor-pointer' : 'opacity-60'}"
          >
            <input
              type="radio"
              name="dark_logo_mode"
              value="variant"
              disabled={!asset('lockup_dark')}
              bind:group={form.dark_logo_mode}
              class="mt-0.5 h-4 w-4 accent-[var(--c-accent)]"
            />
            <span class="text-body-sm leading-relaxed text-ink">
              <span class="font-medium">Use dark variant</span>
              <span class="block text-meta text-ink-2">
                {#if asset('lockup_dark')}
                  The dark-mode lockup is used whenever the console is in dark mode.
                {:else}
                  Selectable once a dark-mode lockup is uploaded above.
                {/if}
              </span>
            </span>
          </label>
        </div>
        <p class="mt-3 text-label text-ink-3">
          This is a text setting — press <span class="font-medium text-ink-2">Save text</span> to apply it.
        </p>
      </div>
    </div>
  {/if}

  <!-- ================================================================ PARENT -->
  {#if tab === 'parent'}
    <div id="panel-parent" role="tabpanel" aria-labelledby="tab-parent" tabindex="-1" class="mt-5">
      <div class="grid gap-4 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
        <div class="rounded-panel border border-line bg-surface p-5">
          <div class="flex items-center gap-2">
            <Building2 size={16} class="text-ink-2" />
            <h2 class="text-body font-semibold text-ink">Owning organisation</h2>
          </div>
          <p class="mt-2 text-meta leading-relaxed text-ink-2">
            Who owns the deployment, as it appears on the sign-in footer and on the screen a
            not-yet-approved account is held on.
          </p>
          <div class="mt-4 space-y-4">
            {#each PARENT_FIELDS as f (f.key)}
              {@render textField(f, false)}
            {/each}
          </div>
        </div>
        <div class="space-y-4">
          {@render assetCard(ASSETS[3])}
          <div class="rounded-panel border border-line bg-surface p-5">
            <h3 class="text-body-sm font-semibold text-ink">Hold screen</h3>
            <p class="mt-1.5 text-meta leading-relaxed text-ink-2">
              What an authenticated but unapproved account sees instead of the console.
            </p>
            <div class="mt-3 rounded-panel border border-line bg-page px-5 py-6 text-center">
              <div class="page-title text-title text-ink">{form.pending_title || 'Thanks for signing in to ' + (form.product_name || 'City Care Agent')}</div>
              <p class="mx-auto mt-2 max-w-[320px] text-meta leading-relaxed text-ink-2">
                You are accessing a restricted {form.parent_name || 'CMHL'} system. Activity is logged.
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  {/if}

  <!-- =============================================================== PREVIEW -->
  {#if tab === 'preview'}
    <div id="panel-preview" role="tabpanel" aria-labelledby="tab-preview" tabindex="-1" class="mt-5 space-y-4">
      <div class="rounded-panel border border-line bg-surface p-5">
        <h2 class="text-body font-semibold text-ink">Login screen</h2>
        <p class="mt-1.5 text-meta text-ink-2">
          Rendered from the values above, including anything not yet saved.
        </p>
        <div class="mt-4 flex flex-wrap gap-5">
          {@render previewFrame('Light', false, PV_H, loginMock)}
          {@render previewFrame('Dark', true, PV_H, loginMock)}
        </div>
      </div>

      <div class="rounded-panel border border-line bg-surface p-5">
        <h2 class="text-body font-semibold text-ink">Rail</h2>
        <p class="mt-1.5 text-meta text-ink-2">
          Watch the dark card: a logo with a solid light background is why the chip option exists.
        </p>
        <div class="mt-4 flex flex-wrap gap-5">
          {@render previewFrame('Light', false, RAIL_H, railMock)}
          {@render previewFrame('Dark', true, RAIL_H, railMock)}
        </div>
      </div>

      <p class="text-label leading-relaxed text-ink-3">
        The preview is a faithful re-render of the same markup and the same tokens, not a screenshot.
        It cannot show a font a browser has not loaded, and the dark palette itself is derived rather
        than reviewed — treat dark colour as provisional.
      </p>
    </div>
  {/if}

  <!-- ============================================================== CONFIRM -->
  {#if confirmReset}
    <div class="fixed inset-0 z-[80] flex items-center justify-center p-4">
      <!-- Pointer affordance only; Escape is the keyboard route out and the
           confirm's use:dialog owns it. -->
      <div class="fixed inset-0 cursor-default bg-black/45" onclick={() => (confirmReset = false)} aria-hidden="true"></div>
      <div
        use:dialog={{ onclose: () => (confirmReset = false) }}
        role="dialog"
        aria-modal="true"
        aria-labelledby="reset-title"
        tabindex="-1"
        class="relative w-[460px] max-w-full rounded-hero border border-line bg-surface shadow-[var(--shadow-pop)] outline-none"
      >
        <div class="flex items-center gap-3 border-b border-line px-5 py-4">
          <span class="flex h-9 w-9 shrink-0 items-center justify-center rounded-card bg-warning-soft text-warning">
            <RotateCcw size={18} />
          </span>
          <h2 id="reset-title" class="flex-1 text-body font-semibold text-ink">Reset branding to defaults?</h2>
          <button
            type="button"
            onclick={() => (confirmReset = false)}
            aria-label="Cancel"
            class="flex h-8 w-8 items-center justify-center rounded-panel text-ink-3 hover:bg-surface-2 hover:text-ink"
          >
            <X size={18} />
          </button>
        </div>
        <div class="px-5 py-4 text-body-sm leading-relaxed text-ink-2">
          Every text field goes back to the shipped wording and <span class="font-medium text-ink">all four
          uploaded images are deleted</span>. The console then looks exactly as it does on a fresh
          install. This cannot be undone from here — the files are not kept.
        </div>
        <div class="flex items-center justify-end gap-2 border-t border-line px-5 py-3.5">
          <button type="button" onclick={() => (confirmReset = false)} class={btnCls}>Cancel</button>
          <button
            type="button"
            onclick={doReset}
            disabled={resetting}
            class="inline-flex items-center gap-2 rounded-panel bg-danger px-4 py-1.5 text-body-sm font-semibold text-on-accent hover:opacity-90 disabled:opacity-60"
          >
            {#if resetting}<Loader2 size={15} class="animate-spin" />{:else}<RotateCcw size={15} />{/if}
            Reset everything
          </button>
        </div>
      </div>
    </div>
  {/if}
{/if}

<style>
  /* Transparency checkerboard. Built from the theme's own line/surface tokens so
     it stays legible in both modes and introduces no new colour. */
  .checker {
    background-image:
      linear-gradient(45deg, var(--c-line) 25%, transparent 25%),
      linear-gradient(-45deg, var(--c-line) 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, var(--c-line) 75%),
      linear-gradient(-45deg, transparent 75%, var(--c-line) 75%);
    background-size: 14px 14px;
    background-position: 0 0, 0 7px, 7px -7px, -7px 0;
    background-color: var(--c-surface);
  }

  /* The preview has to show BOTH themes at once, on a page that is itself in one
     of them. Tailwind's colour utilities compile to var(--color-x), which is
     defined as var(--c-x) and resolved at the point of use — so redefining the
     runtime --c-* vars on a wrapper re-themes everything inside it with no
     duplicate markup.

     These values are copied from admin/src/app.css (:root and html.dark). If the
     palette there changes, change it here too — this is the one place in the app
     that deliberately holds a second copy, and only of the tokens the two mocks
     actually use. */
  .pv-light {
    --c-page: #F5F6FA;
    --c-surface: #ffffff;
    --c-surface-2: #EDEFF6;
    --c-ink: #14162E;
    --c-ink-2: #474C63;
    --c-ink-3: #61667E;
    --c-accent: #2F3293;
    --c-accent-soft: #EAEBF7;
    --c-on-accent: #ffffff;
    --c-line: #E2E4EE;
  }
  .pv-dark {
    --c-page: #0B0C1B;
    --c-surface: #14162E;
    --c-surface-2: #1D2043;
    --c-ink: #EEF0F7;
    --c-ink-2: #C7CAEA;
    --c-ink-3: #9AA0C4;
    --c-accent: #9BA0F0;
    --c-accent-soft: #23266F;
    --c-on-accent: #0B0C1B;
    --c-line: #2E3157;
  }
</style>
