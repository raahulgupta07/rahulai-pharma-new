<script>
  import { API_BASE } from '$lib/apiBase.js';
  import '../app.css';
  import { page } from '$app/stores';
  import { base } from '$app/paths';
  import { getJSON } from '$lib/api.js';
  import { goto } from '$app/navigation';
  import { browser } from '$app/environment';
  import { tick } from 'svelte';
  import {
    Activity,
    ArrowRight,
    Building,
    Building2,
    ChartColumn,
    ChevronRight,
    CircleCheckBig,
    Code2,
    Coins,
    Database,
    Eye,
    EyeOff,
    FlaskConical,
    KeyRound,
    LayoutDashboard,
    Loader2,
    Lock,
    LogOut,
    Gavel,
    Menu,
    MessageCircle,
    MessagesSquare,
    Moon,
    Network,
    Package,
    Palette,
    Pill,
    Search,
    Server,
    Share2,
    ShieldCheck,
    Smartphone,
    SlidersHorizontal,
    Sparkles,
    Store,
    Sun,
    Users
  } from '@lucide/svelte';
  import ToastHost from '$lib/aurora/ToastHost.svelte';
  import WhatsNew from '$lib/WhatsNew.svelte';

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

  // ---- branding ------------------------------------------------------------
  // Every product name, mark and legal line on this screen comes from here.
  //
  // The defaults below are the literal strings this file hardcoded before the
  // branding API existed, and they are what renders if `/brand` 404s, fails, or
  // has not answered yet. That is the whole contract: an unconfigured install —
  // or an unreachable one — must look EXACTLY as it did. There is no loading
  // state and no placeholder on the sign-in screen, because a login screen that
  // renders blank or says "{product_name}" for a second is worse than one that
  // renders the shipped name and corrects itself.
  const BRAND_DEFAULTS = {
    product_name: 'City Care Agent',
    short_name: 'City Care',
    tagline: 'Stock Intelligence',
    console_subtitle: 'Admin console',
    login_promise: 'Ask about stock in plain words — English or Burmese. Read-only by design.',
    legal_footer: '© 2026 City Medical Health & Logistics · Read-only',
    pending_title: 'CMHL Secure Platform',
    parent_name: 'CMHL',
    dark_logo_mode: 'chip'
  };
  let brand = $state({ ...BRAND_DEFAULTS, assets: { icon: '', lockup: '', lockup_dark: '', parent: '' } });

  // Asset URLs are same-origin by construction. A relative path is joined to the
  // API base; an absolute URL is accepted ONLY if it points at that same origin.
  // A branding row is operator-written data, and this <img> src ends up on the
  // sign-in screen of every user — it must not be able to become a beacon to a
  // third-party host.
  function brandAsset(u) {
    if (typeof u !== 'string' || !u) return '';
    if (/^https?:\/\//i.test(u)) {
      try {
        return new URL(u).origin === new URL(API).origin ? u : '';
      } catch {
        return '';
      }
    }
    return API + (u.startsWith('/') ? '' : '/') + u;
  }

  function applyBrand(d) {
    const next = { ...BRAND_DEFAULTS };
    for (const k of Object.keys(BRAND_DEFAULTS)) {
      const v = d?.[k];
      // Empty/whitespace means "unset" — fall back, never render a blank label.
      if (typeof v === 'string' && v.trim()) next[k] = v.trim();
    }
    if (next.dark_logo_mode !== 'variant') next.dark_logo_mode = 'chip';
    next.assets = {
      icon: brandAsset(d?.assets?.icon),
      lockup: brandAsset(d?.assets?.lockup),
      lockup_dark: brandAsset(d?.assets?.lockup_dark),
      parent: brandAsset(d?.assets?.parent)
    };
    brand = next;
    if (browser) {
      document.title = next.product_name + ' admin';
      // Cached so app.html can set the tab title synchronously on the next load
      // and avoid a flash of the previous product's name.
      try {
        localStorage.setItem('brand_product_name', next.product_name);
        if (next.assets.icon) localStorage.setItem('brand_icon', next.assets.icon);
        else localStorage.removeItem('brand_icon');
      } catch {
        /* private mode / storage full — the title is still correct this session */
      }
      if (next.assets.icon) {
        for (const rel of ['icon', 'apple-touch-icon']) {
          const el = document.querySelector(`link[rel="${rel}"]`);
          if (el) {
            el.setAttribute('href', next.assets.icon);
            el.removeAttribute('type');
          }
        }
      }
    }
  }

  if (browser) {
    applyBrand(null); // defaults + document.title, before the network answers
    fetch(API + '/brand')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d) applyBrand(d);
      })
      .catch(() => {
        /* offline or an older backend with no /brand — the defaults stand */
      });
  }

  let authToken = $state(browser ? localStorage.getItem('auth_token') || '' : '');
  let email = $state(browser ? localStorage.getItem('login_email') || '' : '');
  let password = $state('');
  let loginErr = $state('');
  let ssoEnabled = $state(false);
  let ssoName = $state('SSO');
  let ldapEnabled = $state(false);
  // The provider type drives the button's logo and default name. Empty means
  // the backend did not say — fall back to the generic key look rather than
  // guessing a brand, which would put someone else's logo on the button.
  let ssoType = $state('');
  const SSO_TYPES = ['keycloak', 'oidc', 'google', 'microsoft'];
  // local | hybrid | sso_only. Derived from oidc_enabled when the backend does
  // not send it, so an older server keeps today's behaviour exactly.
  let signinMode = $state('local');
  const SIGNIN_MODES = ['local', 'hybrid', 'sso_only'];
  // sso_only NEVER removes the password form: a super_admin can always sign in
  // with a password, which is the only way back when a realm breaks.
  let showSso = $derived(ssoEnabled && signinMode !== 'local');
  let ssoPrimary = $derived(showSso && signinMode === 'sso_only');
  let pwOpen = $state(false);
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

  // ---- pending-approval badge on the Users nav entry ----
  // A held account is invisible until someone thinks to open the Users page, and
  // nothing notifies anyone (there is no email path in this system). The badge is
  // the notification. It stays null until the count is actually read: unknown is
  // not zero, and a `0` badge would be a claim we cannot make. The endpoint is
  // super_admin-only and may not exist on an older backend — both fail silently.
  let pendingCount = $state(null);
  async function loadPending() {
    if (!browser || !authToken) return;
    try {
      const r = await fetch(API + '/admin/auth-overview', {
        headers: { Authorization: `Bearer ${authToken}` }
      });
      if (!r.ok) return;
      const d = await r.json();
      const n = d?.local?.pending;
      if (Number.isFinite(n)) pendingCount = n;
    } catch {
      /* offline / not permitted / older backend — show no badge, never a 0 */
    }
  }

  if (browser && authToken) {
    refreshMe();
    loadPending();
    setInterval(loadPending, 60000);
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
        ssoType = SSO_TYPES.includes(c.oidc_provider_type) ? c.oidc_provider_type : '';
        signinMode = SIGNIN_MODES.includes(c.signin_mode)
          ? c.signin_mode
          : ssoEnabled
            ? 'hybrid'
            : 'local';
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

  // ---- build stamp ----
  // /version is PUBLIC, so this needs no token and works on the login screen
  // too — which is where you want it when someone cannot get in and you are
  // trying to establish what they are running. Failure is silent: a missing
  // version stamp must never be able to break the console shell.
  let build = $state(null);
  if (browser) {
    fetch(API + '/version')
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => (build = d))
      .catch(() => {});
  }

  // ---- dark mode ----
  let dark = $state(false);
  if (browser) {
    dark = localStorage.getItem('theme') === 'dark';
    document.documentElement.classList.toggle('dark', dark);
  }

  // ---- "what am I running?" ----
  // Was a bell popover carrying the build stamp, the latest release AND a link
  // to a /version page that said all of it again. One sheet now, opened from
  // the version number in the rail foot. `lib/WhatsNew.svelte` fetches the
  // changelog itself; `build` is already loaded above and is passed in.
  let whatsNewOpen = $state(false);

  /** Move focus past the rail to the page body.
   *
   * The `<main>` is not naturally focusable, so `tabindex="-1"` is on it and
   * `.focus()` is called explicitly. Doing it through the href alone leaves
   * focus on the link — the viewport scrolls, the next Tab returns to the rail
   * row after the link, and the skip link has skipped nothing. */
  async function skipToContent(e) {
    e.preventDefault();
    const main = document.getElementById('main-content');
    if (!main) return;
    // Close FIRST, then await the DOM. While the mobile drawer is open the
    // content column is `inert`, and .focus() on an inert element is silently
    // a no-op — so focusing before closing skipped nothing and left the user on
    // the link. The close and the `inert` removal are the same update, hence
    // the tick.
    menuOpen = false;
    await tick();
    main.focus();
  }

  function toggleTheme() {
    dark = !dark;
    document.documentElement.classList.toggle('dark', dark);
    localStorage.setItem('theme', dark ? 'dark' : 'light');
  }

  // ---- how the brand mark resolves ----
  // Chain, in order: dark variant (only when asked for AND uploaded) → light
  // lockup → square icon + product name → the built-in Pill mark + product name.
  //
  // `chip` is the dark-mode fix: a supplied logo almost always has a solid light
  // background, which on a dark surface reads as a lit tile. Containing it in a
  // rounded chip with its own background makes that deliberate instead of
  // accidental, and costs no second asset.
  let lockupSrc = $derived(
    dark && brand.dark_logo_mode === 'variant' && brand.assets.lockup_dark
      ? brand.assets.lockup_dark
      : brand.assets.lockup
  );
  let lockupChip = $derived(
    !!lockupSrc && dark && !(brand.dark_logo_mode === 'variant' && brand.assets.lockup_dark)
  );
  let iconChip = $derived(!!brand.assets.icon && dark && brand.dark_logo_mode === 'chip');
  let parentChip = $derived(!!brand.assets.parent && dark && brand.dark_logo_mode === 'chip');

  let menuOpen = $state(false);

  // ---- the rail is a DRAWER below `lg`, and that changes the tab order ------
  // Measured at 390x844 with the menu shut: 21 of the first 30 tab stops landed
  // on rail links sitting at x=-276, i.e. off-screen. A keyboard or switch user
  // on a phone tabbed through the whole of an invisible navigation before
  // reaching the page. `-translate-x-full` moves the rail out of SIGHT; it does
  // not move it out of the TAB ORDER.
  //
  // With the menu open the mirror image was true: 15 of 40 stops escaped the
  // drawer into the content behind the scrim, so the "modal" menu was not modal.
  //
  // `inert` fixes both, and it is available here precisely because the rail is a
  // SIBLING of the content rather than nested inside it — the case dialog.js
  // records as out of reach for the modals. Whichever of the two is not the
  // user's current context is inert.
  let isDesktop = $state(true); // assume desktop until measured: nothing inert on first paint
  $effect(() => {
    if (!browser) return;
    const mq = window.matchMedia('(min-width: 1024px)');
    const sync = () => (isDesktop = mq.matches);
    sync();
    mq.addEventListener('change', sync);
    return () => mq.removeEventListener('change', sync);
  });
  let railInert = $derived(!isDesktop && !menuOpen);
  let contentInert = $derived(!isDesktop && menuOpen);

  // ---- signed-in identity (derived from /auth/me, never hardcoded) ----
  // Before /auth/me answers, `me` is null: render a skeleton, never a
  // placeholder identity. A literal "admin / SUPER ADMIN" shown to a
  // branch-pinned user is a lie about their own privileges.
  let meName = $derived(me ? me.name || me.email || '' : '');
  let meInitials = $derived(
    (() => {
      if (!me) return '';
      const src = (me.name || '').trim();
      if (src) {
        const parts = src.split(/\s+/).filter(Boolean);
        return ((parts[0]?.[0] || '') + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
      }
      const local = (me.email || '').split('@')[0] || '';
      const bits = local.split(/[._-]+/).filter(Boolean);
      if (!bits.length) return '';
      return ((bits[0][0] || '') + (bits.length > 1 ? bits[1][0] : '')).toUpperCase();
    })()
  );
  // `super_admin` → "Super admin". Unknown/absent role renders "—", not a guess.
  let meRole = $derived(
    me && me.role
      ? String(me.role).replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase())
      : '—'
  );

  // ---- grouped nav ----
  //
  // The rail is the console's map, and it is grouped the way the work is: what
  // the assistant IS, what it reads FROM, what it is judged BY, what it costs,
  // and who administers it.
  //
  // Two destinations are deliberately NOT rows:
  //   /activity   the raw event feed — reached from "Latest activity" on Today
  //               and from the Security log, which is its authentication slice
  //   /tenants    a tab of People & access
  //   /eval /learning  tabs of Answer quality
  //   /auth /branding /agent  tabs of Configuration
  //   /docs /embed-test       tabs of Embed & integration
  // Every one of them still resolves as a URL and still appears in "/" search
  // via ALL_PAGES below — a merge that removes the only way to reach a page by
  // name is a deletion wearing a tidier label.
  const SECTIONS = [
    {
      label: '',
      items: [{ href: '/', label: 'Today', icon: Sun }]
    },
    {
      label: 'Assistant',
      items: [
        { href: '/widget', label: 'Branch assistant', icon: Smartphone },
        { href: '/chat', label: 'Chat', icon: MessageCircle },
        { href: '/embed', label: 'Embed & integration', icon: Code2 }
      ]
    },
    {
      label: 'Data',
      items: [
        { href: '/data', label: 'Catalog & stock', icon: Package },
        { href: '/stores', label: 'Branches', icon: Store },
        { href: '/ftp', label: 'Data pipeline', icon: Server }
      ]
    },
    {
      label: 'Oversight',
      items: [
        { href: '/analytics', label: 'Health & usage', icon: ChartColumn },
        { href: '/conversations', label: 'Conversations', icon: MessagesSquare },
        { href: '/quality', label: 'Answer quality', icon: CircleCheckBig },
        { href: '/graph', label: 'Knowledge graph', icon: Share2 }
      ]
    },
    {
      label: 'Programme',
      items: [
        { href: '/architecture', label: 'Architecture & health', icon: Network },
        { href: '/cost', label: 'Cost & KPIs', icon: Coins },
        { href: '/governance', label: 'Governance', icon: Gavel }
      ]
    },
    {
      label: 'Administration',
      items: [
        { href: '/users', label: 'People & access', icon: Users },
        { href: '/security-log', label: 'Security log', icon: ShieldCheck },
        { href: '/settings', label: 'Configuration', icon: SlidersHorizontal },
      ]
    },
    {
      label: 'Reference',
      items: [{ href: '/foundations', label: 'Foundations', icon: Palette }]
    }
  ];

  // Destinations that are not rail rows but ARE names people type. Leaving one
  // out means the reorganisation quietly took away the only way to reach it by
  // name — which is a deletion, not a tidy-up.
  const OFF_RAIL = [
    { href: '/activity', label: 'Activity', icon: Activity, section: 'Oversight' },
    { href: '/users?tab=tenants', label: 'Tenants', icon: Building2, section: 'Administration' },
    { href: '/quality?tab=eval', label: 'Evaluation', icon: FlaskConical, section: 'Oversight' },
    { href: '/quality?tab=learning', label: 'Learning', icon: FlaskConical, section: 'Oversight' },
    { href: '/embed?tab=guide', label: 'Guide', icon: Code2, section: 'Assistant' },
    { href: '/embed?tab=test', label: 'Test on a customer domain', icon: Code2, section: 'Assistant' }
  ];

  const ALL_PAGES = [
    ...SECTIONS.flatMap((s) => s.items.map((i) => ({ ...i, section: s.label || 'Overview' }))),
    ...OFF_RAIL
  ];

  // Path relative to the SvelteKit base (e.g. '/admin'), so route matching works
  // whether the app is served at root (dev) or under /admin (docker).
  let relPath = $derived($page.url.pathname.slice(base.length) || '/');

  // Chat is the one screen that owns its whole viewport: it scrolls its thread
  // and docks its composer itself, so `main` gives it no padding and no scroll.
  // The RAIL still stands. It used to be hidden here, which left the chat with
  // a hand-built "← Console" link as the only way back — a second navigation
  // for one screen, and one the design does not have.
  let fullBleed = $derived(relPath.startsWith('/chat'));

  // Matches on the PATH only. That is what keeps the merged destinations lit:
  // a sub-tab is a query string (`/analytics?tab=conversations`,
  // `/settings?tab=auth`, `/analytics?tab=activity`), which never appears in
  // `pathname`, so the parent entry stays highlighted on every tab of it.
  // Do not "improve" this by comparing full URLs.
  function isActive(href) {
    if (href === '/') return relPath === '/';
    return relPath === href || relPath.startsWith(href + '/');
  }

  /**
   * The header's breadcrumb tail — the name of the page you are on.
   *
   * Read from the rail's own list rather than kept as a second copy, so a
   * renamed rail row renames the crumb with it. Off-rail destinations are in
   * ALL_PAGES too, which is what keeps `/activity` from showing as "Console ›
   * Console". The longest matching href wins: `/users` and `/users?tab=tenants`
   * both match a tenants URL, and the more specific one is the truer label.
   */
  let screenTitle = $derived.by(() => {
    const hit = ALL_PAGES.filter((p) => isActive(p.href.split('?')[0])).sort(
      (a, b) => b.href.split('?')[0].length - a.href.split('?')[0].length
    )[0];
    return hit ? hit.label : 'Console';
  });

  // ---- stock freshness, for the header pill ----
  //
  // Every number in this console is only as current as the last stock file, so
  // the design puts its age on EVERY screen. Two rules make that honest:
  //
  //  1. UNKNOWN DRAWS NOTHING. A null timestamp renders no pill at all. A pill
  //     reading "just now" over a file that never loaded is worse than silence,
  //     and it is exactly the failure this console keeps finding — a control
  //     that looks fine while saying nothing true.
  //  2. IT IS A MEASUREMENT, NOT A GUESS. `/admin/data-freshness` is one query
  //     over `ingest_events`, no aggregates, because this renders on every
  //     navigation.
  //
  // Re-read on a timer so the age does not freeze at whatever it was when the
  // console was opened — somebody leaves this tab open all day, and "2 min ago"
  // sitting there at 5pm is a lie the page tells itself.
  let stockAt = $state(null);
  let nowTick = $state(Date.now());

  function relAge(ms) {
    const mins = Math.round(ms / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + ' min ago';
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return hrs + (hrs === 1 ? ' hour ago' : ' hours ago');
    const days = Math.round(hrs / 24);
    return days + (days === 1 ? ' day ago' : ' days ago');
  }

  let stockFreshness = $derived.by(() => {
    if (!stockAt) return null;
    const t = Date.parse(stockAt);
    if (!Number.isFinite(t)) return null;
    return { label: relAge(nowTick - t), exact: new Date(t).toLocaleString() };
  });

  async function loadFreshness() {
    try {
      const d = await getJSON('/admin/data-freshness');
      stockAt = d?.inventory_at ?? null;
    } catch {
      // Silent: a missing timestamp must never be able to break the shell. The
      // pill simply does not draw.
      stockAt = null;
    }
  }

  $effect(() => {
    if (!authToken) return;
    loadFreshness();
    const age = setInterval(() => (nowTick = Date.now()), 30_000);
    const refetch = setInterval(loadFreshness, 300_000);
    return () => {
      clearInterval(age);
      clearInterval(refetch);
    };
  });

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
    if (e.key === 'Escape') {
      searchOpen = false;
      // The mobile menu's scrim used to be a <button>, so Escape was never the
      // way out of it — Tab-to-the-scrim-and-Enter was. The scrim is now a
      // pointer affordance with no tab stop, which leaves Escape as the only
      // keyboard dismissal, so it has to close this too.
      menuOpen = false;
    }
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
      <!--
        Sign-in lockup. A horizontal lockup replaces the whole block when one is
        uploaded (it already contains the name); otherwise the square icon — or
        the built-in Pill mark — sits beside the product name and tagline.
      -->
      {#if lockupSrc}
        <span class={lockupChip ? 'inline-flex rounded-card bg-surface p-2.5' : 'inline-flex'}>
          <!--
            The uploaded lockup already carries the name (and often a tagline) in
            the artwork, so it has to render big enough to READ. h-11 on a narrow
            viewport where the grid is one column, h-14 (~56px) from sm up.
          -->
          <img src={lockupSrc} alt={brand.product_name} class="h-11 w-auto object-contain sm:h-14" />
        </span>
      {:else}
        <div class="flex items-center gap-3.5">
          {#if brand.assets.icon}
            <span
              class="flex h-14 w-14 flex-shrink-0 items-center justify-center rounded-card sm:h-16 sm:w-16 {iconChip
                ? 'bg-surface'
                : ''}"
            >
              <!-- decorative: the product name is right beside it -->
              <img src={brand.assets.icon} alt="" class="h-12 w-12 object-contain sm:h-14 sm:w-14" />
            </span>
          {:else}
            <span
              class="flex h-14 w-14 flex-shrink-0 items-center justify-center rounded-card bg-accent text-on-accent sm:h-16 sm:w-16"
            >
              <Pill size={30} />
            </span>
          {/if}
          <div class="leading-tight">
            <div class="page-title text-title text-ink sm:text-heading">{brand.product_name}</div>
            <div class="mt-0.5 text-micro uppercase tracking-[0.14em] text-ink-3">{brand.tagline}</div>
          </div>
        </div>
      {/if}

      <div class="mx-auto w-full max-w-[440px] py-8 lg:py-10">
        <h1 class="page-title text-display-lg leading-[1.12] tracking-[-0.02em] text-ink [text-wrap:balance] sm:text-hero">
          {greeting},<br />sign in to
          <!-- The name moves as a unit. `text-wrap: balance` on the h1 does not
               rebalance across the <br>, so a two-word product name broke as
               "City Care / Agent" and left the last word orphaned. Keeping the
               name unbroken puts the whole of it on line two. -->
          <span class="whitespace-nowrap text-accent">{brand.product_name}</span>
        </h1>
        <p class="mt-3 max-w-[390px] text-body leading-[1.5] text-ink-3">
          {brand.login_promise}
        </p>

        <!--
          Provider logo for the SSO button. Inline SVG: this SPA is served by the
          app and must render offline, so a remote brand asset is not an option.
          An unknown/absent provider type falls back to the generic key look that
          shipped before — never a guessed brand.
        -->
        {#snippet ssoLogo(id, size)}
          {#if id === 'google'}
            <svg width={size} height={size} viewBox="0 0 48 48" aria-hidden="true" focusable="false">
              <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" />
              <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" />
              <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z" />
              <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z" />
            </svg>
          {:else if id === 'microsoft'}
            <svg width={size} height={size} viewBox="0 0 23 23" aria-hidden="true" focusable="false">
              <rect x="0" y="0" width="10.5" height="10.5" fill="#F25022" />
              <rect x="12.5" y="0" width="10.5" height="10.5" fill="#7FBA00" />
              <rect x="0" y="12.5" width="10.5" height="10.5" fill="#00A4EF" />
              <rect x="12.5" y="12.5" width="10.5" height="10.5" fill="#FFB900" />
            </svg>
          {:else if id === 'keycloak'}
            <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path fill="#008AAA" d="M7 2h10l5 10-5 10H7L2 12z" />
              <path fill="#ffffff" d="M12 7.6a2.6 2.6 0 0 0-.9 5.05V16h1.8v-1.2h1.2v-1.5h-1.2v-.65A2.6 2.6 0 0 0 12 7.6zm0 1.6a1 1 0 1 1 0 2 1 1 0 0 1 0-2z" />
            </svg>
          {:else}
            <KeyRound size={size} class="text-accent" />
          {/if}
        {/snippet}

        {#snippet passwordForm()}
<!--
            Field pattern copied from agentdash: the label lives INSIDE the
            bordered box, and the box (not the input) owns the focus ring via
            focus-within. The input keeps its own `id`/`for` pairing so a screen
            reader still announces the label — nesting alone is not enough for
            every AT.
          -->
          <label
            class="block rounded-card border border-line bg-surface px-[15px] py-2.5 transition-colors focus-within:border-accent focus-within:ring-4 focus-within:ring-accent/15"
            for="email"
          >
            <span class="block text-label font-semibold tracking-[0.03em] text-ink-3">Email</span>
            <input
              id="email"
              type="email"
              autocomplete="username"
              bind:value={email}
              onkeydown={(e) => e.key === 'Enter' && signIn()}
              placeholder="you@company.com"
              class="w-full border-0 bg-transparent p-0 text-body text-ink placeholder:text-ink-3 focus:outline-none"
            />
          </label>

          <div class="relative mt-2.5">
            <label
              class="block rounded-card border border-line bg-surface px-[15px] py-2.5 transition-colors focus-within:border-accent focus-within:ring-4 focus-within:ring-accent/15"
              for="pw"
            >
              <span class="block text-label font-semibold tracking-[0.03em] text-ink-3">Password</span>
              <input
                id="pw"
                type={showPw ? 'text' : 'password'}
                autocomplete="current-password"
                bind:value={password}
                onkeydown={(e) => e.key === 'Enter' && signIn()}
                placeholder="••••••••••"
                class="w-full border-0 bg-transparent p-0 pr-[68px] text-body text-ink placeholder:text-ink-3 focus:outline-none"
              />
            </label>
            <button
              type="button"
              onclick={() => (showPw = !showPw)}
              class="absolute right-3 top-1/2 flex -translate-y-1/2 items-center gap-1 rounded-control bg-surface-2 px-2.5 py-1.5 text-meta font-semibold text-ink-2 hover:bg-line-2"
            >
              {#if showPw}<EyeOff size={13} />Hide{:else}<Eye size={13} />Show{/if}
            </button>
          </div>

          <label class="mt-3 flex cursor-pointer items-center gap-2 text-body-sm text-ink-2">
            <input
              type="checkbox"
              bind:checked={remember}
              class="h-4 w-4 rounded border-line text-accent accent-[var(--c-accent)]"
            />
            Remember me
          </label>

          {#if loginErr}
            <p role="alert" class="mt-3 rounded-card bg-danger-soft px-3.5 py-2.5 text-body-sm text-danger">
              {loginErr}
            </p>
          {/if}

          <button
            onclick={signIn}
            disabled={signingIn}
            class="mt-5 flex min-h-[48px] w-full items-center justify-center gap-2 rounded-card px-4 text-body font-semibold transition-colors disabled:opacity-60
              {ssoPrimary
              ? 'border border-line bg-surface text-ink hover:bg-surface-2'
              : 'bg-accent text-on-accent shadow-[var(--shadow-accent)] hover:bg-accent-hover'}"
          >
            {#if signingIn}<Loader2 size={16} class="animate-spin" /> Signing in…{:else}Continue with email <ArrowRight size={16} />{/if}
          </button>
        {/snippet}

        {#snippet ssoBlock(primary)}
          <button
            onclick={ssoLogin}
            class="flex min-h-[48px] w-full items-center justify-center gap-2.5 rounded-card px-4 text-body font-semibold transition-colors
              {primary
              ? 'bg-accent text-on-accent shadow-[var(--shadow-accent)] hover:bg-accent-hover'
              : 'border border-line bg-page text-ink hover:bg-surface-2'}"
          >
            {@render ssoLogo(ssoType, 18)}
            Sign in with {ssoName}
          </button>
        {/snippet}

        {#if ssoPrimary}
          <!--
            SSO-only. The password form is de-emphasised behind a disclosure and
            NEVER removed: `require_admin` still accepts a super_admin password,
            and that account is the only way back in when the realm is broken.
          -->
          <div class="mt-8">
            {@render ssoBlock(true)}
            <p class="mt-3 flex items-start gap-1.5 text-meta leading-relaxed text-ink-3">
              <ShieldCheck size={13} class="mt-0.5 shrink-0" />
              This console is set to single sign-on. Use your organisation account.
            </p>

            <div class="my-5 flex items-center gap-3.5 text-label font-semibold uppercase tracking-[0.06em] text-ink-3">
              <span class="h-px flex-1 bg-line"></span>Administrators<span class="h-px flex-1 bg-line"></span>
            </div>

            <button
              type="button"
              onclick={() => (pwOpen = !pwOpen)}
              aria-expanded={pwOpen}
              aria-controls="pw-disclosure"
              class="flex w-full items-center justify-center gap-1.5 rounded-card border border-line bg-surface px-4 py-2.5 text-body-sm font-medium text-ink-2 hover:bg-surface-2"
            >
              <Lock size={13} /> Sign in with a password
            </button>
            <div id="pw-disclosure" class="mt-4" hidden={!pwOpen}>
              {@render passwordForm()}
              {#if ldapEnabled}
                <p class="mt-3 flex items-center justify-center gap-1.5 rounded-card bg-surface px-4 py-2.5 text-meta text-ink-2">
                  <Building size={13} class="text-accent" /> Directory (LDAP) sign-in is enabled — use your work email above.
                </p>
              {/if}
            </div>
          </div>
        {:else}
          <div class="mt-8">
            {@render passwordForm()}
          </div>

          {#if showSso || ldapEnabled}
            <div class="my-5 flex items-center gap-3.5 text-label font-semibold uppercase tracking-[0.06em] text-ink-3">
              <span class="h-px flex-1 bg-line"></span>Or continue with<span class="h-px flex-1 bg-line"></span>
            </div>

            <div class="rounded-card border border-line bg-surface p-3">
              <div class="mb-2.5 flex items-center gap-1.5 px-1 text-label font-semibold uppercase tracking-[0.06em] text-ink-3">
                <ShieldCheck size={13} /> Enterprise sign-in
              </div>
              {#if showSso}
                {@render ssoBlock(false)}
              {/if}
              {#if ldapEnabled}
                <p class="mt-2 flex items-center justify-center gap-1.5 rounded-card bg-page px-4 py-2.5 text-meta text-ink-2">
                  <Building size={13} class="text-accent" /> Directory (LDAP) sign-in is enabled — use your work email above.
                </p>
              {/if}
            </div>
          {/if}
        {/if}

        <p class="mt-6 flex items-center gap-1.5 text-meta text-ink-3">
          <Lock size={11} /> No self-signup — accounts are created by an administrator.
        </p>
      </div>

      <div class="flex items-center gap-2.5 text-meta text-ink-3">
        {#if brand.assets.parent}
          <img
            src={brand.assets.parent}
            alt={brand.parent_name + ' logo'}
            class="h-5 w-auto flex-shrink-0 object-contain {parentChip ? 'rounded-control bg-surface p-0.5' : ''}"
          />
        {/if}
        <span>{brand.legal_footer}</span>
      </div>
    </div>

    <!--
      RIGHT · live showcase. The one surface in this console that stays dark in
      BOTH themes — it is a product demo, not a page, and agentdash's sign-in
      showcase is a fixed navy panel too. It reads only from the --c-show-*
      scale, never from --c-accent: in light mode the accent is blue-600, which
      measured 3.0:1 against this panel's own background.
    -->
    <div class="relative hidden p-8 lg:flex lg:items-center lg:justify-center">
      <div
        class="showcase-panel relative w-full max-w-[520px] overflow-hidden rounded-hero border border-show-line-2 p-6 shadow-[var(--shadow-showcase)]"
      >
        <div
          class="pointer-events-none absolute -right-16 -top-16 h-64 w-64 rounded-full bg-show-accent/40 blur-3xl"
        ></div>
        <div class="relative flex items-center gap-2 text-meta text-show-ink-3">
          <span class="relative flex h-2 w-2">
            <span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-show-success opacity-75"></span>
            <span class="relative inline-flex h-2 w-2 rounded-full bg-show-success"></span>
          </span>
          <span class="font-semibold uppercase tracking-wide text-show-success">Live</span>
          How {brand.product_name} turns a question into an answer
        </div>

        <div class="relative mt-4 rounded-card border border-show-line bg-black/25 px-4 py-3 text-body-sm text-show-ink-2">
          Read-only by design — your stock stays untouched.
        </div>
        {#key demoIdx}
          <p class="demo-q relative mt-2 pl-1 text-meta italic text-show-ink-3">
            “{demoQueries[demoIdx].en}” · “{demoQueries[demoIdx].my}”
          </p>
        {/key}

        <div class="relative mt-5 space-y-1">
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
              class="flex items-center gap-3 rounded-card border px-3.5 py-2.5 transition-all duration-300 {active
                ? 'border-show-accent bg-show-accent/15'
                : done
                  ? 'border-transparent opacity-60'
                  : 'border-transparent opacity-35'}"
            >
              <span
                class="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-control transition-colors duration-300 {active
                  ? 'bg-show-accent text-show-bg'
                  : done
                    ? 'bg-show-success/20 text-show-success'
                    : 'bg-white/5 text-show-ink-3'}"
              >
                {#if active}
                  <Loader2 size={14} class="animate-spin" />
                {:else}
                  <st.icon size={14} />
                {/if}
              </span>
              <div class="min-w-0 flex-1">
                <div class="text-body-sm font-medium text-show-ink">{st.t}</div>
                <div class="truncate text-label text-show-ink-3">{st.s}</div>
              </div>
              <span
                class="flex-shrink-0 font-mono text-label {active ? 'text-show-accent' : 'text-show-ink-3'}"
                >{st.n === 5 && answerCount ? answerCount + ' units' : st.m}</span
              >
            </div>
          {/each}
        </div>

        <div class="relative mt-5 flex flex-wrap gap-1.5">
          <!--
            py-1.5/leading-1.7 rather than the py-1 the other chips in this
            console use: the first chip is bilingual, and Noto Sans Myanmar
            stacks marks both above and below the Latin band. Measured — the
            Burmese run is 13px tall inside a 32.7px chip, so it is NOT clipped;
            the extra leading is headroom, not a fix.
          -->
          {#each ['Bilingual EN · မြန်မာ', 'Read-only guard', 'SSO / LDAP', 'Store scope', 'Substitutes'] as chip}
            <span class="rounded-full border border-show-line bg-white/5 px-2.5 py-1.5 text-label leading-[1.7] text-show-ink-2"
              >{chip}</span
            >
          {/each}
        </div>

        <div class="relative mt-5 flex items-center justify-between border-t border-show-line pt-3 text-label text-show-ink-3">
          <span><span class="font-semibold text-show-ink-2">37</span> agents · <span class="font-semibold text-show-ink-2">multi-branch</span></span>
          <span class="flex items-center gap-1.5"><span class="h-1.5 w-1.5 rounded-full bg-show-success"></span> SSO ready</span>
        </div>
      </div>
    </div>
  </div>
{:else if meLoaded && me && !me.approved}
  <!-- Authenticated, but access is held until an admin approves this account. -->
  <div class="flex min-h-screen items-center justify-center bg-page px-4">
    <div class="elev w-[440px] max-w-[94vw] rounded-panel border border-line bg-surface p-9 text-center">
      <span
        class="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-full bg-accent-soft text-accent"
      >
        <ShieldCheck size={28} />
      </span>
      <div class="page-title text-title text-ink">{brand.pending_title}</div>
      <p class="mx-auto mt-3 max-w-[340px] text-body-sm leading-relaxed text-ink-2">
        You are accessing a restricted {brand.parent_name} system. Activity is logged. Access to
        pharmacy stock data is granted only to authorised staff.
      </p>

      <div class="mt-6 rounded-card border border-line bg-page px-4 py-3.5 text-left">
        <div class="flex items-center gap-2 text-body-sm font-medium text-ink">
          <span class="relative flex h-2 w-2">
            <!-- Tokens, not `amber-400/500`: this dot sits on `page`, which
                 flips with the theme, so a raw palette class would not follow
                 it. "Waiting" is the warning tone by the honesty rules. -->
            <span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-warning opacity-70"></span>
            <span class="relative inline-flex h-2 w-2 rounded-full bg-warning"></span>
          </span>
          Awaiting administrator approval
        </div>
        <!--
          Do not claim anything was sent. There is no notification path in this
          system — no email, no webhook, no alert. The only true statements are
          that the account is pending, that a human admin must approve it on the
          Users page, and that this screen re-checks /auth/me every 5s.
        -->
        <p class="mt-1.5 text-meta text-ink-3">
          Signed in as <span class="text-ink-2">{me.email}</span>. Your account is pending — an
          administrator has to approve it on the Users page before the console opens. Nobody is
          alerted automatically, so ask an administrator directly. This screen re-checks every few
          seconds and unlocks itself the moment your access is approved — you do not need to sign
          in again.
        </p>
      </div>

      <div class="mt-6 flex items-center justify-center gap-2 text-meta text-ink-3">
        <Loader2 size={14} class="animate-spin" /> Checking for approval…
      </div>

      <button
        onclick={signOut}
        class="mt-6 inline-flex items-center gap-1.5 text-body-sm font-medium text-ink-2 hover:text-ink"
      >
        <LogOut size={14} /> Sign out
      </button>
    </div>
  </div>
{:else}
  <!--
    The shell, as the design draws it: ONE dark rail running the full height of
    the window, and a header that lives INSIDE the content column beside it.

    It used to be a full-width header above a light rail. Both parts changed
    together because they are one decision: a header that spans the window
    forces the rail to start below it, and the rail then reads as a panel under
    a toolbar rather than as the console's spine. Putting the header in the
    content column lets the rail own its own top — which is where the product's
    identity now sits — and lets the header say where you are (`Console ›
    Today`) rather than what the product is called.

    What moved, and why it is not lost:
      · the brand lockup   → the rail's head
      · who is signed in   → the rail's foot, beside the build stamp
      · search, bell, theme → stay in the header, restyled
    Nothing was deleted; every control is still one click from where it was.
  -->
  <div class="relative flex h-screen overflow-hidden bg-page">
    <!-- First focusable element on every screen, by DOM order. The default
         `href` jump is prevented and focus is moved by hand: this is a router,
         and letting the browser append `#main-content` to the URL would push a
         history entry and, on a route that reads the hash, change the page.
         `tabindex="-1"` on <main> is what makes it focusable as a target at
         all — without it the browser scrolls but focus stays on the link and
         the next Tab goes back into the rail. -->
    <a href="#main-content" class="skip-link" onclick={skipToContent}>Skip to content</a>

    {#if menuOpen}
      <!-- Pointer affordance only. A full-viewport <button> is a tab stop with
           its focus ring drawn around the window edge, and Enter on it closed
           the menu the user had just opened. Escape (onGlobalKey) is the
           keyboard route out. -->
      <div
        class="fixed inset-0 z-[39] cursor-default bg-black/40 lg:hidden"
        onclick={() => (menuOpen = false)}
        aria-hidden="true"
      ></div>
    {/if}

    <!--
      The rail is a flex COLUMN that does not scroll: the nav scrolls inside it
      and the brand head and user foot are siblings, so both are pinned and
      always visible. The build stamp used to slide out of view the moment the
      list was taller than the viewport — exactly the short window where
      somebody is looking for the version.

      Dark in BOTH themes (see --c-rail-* in app.css). The console's own
      light/dark switch moves the page beside it, never the rail.
    -->
    <div
      inert={railInert}
      class="rail fixed inset-y-0 left-0 z-40 flex w-[288px] flex-shrink-0 flex-col overflow-hidden
        bg-rail-bg text-rail-ink transition-transform duration-200
        lg:static lg:z-auto lg:w-[236px] lg:translate-x-0
        {menuOpen ? 'translate-x-0' : '-translate-x-full'}"
    >
      <!-- Rail head. The SQUARE icon only — the horizontal lockup is a sign-in
           asset and would not survive this width. Short name, not product
           name. -->
      <a
        href={base + '/'}
        onclick={() => (menuOpen = false)}
        class="flex flex-none items-center gap-2.5 px-4 pb-4 pt-[18px]"
      >
        {#if brand.assets.icon}
          <span
            class="flex h-[34px] w-[34px] flex-shrink-0 items-center justify-center rounded-card {iconChip
              ? 'bg-surface'
              : ''}"
          >
            <img src={brand.assets.icon} alt="" class="h-[26px] w-[26px] object-contain" />
          </span>
        {:else}
          <span
            class="flex h-[34px] w-[34px] flex-shrink-0 items-center justify-center rounded-card bg-accent text-on-accent"
          >
            <Pill size={19} />
          </span>
        {/if}
        <div class="min-w-0 leading-[1.1]">
          <div class="page-title text-body-sm text-rail-ink">{brand.short_name}</div>
          <div class="text-micro font-semibold uppercase tracking-[0.14em] text-rail-ink-3">
            {brand.console_subtitle}
          </div>
        </div>
      </a>

      <nav aria-label="Main" class="flex min-h-0 flex-1 flex-col gap-px overflow-y-auto px-3 pb-3">
        {#each SECTIONS as section (section.label)}
          <!-- The first group is the console's home and carries no heading: a
               one-row group under a label reads as a category with one thing in
               it rather than as the top of the rail. -->
          {#if section.label}
            <div
              class="px-2.5 pb-[5px] pt-3.5 text-micro font-semibold uppercase tracking-[0.13em] text-rail-ink-3"
            >
              {section.label}
            </div>
          {/if}
          {#each section.items as item (item.href)}
            {@const active = isActive(item.href)}
            <!--
              Active is the ACCENT here, not a neutral fill. On the old light
              rail a blue row would have competed with the page; on this one the
              accent is the only saturated thing in a column of muted lavender,
              so it reads as position rather than as an action.
            -->
            <a
              href={base + item.href}
              onclick={() => (menuOpen = false)}
              aria-current={active ? 'page' : undefined}
              class="tap flex items-center gap-2.5 rounded-card px-2.5 py-1.5 text-body-sm transition-colors
                {active
                ? 'bg-accent font-semibold text-on-accent'
                : 'text-rail-ink-2 hover:bg-rail-fill-2 hover:text-rail-ink'}"
            >
              <span class="flex h-5 w-5 flex-shrink-0 items-center justify-center">
                <item.icon size={17} strokeWidth={active ? 2 : 1.75} />
              </span>
              <span class="flex-1">{item.label}</span>
              <!-- Hidden at zero and while unknown — never a `0` badge. -->
              {#if item.href === '/users' && pendingCount}
                <span
                  class="tnum inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-warning px-1.5 text-micro font-bold text-on-accent"
                  title="{pendingCount} account{pendingCount === 1 ? '' : 's'} awaiting approval"
                >
                  {pendingCount}
                </span>
              {/if}
            </a>
          {/each}
        {/each}
      </nav>

      <!--
        Rail foot: who is signed in, then who owns the deployment and what it is
        running. The point of the build stamp is that nobody has to go looking —
        when a pharmacist reports "it did X", the version is already on screen.
      -->
      <div class="flex-none border-t border-rail-line px-4 py-3">
          <div class="flex items-center gap-2">
            {#if me}
              <span
                class="page-title flex h-[30px] w-[30px] flex-shrink-0 items-center justify-center rounded-full bg-rail-fill text-label text-rail-ink"
                aria-hidden="true">{meInitials}</span
              >
              <div class="min-w-0 leading-[1.2]">
                <div class="max-w-[118px] truncate text-meta font-medium text-rail-ink" title={meName}>
                  {meName}
                </div>
                <div class="truncate text-micro text-rail-ink-3">{meRole}</div>
              </div>
            {:else if !meLoaded}
              <!-- identity not loaded yet — skeleton, not a placeholder that reads as real -->
              <span class="skel h-8 w-8 flex-shrink-0 rounded-full"></span>
              <div class="min-w-0 leading-[1.2]">
                <span class="skel block h-3 w-[86px]"></span>
                <span class="skel mt-1.5 block h-2.5 w-[54px]"></span>
              </div>
            {:else}
              <!-- /auth/me could not be read (backend offline). Say so; do not invent. -->
              <span
                class="page-title flex h-[30px] w-[30px] flex-shrink-0 items-center justify-center rounded-full bg-rail-fill text-label text-rail-ink-3"
                aria-hidden="true">—</span
              >
              <div class="min-w-0 leading-[1.2]">
                <div class="text-meta font-semibold text-rail-ink-3">Signed in</div>
                <div class="text-micro tracking-[0.03em] text-rail-ink-3">account not loaded</div>
              </div>
            {/if}
            <button
              onclick={signOut}
              aria-label="Sign out"
              class="ml-auto flex h-7 w-7 flex-none items-center justify-center rounded-card text-rail-ink-3 hover:bg-rail-fill hover:text-rail-ink"
            >
              <LogOut size={16} />
            </button>
          </div>

        <!-- The version number is the control. Nothing beside it: the operator's
             mark moved to the header, and the build channel moved inside the
             sheet — both were noise on every screen for facts read twice a year.
             The chevron is permanent because a bare version number does not read
             as clickable, and nobody hovers a version number to find out. -->
        <button
          onclick={() => (whatsNewOpen = true)}
          aria-haspopup="dialog"
          class="-ml-[7px] mt-[11px] flex w-[calc(100%+14px)] cursor-pointer items-center gap-1
            rounded-control px-[7px] py-[5px] text-micro text-rail-ink-3
            hover:bg-rail-fill-2 hover:text-rail-ink-2"
        >
          <span class="tnum text-micro">{build ? 'v' + build.version : '—'}</span>
          <ChevronRight size={12} class="opacity-70" />
          <span class="sr-only">What's new</span>
        </button>
      </div>
    </div>

    <!-- ---------- content column: header, then the page ---------- -->
    <div inert={contentInert} class="flex min-w-0 flex-1 flex-col">
      <header
        class="relative z-50 flex h-[60px] flex-none items-center gap-3 border-b border-line bg-page px-6"
      >
        <button
          onclick={() => (menuOpen = !menuOpen)}
          aria-label="Toggle menu"
          class="-ml-2 flex h-9 w-9 flex-none items-center justify-center rounded-card text-ink-2 hover:bg-surface-2 lg:hidden"
        >
          <Menu size={22} />
        </button>

        <!-- Where you are. Two levels is the whole hierarchy — this console has
             no deeper nesting, and a crumb trail that could only ever be two
             long is a label, so it is written as one. -->
        <nav
          aria-label="Breadcrumb"
          class="flex flex-none items-center gap-2 whitespace-nowrap text-body-sm text-ink-3"
        >
          <a href={base + '/'} class="hidden hover:text-ink sm:inline">Console</a>
          <ChevronRight size={13} class="hidden flex-none sm:block" />
          <span class="font-semibold text-ink">{screenTitle}</span>
        </nav>

        <div class="relative hidden min-w-[150px] max-w-[280px] flex-[1_1_auto] md:block">
                <!-- `field-shell`: this container owns the focus ring, because
                     the input inside it is borderless. See app.css. -->
                <div
                  class="field-shell flex w-full items-center gap-2 rounded-card border border-line bg-surface px-2.5 py-[7px]
                    focus-within:border-accent"
                >
                  <Search size={15} class="flex-none text-ink-3" />
                  <input
                    bind:this={searchEl}
                    bind:value={searchQuery}
                    onfocus={() => (searchOpen = true)}
                    onkeydown={onSearchKey}
                    aria-label="Search pages"
                    placeholder="Search a product, branch or page…"
                    class="min-w-0 flex-1 border-0 bg-transparent text-body-sm text-ink outline-none placeholder:text-ink-3"
                  />
                  <span
                    class="flex-none rounded-control border border-line px-1.5 text-micro text-ink-3"
                    style="font-family:var(--font-mono)">/</span
                  >
                </div>

                {#if searchOpen && searchQuery.trim()}
                  <!-- Pointer affordance only: it paints nothing, so as a
                       <button> it was an invisible full-viewport tab stop.
                       Escape closes the popover (onGlobalKey). -->
                  <div
                    class="fixed inset-0 z-[54] cursor-default"
                    onclick={() => (searchOpen = false)}
                    aria-hidden="true"
                  ></div>
                  <div
                    class="absolute left-0 top-[46px] z-[55] w-[320px] overflow-hidden rounded-card border border-line bg-surface"
                    style="box-shadow:var(--shadow-pop)"
                  >
                    {#if searchResults.length === 0}
                      <div class="p-4 text-center text-meta text-ink-3">
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
                            <div class="text-body-sm font-semibold text-ink">{r.label}</div>
                            <div class="text-label text-ink-3">{r.section}</div>
                          </div>
                        </button>
                      {/each}
                    {/if}
                  </div>
                {/if}
              </div>

        <div class="ml-auto flex flex-none items-center gap-2">
          <!--
            Stock freshness, on every screen, because every number in this
            console is only as current as the last file. It is drawn ONLY when
            the timestamp is known: a pill reading "just now" over a stock file
            that never loaded is worse than no pill, and "unknown" is not a
            time. See /admin/data-freshness — one query, no aggregates, because
            this renders on every navigation.
          -->
          {#if stockFreshness}
            <a
              href={base + '/ftp'}
              title="Last stock file: {stockFreshness.exact}"
              class="hidden flex-none items-center gap-2 whitespace-nowrap rounded-full border border-accent-2 bg-accent-soft py-[5px] pl-2.5 pr-3 text-meta text-accent hover:border-accent sm:flex"
            >
              <span class="h-[7px] w-[7px] flex-none rounded-full bg-accent"></span>
              <span><b class="font-semibold">Stock data</b> {stockFreshness.label}</span>
            </a>
          {/if}

          <!-- The header's one call to action. It is not drawn on the chat
               itself, where it links to the page you are already reading. -->
          {#if !fullBleed}
            <a
              href={base + '/chat'}
              class="flex flex-none items-center gap-[7px] whitespace-nowrap rounded-card bg-accent px-3.5 py-2 text-body-sm font-semibold text-on-accent hover:bg-accent-hover"
            >
              <MessageCircle size={15} /> <span class="hidden sm:inline">Ask the agent</span>
            </a>
          {/if}

        <button
          onclick={toggleTheme}
          aria-label="Toggle theme"
          class="flex h-9 w-9 items-center justify-center rounded-card text-ink-3 hover:bg-surface-2 hover:text-ink"
        >
          {#if dark}<Sun size={18} />{:else}<Moon size={18} />{/if}
        </button>

        <!-- Who operates this deployment. It used to be squeezed into the rail
             foot beside the version, at 16px, where it read as a label on the
             build stamp rather than as a mark. Here it is separated from the
             controls by a rule so it reads as ownership, not as another button.

             The artwork is ink-on-white with the company name set inside it, so
             it keeps a light chip in dark mode (this is what the existing
             `dark_logo_mode: "chip"` setting already does for it) and is given
             enough height that the name resolves as words rather than noise. -->
        {#if brand.assets.parent}
          <span class="ml-1 flex flex-none items-center border-l border-line pl-3">
            <img
              src={brand.assets.parent}
              alt={brand.parent_name + ' logo'}
              class="block h-7 w-auto rounded-xs bg-white px-1.5 py-0.5 dark:h-[34px]"
            />
          </span>
        {:else if brand.parent_name}
          <span class="ml-1 flex-none border-l border-line pl-3 text-meta text-ink-3"
            >{brand.parent_name}</span
          >
        {/if}
        </div>
      </header>

      {#if fullBleed}
        <main id="main-content" tabindex="-1" class="min-h-0 min-w-0 flex-1 overflow-hidden">
          {@render children()}
        </main>
      {:else}
        <main id="main-content" tabindex="-1" class="min-h-0 min-w-0 flex-1 overflow-y-auto px-5 py-6 sm:px-7 xl:px-9">
          <!-- Content cap. Was max-w-5xl (1024px), which on a 1500px+ window left
               ~400-500px of empty gutter while 7-column tables (analytics, data,
               ftp) squeezed and scrolled inside their own boxes. 1680px keeps
               ultra-wide monitors from stretching a table to an unreadable span
               while removing the gap on every normal screen. Prose stays capped
               where it is written (PageHeader subtitles use max-w-xl), because
               line length is a readability rule and page width is not. -->
          <div class="mx-auto max-w-[1680px]">
            {@render children()}
          </div>
        </main>
      {/if}
    </div>
  </div>

  <ToastHost />
  <WhatsNew bind:open={whatsNewOpen} {build} />
{/if}
