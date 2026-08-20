<script>
  import { dialog } from '$lib/aurora/dialog.js';
  // Authentication — four tabs over the sign-in surface:
  //
  //   Methods   what can sign in at all, plus the truth about who gets an account
  //   SSO       the OIDC/Keycloak client, in the order you fill it in
  //   LDAP      the directory bind, in the order you fill it in
  //   Security  lockout / session / signing key / cookies
  //
  // Rules that run through the whole file and are easy to break:
  //
  //  1. A SECRET IS NEVER RENDERED. The backend masks `oidc_client_secret` and
  //     `ldap_bind_password` to "" and sends a `_set` bool instead. A blank field
  //     on save means "keep the stored one" — so the field must stay blank after
  //     a save, which is why save() reloads.
  //  2. LOCAL ACCOUNTS HAVE NO TOGGLE. It is the break-glass path: if SSO or LDAP
  //     is misconfigured, the local admin password is the only way back in. A
  //     sibling project shipped a toggle here and locked everyone out of its own
  //     console. There is deliberately no control for it on this page.
  //  3. A MISSING ENDPOINT IS NOT A CRASH. `/admin/auth-overview` and
  //     `/admin/security-config` ship after this console may be deployed. A 404
  //     on either says so in that one section; every other section keeps working.
  //  4. UNKNOWN IS NOT ZERO. A count the backend did not send renders "—" or
  //     "not recorded", never a fabricated 0.
  import { onMount, tick } from 'svelte';
  import { page } from '$app/stores';
  import { base as appBase } from '$app/paths';
  import { goto } from '$app/navigation';
  import TabStrip from '$lib/TabStrip.svelte';
  import { API_BASE } from '$lib/apiBase.js';
  import PageHeader from '$lib/PageHeader.svelte';
  import ErrorState from '$lib/ErrorState.svelte';
  import { getJSON } from '$lib/api.js';
  import { toast } from '$lib/aurora/toast.js';
  import {
    ShieldCheck,
    KeyRound,
    Server,
    Lock,
    Users,
    Info,
    Save,
    PlugZap,
    Loader2,
    CircleCheck,
    CircleAlert,
    TriangleAlert,
    Copy,
    Check,
    ArrowRight,
    ExternalLink,
    Timer,
    FileKey,
    Cookie,
    ScrollText,
    RefreshCw,
    X,
    Pencil,
    Settings2,
    UserPlus
  } from '@lucide/svelte';

  const BASE = API_BASE;

  // ------------------------------------------------------------------ tabs
  const TABS = [
    { id: 'methods', label: 'Methods', icon: ShieldCheck },
    { id: 'sso', label: 'SSO (OIDC)', icon: KeyRound },
    { id: 'ldap', label: 'LDAP / AD', icon: Server },
    { id: 'security', label: 'Security', icon: Lock }
  ];
  const TAB_IDS = TABS.map((t) => t.id);

  // The tab lives in the URL so it is linkable and survives a refresh. This is
  // now a PANEL inside /settings, whose own tab bar owns `?tab=`; the inner tab
  // therefore rides `?sub=`. Two tab bars sharing one parameter would fight:
  // picking `sub=ldap` would read back as an unknown outer tab and bounce the
  // page to Behaviour.
  let tab = $derived.by(() => {
    const t = $page.url.searchParams.get('sub');
    return TAB_IDS.includes(t) ? t : 'methods';
  });

  function setTab(id) {
    const u = new URL($page.url);
    u.searchParams.set('sub', id);
    goto(u.pathname + u.search, { replaceState: true, noScroll: true, keepFocus: true });
  }


  // ------------------------------------------------------- the auth config
  // Mirrors AUTH_KEYS in app/auth.py exactly. Listed here rather than taken
  // from the response so a key the backend stops sending still round-trips as
  // an empty value instead of silently vanishing from the PUT.
  const BOOL_KEYS = new Set([
    'ldap_enabled',
    'ldap_use_ssl',
    'ldap_start_tls',
    'ldap_validate_cert',
    'ldap_auto_create',
    'oidc_enabled',
    'oidc_auto_create'
  ]);
  const AUTH_FIELDS = [
    'signin_mode',
    'ldap_enabled',
    'ldap_auto_create',
    'ldap_host',
    'ldap_port',
    'ldap_use_ssl',
    'ldap_start_tls',
    'ldap_validate_cert',
    'ldap_ca_cert_file',
    'ldap_bind_dn',
    'ldap_bind_password',
    'ldap_base_dn',
    'ldap_user_filter',
    'ldap_email_attr',
    'ldap_name_attr',
    'oidc_enabled',
    'oidc_auto_create',
    'oidc_provider_type',
    'oidc_provider_name',
    'oidc_discovery_url',
    'oidc_client_id',
    'oidc_client_secret',
    'oidc_redirect_uri',
    'oidc_scopes'
  ];

  let loading = $state(true);
  let error = $state(null);
  let saving = $state(false);
  let saveMsg = $state('');

  let f = $state({});
  // whether a stored secret exists (the server never sends the value back)
  let secretSet = $state({ oidc_client_secret_set: false, ldap_bind_password_set: false });

  // Snapshot taken at load; the unsaved-changes indicator compares against it.
  // Key order is normalised so a reordered response is not read as an edit.
  function sig(o) {
    return JSON.stringify(
      Object.keys(o)
        .sort()
        .map((k) => [k, o[k] ?? ''])
    );
  }
  let cfgSnap = $state(null);

  async function load() {
    loading = true;
    error = null;
    try {
      // getJSON, not a bare fetch: the status has to survive so an expired
      // token is reported as an expired token.
      const d = await getJSON('/admin/auth-config');
      secretSet = {
        oidc_client_secret_set: !!d.oidc_client_secret_set,
        ldap_bind_password_set: !!d.ldap_bind_password_set
      };
      const model = {};
      for (const k of AUTH_FIELDS) {
        if (k in d && d[k] !== null && d[k] !== undefined) model[k] = d[k];
        else model[k] = BOOL_KEYS.has(k) ? false : k === 'ldap_port' ? 389 : '';
      }
      // Three keys this console writes may be newer than the backend serving it.
      // A key the server does not know is IGNORED by the PUT whitelist, so the
      // page still works — but it must not pretend it stored something. Track
      // support, and DERIVE the mode from what is actually on rather than
      // defaulting to a value that would misdescribe the running system.
      modeSupported = 'signin_mode' in d;
      typeSupported = 'oidc_provider_type' in d;
      autoCreateSupported = 'oidc_auto_create' in d || 'ldap_auto_create' in d;
      if (!MODE_IDS.includes(model.signin_mode))
        model.signin_mode = model.oidc_enabled ? 'hybrid' : 'local';
      if (!PROVIDER_IDS.includes(model.oidc_provider_type)) model.oidc_provider_type = 'keycloak';
      f = model;
      cfgSnap = sig(model);
    } catch (e) {
      error = e;
    } finally {
      loading = false;
    }
  }

  // ------------------------------------------------- overview (may be absent)
  // GET /admin/auth-overview — counts for the Methods cards. 'absent' = 404,
  // i.e. this backend predates the endpoint; the cards still render from the
  // form itself, only the counts go missing.
  let ov = $state(null);
  let ovState = $state('loading'); // loading | ok | absent | error

  async function loadOverview() {
    ovState = 'loading';
    try {
      const res = await fetch(`${BASE}/admin/auth-overview`);
      if (res.status === 404) {
        ov = null;
        ovState = 'absent';
        return;
      }
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      ov = await res.json();
      ovState = 'ok';
    } catch {
      ov = null;
      ovState = 'error';
    }
  }

  // ------------------------------------------------- security (may be absent)
  // GET/PUT /admin/security-config. Only `lockout` is writable; everything else
  // is env-owned and shown read-only, because a control that cannot change the
  // thing it names is worse than no control (see the catalog_mode landmine).
  let sec = $state(null);
  let secState = $state('loading'); // loading | ok | absent | error
  let lock = $state({ max_fail: '', lock_minutes: '', ip_max_fail: '' });
  let lockSnap = $state(null);

  async function loadSecurity() {
    secState = 'loading';
    try {
      const res = await fetch(`${BASE}/admin/security-config`);
      if (res.status === 404) {
        sec = null;
        lockSnap = null;
        secState = 'absent';
        return;
      }
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      const d = await res.json();
      sec = d;
      const l = d.lockout || {};
      lock = {
        max_fail: Number.isFinite(l.max_fail) ? l.max_fail : '',
        lock_minutes: Number.isFinite(l.lock_minutes) ? l.lock_minutes : '',
        ip_max_fail: Number.isFinite(l.ip_max_fail) ? l.ip_max_fail : ''
      };
      lockSnap = sig(lock);
      secState = 'ok';
    } catch {
      sec = null;
      lockSnap = null;
      secState = 'error';
    }
  }

  // ------------------------------------------------------------ dirty + save
  let cfgDirty = $derived(cfgSnap !== null && sig(f) !== cfgSnap);
  let lockDirty = $derived(lockSnap !== null && sig(lock) !== lockSnap);
  let dirty = $derived(cfgDirty || lockDirty);

  // One Save for the page: the auth config (Methods toggles + SSO + LDAP) and,
  // when the security endpoint exists and its numbers changed, the lockout
  // block. A 404 from the security PUT is reported without failing the rest.
  async function save() {
    saving = true;
    saveMsg = '';
    const notes = [];
    // Only re-read the security block when its write actually landed — a reload
    // after a failed PUT would overwrite the operator's numbers with the server's
    // and quietly throw away the edit they were trying to save.
    let lockSaved = false;
    try {
      const res = await fetch(`${BASE}/admin/auth-config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(f)
      });
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      notes.push('Sign-in settings saved — applies on the next login');

      if (lockDirty) {
        const r2 = await fetch(`${BASE}/admin/security-config`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            lockout: {
              max_fail: Number(lock.max_fail),
              lock_minutes: Number(lock.lock_minutes),
              ip_max_fail: Number(lock.ip_max_fail)
            }
          })
        });
        if (r2.status === 404) {
          notes.push('sign-in protection was not saved: this backend is older than the console');
        } else if (!r2.ok) {
          const d2 = await r2.json().catch(() => ({}));
          notes.push(`sign-in protection was not saved: ${d2.detail || `request failed (${r2.status})`}`);
        } else {
          notes.push('sign-in protection saved');
          lockSaved = true;
        }
      }

      saveMsg = notes.join(' · ');
      toast(notes[0]);
      await load(); // re-mask secrets, refresh the *_set chips and the snapshot
      await loadOverview();
      if (lockSaved) await loadSecurity();
    } catch (e) {
      saveMsg = e.message || 'could not save';
      toast(saveMsg, 'alert-triangle');
    } finally {
      saving = false;
    }
  }

  // ------------------------------------------------------- test connections
  // Two independent probes. Each result is one of:
  //   null            nothing run yet
  //   {state:'busy'}  in flight
  //   {state:'ok'|'fail', detail, ms, issuer, endpoints}
  //   {state:'absent'} the backend predates these endpoints (404)
  // The probe tests the SAVED configuration — the backend reads its own stored
  // values — so an unsaved edit is not what is being tested. Say that on screen
  // rather than letting someone chase a result for a config that isn't live.
  let ldapTest = $state(null);
  let oidcTest = $state(null);

  async function runTest(which) {
    const set = (v) => (which === 'ldap' ? (ldapTest = v) : (oidcTest = v));
    set({ state: 'busy' });
    try {
      const res = await fetch(`${BASE}/admin/auth-config/test-${which}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}'
      });
      if (res.status === 404) {
        set({ state: 'absent' });
        return;
      }
      const d = await res.json().catch(() => ({}));
      if (!res.ok && !('ok' in d)) throw new Error(d.detail || `request failed (${res.status})`);
      set({
        state: d.ok ? 'ok' : 'fail',
        // never invent a reason; if the backend sent none, say so
        detail: d.detail || 'no detail reported',
        ms: Number.isFinite(d.ms) ? d.ms : null,
        issuer: d.issuer ?? null,
        endpoints: d.endpoints ?? null
      });
    } catch (e) {
      set({ state: 'fail', detail: e.message || 'could not reach the backend', ms: null });
    }
  }

  // The four endpoints the discovery probe reports back, in the order they are
  // used during a sign-in. jwks is optional for a confidential client.
  const OIDC_ENDPOINTS = [
    { key: 'authorization_endpoint', label: 'authorize' },
    { key: 'token_endpoint', label: 'token' },
    { key: 'userinfo_endpoint', label: 'userinfo' },
    { key: 'jwks_uri', label: 'jwks' }
  ];

  // ------------------------------------------------------ redirect-URI copy
  // A byte mismatch against the Keycloak client is the most common setup
  // failure (docs/SSO.md), so the exact stored value is shown verbatim and can
  // be copied without retyping.
  let copied = $state(false);
  async function copyRedirect() {
    const v = f.oidc_redirect_uri || '';
    if (!v) return;
    try {
      await navigator.clipboard.writeText(v);
      copied = true;
      setTimeout(() => (copied = false), 1600);
    } catch {
      toast('could not copy — select the text instead', 'alert-triangle');
    }
  }

  // ------------------------------------------------------------- LDAP: TLS
  // One segmented control instead of two checkboxes that could both be ticked.
  // The old pair allowed LDAPS + StartTLS together, which is not a thing: you
  // either connect over TLS or upgrade a plain connection, never both.
  const ENCRYPTIONS = [
    { id: 'none', label: 'None', port: 389 },
    { id: 'starttls', label: 'StartTLS', port: 389 },
    { id: 'ldaps', label: 'LDAPS', port: 636 }
  ];
  let encryption = $derived(f.ldap_use_ssl ? 'ldaps' : f.ldap_start_tls ? 'starttls' : 'none');

  function setEncryption(id) {
    f.ldap_use_ssl = id === 'ldaps';
    f.ldap_start_tls = id === 'starttls';
    const e = ENCRYPTIONS.find((x) => x.id === id);
    if (e) f.ldap_port = e.port;
  }

  const FILTER_PRESETS = [
    { id: 'ad', label: 'AD', value: '(sAMAccountName={username})' },
    { id: 'openldap', label: 'OpenLDAP', value: '(uid={username})' }
  ];

  // ------------------------------------------------------------ sign-in mode
  // Three mutually exclusive shapes for the login screen. `sso_only` is NOT a
  // lockout: the backend still accepts a super_admin password, deliberately, so
  // a broken realm cannot shut everyone out of their own console.
  const MODES = [
    { id: 'local', label: 'Local only', note: 'Email + password only' },
    { id: 'hybrid', label: 'Hybrid', note: 'Local and SSO' },
    { id: 'sso_only', label: 'SSO only', note: 'Enforce SSO sign-in' }
  ];
  const MODE_IDS = MODES.map((m) => m.id);
  let modeSupported = $state(true);
  let typeSupported = $state(true);
  let autoCreateSupported = $state(true);

  // Changing the mode is meaningless unless the SSO switch follows it, so it
  // does — visibly, in the rows below, rather than as a hidden side effect.
  function setMode(id) {
    f.signin_mode = id;
    if (id === 'local') f.oidc_enabled = false;
    else if (oidcConfigured) f.oidc_enabled = true;
  }

  // ------------------------------------------------------- provider catalogue
  //
  // READ THIS BEFORE ADDING A ROW. The backend has ONE OIDC configuration slot
  // (`oidc_discovery_url` + `oidc_client_id` + `oidc_client_secret`), not four.
  // These rows are a catalogue of provider *types*: picking one sets
  // `oidc_provider_type`, which drives the logo and the default button label,
  // and then fills that single slot. Exactly one can be On. Rendering this as
  // four independent providers would be a lie the first operator discovers by
  // losing their Keycloak connection to a Google row they were "just trying".
  const PROVIDERS = [
    {
      id: 'keycloak',
      name: 'Keycloak',
      label: 'Keycloak',
      scopes: 'openid email profile',
      issuerHint: 'https://keycloak.example.com/realms/citcare/.well-known/openid-configuration'
    },
    {
      id: 'oidc',
      name: 'Generic OIDC',
      label: 'Single sign-on',
      scopes: 'openid email profile',
      issuerHint: 'https://idp.example.com/.well-known/openid-configuration'
    },
    {
      id: 'google',
      name: 'Google',
      label: 'Google',
      scopes: 'openid email profile',
      issuerHint: 'https://accounts.google.com/.well-known/openid-configuration'
    },
    {
      id: 'microsoft',
      name: 'Microsoft Entra ID',
      label: 'Microsoft',
      scopes: 'openid email profile',
      issuerHint:
        'https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration'
    }
  ];
  const PROVIDER_IDS = PROVIDERS.map((p) => p.id);
  function providerOf(id) {
    return PROVIDERS.find((p) => p.id === id) || PROVIDERS[1];
  }

  // The type currently occupying the single slot.
  let activeType = $derived(PROVIDER_IDS.includes(f.oidc_provider_type) ? f.oidc_provider_type : 'keycloak');

  // A row's status. Only the active type can be configured or on; every other
  // row is genuinely "not configured", because there is nothing stored for it.
  function rowStatus(id) {
    if (id !== activeType || !oidcConfigured)
      return { mark: '⚠', text: 'Not configured', cls: 'text-ink-3' };
    if (f.oidc_enabled) return { mark: '●', text: 'On', cls: 'text-success' };
    return { mark: '○', text: 'Off', cls: 'text-ink-3' };
  }

  // ------------------------------------------------------------------- modal
  // A local dialog rather than $lib/aurora/Modal.svelte: that component is a
  // fixed 430px confirm box with no focus trap, no focus restore and no window
  // Escape handler — right for "are you sure", wrong for a five-field form that
  // must return focus to the row that opened it. This follows the analytics
  // drawer idiom instead (trap + restore + Escape + backdrop).
  let modal = $state(null); // { kind:'oidc'|'ldap', replacing:boolean }
  let lastFocus = null;
  let dr = $state({}); // the draft being edited; nothing touches `f` until Save
  let confirmReplace = $state(null); // { id } — the type a row toggle wants to switch to

  function draftFromOidc(typeId, replacing) {
    const p = providerOf(typeId);
    if (!replacing) {
      return {
        provider_type: typeId,
        provider_name: f.oidc_provider_name || p.label,
        discovery_url: f.oidc_discovery_url || '',
        client_id: f.oidc_client_id || '',
        client_secret: '',
        redirect_uri: f.oidc_redirect_uri || '',
        scopes: f.oidc_scopes || p.scopes,
        enabled: !!f.oidc_enabled,
        auto_create: !!f.oidc_auto_create
      };
    }
    // Replacing: the connection belongs to the OLD provider, so none of it is
    // carried over. The redirect URI is the exception — it is this app's own
    // callback and does not change when the realm does.
    return {
      provider_type: typeId,
      provider_name: p.label,
      discovery_url: '',
      client_id: '',
      client_secret: '',
      redirect_uri: f.oidc_redirect_uri || '',
      scopes: p.scopes,
      enabled: true,
      auto_create: !!f.oidc_auto_create
    };
  }

  function draftFromLdap() {
    return {
      host: f.ldap_host || '',
      port: f.ldap_port || 389,
      use_ssl: !!f.ldap_use_ssl,
      start_tls: !!f.ldap_start_tls,
      validate_cert: !!f.ldap_validate_cert,
      ca_cert_file: f.ldap_ca_cert_file || '',
      bind_dn: f.ldap_bind_dn || '',
      bind_password: '',
      base_dn: f.ldap_base_dn || '',
      user_filter: f.ldap_user_filter || '',
      email_attr: f.ldap_email_attr || '',
      name_attr: f.ldap_name_attr || '',
      enabled: !!f.ldap_enabled,
      auto_create: !!f.ldap_auto_create
    };
  }

  async function openProvider(typeId, ev, replacing = false) {
    lastFocus = ev?.currentTarget ?? (typeof document !== 'undefined' ? document.activeElement : null);
    dr = draftFromOidc(typeId, replacing);
    modal = { kind: 'oidc', replacing };
  }

  async function openLdap(ev) {
    lastFocus = ev?.currentTarget ?? (typeof document !== 'undefined' ? document.activeElement : null);
    dr = draftFromLdap();
    modal = { kind: 'ldap', replacing: false };
  }

  function closeModal() {
    modal = null;
  }

  function cancelReplace() {
    confirmReplace = null;
  }

  // Escape, the Tab trap and focus restoration all used to be hand-rolled
  // here. The trap filtered candidates with `offsetParent !== null`, which is
  // null for any fixed-position element — it happened to work only because
  // these two dialogs are `position: relative`. `use:dialog` owns all of it
  // now, and uses getClientRects so it holds for a fixed drawer too.
  function onWindowKey(e) {
    if (e.key !== 'Escape') return;
    if (modal) closeModal();
    else if (confirmReplace) cancelReplace();
  }

  // Replacing the provider does NOT clear the stored client secret — a blank
  // field means "keep the saved one", which after a replace would mean keeping
  // the OLD realm's secret against the NEW realm's client id. So the secret is
  // required on a replace, and only on a replace.
  let secretRequired = $derived(!!modal?.replacing);
  let canSaveProvider = $derived(!(secretRequired && !(dr.client_secret || '').trim()));

  async function saveProvider() {
    if (modal?.kind === 'oidc') {
      if (!canSaveProvider) return;
      f.oidc_provider_type = dr.provider_type;
      f.oidc_provider_name = dr.provider_name;
      f.oidc_discovery_url = dr.discovery_url;
      f.oidc_client_id = dr.client_id;
      f.oidc_client_secret = dr.client_secret; // blank == keep stored (server-side)
      f.oidc_redirect_uri = dr.redirect_uri;
      f.oidc_scopes = dr.scopes;
      f.oidc_enabled = !!dr.enabled;
      f.oidc_auto_create = !!dr.auto_create;
      if (f.oidc_enabled && f.signin_mode === 'local') f.signin_mode = 'hybrid';
      if (!f.oidc_enabled && f.signin_mode !== 'local') f.signin_mode = 'local';
    } else if (modal?.kind === 'ldap') {
      f.ldap_host = dr.host;
      f.ldap_port = dr.port;
      f.ldap_use_ssl = !!dr.use_ssl;
      f.ldap_start_tls = !!dr.start_tls;
      f.ldap_validate_cert = !!dr.validate_cert;
      f.ldap_ca_cert_file = dr.ca_cert_file;
      f.ldap_bind_dn = dr.bind_dn;
      f.ldap_bind_password = dr.bind_password; // blank == keep stored
      f.ldap_base_dn = dr.base_dn;
      f.ldap_user_filter = dr.user_filter;
      f.ldap_email_attr = dr.email_attr;
      f.ldap_name_attr = dr.name_attr;
      f.ldap_enabled = !!dr.enabled;
      f.ldap_auto_create = !!dr.auto_create;
    }
    closeModal();
    await save();
  }

  // The row toggle. Turning ON a row that is not the configured one moves the
  // single slot to that type — which destroys the current connection, so it
  // asks first and then opens the form. It never overwrites silently.
  function toggleRow(id, want, ev) {
    if (id === activeType) {
      if (want && !oidcConfigured) {
        openProvider(id, ev, false);
        return;
      }
      f.oidc_enabled = want;
      if (want && f.signin_mode === 'local') f.signin_mode = 'hybrid';
      if (!want) f.signin_mode = 'local';
      return;
    }
    if (!want) return; // an already-off row cannot be turned further off
    if (oidcConfigured) {
      lastFocus = ev?.currentTarget ?? null;
      confirmReplace = { id };
    } else {
      openProvider(id, ev, false);
    }
  }

  function acceptReplace() {
    const id = confirmReplace?.id;
    // Keep the ORIGINAL trigger (the row's toggle) as the focus-restore target.
    // The Continue button is about to be removed from the DOM, so restoring to
    // it would drop focus to <body> when the modal closes.
    const prev = lastFocus;
    confirmReplace = null;
    if (!id) return;
    openProvider(id, null, true); // sets lastFocus synchronously…
    lastFocus = prev; // …so this correction lands before any await resumes
  }

  // The modal's own encryption control edits the draft, not the live config.
  let drEncryption = $derived(dr.use_ssl ? 'ldaps' : dr.start_tls ? 'starttls' : 'none');
  function setDrEncryption(id) {
    dr.use_ssl = id === 'ldaps';
    dr.start_tls = id === 'starttls';
    const e = ENCRYPTIONS.find((x) => x.id === id);
    if (e) dr.port = e.port;
  }

  let drCopied = $state(false);
  async function copyDraftRedirect() {
    const v = dr.redirect_uri || '';
    if (!v) return;
    try {
      await navigator.clipboard.writeText(v);
      drCopied = true;
      setTimeout(() => (drCopied = false), 1600);
    } catch {
      toast('could not copy — select the text instead', 'alert-triangle');
    }
  }

  // ---------------------------------------------------------- method status
  // "Configured" is derived from the form, not from the server, so the cards
  // are honest about an edit that has not been saved yet. A stored secret
  // counts as configured even though its field is blank.
  let oidcConfigured = $derived(
    !!(
      (f.oidc_discovery_url || '').trim() &&
      (f.oidc_client_id || '').trim() &&
      (f.oidc_redirect_uri || '').trim() &&
      ((f.oidc_client_secret || '').trim() || secretSet.oidc_client_secret_set)
    )
  );
  let ldapConfigured = $derived(
    !!((f.ldap_host || '').trim() && (f.ldap_base_dn || '').trim() && (f.ldap_user_filter || '').trim())
  );

  function statusOf(enabled, configured) {
    if (enabled && configured) return { mark: '●', text: 'Enabled', cls: 'text-success' };
    if (enabled && !configured) return { mark: '⚠', text: 'On — not configured', cls: 'text-warning' };
    return { mark: '○', text: 'Disabled', cls: 'text-ink-3' };
  }

  // Local accounts have no toggle, so their status is a constant, not a lookup.
  const localSt = { mark: '●', text: 'Always on', cls: 'text-success' };
  let ssoSt = $derived(statusOf(f.oidc_enabled, oidcConfigured));
  let ldapSt = $derived(statusOf(f.ldap_enabled, ldapConfigured));

  // A count the backend did not send is unknown, never 0.
  function count(v) {
    return Number.isFinite(v) ? String(v) : '—';
  }

  const inputCls =
    'w-full rounded-panel border border-line bg-page px-3 py-2 text-body-sm text-ink outline-none placeholder:text-ink-3 focus:border-accent';
  const checkCls = 'h-4 w-4 accent-[var(--c-accent)]';
  const btnCls =
    'inline-flex items-center gap-2 rounded-panel border border-line px-3 py-1.5 text-body-sm font-medium text-ink hover:bg-surface-2 disabled:opacity-60';

  onMount(() => {
    load();
    loadOverview();
    loadSecurity();
  });
</script>

<svelte:window onkeydown={onWindowKey} />

<PageHeader
  level={2}
  title="Authentication"
  subtitle="Who can sign in, and how. Sign-in settings apply on the next login — no restart."
>
  {#snippet actions()}
    <button
      type="button"
      onclick={() => {
        load();
        loadOverview();
        loadSecurity();
      }}
      class={btnCls}
    >
      <RefreshCw size={15} /> Reload
    </button>
  {/snippet}
</PageHeader>

<!-- tabs -->
<TabStrip tabs={TABS} value={tab} onchange={setTab} gap="gap-x-5" label="Authentication sections" />

{#if loading}
  <p class="mt-5 text-body-sm text-ink-2">Loading…</p>
{:else if error}
  <div class="mt-5">
    <ErrorState {error} retry={load} what="the authentication settings" />
  </div>
{:else}
  <!--
    One result renderer for both probes. aria-live so a screen reader hears the
    outcome; the region is always in the DOM so the announcement is not missed.
    A secret is never part of what the backend returns and is never rendered.
  -->
  {#snippet testResult(r, label)}
    <div class="mt-2.5 min-h-[20px] text-meta" role="status" aria-live="polite">
      {#if !r}
        <span class="text-ink-3">Not tested yet.</span>
      {:else if r.state === 'busy'}
        <span class="inline-flex items-center gap-1.5 text-ink-2">
          <Loader2 size={13} class="animate-spin" /> Testing {label}…
        </span>
      {:else if r.state === 'absent'}
        <span class="inline-flex items-center gap-1.5 text-warning">
          <CircleAlert size={13} /> This backend has no test endpoint yet — update the server to test
          {label} from here.
        </span>
      {:else if r.state === 'ok'}
        <div class="inline-flex flex-wrap items-center gap-1.5 text-success">
          <CircleCheck size={13} />
          <span>{r.detail}</span>
          {#if r.ms != null}<span class="tnum text-ink-3">· {r.ms}ms</span>{/if}
        </div>
        {#if r.issuer}
          <div class="mt-1 break-all font-mono text-label text-ink-3">issuer: {r.issuer}</div>
        {/if}
        {#if r.endpoints}
          <div class="mt-2 flex flex-wrap gap-1.5">
            {#each OIDC_ENDPOINTS as ep (ep.key)}
              {@const got = !!r.endpoints[ep.key]}
              <span
                title={r.endpoints[ep.key] || 'not published'}
                class="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-label font-medium
                  {got ? 'border-line bg-success-soft text-success' : 'border-line bg-surface-2 text-ink-3'}"
              >
                {#if got}<Check size={11} />{:else}<CircleAlert size={11} />{/if}
                {ep.label}
              </span>
            {/each}
          </div>
        {/if}
      {:else}
        <div class="inline-flex flex-wrap items-center gap-1.5 text-danger">
          <CircleAlert size={13} />
          <span>{r.detail}</span>
          {#if r.ms != null}<span class="tnum text-ink-3">· {r.ms}ms</span>{/if}
        </div>
      {/if}
    </div>
  {/snippet}

  <!--
    Provider logos. Inline SVG on purpose: this SPA is served by the app itself
    and has to render with no network, so a remote brand asset is not an option.
    Brand hexes are literal here because they ARE the brand — they are not part
    of the theme and must not follow it. The swatch around them is themed.
  -->
  {#snippet logo(id, size)}
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
      <KeyRound size={size} class="text-ink-2" />
    {/if}
  {/snippet}

  {#snippet sectionHead(n, title, note)}
    <div class="mb-3 flex flex-wrap items-baseline gap-2">
      <span
        class="inline-flex h-5 w-5 items-center justify-center rounded-full bg-accent-soft text-label font-semibold text-accent tnum"
        >{n}</span
      >
      <h4 class="text-body-sm font-semibold text-ink">{title}</h4>
      {#if note}<span class="text-meta text-ink-3">{note}</span>{/if}
    </div>
  {/snippet}

  <!-- ================================================================ METHODS -->
  {#if tab === 'methods'}
    <div id="panel-methods" role="tabpanel" aria-labelledby="tab-methods" tabindex="-1" class="mt-5">
      <div class="grid gap-3 lg:grid-cols-3">
        <!-- Local accounts — deliberately no toggle -->
        <div class="rounded-panel border border-line bg-surface p-5">
          <div class="flex items-center gap-2">
            <Users size={16} class="text-ink-2" />
            <h3 class="text-body font-semibold text-ink">Local accounts</h3>
          </div>
          <p class="mt-2 text-body-sm leading-relaxed text-ink-2">
            Email and password held by this app, created on the Users page.
          </p>
          <p class="mt-3 text-body-sm font-medium {localSt.cls}">{localSt.mark} {localSt.text}</p>
          <p class="mt-2 text-meta leading-relaxed text-ink-3">
            There is no switch for this on purpose. It is the break-glass path — if SSO or the
            directory is misconfigured, a local admin password is the only way back into this
            console.
          </p>
          <dl class="mt-3 grid grid-cols-3 gap-2 border-t border-line pt-3 text-meta">
            <div>
              <dt class="text-ink-3">Users</dt>
              <dd class="tnum text-body-sm font-semibold text-ink">{count(ov?.local?.users)}</dd>
            </div>
            <div>
              <dt class="text-ink-3">Pending</dt>
              <dd class="tnum text-body-sm font-semibold text-ink">{count(ov?.local?.pending)}</dd>
            </div>
            <div>
              <dt class="text-ink-3">Super admins</dt>
              <dd class="tnum text-body-sm font-semibold text-ink">{count(ov?.local?.super_admins)}</dd>
            </div>
          </dl>
        </div>

        <!-- SSO -->
        <div class="rounded-panel border border-line bg-surface p-5">
          <div class="flex items-center gap-2">
            {@render logo(activeType, 16)}
            <h3 class="text-body font-semibold text-ink">SSO (OIDC)</h3>
          </div>
          <p class="mt-2 text-body-sm leading-relaxed text-ink-2">
            A “Sign in with {f.oidc_provider_name || providerOf(activeType).label}” button on the
            login screen.
          </p>
          <p class="mt-3 text-body-sm font-medium {ssoSt.cls}">{ssoSt.mark} {ssoSt.text}</p>
          {#if ov?.oidc?.issuer}
            <p class="mt-1 break-all font-mono text-label text-ink-3">{ov.oidc.issuer}</p>
          {/if}
          <label class="mt-3 flex items-center gap-2.5 text-body-sm text-ink">
            <input type="checkbox" bind:checked={f.oidc_enabled} class={checkCls} />
            Enable SSO sign-in
          </label>
          <button
            type="button"
            onclick={() => setTab('sso')}
            class="mt-3 inline-flex items-center gap-1.5 text-body-sm font-medium text-accent hover:underline"
          >
            Configure <ArrowRight size={13} />
          </button>
        </div>

        <!-- LDAP -->
        <div class="rounded-panel border border-line bg-surface p-5">
          <div class="flex items-center gap-2">
            <Server size={16} class="text-ink-2" />
            <h3 class="text-body font-semibold text-ink">LDAP / Active Directory</h3>
          </div>
          <p class="mt-2 text-body-sm leading-relaxed text-ink-2">
            Directory passwords accepted in the normal password box, tried when local login fails.
          </p>
          <p class="mt-3 text-body-sm font-medium {ldapSt.cls}">{ldapSt.mark} {ldapSt.text}</p>
          {#if f.ldap_host}
            <p class="mt-1 break-all font-mono text-label text-ink-3">
              {f.ldap_host}:{f.ldap_port || '—'} · {encryption}
            </p>
          {/if}
          <label class="mt-3 flex items-center gap-2.5 text-body-sm text-ink">
            <input type="checkbox" bind:checked={f.ldap_enabled} class={checkCls} />
            Enable directory sign-in
          </label>
          <button
            type="button"
            onclick={() => setTab('ldap')}
            class="mt-3 inline-flex items-center gap-1.5 text-body-sm font-medium text-accent hover:underline"
          >
            Configure <ArrowRight size={13} />
          </button>
        </div>
      </div>

      {#if ovState === 'absent'}
        <p class="mt-3 flex items-start gap-2 text-meta text-ink-3">
          <CircleAlert size={14} class="mt-0.5 shrink-0" />
          Account counts are unavailable: this backend is older than the console and has no
          <span class="font-mono">/admin/auth-overview</span>. Everything else on this page works.
        </p>
      {:else if ovState === 'error'}
        <p class="mt-3 flex items-start gap-2 text-meta text-ink-3">
          <CircleAlert size={14} class="mt-0.5 shrink-0" />
          Account counts could not be read just now — shown as “—” rather than guessed.
        </p>
      {/if}

      <div class="mt-3 flex items-start gap-2 rounded-panel bg-info-soft p-3 text-body-sm text-info">
        <Info size={15} class="mt-0.5 shrink-0" />
        <span>
          Fronting Active Directory with Keycloak gives you AD logins <b>and</b> MFA, and leaves no
          LDAP service account on this server to protect. Prefer it to pointing LDAP straight at a
          domain controller.
        </span>
      </div>

      <!-- Who gets in — read-only, and it replaces controls that would do nothing -->
      <section class="mt-4 rounded-panel border border-line bg-surface p-5">
        <div class="mb-3 flex items-center gap-2">
          <ShieldCheck size={16} class="text-ink-2" />
          <h3 class="text-body font-semibold text-ink">Who gets in</h3>
          <span class="rounded-full border border-line bg-surface-2 px-2 py-0.5 text-label font-medium text-ink-3"
            >Read-only</span
          >
        </div>
        <ul class="space-y-2.5 text-body-sm leading-relaxed text-ink-2">
          <li class="flex gap-2">
            <Check size={14} class="mt-1 shrink-0 text-accent" />
            <span
              ><b class="text-ink">There is no self-signup.</b> Nobody can create an account from the
              login screen, with any method.</span
            >
          </li>
          <li class="flex gap-2">
            <Check size={14} class="mt-1 shrink-0 text-accent" />
            <span
              >The identity provider proves <b class="text-ink">who</b> someone is. The Users page
              decides <b class="text-ink">what</b> they may do.</span
            >
          </li>
          <li class="flex gap-2">
            <Check size={14} class="mt-1 shrink-0 text-accent" />
            <span
              >An email that signs in successfully at the IdP but has <b class="text-ink">no row</b> on
              the Users page is <b class="text-ink">refused</b>. Nothing is auto-created — create the
              user first, with the same email the directory reports.</span
            >
          </li>
          <li class="flex gap-2">
            <Check size={14} class="mt-1 shrink-0 text-accent" />
            <span
              >New accounts start <b class="text-ink">pending</b> and stay on the hold screen until an
              admin approves them.</span
            >
          </li>
          <li class="flex gap-2">
            <Check size={14} class="mt-1 shrink-0 text-accent" />
            <span
              ><b class="text-ink">Roles never come from the IdP.</b> They live in this app's users
              table, so a realm administrator cannot mint an admin here.</span
            >
          </li>
        </ul>
        <p class="mt-3 border-t border-line pt-3 text-meta leading-relaxed text-ink-3">
          This is why there is no “default role for new SSO users”, no “first user becomes admin” and
          no “merge accounts by email” switch on this page: this backend never creates a user from a
          sign-in, so those settings would control nothing.
          {#if ov && ov.self_signup === true}
            <span class="text-warning"
              >The backend reports self-signup is ON, which contradicts the above — check the server.</span
            >
          {/if}
        </p>
        <a
          href={appBase + '/users'}
          class="mt-3 inline-flex items-center gap-1.5 text-body-sm font-medium text-accent hover:underline"
        >
          Open the Users page <ArrowRight size={13} />
        </a>
      </section>
    </div>
  {/if}

  <!-- ==================================================================== SSO -->
  {#if tab === 'sso'}
    <div id="panel-sso" role="tabpanel" aria-labelledby="tab-sso" tabindex="-1" class="mt-5 space-y-4">
      <!-- 1 · sign-in mode ------------------------------------------------- -->
      <section class="rounded-panel border border-line bg-surface p-5">
        <fieldset role="radiogroup" aria-labelledby="mode-legend" class="min-w-0">
          <legend id="mode-legend" class="mb-1 text-body font-semibold text-ink">Sign-in mode</legend>
          <p class="mb-3 text-meta text-ink-2">What the login screen offers.</p>
          <div class="grid gap-2.5 sm:grid-cols-3">
            {#each MODES as m (m.id)}
              {@const on = f.signin_mode === m.id}
              <label
                for={'mode-' + m.id}
                class="flex cursor-pointer items-start gap-2.5 rounded-card border p-3.5 transition-colors
                  {on ? 'border-accent bg-accent-soft' : 'border-line bg-surface hover:bg-surface-2'}"
              >
                <input
                  id={'mode-' + m.id}
                  type="radio"
                  name="signin_mode"
                  value={m.id}
                  checked={on}
                  onchange={() => setMode(m.id)}
                  class="{checkCls} mt-0.5 rounded-full"
                />
                <span class="min-w-0">
                  <span class="block text-body-sm font-semibold {on ? 'text-accent' : 'text-ink'}">{m.label}</span>
                  <span class="mt-0.5 block text-meta leading-relaxed text-ink-2">{m.note}</span>
                </span>
              </label>
            {/each}
          </div>
        </fieldset>

        <p class="mt-3 flex items-start gap-2 rounded-panel bg-info-soft p-3 text-meta leading-relaxed text-info">
          <Info size={14} class="mt-0.5 shrink-0" />
          <span>
            <b>SSO only still accepts a super_admin password.</b> The password box is de-emphasised on
            the login screen but never removed — if the realm breaks, that account is the only way
            back into this console.
          </span>
        </p>

        {#if f.signin_mode !== 'local' && !oidcConfigured}
          <p class="mt-2.5 flex items-start gap-2 rounded-panel bg-warning-soft p-3 text-meta text-warning">
            <TriangleAlert size={14} class="mt-0.5 shrink-0" />
            <span>No provider is configured yet, so this mode changes nothing on the login screen. Configure one below first.</span>
          </p>
        {/if}
        {#if !modeSupported}
          <p class="mt-2.5 flex items-start gap-2 text-meta text-ink-3">
            <CircleAlert size={13} class="mt-0.5 shrink-0" />
            This backend did not send <span class="font-mono">signin_mode</span>, so it is shown
            derived from the SSO switch. Saving it here has no effect until the server supports the
            field.
          </p>
        {/if}
      </section>

      <!-- 2 · provider catalogue ------------------------------------------- -->
      <section class="rounded-panel border border-line bg-surface">
        <div class="flex flex-wrap items-start gap-2 border-b border-line px-5 py-4">
          <div class="min-w-0 flex-1">
            <h3 class="text-body font-semibold text-ink">Identity providers</h3>
            <!--
              This caption is load-bearing honesty, not decoration. There is ONE
              OIDC slot in the backend; these rows choose which provider type
              fills it. Delete the caption and the page starts implying four
              independent connections that cannot exist.
            -->
            <p class="mt-1 text-meta leading-relaxed text-ink-2">
              One provider is active at a time. Choosing a provider sets its logo and default button
              label, then configures this app's single OIDC connection — enabling a different one
              replaces it.
            </p>
          </div>
        </div>

        <ul class="divide-y divide-line">
          {#each PROVIDERS as p (p.id)}
            {@const st = rowStatus(p.id)}
            {@const isActive = p.id === activeType && oidcConfigured}
            <li class="flex flex-wrap items-center gap-3 px-5 py-3.5">
              <span
                class="flex h-9 w-9 shrink-0 items-center justify-center rounded-card border border-line bg-surface-2"
              >
                {@render logo(p.id, 20)}
              </span>

              <div class="min-w-0 flex-1">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-body-sm font-semibold text-ink">{p.name}</span>
                  <span
                    class="rounded-full border border-line bg-surface-2 px-1.5 py-0.5 text-micro font-semibold uppercase tracking-wide text-ink-3"
                    >OIDC</span
                  >
                  {#if isActive && f.oidc_provider_name}
                    <span class="text-label text-ink-3">button: “{f.oidc_provider_name}”</span>
                  {/if}
                </div>
                {#if isActive}
                  <p class="mt-0.5 break-all font-mono text-label text-ink-3">
                    {f.oidc_discovery_url || '—'}
                  </p>
                {:else}
                  <p class="mt-0.5 text-label text-ink-3">Not the configured provider.</p>
                {/if}
              </div>

              <span class="w-[132px] shrink-0 text-meta font-medium {st.cls}">{st.mark} {st.text}</span>

              <button
                type="button"
                onclick={(e) => openProvider(p.id, e, p.id !== activeType && oidcConfigured)}
                class="inline-flex shrink-0 items-center gap-1.5 text-meta font-medium text-accent hover:underline"
              >
                {#if isActive}<Pencil size={13} /> Edit{:else}<Settings2 size={13} /> Configure{/if}
                <span class="sr-only">{p.name}</span>
              </button>

              <label class="flex shrink-0 items-center gap-2 text-meta text-ink-2">
                <input
                  type="checkbox"
                  role="switch"
                  checked={p.id === activeType && !!f.oidc_enabled}
                  onchange={(e) => {
                    const want = e.currentTarget.checked;
                    e.currentTarget.checked = p.id === activeType && !!f.oidc_enabled;
                    toggleRow(p.id, want, e);
                  }}
                  aria-label={'Enable ' + p.name}
                  class={checkCls}
                />
                Enable
              </label>
            </li>
          {/each}
        </ul>

        <div class="border-t border-line px-5 py-3 text-meta text-ink-3">
          <p>Secrets are stored server-side and never returned to this page.</p>
          <p class="mt-1">
            Discovery and JWKS lookup are always on, and PKCE and group sync are not implemented in
            this backend — so there are no switches for them here. A switch that controlled nothing
            would be worse than its absence.
          </p>
          {#if !typeSupported}
            <p class="mt-1 flex items-start gap-1.5">
              <CircleAlert size={13} class="mt-0.5 shrink-0" />
              This backend did not send <span class="font-mono">oidc_provider_type</span>; the row
              shown as configured is this console's default (Keycloak) until the server stores a type.
            </p>
          {/if}
        </div>
      </section>

      <!-- 3 · probe (also available from inside the modal) ------------------ -->
      <section class="rounded-panel border border-line bg-surface p-5">
        <div class="flex flex-wrap items-center gap-3">
          <button type="button" onclick={() => runTest('oidc')} disabled={oidcTest?.state === 'busy'} class={btnCls}>
            <PlugZap size={15} /> Test connection
          </button>
          <span class="text-label text-ink-3">Tests the saved settings — save first.</span>
        </div>
        {@render testResult(oidcTest, providerOf(activeType).name)}
      </section>
    </div>
  {/if}

  <!-- =================================================================== LDAP -->
  {#if tab === 'ldap'}
    <div id="panel-ldap" role="tabpanel" aria-labelledby="tab-ldap" tabindex="-1" class="mt-5 space-y-4">
      <section class="rounded-panel border border-line bg-surface">
        <div class="border-b border-line px-5 py-4">
          <h3 class="text-body font-semibold text-ink">Directory</h3>
          <p class="mt-1 text-meta leading-relaxed text-ink-2">
            One directory. Directory passwords are accepted in the normal password box and tried when
            local login fails — there is no separate button on the login screen.
          </p>
        </div>

        <ul class="divide-y divide-line">
          <li class="flex flex-wrap items-center gap-3 px-5 py-3.5">
            <span
              class="flex h-9 w-9 shrink-0 items-center justify-center rounded-card border border-line bg-surface-2 text-ink-2"
            >
              <Server size={18} />
            </span>

            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-body-sm font-semibold text-ink">LDAP / Active Directory</span>
                <span
                  class="rounded-full border border-line bg-surface-2 px-1.5 py-0.5 text-micro font-semibold uppercase tracking-wide text-ink-3"
                  >LDAP</span
                >
              </div>
              {#if ldapConfigured}
                <p class="mt-0.5 break-all font-mono text-label text-ink-3">
                  {f.ldap_host}:{f.ldap_port || '—'} · {encryption} · {f.ldap_base_dn || '—'}
                </p>
              {:else}
                <p class="mt-0.5 text-label text-ink-3">
                  Needs a host, a base DN and a user filter.
                </p>
              {/if}
            </div>

            <span class="w-[132px] shrink-0 text-meta font-medium {ldapSt.cls}">{ldapSt.mark} {ldapSt.text}</span>

            <button
              type="button"
              onclick={(e) => openLdap(e)}
              class="inline-flex shrink-0 items-center gap-1.5 text-meta font-medium text-accent hover:underline"
            >
              {#if ldapConfigured}<Pencil size={13} /> Edit{:else}<Settings2 size={13} /> Configure{/if}
              <span class="sr-only">the directory</span>
            </button>

            <label class="flex shrink-0 items-center gap-2 text-meta text-ink-2">
              <input
                type="checkbox"
                role="switch"
                bind:checked={f.ldap_enabled}
                aria-label="Enable directory sign-in"
                class={checkCls}
              />
              Enable
            </label>
          </li>
        </ul>

        <div class="border-t border-line px-5 py-3 text-meta text-ink-3">
          <p>Secrets are stored server-side and never returned to this page.</p>
          <p class="mt-1">
            Prefer fronting Active Directory with Keycloak: you get AD logins <b>and</b> MFA, and no
            LDAP service-account password is left on this server to protect.
          </p>
        </div>
      </section>

      <section class="rounded-panel border border-line bg-surface p-5">
        <div class="flex flex-wrap items-center gap-3">
          <button type="button" onclick={() => runTest('ldap')} disabled={ldapTest?.state === 'busy'} class={btnCls}>
            <PlugZap size={15} /> Test connection
          </button>
          <span class="text-label text-ink-3">
            Binds with the service account only — no user password is sent. Tests the saved settings.
          </span>
        </div>
        {@render testResult(ldapTest, 'LDAP')}
      </section>
    </div>
  {/if}

  <!-- =============================================================== SECURITY -->
  {#if tab === 'security'}
    <div
      id="panel-security"
      role="tabpanel"
      aria-labelledby="tab-security"
      tabindex="-1"
      class="mt-5 space-y-4"
    >
      {#if secState === 'loading'}
        <p class="text-body-sm text-ink-2">Loading…</p>
      {:else if secState === 'absent'}
        <div class="rounded-panel border border-line bg-surface p-5 text-body-sm text-ink-2">
          <p class="flex items-center gap-2 font-medium text-ink">
            <CircleAlert size={15} /> This backend is older than the console
          </p>
          <p class="mt-1.5">
            It has no <span class="font-mono">/admin/security-config</span>, so lockout, session,
            signing key and cookie settings cannot be shown. The Methods, SSO and LDAP tabs are
            unaffected.
          </p>
        </div>
      {:else if secState === 'error'}
        <div class="rounded-panel border border-line bg-surface p-5 text-body-sm text-ink-2">
          <p class="font-medium text-ink">Could not read the security settings</p>
          <button onclick={loadSecurity} class="mt-3 {btnCls}">Retry</button>
        </div>
      {:else}
        <!-- Sign-in protection — the only editable block -->
        <section class="rounded-panel border border-line bg-surface p-5">
          <div class="mb-3 flex flex-wrap items-center gap-2">
            <ShieldCheck size={16} class="text-ink-2" />
            <h3 class="text-body font-semibold text-ink">Sign-in protection</h3>
            <span class="rounded-full bg-accent-soft px-2 py-0.5 text-label font-medium text-accent"
              >Editable</span
            >
          </div>
          <div class="grid gap-3.5 sm:grid-cols-3">
            <label class="block">
              <span class="mb-1 block text-meta text-ink-3">Failed sign-ins before lockout</span>
              <input type="number" min="1" bind:value={lock.max_fail} class="{inputCls} tnum" />
            </label>
            <label class="block">
              <span class="mb-1 block text-meta text-ink-3">Lockout length (minutes)</span>
              <input type="number" min="1" bind:value={lock.lock_minutes} class="{inputCls} tnum" />
            </label>
            <label class="block">
              <span class="mb-1 block text-meta text-ink-3">Failed sign-ins per IP before block</span>
              <input type="number" min="1" bind:value={lock.ip_max_fail} class="{inputCls} tnum" />
            </label>
          </div>
          <p class="mt-3 text-meta leading-relaxed text-ink-3">
            The per-account limit stops password guessing against one person; the per-IP limit stops
            one source spraying many accounts. Both are counted by the backend, not the browser.
          </p>
          <div class="mt-3 flex flex-wrap items-center gap-2 border-t border-line pt-3">
            <ScrollText size={14} class="text-ink-3" />
            <a
              href={appBase + '/analytics?tab=activity&sec=audit&source=auth'}
              class="text-body-sm font-medium text-accent hover:underline"
            >
              Security log
            </a>
            <span class="text-meta text-ink-2">
              {#if Number.isFinite(sec?.events_24h)}
                <span class="tnum font-semibold text-ink">{sec.events_24h}</span> event{sec.events_24h ===
                1
                  ? ''
                  : 's'} in the last 24 hours
              {:else}
                event count not recorded
              {/if}
            </span>
          </div>
        </section>

        <!-- Everything below is env-owned -->
        <p class="flex items-start gap-2 rounded-panel bg-info-soft p-3 text-meta text-info">
          <Info size={14} class="mt-0.5 shrink-0" />
          <span>
            The three sections below are read-only. They come from the server's environment, not from
            this console — changing them means editing the environment and <b>restarting</b> the app.
            <span class="ml-1"
              >Environment: <b class="font-mono">{sec?.app_env || 'not recorded'}</b></span
            >
          </span>
        </p>

        <!-- Session -->
        <section class="rounded-panel border border-line bg-surface p-5">
          <div class="mb-3 flex items-center gap-2">
            <Timer size={16} class="text-ink-2" />
            <h3 class="text-body font-semibold text-ink">Session</h3>
            <span class="rounded-full border border-line bg-surface-2 px-2 py-0.5 text-label font-medium text-ink-3"
              >Env-owned</span
            >
          </div>
          <dl class="grid gap-3 sm:grid-cols-2">
            <div class="rounded-card border border-line bg-surface-2 p-3">
              <dt class="text-meta text-ink-3">Token lifetime</dt>
              <dd
                class="tnum mt-0.5 text-heading font-semibold {sec?.session?.exceeds_recommended
                  ? 'text-warning'
                  : 'text-ink'}"
              >
                {Number.isFinite(sec?.session?.token_ttl_hours) ? sec.session.token_ttl_hours : '—'}
                <span class="text-body-sm font-medium text-ink-3">hours</span>
              </dd>
            </div>
            <div class="rounded-card border border-line bg-surface-2 p-3">
              <dt class="text-meta text-ink-3">Recommended</dt>
              <dd class="tnum mt-0.5 text-heading font-semibold text-ink">
                {Number.isFinite(sec?.session?.token_ttl_default) ? sec.session.token_ttl_default : '—'}
                <span class="text-body-sm font-medium text-ink-3">hours</span>
              </dd>
            </div>
          </dl>
          {#if sec?.session?.exceeds_recommended}
            <p class="mt-3 flex items-start gap-2 rounded-panel bg-warning-soft p-3 text-meta text-warning">
              <TriangleAlert size={14} class="mt-0.5 shrink-0" />
              <span>
                A token issued here stays valid far longer than recommended. A stolen token is usable
                for that whole window — revoking the account does not shorten it.
              </span>
            </p>
          {/if}
        </section>

        <!-- Signing key -->
        <section
          class="rounded-panel border bg-surface p-5 {sec?.secret?.is_default
            ? 'border-danger'
            : 'border-line'}"
        >
          <div class="mb-3 flex items-center gap-2">
            <FileKey size={16} class="text-ink-2" />
            <h3 class="text-body font-semibold text-ink">Signing key</h3>
            <span class="rounded-full border border-line bg-surface-2 px-2 py-0.5 text-label font-medium text-ink-3"
              >Env-owned</span
            >
          </div>
          <dl class="grid gap-3 sm:grid-cols-3">
            <div class="rounded-card border border-line bg-surface-2 p-3">
              <dt class="text-meta text-ink-3">Set</dt>
              <dd class="mt-0.5 text-body-sm font-semibold text-ink">
                {#if sec?.secret?.is_set === true}Yes{:else if sec?.secret?.is_set === false}No{:else}—{/if}
              </dd>
            </div>
            <div class="rounded-card border border-line bg-surface-2 p-3">
              <dt class="text-meta text-ink-3">Length</dt>
              <dd class="tnum mt-0.5 text-body-sm font-semibold text-ink">
                {Number.isFinite(sec?.secret?.length) ? sec.secret.length : '—'}
                <span class="text-meta font-medium text-ink-3">chars</span>
              </dd>
            </div>
            <div class="rounded-card border border-line bg-surface-2 p-3">
              <dt class="text-meta text-ink-3">Still the default</dt>
              <dd
                class="mt-0.5 text-body-sm font-semibold {sec?.secret?.is_default
                  ? 'text-danger'
                  : 'text-ink'}"
              >
                {#if sec?.secret?.is_default === true}Yes{:else if sec?.secret?.is_default === false}No{:else}—{/if}
              </dd>
            </div>
          </dl>
          {#if sec?.secret?.is_default}
            <p class="mt-3 flex items-start gap-2 rounded-panel bg-danger-soft p-3 text-meta text-danger">
              <CircleAlert size={14} class="mt-0.5 shrink-0" />
              <span>
                <b>This is the shipped default key.</b> Anyone who has seen the source can forge an
                admin session, an embed token and a widget signature. Set a real 32-byte
                <span class="font-mono">SECRET_KEY</span> before this is reachable from anywhere but
                localhost.
              </span>
            </p>
          {/if}
          <p class="mt-3 text-meta leading-relaxed text-ink-3">
            One key signs all four: admin sessions, embed tokens, the widget HMAC and the SSO state
            nonce. <b class="text-ink-2">Rotating it signs everyone out</b> and invalidates every
            issued embed token — reissue the embed snippets afterwards.
          </p>
        </section>

        <!-- Cookies -->
        <section class="rounded-panel border border-line bg-surface p-5">
          <div class="mb-3 flex items-center gap-2">
            <Cookie size={16} class="text-ink-2" />
            <h3 class="text-body font-semibold text-ink">Cookies</h3>
            <span class="rounded-full border border-line bg-surface-2 px-2 py-0.5 text-label font-medium text-ink-3"
              >Env-owned</span
            >
          </div>
          <dl class="grid gap-3 sm:grid-cols-2">
            <div class="rounded-card border border-line bg-surface-2 p-3">
              <dt class="text-meta text-ink-3">Secure flag</dt>
              <dd
                class="mt-0.5 text-body-sm font-semibold {sec?.cookies?.warn ? 'text-warning' : 'text-ink'}"
              >
                {#if sec?.cookies?.cookie_secure === true}On{:else if sec?.cookies?.cookie_secure === false}Off{:else}—{/if}
              </dd>
            </div>
            <div class="rounded-card border border-line bg-surface-2 p-3">
              <dt class="text-meta text-ink-3">SSO in use</dt>
              <dd class="mt-0.5 text-body-sm font-semibold text-ink">
                {#if sec?.cookies?.oidc_enabled === true}Yes{:else if sec?.cookies?.oidc_enabled === false}No{:else}—{/if}
              </dd>
            </div>
          </dl>
          {#if sec?.cookies?.warn}
            <p class="mt-3 flex items-start gap-2 rounded-panel bg-warning-soft p-3 text-meta text-warning">
              <TriangleAlert size={14} class="mt-0.5 shrink-0" />
              <span>
                The SSO state cookie is being set without the <span class="font-mono">Secure</span>
                flag, so it can travel over plain HTTP. Behind TLS, turn it on.
              </span>
            </p>
          {/if}
        </section>
      {/if}
    </div>
  {/if}

  <!-- ========================================================= REPLACE CONFIRM -->
  <!--
    A confirm step, never a silent overwrite. There is one OIDC slot: turning on
    a different provider type discards the connection currently in it. The
    operator is told what is about to be lost, by name, before anything moves.
  -->
  {#if confirmReplace}
    <div class="fixed inset-0 z-[90] flex items-center justify-center p-4">
      <!-- Pointer affordance only; Escape is the keyboard route out and the
           confirm's use:dialog owns it. -->
      <div class="absolute inset-0 cursor-default bg-black/50" onclick={cancelReplace} aria-hidden="true"></div>
      <div
        use:dialog={{ onclose: cancelReplace, returnTo: () => lastFocus }}
        role="dialog"
        aria-modal="true"
        aria-labelledby="replace-title"
        tabindex="-1"
        class="elev relative w-[460px] max-w-full rounded-panel border border-line bg-surface p-5"
      >
        <h2 id="replace-title" class="flex items-center gap-2 text-body font-semibold text-ink">
          <TriangleAlert size={16} class="text-warning" />
          Replace the configured provider?
        </h2>
        <p class="mt-2.5 text-body-sm leading-relaxed text-ink-2">
          This app has one SSO connection. Configuring
          <b class="text-ink">{providerOf(confirmReplace.id).name}</b> replaces the
          <b class="text-ink">{providerOf(activeType).name}</b> connection currently in use —
          its issuer, client ID and stored secret stop being used the moment you save.
        </p>
        <p class="mt-2 text-meta leading-relaxed text-ink-3">
          Nothing changes until you save in the next step. Anyone signing in through
          {providerOf(activeType).name} will be signing in through
          {providerOf(confirmReplace.id).name} afterwards.
        </p>
        <div class="mt-5 flex flex-wrap justify-end gap-2">
          <button type="button" onclick={cancelReplace} class={btnCls}>Cancel</button>
          <button
            type="button"
            onclick={acceptReplace}
            class="inline-flex items-center gap-2 rounded-panel bg-accent px-4 py-1.5 text-body-sm font-semibold text-on-accent hover:bg-accent-hover"
          >
            Continue <ArrowRight size={14} />
          </button>
        </div>
      </div>
    </div>
  {/if}

  <!-- ================================================================= MODAL -->
  {#if modal}
    <div class="fixed inset-0 z-[80] flex items-start justify-center overflow-y-auto p-4 sm:p-8">
      <!-- Pointer affordance only; Escape is the keyboard route out and the
           modal's use:dialog owns it. -->
      <div class="fixed inset-0 cursor-default bg-black/45" onclick={closeModal} aria-hidden="true"></div>
      <div
        use:dialog={{ onclose: closeModal, returnTo: () => lastFocus }}
        role="dialog"
        aria-modal="true"
        aria-labelledby="provider-modal-title"
        tabindex="-1"
        class="relative w-[720px] max-w-full rounded-hero border border-line bg-surface shadow-[var(--shadow-pop)]"
      >
        <!-- header -->
        <div class="flex items-center gap-3 border-b border-line px-5 py-4">
          <span class="flex h-9 w-9 shrink-0 items-center justify-center rounded-card border border-line bg-surface-2 text-ink-2">
            {#if modal.kind === 'oidc'}{@render logo(dr.provider_type, 20)}{:else}<Server size={18} />{/if}
          </span>
          <div class="min-w-0 flex-1">
            <h2 id="provider-modal-title" class="truncate text-body font-semibold text-ink">
              {#if modal.kind === 'oidc'}{providerOf(dr.provider_type).name}{:else}LDAP / Active Directory{/if}
            </h2>
            <p class="text-label text-ink-3">
              {#if modal.kind === 'oidc'}
                {modal.replacing ? 'Replaces the current SSO connection' : 'The single SSO connection'}
              {:else}
                The directory this app binds to
              {/if}
            </p>
          </div>
          <button
            type="button"
            onclick={closeModal}
            aria-label="Close"
            class="flex h-8 w-8 items-center justify-center rounded-panel text-ink-3 hover:bg-surface-2 hover:text-ink"
          >
            <X size={18} />
          </button>
        </div>

        <div class="max-h-[calc(100vh-220px)] overflow-y-auto px-5 py-4">
          {#if modal.kind === 'oidc'}
            {#if modal.replacing}
              <p class="mb-4 flex items-start gap-2 rounded-panel bg-warning-soft p-3 text-meta leading-relaxed text-warning">
                <TriangleAlert size={14} class="mt-0.5 shrink-0" />
                <span>
                  Saving replaces the <b>{providerOf(activeType).name}</b> connection. The stored
                  client secret belongs to that provider, and a blank secret means “keep the stored
                  one” — so a new secret is <b>required</b> here, or the old realm's secret would be
                  sent to the new one.
                </span>
              </p>
            {/if}

            <!-- CONNECTION -->
            <h3 class="mb-2.5 text-micro font-bold uppercase tracking-[0.08em] text-ink-3">Connection</h3>
            <div class="grid gap-3.5 sm:grid-cols-2">
              <label class="block sm:col-span-2">
                <span class="mb-1 block text-meta text-ink-3">Issuer / discovery URL (.well-known/openid-configuration)</span>
                <input bind:value={dr.discovery_url} placeholder={providerOf(dr.provider_type).issuerHint} class="{inputCls} font-mono text-meta" />
              </label>
              <label class="block">
                <span class="mb-1 block text-meta text-ink-3">Client ID</span>
                <input bind:value={dr.client_id} placeholder="pharmacy-agent" class={inputCls} />
              </label>
              <label class="block">
                <span class="mb-1 block text-meta text-ink-3">
                  Client secret
                  {#if secretSet.oidc_client_secret_set && !modal.replacing}<span class="text-success">· stored</span>{/if}
                  {#if secretRequired}<span class="text-warning">· required</span>{/if}
                </span>
                <input
                  bind:value={dr.client_secret}
                  type="password"
                  placeholder={secretSet.oidc_client_secret_set && !modal.replacing
                    ? '•••••••• (leave blank to keep the saved secret)'
                    : 'client secret'}
                  class="{inputCls} font-mono text-body-sm"
                />
              </label>
              <div class="sm:col-span-2">
                <span class="mb-1 block text-meta text-ink-3">Redirect URI (read-only)</span>
                <div class="flex items-start gap-2">
                  <code class="min-w-0 flex-1 break-all rounded-panel border border-line bg-page px-2.5 py-2 font-mono text-meta text-ink">
                    {dr.redirect_uri || 'not set'}
                  </code>
                  <button
                    type="button"
                    onclick={copyDraftRedirect}
                    disabled={!dr.redirect_uri}
                    aria-label="Copy redirect URI"
                    title="Copy redirect URI"
                    class="flex h-9 w-9 shrink-0 items-center justify-center rounded-panel border border-line text-ink-2 hover:bg-surface-2 disabled:opacity-40"
                  >
                    {#if drCopied}<Check size={14} class="text-success" />{:else}<Copy size={14} />{/if}
                  </button>
                </div>
                <p class="mt-1.5 text-label leading-relaxed text-ink-3">
                  Register this at the provider <b>exactly</b> as shown. One differing character — a
                  trailing slash, http vs https, a port — fails the login with
                  <span class="font-mono">Invalid redirect_uri</span> at the provider, before this app
                  is reached. It is this app's own callback, so it does not change with the provider.
                </p>
              </div>
              <label class="block sm:col-span-2">
                <span class="mb-1 block text-meta text-ink-3">Scopes</span>
                <input bind:value={dr.scopes} placeholder="openid email profile" class={inputCls} />
              </label>
            </div>

            <p class="mt-3 flex items-start gap-2 rounded-panel bg-warning-soft p-3 text-meta leading-relaxed text-warning">
              <TriangleAlert size={14} class="mt-0.5 shrink-0" />
              <span>
                Use a <b>confidential</b> client (client authentication ON). This app trusts the token
                because it redeemed the code over TLS with a secret; a public client has no secret and
                breaks that reasoning.
              </span>
            </p>

            <!-- SIGN-IN BUTTON -->
            <h3 class="mb-2.5 mt-6 text-micro font-bold uppercase tracking-[0.08em] text-ink-3">Sign-in button</h3>
            <div class="grid gap-3.5 sm:grid-cols-2">
              <label class="block">
                <span class="mb-1 block text-meta text-ink-3">Display name</span>
                <input bind:value={dr.provider_name} placeholder={providerOf(dr.provider_type).label} class={inputCls} />
              </label>
              <label class="block">
                <span class="mb-1 block text-meta text-ink-3">Logo</span>
                <span class="flex items-center gap-2">
                  <span class="flex h-9 w-9 shrink-0 items-center justify-center rounded-card border border-line bg-surface-2">
                    {@render logo(dr.provider_type, 20)}
                  </span>
                  <select bind:value={dr.provider_type} class={inputCls}>
                    {#each PROVIDERS as p (p.id)}
                      <option value={p.id}>{p.name}</option>
                    {/each}
                  </select>
                </span>
              </label>
            </div>
            <p class="mt-2 text-label leading-relaxed text-ink-3">
              The login screen shows this logo and name on the button. Changing the logo also changes
              which provider type this connection is recorded as — it does not change the issuer above.
            </p>

            <div class="mt-3 rounded-card border border-line bg-page p-3">
              <div class="mb-2 text-label font-medium uppercase tracking-wide text-ink-3">Preview</div>
              <span class="inline-flex items-center gap-2 rounded-card border border-line bg-surface px-4 py-2.5 text-body-sm font-medium text-ink">
                {@render logo(dr.provider_type, 16)}
                Sign in with {dr.provider_name || providerOf(dr.provider_type).label}
              </span>
            </div>

            <!-- BEHAVIOUR -->
            <h3 class="mb-2.5 mt-6 text-micro font-bold uppercase tracking-[0.08em] text-ink-3">Behaviour</h3>
            <label class="flex items-start gap-2.5 text-body-sm text-ink">
              <input type="checkbox" bind:checked={dr.enabled} class="{checkCls} mt-0.5" />
              <span>Enabled<span class="mt-0.5 block text-meta text-ink-2">Show this provider's button on the login screen.</span></span>
            </label>
            <label class="mt-3 flex items-start gap-2.5 text-body-sm text-ink">
              <input type="checkbox" bind:checked={dr.auto_create} class="{checkCls} mt-0.5" />
              <span>
                <span class="inline-flex items-center gap-1.5">
                  <UserPlus size={14} class="text-ink-3" /> Let this provider create accounts
                </span>
                <span class="mt-1 block text-meta leading-relaxed text-ink-2">
                  On: an email that signs in successfully but has no account here gets one created in
                  <b class="text-ink">pending</b> state — the person is held on the approval screen and
                  an administrator must approve them on the Users page before they can see anything.
                  Off: an unknown email is <b class="text-ink">refused</b>, and the account has to be
                  created on the Users page first.
                </span>
                <span class="mt-1 block text-meta leading-relaxed text-ink-3">
                  Roles never come from the provider either way — a realm administrator cannot mint an
                  admin here.
                </span>
              </span>
            </label>
            {#if !autoCreateSupported}
              <p class="mt-2 flex items-start gap-1.5 text-meta text-ink-3">
                <CircleAlert size={13} class="mt-0.5 shrink-0" />
                This backend did not send this field, so it may be ignored on save — with an older
                server, an unknown email is refused regardless.
              </p>
            {/if}
          {:else}
            <!-- =============================== LDAP =============================== -->
            <h3 class="mb-2.5 text-micro font-bold uppercase tracking-[0.08em] text-ink-3">Connection</h3>
            <div class="grid gap-3.5 sm:grid-cols-2">
              <label class="block">
                <span class="mb-1 block text-meta text-ink-3">Server URL (host)</span>
                <input bind:value={dr.host} placeholder="ldap.corp.com" class={inputCls} />
              </label>
              <label class="block">
                <span class="mb-1 block text-meta text-ink-3">Port</span>
                <input bind:value={dr.port} type="number" placeholder="636" class="{inputCls} tnum" />
              </label>
            </div>

            <div class="mt-4">
              <span class="mb-1.5 block text-meta text-ink-3" id="dr-enc-label">Encryption</span>
              <div class="flex flex-wrap gap-1.5" role="group" aria-labelledby="dr-enc-label">
                {#each ENCRYPTIONS as e (e.id)}
                  <button
                    type="button"
                    onclick={() => setDrEncryption(e.id)}
                    aria-pressed={drEncryption === e.id}
                    class={'rounded-full border px-3 py-1 text-meta font-medium transition-colors ' +
                      (drEncryption === e.id
                        ? 'border-accent bg-accent text-on-accent'
                        : 'border-line bg-surface text-ink-2 hover:bg-surface-2')}
                  >
                    {e.label}
                  </button>
                {/each}
              </div>
              <p class="mt-1.5 text-label leading-relaxed text-ink-3">
                Switching also sets the port (<span class="tnum">389</span> for None and StartTLS,
                <span class="tnum">636</span> for LDAPS). Never run None in production: the user's
                password crosses the wire in the clear on the verify bind.
              </p>
            </div>

            <label class="mt-4 block">
              <span class="mb-1 block text-meta text-ink-3">Timeout (seconds)</span>
              <input value="8" disabled class="{inputCls} tnum opacity-70" />
            </label>
            <p class="mt-1 text-label text-ink-3">
              Fixed at 8 seconds by the server and not configurable from here — shown so you know what
              a hanging directory costs a sign-in, not as a control that would do nothing.
            </p>

            <label class="mt-4 flex items-start gap-2 text-body-sm text-ink">
              <input type="checkbox" bind:checked={dr.validate_cert} class="{checkCls} mt-0.5" />
              <span>
                Validate certificate
                {#if !dr.validate_cert}
                  <span class="mt-1 flex items-start gap-1.5 text-meta leading-relaxed text-warning">
                    <TriangleAlert size={13} class="mt-0.5 shrink-0" />
                    Off makes the bind MITM-able — an attacker between this server and the directory can
                    present any certificate and read the password.
                  </span>
                {/if}
              </span>
            </label>
            <label class="mt-3 block">
              <span class="mb-1 block text-meta text-ink-3">CA bundle path (optional, on this server)</span>
              <input bind:value={dr.ca_cert_file} placeholder="/etc/ssl/certs/corp-ca.pem" class="{inputCls} font-mono text-meta" />
            </label>
            <label class="mt-3 block">
              <span class="mb-1 block text-meta text-ink-3">Bind DN</span>
              <input bind:value={dr.bind_dn} placeholder="cn=svc-pharmacy,ou=service,dc=corp,dc=com" class="{inputCls} font-mono text-body-sm" />
            </label>
            <label class="mt-3 block">
              <span class="mb-1 block text-meta text-ink-3">
                Bind password
                {#if secretSet.ldap_bind_password_set}<span class="text-success">· stored</span>{/if}
              </span>
              <input
                bind:value={dr.bind_password}
                type="password"
                placeholder={secretSet.ldap_bind_password_set
                  ? '•••••••• (leave blank to keep the saved secret)'
                  : 'service account password'}
                class="{inputCls} font-mono text-body-sm"
              />
            </label>
            <p class="mt-2 text-meta leading-relaxed text-ink-3">
              This account only needs to <b>search</b> the base DN — it never writes to the directory.
              Grant it nothing more.
            </p>

            <!-- DIRECTORY TREE -->
            <h3 class="mb-2.5 mt-6 text-micro font-bold uppercase tracking-[0.08em] text-ink-3">Directory tree</h3>
            <label class="block">
              <span class="mb-1 block text-meta text-ink-3">Base DN</span>
              <input bind:value={dr.base_dn} placeholder="ou=users,dc=corp,dc=com" class="{inputCls} font-mono text-body-sm" />
            </label>
            <label class="mt-3 block">
              <span class="mb-1 block text-meta text-ink-3">User filter ({'{username}'} is substituted and escaped)</span>
              <input bind:value={dr.user_filter} placeholder="(uid={'{username}'})" class="{inputCls} font-mono text-body-sm" />
            </label>
            <div class="mt-2 flex flex-wrap items-center gap-1.5">
              <span class="text-meta text-ink-3">Presets:</span>
              {#each FILTER_PRESETS as p (p.id)}
                <button
                  type="button"
                  onclick={() => (dr.user_filter = p.value)}
                  class="rounded-full border border-line bg-surface px-2.5 py-1 text-meta font-medium text-ink-2 hover:bg-surface-2"
                >
                  {p.label} <span class="font-mono text-label text-ink-3">{p.value}</span>
                </button>
              {/each}
            </div>
            <div class="mt-3 grid gap-3.5 sm:grid-cols-2">
              <label class="block">
                <span class="mb-1 block text-meta text-ink-3">Email attribute</span>
                <input bind:value={dr.email_attr} placeholder="mail" class={inputCls} />
              </label>
              <label class="block">
                <span class="mb-1 block text-meta text-ink-3">Name attribute</span>
                <input bind:value={dr.name_attr} placeholder="cn" class={inputCls} />
              </label>
            </div>
            <p class="mt-2 text-meta leading-relaxed text-ink-3">
              The email attribute is what matches the account on the Users page. If the directory
              reports a different address than the row here, the sign-in is refused.
            </p>

            <!-- BEHAVIOUR -->
            <h3 class="mb-2.5 mt-6 text-micro font-bold uppercase tracking-[0.08em] text-ink-3">Behaviour</h3>
            <label class="flex items-start gap-2.5 text-body-sm text-ink">
              <input type="checkbox" bind:checked={dr.enabled} class="{checkCls} mt-0.5" />
              <span>Enabled<span class="mt-0.5 block text-meta text-ink-2">Directory passwords are tried in the normal password box when local login fails.</span></span>
            </label>
            <label class="mt-3 flex items-start gap-2.5 text-body-sm text-ink">
              <input type="checkbox" bind:checked={dr.auto_create} class="{checkCls} mt-0.5" />
              <span>
                <span class="inline-flex items-center gap-1.5">
                  <UserPlus size={14} class="text-ink-3" /> Let the directory create accounts
                </span>
                <span class="mt-1 block text-meta leading-relaxed text-ink-2">
                  On: an email that binds successfully but has no account here gets one created in
                  <b class="text-ink">pending</b> state — the person is held on the approval screen and
                  an administrator must approve them on the Users page before they can see anything.
                  Off: an unknown email is <b class="text-ink">refused</b>, and the account has to be
                  created on the Users page first.
                </span>
              </span>
            </label>
            {#if !autoCreateSupported}
              <p class="mt-2 flex items-start gap-1.5 text-meta text-ink-3">
                <CircleAlert size={13} class="mt-0.5 shrink-0" />
                This backend did not send this field, so it may be ignored on save — with an older
                server, an unknown email is refused regardless.
              </p>
            {/if}
          {/if}

          {@render testResult(modal.kind === 'oidc' ? oidcTest : ldapTest, modal.kind === 'oidc' ? providerOf(dr.provider_type).name : 'LDAP')}
        </div>

        <!-- footer -->
        <div class="flex flex-wrap items-center gap-2 border-t border-line px-5 py-3.5">
          <button
            type="button"
            onclick={() => runTest(modal.kind === 'oidc' ? 'oidc' : 'ldap')}
            disabled={(modal.kind === 'oidc' ? oidcTest : ldapTest)?.state === 'busy'}
            class={btnCls}
          >
            <PlugZap size={15} /> Test connection
          </button>
          <span class="text-label text-ink-3">Tests the settings already saved on the server, not the fields above.</span>
          <span class="ml-auto flex flex-wrap items-center gap-2">
            <button type="button" onclick={closeModal} class={btnCls}>Cancel</button>
            <button
              type="button"
              onclick={saveProvider}
              disabled={saving || (modal.kind === 'oidc' && !canSaveProvider)}
              class="inline-flex items-center gap-2 rounded-panel bg-accent px-4 py-1.5 text-body-sm font-semibold text-on-accent hover:bg-accent-hover disabled:opacity-60"
            >
              {#if saving}<Loader2 size={15} class="animate-spin" />{:else}<Save size={15} />{/if}
              Save
            </button>
          </span>
          {#if modal.kind === 'oidc' && !canSaveProvider}
            <p class="w-full text-label text-warning">
              Enter the new client secret — a blank field keeps the previous provider's stored secret.
            </p>
          {/if}
        </div>
      </div>
    </div>
  {/if}

  <!-- ================================================================ save bar -->
  <div
    class="elev sticky bottom-0 z-20 mt-5 flex flex-wrap items-center gap-3 rounded-panel border border-line bg-surface px-4 py-3"
  >
    <button
      onclick={save}
      disabled={saving || !dirty}
      class="inline-flex items-center gap-2 rounded-panel bg-accent px-4 py-2 text-body-sm font-medium text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
    >
      {#if saving}<Loader2 size={15} class="animate-spin" />{:else}<Save size={15} />{/if}
      {saving ? 'Saving…' : 'Save'}
    </button>

    {#if dirty}
      <span class="inline-flex items-center gap-1.5 text-meta font-medium text-warning">
        <CircleAlert size={14} /> Unsaved changes
      </span>
    {:else}
      <span class="inline-flex items-center gap-1.5 text-meta text-ink-3">
        <Check size={14} /> No unsaved changes
      </span>
    {/if}

    <span class="text-meta text-ink-3">Secrets are write-only — stored, never shown again.</span>

    <a
      href="https://openid.net/developers/how-connect-works/"
      target="_blank"
      rel="noopener noreferrer"
      class="ml-auto inline-flex items-center gap-1.5 text-meta text-ink-3 hover:text-ink-2"
    >
      How OIDC works <ExternalLink size={12} />
    </a>
  </div>

  <div class="mt-2 min-h-[18px] text-meta text-ink-2" role="status" aria-live="polite">
    {saveMsg}
  </div>
{/if}
