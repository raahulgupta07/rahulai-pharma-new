<script>
  import '../app.css';
  import { page } from '$app/stores';
  import { base } from '$app/paths';
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
    Settings,
    Search,
    MessageCircle,
    Lock,
    Users,
    Code2,
    Sun,
    Moon,
    Share2,
    Menu,
    Bell,
    Pill,
    Boxes,
    MessageSquare,
    Brain
  } from '@lucide/svelte';
  import ToastHost from '$lib/aurora/ToastHost.svelte';

  let { children } = $props();

  const API = 'http://localhost:8088';

  // capture SSO token handed back via ?sso_token= (Keycloak callback redirect)
  if (browser) {
    const p = new URLSearchParams(location.search);
    const sso = p.get('sso_token');
    if (sso) {
      localStorage.setItem('auth_token', sso);
      history.replaceState({}, '', location.pathname);
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

  // ---- two-level nav: top product tabs + grouped left sub-nav ----
  const TABS = [
    {
      id: 'chat',
      label: 'Chat',
      icon: MessageCircle,
      groups: [
        { label: 'Assistant', items: [{ href: '/chat', label: 'Chat tester', icon: MessageCircle }] }
      ]
    },
    {
      id: 'workspace',
      label: 'Workspace',
      icon: LayoutDashboard,
      groups: [
        {
          label: 'Insights',
          items: [
            { href: '/', label: 'Overview', icon: LayoutDashboard },
            { href: '/conversations', label: 'Conversations', icon: MessagesSquare },
            { href: '/eval', label: 'Evaluation', icon: FlaskConical },
            { href: '/graph', label: 'Knowledge graph', icon: Share2 },
            { href: '/learning', label: 'Learning', icon: Brain },
            { href: '/stores', label: 'Stores', icon: Store }
          ]
        }
      ]
    },
    {
      id: 'data',
      label: 'Data',
      icon: Database,
      groups: [
        {
          label: 'Data',
          items: [
            { href: '/data', label: 'Catalog & inventory', icon: Database },
            { href: '/ftp', label: 'SFTP uploads', icon: Server }
          ]
        }
      ]
    },
    {
      id: 'settings',
      label: 'Settings',
      icon: Settings,
      groups: [
        {
          label: 'Config',
          items: [
            { href: '/settings', label: 'Answer behaviour', icon: MessageSquare },
            { href: '/agent', label: 'Agent', icon: Bot },
            { href: '/users', label: 'Users', icon: Users },
            { href: '/tenants', label: 'Tenants', icon: Building2 },
            { href: '/embed', label: 'Embed widget', icon: Code2 }
          ]
        }
      ]
    }
  ];

  function tabOfPath(path) {
    for (const tab of TABS) {
      for (const g of tab.groups) {
        for (const it of g.items) {
          if (it.href === '/' ? path === '/' : path === it.href || path.startsWith(it.href + '/')) {
            return tab.id;
          }
        }
      }
    }
    return 'workspace';
  }

  // Path relative to the SvelteKit base (e.g. '/admin'), so route matching works
  // whether the app is served at root (dev) or under /admin (docker).
  let relPath = $derived($page.url.pathname.slice(base.length) || '/');

  let activeTab = $derived(tabOfPath(relPath));
  let activeTabObj = $derived(TABS.find((t) => t.id === activeTab) ?? TABS[1]);

  // Chat renders full-bleed with its own history sidebar (Claude-style),
  // so we hide the app's grouped sub-nav and drop the centered padding.
  let fullBleed = $derived(relPath.startsWith('/chat'));

  function isActive(href) {
    const path = relPath;
    if (href === '/') return path === '/';
    return path === href || path.startsWith(href + '/');
  }
  function activeLabel() {
    for (const g of activeTabObj.groups)
      for (const it of g.items) if (isActive(it.href)) return it.label;
    return activeTabObj.label;
  }
</script>

{#if !authToken}
  <div class="flex h-screen items-center justify-center bg-page">
    <div class="w-[380px] rounded-xl border-[0.5px] border-line bg-surface p-7">
      <div class="mb-5 flex items-center gap-2.5">
        <span
          class="flex h-9 w-9 items-center justify-center rounded-[10px] bg-gradient-to-br from-accent to-accent-hover text-white shadow-[0_2px_8px_rgba(201,99,66,.35)]"
        >
          <Pill size={18} />
        </span>
        <div>
          <div class="page-title text-[18px] text-ink">City Pharma admin</div>
          <div class="text-[12px] text-ink-2">Sign in to continue</div>
        </div>
      </div>

      <label class="mb-1 block text-[12px] text-ink-2" for="email">Email</label>
      <input
        id="email"
        type="email"
        bind:value={email}
        onkeydown={(e) => e.key === 'Enter' && signIn()}
        placeholder="you@company.com"
        class="mb-3 w-full rounded-lg border border-line bg-surface px-3.5 py-2.5 text-[14px] text-ink placeholder:text-ink-3 focus:border-accent"
      />
      <label class="mb-1 block text-[12px] text-ink-2" for="pw">Password</label>
      <input
        id="pw"
        type="password"
        bind:value={password}
        onkeydown={(e) => e.key === 'Enter' && signIn()}
        placeholder="••••••••"
        class="w-full rounded-lg border border-line bg-surface px-3.5 py-2.5 text-[14px] text-ink placeholder:text-ink-3 focus:border-accent"
      />

      {#if loginErr}
        <p class="mt-3 rounded-lg bg-danger-soft px-3 py-2 text-[12px] text-danger">{loginErr}</p>
      {/if}

      <button
        onclick={signIn}
        class="mt-4 w-full rounded-lg bg-accent px-4 py-2.5 text-[14px] font-medium text-white transition-colors hover:bg-accent-hover"
      >
        Sign in
      </button>

      {#if ssoEnabled}
        <button
          onclick={ssoLogin}
          class="mt-2 w-full rounded-lg border border-line bg-surface px-4 py-2.5 text-[14px] font-medium text-ink transition-colors hover:bg-surface-2"
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
  <div class="flex h-screen flex-col overflow-hidden">
    <!-- TOP PRODUCT TAB BAR -->
    <header
      class="z-40 flex flex-shrink-0 items-center gap-1.5 border-b border-line bg-surface px-4 py-2.5"
    >
      <button
        onclick={() => (menuOpen = !menuOpen)}
        aria-label="Toggle menu"
        class="flex h-9 w-9 items-center justify-center rounded-lg text-ink-2 hover:bg-surface-2 lg:hidden"
      >
        <Menu size={18} />
      </button>

      <div class="mr-2 flex items-center gap-2.5 pr-2">
        <span
          class="flex h-8 w-8 items-center justify-center rounded-[10px] bg-gradient-to-br from-accent to-accent-hover text-white shadow-[0_2px_8px_rgba(201,99,66,.35)]"
        >
          <Pill size={17} />
        </span>
        <div class="leading-none">
          <div class="text-[15px] font-bold tracking-tight text-ink">City Pharma</div>
          <div class="mt-0.5 text-[9px] font-semibold tracking-[0.18em] text-ink-3">PHARMA</div>
        </div>
      </div>

      <nav class="hidden items-center gap-1 sm:flex">
        {#each TABS as tab}
          {@const on = activeTab === tab.id}
          <a
            href={base + tab.groups[0].items[0].href}
            class="flex items-center gap-2 rounded-[10px] px-3 py-1.5 text-[13.5px] transition-colors
              {on ? 'bg-surface-2 font-semibold text-ink' : 'font-medium text-ink-2 hover:bg-surface-2 hover:text-ink'}"
          >
            <tab.icon size={16} />
            <span>{tab.label}</span>
          </a>
        {/each}
      </nav>

      <div class="ml-auto flex items-center gap-1">
        <button
          aria-label="Search"
          class="flex h-9 w-9 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 hover:text-ink"
        >
          <Search size={18} />
        </button>
        <button
          aria-label="Notifications"
          class="relative flex h-9 w-9 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 hover:text-ink"
        >
          <Bell size={18} />
        </button>
        <button
          onclick={toggleTheme}
          aria-label="Toggle theme"
          class="flex h-9 w-9 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 hover:text-ink"
        >
          {#if dark}<Sun size={18} />{:else}<Moon size={18} />{/if}
        </button>
        <div class="ml-1 flex items-center gap-2 pl-1">
          <span
            class="flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-accent to-accent-hover text-[12px] font-bold text-white"
            >AD</span
          >
          <div class="hidden leading-tight sm:block">
            <div class="text-[13px] font-semibold text-ink">admin</div>
            <div class="text-[10px] tracking-[0.05em] text-ink-3">SUPER ADMIN</div>
          </div>
          <button
            onclick={signOut}
            aria-label="Sign out"
            class="flex h-9 w-9 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 hover:text-ink"
          >
            <Lock size={16} />
          </button>
        </div>
      </div>
    </header>

    <div class="flex min-h-0 flex-1 overflow-hidden">
      <!-- Mobile backdrop -->
      {#if menuOpen}
        <div
          class="fixed inset-0 top-[57px] z-30 bg-black/40 lg:hidden"
          onclick={() => (menuOpen = false)}
          aria-hidden="true"
        ></div>
      {/if}

      <!-- GROUPED LEFT SUB-NAV (hidden on full-bleed chat) -->
      <aside
        class="fixed top-[57px] bottom-0 z-40 w-[212px] flex-shrink-0 overflow-y-auto border-r border-line bg-surface
          px-2.5 py-3 transition-transform duration-200 lg:static lg:top-0 lg:z-auto lg:translate-x-0
          {fullBleed ? 'lg:hidden' : ''}
          {menuOpen ? 'translate-x-0' : '-translate-x-full'}"
      >
        {#each activeTabObj.groups as g}
          <div class="px-2.5 pb-1.5 pt-3.5 text-[10px] font-bold uppercase tracking-[0.1em] text-ink-3">
            {g.label}
          </div>
          {#each g.items as item}
            {@const active = isActive(item.href)}
            <a
              href={base + item.href}
              onclick={() => (menuOpen = false)}
              class="flex items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-[13.5px] transition-colors
                {active
                ? 'bg-accent-soft font-semibold text-accent'
                : 'font-medium text-ink-2 hover:bg-surface-2 hover:text-ink'}"
            >
              <item.icon size={17} strokeWidth={active ? 2 : 1.75} />
              <span>{item.label}</span>
            </a>
          {/each}
        {/each}
      </aside>

      <!-- MAIN -->
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
