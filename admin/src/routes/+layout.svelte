<script>
  import { API_BASE } from '$lib/apiBase.js';
  import '../app.css';
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { goto } from '$app/navigation';
  import { browser } from '$app/environment';
  import {
    LayoutDashboard,
    Database,
    Server,
    MessagesSquare,
    Building2,
    Store,
    Bot,
    FlaskConical,
    SlidersHorizontal,
    Search,
    MessageCircle,
    LogOut,
    Users,
    Code2,
    Sun,
    Moon,
    Share2,
    Menu,
    Bell,
    Pill,
    Brain,
    KeyRound,
    ShieldCheck,
    Loader2,
    ArrowRight,
    Eye,
    EyeOff,
    Package,
    Sparkles,
    Lock,
    Building
  } from '@lucide/svelte';
  import ToastHost from '$lib/aurora/ToastHost.svelte';

  let { children } = $props();

  const API = API_BASE;

  // Capture the SSO token handed back in the URL *fragment* (#sso_token=…) by the
  // Keycloak callback. A fragment never reaches a server, so the token stays out
  // of access logs and Referer headers. Scrub it from the address bar at once so
  // it does not linger in browser history.
  if (browser) {
    const frag = new URLSearchParams(location.hash.replace(/^#/, ''));
    const sso = frag.get('sso_token');
    if (sso) {
      localStorage.setItem('auth_token', sso);
      history.replaceState({}, '', location.pathname + location.search);
    }
  }

  let authToken = $state(browser ? localStorage.getItem('auth_token') || '' : '');
  let email = $state(browser ? localStorage.getItem('login_email') || '' : '');
  let password = $state('');
  let loginErr = $state('');
  let ssoEnabled = $state(false);
  let ssoName = $state('SSO');
  let ldapEnabled = $state(false);
  let showPw = $state(false);
  let remember = $state(browser ? localStorage.getItem('login_email') != null : true);
  let signingIn = $state(false);

  // Time-of-day greeting for the sign-in headline (browser-local clock).
  const greeting = browser
    ? (() => {
        const h = new Date().getHours();
        return h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening';
      })()
    : 'Welcome';

  // Login showcase animation: a "worked step" walks 1→5 on a loop, the example
  // question rotates, and the answer counts up — so the right panel reads live,
  // not a static screenshot. Pure decoration; only the login DOM consumes it.
  const demoQueries = [
    { en: 'Do we have Relyte in stock?', my: 'ဖျားနာ ဆေး ရှိလား?' },
    { en: "What can I give instead of Alaxan?", my: 'Alaxan အစား ဘာပေးလို့ရလဲ?' },
    { en: 'Show Royal-D stock at Yankin', my: 'Royal-D ဘယ်လောက် ကျန်လဲ?' }
  ];
  let activeStep = $state(0);
  let demoIdx = $state(0);
  let answerCount = $state(0);
  if (browser) {
    setInterval(() => {
      activeStep = (activeStep + 1) % 5;
      if (activeStep === 0) demoIdx = (demoIdx + 1) % demoQueries.length;
      // count settles on the "Answer" step, resets when the walk restarts
      if (activeStep === 4) answerCount = 8 + ((demoIdx * 5 + 4) % 20);
      else if (activeStep === 0) answerCount = 0;
    }, 1400);
  }

  // Approval gate: an authenticated account only reaches the console once an
  // admin has approved it. `me` is null until /auth/me answers; a pending
  // account is held on the CMHL notice screen and re-checked on a timer, so the
  // moment an admin approves, the held session lets itself in with no re-login.
  let me = $state(null);
  let meLoaded = $state(false);

  async function refreshMe() {
    if (!browser || !authToken) {
      meLoaded = true;
      return;
    }
    try {
      // Send the token explicitly: this can fire before the global fetch
      // Authorization patch is installed, so relying on it would 401 the first
      // /auth/me and bounce a freshly-logged-in user straight back to login.
      const r = await fetch(API + '/auth/me', {
        headers: { Authorization: `Bearer ${authToken}` }
      });
      if (r.status === 401) {
        localStorage.removeItem('auth_token');
        authToken = '';
        me = null;
      } else if (r.ok) {
        me = await r.json();
      }
    } catch {
      /* backend offline — keep the current view, try again on the next tick */
    } finally {
      meLoaded = true;
    }
  }

  if (browser && authToken) {
    refreshMe();
    // While pending, poll so approval lands without the user doing anything.
    setInterval(() => {
      if (authToken && me && !me.approved) refreshMe();
    }, 5000);
  }

  if (browser && !window.__authFetchPatched) {
    window.__authFetchPatched = true;
    const orig = window.fetch.bind(window);
    window.fetch = async (input, init = {}) => {
      const url = typeof input === 'string' ? input : input?.url || '';
      const t = localStorage.getItem('auth_token');
      if (url.includes(API) && t) {
        init = { ...init, headers: { ...(init.headers || {}), Authorization: `Bearer ${t}` } };
      }
      const res = await orig(input, init);
      // Expired/invalid token on an admin call → don't mislabel as "backend
      // offline"; clear the dead token and bounce to the login screen.
      if (res.status === 401 && url.includes(API) && url.includes('/admin/') && t) {
        localStorage.removeItem('auth_token');
        location.reload();
      }
      return res;
    };
    fetch(API + '/auth/config')
      .then((r) => r.json())
      .then((c) => {
        ssoEnabled = !!c.oidc_enabled;
        ssoName = c.oidc_provider_name || 'SSO';
        ldapEnabled = !!c.ldap_enabled;
      })
      .catch(() => {});
  }

  async function signIn() {
    if (signingIn) return;
    loginErr = '';
    signingIn = true;
    try {
      const r = await fetch(API + '/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email.trim(), password })
      });
      if (!r.ok) {
        loginErr = (await r.json().catch(() => ({}))).detail || 'invalid credentials';
        signingIn = false;
        return;
      }
      const d = await r.json();
      if (remember) localStorage.setItem('login_email', email.trim());
      else localStorage.removeItem('login_email');
      localStorage.setItem('auth_token', d.token);
      location.reload();
    } catch {
      loginErr = 'backend offline';
      signingIn = false;
    }
  }
  function ssoLogin() {
    location.href = API + '/auth/sso/login';
  }
  function signOut() {
    localStorage.removeItem('auth_token');
    location.reload();
  }

  // ---- dark mode ----
  let dark = $state(false);
  if (browser) {
    dark = localStorage.getItem('theme') === 'dark';
    document.documentElement.classList.toggle('dark', dark);
  }
  function toggleTheme() {
    dark = !dark;
    document.documentElement.classList.toggle('dark', dark);
    localStorage.setItem('theme', dark ? 'dark' : 'light');
  }

  let menuOpen = $state(false);

  // ---- single-level grouped nav ----
  const SECTIONS = [
    { label: 'Overview', items: [{ href: '/', label: 'Overview', icon: LayoutDashboard }] },
    {
      label: 'Assistant',
      items: [
        { href: '/chat', label: 'Chat tester', icon: MessageCircle },
        { href: '/conversations', label: 'Conversations', icon: MessagesSquare }
      ]
    },
    {
      label: 'Data',
      items: [
        { href: '/data', label: 'Catalog & inventory', icon: Database },
        { href: '/ftp', label: 'SFTP uploads', icon: Server }
      ]
    },
    {
      label: 'Intelligence',
      items: [
        { href: '/graph', label: 'Knowledge graph', icon: Share2 },
        { href: '/eval', label: 'Evaluation', icon: FlaskConical },
        { href: '/learning', label: 'Learning', icon: Brain }
      ]
    },
    {
      label: 'Organization',
      items: [
        { href: '/stores', label: 'Stores', icon: Store },
        { href: '/tenants', label: 'Tenants', icon: Building2 },
        { href: '/users', label: 'Users', icon: Users }
      ]
    },
    {
      label: 'Configuration',
      items: [
        { href: '/settings', label: 'Answer behaviour', icon: SlidersHorizontal },
        { href: '/auth', label: 'Authentication', icon: KeyRound },
        { href: '/agent', label: 'Agent', icon: Bot },
        { href: '/embed', label: 'Embed widget', icon: Code2 }
      ]
    }
  ];

  const ALL_PAGES = SECTIONS.flatMap((s) => s.items.map((i) => ({ ...i, section: s.label })));

  // Path relative to the SvelteKit base (e.g. '/admin'), so route matching works
  // whether the app is served at root (dev) or under /admin (docker).
  let relPath = $derived($page.url.pathname.slice(base.length) || '/');

  // Chat renders full-bleed with its own history sidebar (Claude-style),
  // so we hide the sidebar and drop the centered padding.
  let fullBleed = $derived(relPath.startsWith('/chat'));

  function isActive(href) {
    if (href === '/') return relPath === '/';
    return relPath === href || relPath.startsWith(href + '/');
  }

  // ---- command search over pages ("/" focuses it) ----
  let searchEl = $state(null);
  let searchQuery = $state('');
  let searchOpen = $state(false);

  let searchResults = $derived(
    searchQuery.trim()
      ? ALL_PAGES.filter((p) => p.label.toLowerCase().includes(searchQuery.trim().toLowerCase()))
      : []
  );

  function onGlobalKey(e) {
    if (e.key === '/' && e.target === document.body) {
      e.preventDefault();
      searchEl?.focus();
    }
    if (e.key === 'Escape') searchOpen = false;
  }
  function openPage(href) {
    searchQuery = '';
    searchOpen = false;
    menuOpen = false;
    goto(base + href);
  }
  function onSearchKey(e) {
    if (e.key === 'Enter' && searchResults.length) openPage(searchResults[0].href);
  }
</script>

<svelte:window onkeydown={onGlobalKey} />

{#if !authToken}
  <div class="grid min-h-screen grid-cols-1 bg-page lg:grid-cols-[minmax(0,1fr)_minmax(0,1.05fr)]">
    <!-- LEFT · sign-in (theme-aware) -->
    <div class="flex flex-col justify-between px-6 py-8 sm:px-10 lg:px-14">
      <div class="flex items-center gap-2.5">
        <span
          class="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-[10px] bg-accent text-on-accent"
        >
          <Pill size={19} />
        </span>
        <div class="leading-tight">
          <div class="page-title text-[15px] text-ink">City Pharma</div>
          <div class="text-[10.5px] uppercase tracking-[0.14em] text-ink-3">Stock Intelligence</div>
        </div>
      </div>

      <div class="mx-auto w-full max-w-[380px] py-10">
        <h1 class="page-title text-[30px] leading-[1.18] text-ink sm:text-[34px]">
          {greeting},<br />sign in to <span class="text-accent">City Pharma</span>
        </h1>
        <p class="mt-3 text-[13.5px] leading-relaxed text-ink-2">
          Ask about stock in plain words — English or Burmese. Read-only by design.
        </p>

        <label class="mb-1.5 mt-8 block text-[11px] font-medium uppercase tracking-wide text-ink-3" for="email">Email</label>
        <input
          id="email"
          type="email"
          bind:value={email}
          onkeydown={(e) => e.key === 'Enter' && signIn()}
          placeholder="you@citypharma.mm"
          class="w-full rounded-[11px] border border-line bg-surface px-3.5 py-3 text-[14px] text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none"
        />

        <label class="mb-1.5 mt-4 block text-[11px] font-medium uppercase tracking-wide text-ink-3" for="pw">Password</label>
        <div class="relative">
          <input
            id="pw"
            type={showPw ? 'text' : 'password'}
            bind:value={password}
            onkeydown={(e) => e.key === 'Enter' && signIn()}
            placeholder="••••••••"
            class="w-full rounded-[11px] border border-line bg-surface px-3.5 py-3 pr-16 text-[14px] text-ink placeholder:text-ink-3 focus:border-accent focus:outline-none"
          />
          <button
            type="button"
            onclick={() => (showPw = !showPw)}
            class="absolute right-2 top-1/2 flex -translate-y-1/2 items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium text-ink-2 hover:bg-surface-2"
          >
            {#if showPw}<EyeOff size={13} />Hide{:else}<Eye size={13} />Show{/if}
          </button>
        </div>

        <label class="mt-4 flex cursor-pointer items-center gap-2 text-[12.5px] text-ink-2">
          <input
            type="checkbox"
            bind:checked={remember}
            class="h-4 w-4 rounded border-line text-accent accent-[var(--accent,#c2410c)]"
          />
          Remember me
        </label>

        {#if loginErr}
          <p class="mt-3 rounded-lg bg-danger-soft px-3 py-2 text-[12px] text-danger">{loginErr}</p>
        {/if}

        <button
          onclick={signIn}
          disabled={signingIn}
          class="mt-5 flex w-full items-center justify-center gap-2 rounded-[11px] bg-accent px-4 py-3 text-[14px] font-semibold text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
        >
          {#if signingIn}<Loader2 size={16} class="animate-spin" /> Signing in…{:else}Continue with email <ArrowRight size={16} />{/if}
        </button>

        {#if ssoEnabled || ldapEnabled}
          <div class="my-6 flex items-center gap-3 text-[10.5px] uppercase tracking-[0.14em] text-ink-3">
            <span class="h-px flex-1 bg-line"></span>Or continue with<span class="h-px flex-1 bg-line"></span>
          </div>

          <div class="rounded-[13px] border border-line bg-surface p-3">
            <div class="mb-2.5 flex items-center gap-1.5 px-1 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-ink-3">
              <ShieldCheck size={13} /> Enterprise sign-in
            </div>
            {#if ssoEnabled}
              <button
                onclick={ssoLogin}
                class="flex w-full items-center justify-center gap-2 rounded-[10px] border border-line bg-page px-4 py-3 text-[13.5px] font-medium text-ink transition-colors hover:bg-surface-2"
              >
                <KeyRound size={16} class="text-accent" /> Sign in with {ssoName}
              </button>
            {/if}
            {#if ldapEnabled}
              <p class="mt-2 flex items-center justify-center gap-1.5 rounded-[10px] bg-page px-4 py-2.5 text-[12px] text-ink-2">
                <Building size={13} class="text-accent" /> Directory (LDAP) sign-in is enabled — use your work email above.
              </p>
            {/if}
          </div>
        {/if}

        <p class="mt-6 flex items-center gap-1.5 text-[11px] text-ink-3">
          <Lock size={11} /> No self-signup — accounts are created by an administrator.
        </p>
      </div>

      <div class="text-[11px] text-ink-3">© 2026 City Medical Health &amp; Logistics · Read-only</div>
    </div>

    <!-- RIGHT · live showcase (deliberately dark, single-look product panel) -->
    <div class="relative hidden overflow-hidden bg-[#141110] p-8 lg:flex lg:items-center lg:justify-center">
      <div
        class="pointer-events-none absolute -right-24 -top-24 h-96 w-96 rounded-full bg-accent/20 blur-3xl"
      ></div>
      <div class="relative w-full max-w-[520px] rounded-[20px] border border-white/10 bg-[#1b1613]/80 p-6 shadow-2xl backdrop-blur">
        <div class="flex items-center gap-2 text-[12px] text-zinc-400">
          <span class="relative flex h-2 w-2">
            <span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75"></span>
            <span class="relative inline-flex h-2 w-2 rounded-full bg-emerald-500"></span>
          </span>
          <span class="font-semibold uppercase tracking-wide text-emerald-400">Live</span>
          How City Pharma turns a question into an answer
        </div>

        <div class="mt-4 rounded-xl border border-white/10 bg-black/30 px-4 py-3 text-[13px] text-zinc-500">
          Read-only by design — your stock stays untouched.
        </div>
        {#key demoIdx}
          <p class="demo-q mt-2 pl-1 text-[12px] italic text-zinc-500">
            “{demoQueries[demoIdx].en}” · “{demoQueries[demoIdx].my}”
          </p>
        {/key}

        <div class="mt-5 space-y-1">
          {#each [
            { n: 1, icon: Search, t: 'Understand', s: 'Reads the question — English or Burmese.', m: 'name → code' },
            { n: 2, icon: Package, t: 'Find product', s: 'Resolves the drug, brand or alias.', m: 'RELYTE' },
            { n: 3, icon: ShieldCheck, t: 'Check stock', s: 'Looks up live inventory for your branch.', m: 'read-only' },
            { n: 4, icon: Sparkles, t: 'Suggest', s: 'Offers a substitute if it is out.', m: '3 options' },
            { n: 5, icon: ArrowRight, t: 'Answer', s: 'Delivered with the branch and count.', m: '12 units' }
          ] as st}
            {@const active = st.n - 1 === activeStep}
            {@const done = st.n - 1 < activeStep}
            <div
              class="flex items-center gap-3 rounded-xl border px-3.5 py-2.5 transition-all duration-300 {active
                ? 'border-accent/50 bg-accent/10'
                : done
                  ? 'border-transparent opacity-60'
                  : 'border-transparent opacity-35'}"
            >
              <span
                class="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-lg transition-colors duration-300 {active
                  ? 'bg-accent text-white'
                  : done
                    ? 'bg-emerald-500/20 text-emerald-400'
                    : 'bg-white/5 text-zinc-400'}"
              >
                {#if active}
                  <Loader2 size={14} class="animate-spin" />
                {:else}
                  <st.icon size={14} />
                {/if}
              </span>
              <div class="min-w-0 flex-1">
                <div class="text-[13px] font-medium text-zinc-100">{st.t}</div>
                <div class="truncate text-[11.5px] text-zinc-500">{st.s}</div>
              </div>
              <span
                class="flex-shrink-0 font-mono text-[11px] {active ? 'text-accent' : 'text-zinc-400'}"
                >{st.n === 5 && answerCount ? answerCount + ' units' : st.m}</span
              >
            </div>
          {/each}
        </div>

        <div class="mt-5 flex flex-wrap gap-1.5">
          {#each ['Bilingual EN · မြန်မာ', 'Read-only guard', 'SSO / LDAP', 'Store scope', 'Substitutes'] as chip}
            <span class="rounded-full border border-white/10 bg-white/5 px-2.5 py-1 text-[11px] text-zinc-300"
              >{chip}</span
            >
          {/each}
        </div>

        <div class="mt-5 flex items-center justify-between border-t border-white/10 pt-3 text-[11px] text-zinc-500">
          <span><span class="font-semibold text-zinc-300">37</span> agents · <span class="font-semibold text-zinc-300">multi-branch</span></span>
          <span class="flex items-center gap-1.5"><span class="h-1.5 w-1.5 rounded-full bg-emerald-500"></span> SSO ready</span>
        </div>
      </div>
    </div>
  </div>
{:else if meLoaded && me && !me.approved}
  <!-- Authenticated, but access is held until an admin approves this account. -->
  <div class="flex min-h-screen items-center justify-center bg-page px-4">
    <div class="elev w-[440px] max-w-[94vw] rounded-[20px] border border-line bg-surface p-9 text-center">
      <span
        class="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-full bg-accent-soft text-accent"
      >
        <ShieldCheck size={28} />
      </span>
      <div class="page-title text-[19px] text-ink">CMHL Secure Platform</div>
      <p class="mx-auto mt-3 max-w-[340px] text-[13.5px] leading-relaxed text-ink-2">
        You are accessing a restricted City Medical Health &amp; Logistics system. Activity is
        logged. Access to pharmacy stock data is granted only to authorised staff.
      </p>

      <div class="mt-6 rounded-[13px] border border-line bg-page px-4 py-3.5 text-left">
        <div class="flex items-center gap-2 text-[13px] font-medium text-ink">
          <span class="relative flex h-2 w-2">
            <span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-70"></span>
            <span class="relative inline-flex h-2 w-2 rounded-full bg-amber-500"></span>
          </span>
          Awaiting administrator approval
        </div>
        <p class="mt-1.5 text-[12px] text-ink-3">
          Signed in as <span class="text-ink-2">{me.email}</span>. An administrator has been
          notified. This screen unlocks itself the moment your access is approved — you do not
          need to sign in again.
        </p>
      </div>

      <div class="mt-6 flex items-center justify-center gap-2 text-[12px] text-ink-3">
        <Loader2 size={14} class="animate-spin" /> Checking for approval…
      </div>

      <button
        onclick={signOut}
        class="mt-6 inline-flex items-center gap-1.5 text-[13px] font-medium text-ink-2 hover:text-ink"
      >
        <LogOut size={14} /> Sign out
      </button>
    </div>
  </div>
{:else}
  <div class="relative flex h-screen flex-col overflow-hidden bg-page">
    <header
      class="relative z-50 flex h-[60px] flex-shrink-0 items-center gap-2.5 border-b border-line bg-surface px-[18px]"
    >
      <button
        onclick={() => (menuOpen = !menuOpen)}
        aria-label="Toggle menu"
        class="flex h-9 w-9 items-center justify-center rounded-lg text-ink-2 hover:bg-surface-2 lg:hidden"
      >
        <Menu size={22} />
      </button>

      <a href={base + '/'} class="flex items-center gap-2.5">
        <span
          class="flex h-[34px] w-[34px] flex-shrink-0 items-center justify-center rounded-[9px] bg-accent text-on-accent"
        >
          <Pill size={19} />
        </span>
        <div class="leading-[1.15]">
          <div class="page-title text-[15px] text-ink">City Pharma</div>
          <div class="text-[9.5px] font-semibold uppercase tracking-[0.14em] text-ink-3">
            Admin console
          </div>
        </div>
      </a>

      <div class="relative ml-[18px] hidden md:block">
        <div
          class="flex w-[260px] items-center gap-2 rounded-[10px] border border-line bg-surface-2 px-3 py-2
            focus-within:border-accent focus-within:bg-surface"
        >
          <Search size={18} class="text-ink-3" />
          <input
            bind:this={searchEl}
            bind:value={searchQuery}
            onfocus={() => (searchOpen = true)}
            onkeydown={onSearchKey}
            aria-label="Search pages"
            placeholder="Search pages…"
            class="min-w-0 flex-1 border-0 bg-transparent text-[13.5px] text-ink outline-none placeholder:text-ink-3"
          />
          <span
            class="rounded-[5px] border border-line px-1.5 text-[10.5px] text-ink-3"
            style="font-family:var(--font-mono)">/</span
          >
        </div>

        {#if searchOpen && searchQuery.trim()}
          <button
            class="fixed inset-0 z-[54] cursor-default"
            aria-label="Close search"
            onclick={() => (searchOpen = false)}
          ></button>
          <div
            class="absolute left-0 top-[46px] z-[55] w-[320px] overflow-hidden rounded-xl border border-line bg-surface"
            style="box-shadow:var(--shadow-pop)"
          >
            {#if searchResults.length === 0}
              <div class="p-4 text-center text-[12.5px] text-ink-3">
                No matches for "{searchQuery}"
              </div>
            {:else}
              {#each searchResults as r (r.href)}
                <button
                  onclick={() => openPage(r.href)}
                  class="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-left hover:bg-surface-2"
                >
                  <r.icon size={17} class="text-ink-3" />
                  <div class="min-w-0 flex-1">
                    <div class="text-[13px] font-semibold text-ink">{r.label}</div>
                    <div class="text-[11px] text-ink-3">{r.section}</div>
                  </div>
                </button>
              {/each}
            {/if}
          </div>
        {/if}
      </div>

      <div class="ml-auto flex items-center gap-1">
        <button
          aria-label="Notifications"
          class="flex h-9 w-9 items-center justify-center rounded-[9px] text-ink-3 hover:bg-surface-2 hover:text-ink"
        >
          <Bell size={20} />
        </button>
        <button
          onclick={toggleTheme}
          aria-label="Toggle theme"
          class="flex h-9 w-9 items-center justify-center rounded-[9px] text-ink-3 hover:bg-surface-2 hover:text-ink"
        >
          {#if dark}<Sun size={18} />{:else}<Moon size={18} />{/if}
        </button>
        <div class="ml-1.5 flex items-center gap-2 border-l border-line pl-3">
          <span
            class="page-title flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-accent-2 text-[12px] text-on-accent"
            >AD</span
          >
          <div class="hidden leading-tight sm:block">
            <div class="text-[12.5px] font-semibold text-ink">admin</div>
            <div class="text-[10px] tracking-[0.03em] text-ink-3">SUPER ADMIN</div>
          </div>
          <button
            onclick={signOut}
            aria-label="Sign out"
            class="flex h-8 w-8 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 hover:text-ink"
          >
            <LogOut size={18} />
          </button>
        </div>
      </div>
    </header>

    <div class="relative flex min-h-0 flex-1">
      {#if menuOpen}
        <button
          class="fixed inset-x-0 bottom-0 top-[60px] z-[39] cursor-default bg-black/40 lg:hidden"
          aria-label="Close menu"
          onclick={() => (menuOpen = false)}
        ></button>
      {/if}

      <aside
        class="fixed bottom-0 top-[60px] z-40 w-[228px] flex-shrink-0 overflow-y-auto border-r border-line
          bg-surface px-2.5 pb-4 transition-transform duration-200 lg:static lg:top-0 lg:z-auto lg:translate-x-0
          {fullBleed ? 'lg:hidden' : ''}
          {menuOpen ? 'translate-x-0' : '-translate-x-full'}"
      >
        {#each SECTIONS as section (section.label)}
          <div
            class="px-2.5 pb-1.5 pt-4 text-[10px] font-bold uppercase tracking-[0.1em] text-ink-3"
          >
            {section.label}
          </div>
          {#each section.items as item (item.href)}
            {@const active = isActive(item.href)}
            <a
              href={base + item.href}
              onclick={() => (menuOpen = false)}
              class="flex items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-[13.5px] transition-colors
                {active
                ? 'bg-accent-soft font-semibold text-accent'
                : 'font-medium text-ink-2 hover:bg-surface-2 hover:text-ink'}"
            >
              <item.icon size={19} strokeWidth={active ? 2 : 1.75} />
              <span>{item.label}</span>
            </a>
          {/each}
        {/each}
      </aside>

      {#if fullBleed}
        <main class="min-w-0 flex-1 overflow-hidden">
          {@render children()}
        </main>
      {:else}
        <main class="min-w-0 flex-1 overflow-y-auto px-6 py-6 sm:px-8">
          <div class="mx-auto max-w-5xl">
            {@render children()}
          </div>
        </main>
      {/if}
    </div>
  </div>

  <ToastHost />
{/if}

<style>
  .demo-q {
    animation: demoFade 0.5s ease-out;
  }
  @keyframes demoFade {
    from {
      opacity: 0;
      transform: translateY(4px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
</style>
