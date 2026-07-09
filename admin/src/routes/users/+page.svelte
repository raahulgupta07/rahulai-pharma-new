<script>
  import { onMount } from 'svelte';
  import { Trash2, Plus } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import Badge from '$lib/Badge.svelte';
  import Modal from '$lib/aurora/Modal.svelte';
  import { toast } from '$lib/aurora/toast.js';

  let delTarget = $state(null);
  let delOpen = $state(false);

  const BASE = 'http://localhost:8088';
  const ROLES = ['user', 'admin', 'super_admin'];

  let loading = $state(true);
  let error = $state(null);
  let users = $state([]);

  let email = $state('');
  let name = $state('');
  let password = $state('');
  let role = $state('user');
  let creating = $state(false);
  let formError = $state(null);

  function roleTone(r) {
    return r === 'super_admin' ? 'warn' : r === 'admin' ? 'info' : 'neutral';
  }

  async function load() {
    loading = true;
    error = null;
    try {
      const res = await fetch(`${BASE}/admin/users`);
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      const data = await res.json();
      users = Array.isArray(data) ? data : [];
    } catch (e) {
      error = e.message || 'backend offline';
    } finally {
      loading = false;
    }
  }

  async function create(e) {
    e.preventDefault();
    formError = null;
    const em = email.trim();
    const nm = name.trim();
    if (!em) {
      formError = 'Email is required.';
      return;
    }
    creating = true;
    try {
      const res = await fetch(`${BASE}/admin/users`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: em, name: nm, password: password || undefined, role })
      });
      if (!res.ok) {
        let detail = `request failed (${res.status})`;
        try {
          const body = await res.json();
          if (body && body.detail) detail = body.detail;
        } catch {}
        throw new Error(detail);
      }
      toast(`Created ${em}`);
      email = '';
      name = '';
      password = '';
      role = 'user';
      await load();
    } catch (e) {
      formError = e.message || 'could not create user';
    } finally {
      creating = false;
    }
  }

  async function patch(id, body) {
    try {
      const res = await fetch(`${BASE}/admin/users/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      await load();
    } catch (e) {
      error = e.message || 'could not update user';
    }
  }

  function remove(u) {
    if (u.role === 'super_admin') return;
    delTarget = u;
    delOpen = true;
  }

  async function doRemove() {
    const u = delTarget;
    if (!u) return;
    try {
      const res = await fetch(`${BASE}/admin/users/${encodeURIComponent(u.id)}`, {
        method: 'DELETE'
      });
      if (!res.ok) throw new Error(`request failed (${res.status})`);
      toast(`Deleted ${u.email}`, 'trash-2');
      await load();
    } catch (e) {
      error = e.message || 'could not delete user';
    }
  }

  onMount(load);
</script>

<PageHeader
  title="Users"
  subtitle="Create and manage admin-panel accounts. No self-signup — you create users here. Email is the identity; LDAP/SSO logins merge into the matching email."
/>

{#if loading}
  <p class="text-[14px] text-ink-2">Loading users…</p>
{:else if error}
  <div class="rounded-xl border border-line bg-surface px-5 py-6 text-[14px] text-ink-2">
    <p class="font-medium text-ink">Backend offline</p>
    <p class="mt-1">
      Could not reach the agent at <span class="text-ink">localhost:8088</span>. Start the
      backend and reload.
    </p>
    <button
      onclick={load}
      class="mt-4 rounded-lg border border-line px-3 py-1.5 text-[13px] font-medium text-ink hover:bg-surface-2"
    >
      Retry
    </button>
  </div>
{:else}
  <!-- Create user -->
  <div class="mb-4 rounded-[14px] border-[0.5px] border-line bg-surface p-4">
    <div class="mb-2.5 text-[13px] font-medium text-ink">Create user</div>
    <form onsubmit={create} class="flex flex-col gap-2.5 sm:flex-row sm:items-end sm:flex-wrap">
      <label class="flex w-full flex-1 flex-col gap-1 sm:w-auto">
        <span class="text-[12px] text-ink-3">Email</span>
        <input
          bind:value={email}
          type="email"
          placeholder="user@org.com"
          class="w-full rounded-lg border border-line bg-page px-3 py-2 text-[14px] text-ink outline-none placeholder:text-ink-3 focus:border-accent"
        />
      </label>
      <label class="flex w-full flex-1 flex-col gap-1 sm:w-auto">
        <span class="text-[12px] text-ink-3">Name</span>
        <input
          bind:value={name}
          placeholder="Full name"
          class="w-full rounded-lg border border-line bg-page px-3 py-2 text-[14px] text-ink outline-none placeholder:text-ink-3 focus:border-accent"
        />
      </label>
      <label class="flex w-full flex-1 flex-col gap-1 sm:w-auto">
        <span class="text-[12px] text-ink-3">Password</span>
        <input
          bind:value={password}
          type="password"
          placeholder="leave blank for SSO/LDAP-only user"
          class="w-full rounded-lg border border-line bg-page px-3 py-2 text-[14px] text-ink outline-none placeholder:text-ink-3 focus:border-accent"
        />
      </label>
      <label class="flex w-full flex-col gap-1 sm:w-auto">
        <span class="text-[12px] text-ink-3">Role</span>
        <select
          bind:value={role}
          class="w-full rounded-lg border border-line bg-page px-3 py-2 text-[14px] text-ink outline-none focus:border-accent"
        >
          {#each ROLES as r}
            <option value={r}>{r}</option>
          {/each}
        </select>
      </label>
      <button
        type="submit"
        disabled={creating}
        class="inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg bg-accent px-3.5 py-2 text-[13px] font-medium text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
      >
        <Plus size={15} />
        {creating ? 'Creating' : 'Create'}
      </button>
    </form>
    {#if formError}
      <p class="mt-3 text-[13px] text-danger">{formError}</p>
    {/if}
  </div>

  <!-- Users table -->
  <div class="overflow-hidden rounded-[14px] border border-line bg-surface">
    {#if users.length === 0}
      <div class="px-6 py-10 text-center text-[14px] text-ink-2">
        No users yet. Create one above to grant admin-panel access.
      </div>
    {:else}
      <div class="max-h-[460px] overflow-auto">
        <table class="tbl">
          <thead>
            <tr>
              <th>Email</th>
              <th>Name</th>
              <th>Role</th>
              <th>Sources</th>
              <th>Active</th>
              <th>Last login</th>
              <th style="text-align:right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {#each users as u (u.id)}
              <tr>
                <td class="text-ink">{u.email}</td>
                <td class="text-ink-2">{u.name || '–'}</td>
                <td><Badge tone={roleTone(u.role)}>{u.role}</Badge></td>
                <td class="text-[12px] text-ink-3">
                  {(u.auth_sources ?? []).join(', ') || '–'}
                </td>
                <td>
                  <button
                    onclick={() => patch(u.id, { active: !u.active })}
                    class={'inline-flex items-center rounded-lg px-2 py-0.5 text-[11px] font-medium transition-colors ' +
                      (u.active
                        ? 'bg-success-soft text-success'
                        : 'bg-surface-2 text-ink-3')}
                    title={u.active ? 'Click to disable' : 'Click to enable'}
                  >
                    {u.active ? 'active' : 'disabled'}
                  </button>
                </td>
                <td class="text-ink-2 tnum">{u.last_login || '–'}</td>
                <td style="text-align:right">
                  <div class="inline-flex items-center gap-2">
                    <select
                      value={u.role}
                      onchange={(e) => patch(u.id, { role: e.currentTarget.value })}
                      class="rounded-lg border border-line bg-page px-2 py-1 text-[12px] text-ink outline-none focus:border-accent"
                      title="Change role"
                    >
                      {#each ROLES as r}
                        <option value={r}>{r}</option>
                      {/each}
                    </select>
                    <button
                      onclick={() => remove(u)}
                      disabled={u.role === 'super_admin'}
                      title={u.role === 'super_admin'
                        ? 'Cannot delete a super_admin'
                        : 'Delete user'}
                      class="inline-flex items-center rounded-lg p-1.5 text-ink-3 transition-colors hover:text-danger disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:text-ink-3"
                    >
                      <Trash2 size={15} />
                    </button>
                  </div>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>
{/if}

<Modal
  bind:open={delOpen}
  title="Delete user"
  confirmLabel="Delete"
  tone="danger"
  onconfirm={doRemove}
>
  Delete <b class="text-ink">{delTarget?.email}</b>? This cannot be undone. LDAP/SSO logins for this
  email will no longer have access.
</Modal>
