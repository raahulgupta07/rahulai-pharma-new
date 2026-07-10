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
    Brain
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
  let email = $state('');
  let password = $state('');
  let loginErr = $state('');
  let ssoEnabled = $state(false);
  let ssoName = $state('SSO');

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
      })
      .catch(() => {});
  }

  async function signIn() {
    loginErr = '';
    try {
      const r = await fetch(API + '/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email.trim(), password })
      });
      if (!r.ok) {
        loginErr = (await r.json().catch(() => ({}))).detail || 'invalid credentials';
        return;
      }
      const d = await r.json();
      localStorage.setItem('auth_token', d.token);
      location.reload();
    } catch {
      loginErr = 'backend offline';
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
  <div class="flex min-h-screen items-center justify-center bg-page">
    <div class="elev w-[380px] max-w-[92vw] rounded-[18px] border border-line bg-surface p-8">
      <div class="mb-[22px] flex items-center gap-3">
        <span
          class="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-[11px] bg-accent text-on-accent"
        >
          <Pill size={21} />
        </span>
        <div>
          <div class="page-title text-[18px] text-ink">City Pharma admin</div>
          <div class="text-[12.5px] text-ink-2">Sign in to continue</div>
        </div>
      </div>

      <label class="mb-1.5 block text-[12px] text-ink-2" for="email">Email</label>
      <input
        id="email"
        type="email"
        bind:value={email}
        onkeydown={(e) => e.key === 'Enter' && signIn()}
        placeholder="you@citypharma.mm"
        class="mb-3.5 w-full rounded-[10px] border border-line bg-surface px-3.5 py-2.5 text-[14px] text-ink placeholder:text-ink-3 focus:border-accent"
      />
      <label class="mb-1.5 block text-[12px] text-ink-2" for="pw">Password</label>
      <input
        id="pw"
        type="password"
        bind:value={password}
        onkeydown={(e) => e.key === 'Enter' && signIn()}
        placeholder="••••••••"
        class="w-full rounded-[10px] border border-line bg-surface px-3.5 py-2.5 text-[14px] text-ink placeholder:text-ink-3 focus:border-accent"
      />

      {#if loginErr}
        <p class="mt-3 rounded-lg bg-danger-soft px-3 py-2 text-[12px] text-danger">{loginErr}</p>
      {/if}

      <button
        onclick={signIn}
        class="mt-4 w-full rounded-[10px] bg-accent px-4 py-2.5 text-[14px] font-semibold text-on-accent transition-colors hover:bg-accent-hover"
      >
        Sign in
      </button>

      {#if ssoEnabled}
        <button
          onclick={ssoLogin}
          class="mt-2 w-full rounded-[10px] border border-line bg-surface px-4 py-2.5 text-[14px] font-medium text-ink transition-colors hover:bg-surface-2"
        >
          Sign in with {ssoName}
        </button>
      {/if}

      <p class="mt-4 text-[11px] text-ink-3">
        No self-signup — accounts are created by an administrator.
      </p>
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
