<script>
  import { API_BASE } from '$lib/apiBase.js';
  import { onMount } from 'svelte';
  import { KeyRound, Server, Info, Save } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import { toast } from '$lib/aurora/toast.js';

  const BASE = API_BASE;

  let loading = $state(true);
  let error = $state(null);
  let saving = $state(false);

  // form model — mirrors AUTH_KEYS on the backend
  let f = $state({});
  // whether a stored secret exists (server never sends the value back)
  let secretSet = $state({ oidc_client_secret_set: false, ldap_bind_password_set: false });

  async function load() {
    loading = true;
    error = null;
    try {
      const res = await fetch(`${BASE}/admin/auth-config`);
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      const d = await res.json();
      secretSet = {
        oidc_client_secret_set: !!d.oidc_client_secret_set,
        ldap_bind_password_set: !!d.ldap_bind_password_set
      };
      // strip the *_set flags out of the editable model
      f = Object.fromEntries(Object.entries(d).filter(([k]) => !k.endsWith('_set')));
    } catch (e) {
      error = e.message || 'backend offline';
    } finally {
      loading = false;
    }
  }

  async function save() {
    saving = true;
    try {
      const res = await fetch(`${BASE}/admin/auth-config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(f)
      });
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      toast('Authentication settings saved — applies on next login');
      await load(); // re-mask secrets, refresh *_set
    } catch (e) {
      toast(e.message || 'could not save', 'alert-triangle');
    } finally {
      saving = false;
    }
  }

  const inputCls =
    'w-full rounded-lg border border-line bg-page px-3 py-2 text-[14px] text-ink outline-none placeholder:text-ink-3 focus:border-accent';

  onMount(load);
</script>

<PageHeader
  title="Authentication"
  subtitle="Keycloak (SSO) and LDAP sign-in. Changes take effect on the next login — no restart."
/>

{#if loading}
  <p class="text-[14px] text-ink-2">Loading…</p>
{:else if error}
  <div class="rounded-xl border border-line bg-surface px-5 py-6 text-[14px] text-ink-2">
    <p class="font-medium text-ink">Backend offline</p>
    <p class="mt-1">Could not reach the agent at <span class="text-ink">{API_BASE}</span>.</p>
    <button onclick={load} class="mt-4 rounded-lg border border-line px-3 py-1.5 text-[13px] font-medium text-ink hover:bg-surface-2">Retry</button>
  </div>
{:else}
  <div class="flex items-start gap-2 rounded-lg bg-info-soft p-3 text-[13px] text-info">
    <Info size={15} class="mt-0.5 shrink-0" />
    <span>
      Identity only — Keycloak and LDAP prove <b>who</b> someone is; the Users page decides
      <b>what</b> they may do. There is no self-signup: create the user on the Users page first,
      with the same email the directory reports, or their login is refused.
    </span>
  </div>

  <!-- Keycloak / OIDC -->
  <section class="mt-4 rounded-[14px] border border-line bg-surface p-5">
    <div class="mb-3 flex items-center gap-2">
      <KeyRound size={16} class="text-ink-2" />
      <h3 class="text-[15px] font-semibold text-ink">Keycloak (OIDC SSO)</h3>
    </div>

    <label class="mb-4 flex items-center gap-2.5 text-[14px] text-ink">
      <input type="checkbox" bind:checked={f.oidc_enabled} class="h-4 w-4 accent-[var(--accent,#0d9488)]" />
      Enable “Sign in with {f.oidc_provider_name || 'Keycloak'}” on the login screen
    </label>

    <div class="grid gap-3.5 sm:grid-cols-2">
      <label class="block">
        <span class="mb-1 block text-[12px] text-ink-3">Button label</span>
        <input bind:value={f.oidc_provider_name} placeholder="Keycloak" class={inputCls} />
      </label>
      <label class="block">
        <span class="mb-1 block text-[12px] text-ink-3">Client ID</span>
        <input bind:value={f.oidc_client_id} placeholder="pharmacy-agent" class={inputCls} />
      </label>
      <label class="block sm:col-span-2">
        <span class="mb-1 block text-[12px] text-ink-3">Discovery URL (.well-known/openid-configuration)</span>
        <input bind:value={f.oidc_discovery_url} placeholder="https://keycloak.example.com/realms/citcare/.well-known/openid-configuration" class="{inputCls} font-mono text-[12px]" />
      </label>
      <label class="block sm:col-span-2">
        <span class="mb-1 block text-[12px] text-ink-3">Redirect URI (must match the Keycloak client exactly)</span>
        <input bind:value={f.oidc_redirect_uri} placeholder="https://pharmacy.example.com/auth/sso/callback" class="{inputCls} font-mono text-[12px]" />
      </label>
      <label class="block">
        <span class="mb-1 block text-[12px] text-ink-3">Scopes</span>
        <input bind:value={f.oidc_scopes} placeholder="openid email profile" class={inputCls} />
      </label>
      <label class="block">
        <span class="mb-1 block text-[12px] text-ink-3">
          Client secret
          {#if secretSet.oidc_client_secret_set}<span class="text-emerald-500">· stored</span>{/if}
        </span>
        <input
          bind:value={f.oidc_client_secret}
          type="password"
          placeholder={secretSet.oidc_client_secret_set ? '•••••••• (leave blank to keep)' : 'from the Keycloak Credentials tab'}
          class="{inputCls} font-mono text-[13px]"
        />
      </label>
    </div>
    <p class="mt-3 text-[12px] text-ink-3">
      Use a <b>confidential</b> client (Client authentication ON). A public client has no secret and
      is not safe here.
    </p>
  </section>

  <!-- LDAP -->
  <section class="mt-4 rounded-[14px] border border-line bg-surface p-5">
    <div class="mb-3 flex items-center gap-2">
      <Server size={16} class="text-ink-2" />
      <h3 class="text-[15px] font-semibold text-ink">LDAP / Active Directory</h3>
    </div>

    <label class="mb-4 flex items-center gap-2.5 text-[14px] text-ink">
      <input type="checkbox" bind:checked={f.ldap_enabled} class="h-4 w-4 accent-[var(--accent,#0d9488)]" />
      Allow LDAP sign-in through the password box (tried when local login fails)
    </label>

    <div class="grid gap-3.5 sm:grid-cols-2">
      <label class="block">
        <span class="mb-1 block text-[12px] text-ink-3">Host</span>
        <input bind:value={f.ldap_host} placeholder="ldap.corp.com" class={inputCls} />
      </label>
      <label class="block">
        <span class="mb-1 block text-[12px] text-ink-3">Port</span>
        <input bind:value={f.ldap_port} type="number" placeholder="636" class={inputCls} />
      </label>
      <label class="block sm:col-span-2">
        <span class="mb-1 block text-[12px] text-ink-3">Base DN</span>
        <input bind:value={f.ldap_base_dn} placeholder="ou=users,dc=corp,dc=com" class="{inputCls} font-mono text-[13px]" />
      </label>
      <label class="block sm:col-span-2">
        <span class="mb-1 block text-[12px] text-ink-3">User filter · {'{username}'} is substituted and escaped</span>
        <input bind:value={f.ldap_user_filter} placeholder="(uid={'{username}'})  ·  AD: (sAMAccountName={'{username}'})" class="{inputCls} font-mono text-[13px]" />
      </label>
      <label class="block">
        <span class="mb-1 block text-[12px] text-ink-3">Email attribute</span>
        <input bind:value={f.ldap_email_attr} placeholder="mail" class={inputCls} />
      </label>
      <label class="block">
        <span class="mb-1 block text-[12px] text-ink-3">Name attribute</span>
        <input bind:value={f.ldap_name_attr} placeholder="cn" class={inputCls} />
      </label>
      <label class="block sm:col-span-2">
        <span class="mb-1 block text-[12px] text-ink-3">Service account DN (read-only bind used to find the user)</span>
        <input bind:value={f.ldap_bind_dn} placeholder="cn=svc-pharmacy,ou=service,dc=corp,dc=com" class="{inputCls} font-mono text-[13px]" />
      </label>
      <label class="block sm:col-span-2">
        <span class="mb-1 block text-[12px] text-ink-3">
          Service account password
          {#if secretSet.ldap_bind_password_set}<span class="text-emerald-500">· stored</span>{/if}
        </span>
        <input
          bind:value={f.ldap_bind_password}
          type="password"
          placeholder={secretSet.ldap_bind_password_set ? '•••••••• (leave blank to keep)' : 'service account password'}
          class="{inputCls} font-mono text-[13px]"
        />
      </label>
    </div>

    <div class="mt-4 flex flex-wrap gap-x-6 gap-y-2">
      <label class="flex items-center gap-2 text-[13px] text-ink">
        <input type="checkbox" bind:checked={f.ldap_use_ssl} class="h-4 w-4 accent-[var(--accent,#0d9488)]" /> LDAPS (SSL on connect, port 636)
      </label>
      <label class="flex items-center gap-2 text-[13px] text-ink">
        <input type="checkbox" bind:checked={f.ldap_start_tls} class="h-4 w-4 accent-[var(--accent,#0d9488)]" /> StartTLS (upgrade on port 389)
      </label>
      <label class="flex items-center gap-2 text-[13px] text-ink">
        <input type="checkbox" bind:checked={f.ldap_validate_cert} class="h-4 w-4 accent-[var(--accent,#0d9488)]" /> Validate certificate
      </label>
    </div>
    <p class="mt-3 text-[12px] text-ink-3">
      Never run plain (no SSL, no StartTLS): the user’s password crosses the wire on the verify
      bind. Turning off certificate validation makes that bind MITM-able.
    </p>
  </section>

  <div class="mt-5 flex items-center gap-3">
    <button
      onclick={save}
      disabled={saving}
      class="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-[13px] font-medium text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
    >
      <Save size={15} />
      {saving ? 'Saving…' : 'Save'}
    </button>
    <span class="text-[12px] text-ink-3">Secrets are write-only — stored, never shown again.</span>
  </div>
{/if}
