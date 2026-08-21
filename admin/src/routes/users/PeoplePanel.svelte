<script>
  import { onMount } from 'svelte';
  import { Trash2, Plus, Check, ShieldCheck } from '@lucide/svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import Badge from '$lib/Badge.svelte';
  import Modal from '$lib/aurora/Modal.svelte';
  import { toast } from '$lib/aurora/toast.js';
  import { getJSON } from '$lib/api.js';
  import { when } from '$lib/charts/format.js';
  import ErrorState from '$lib/ErrorState.svelte';

  let delTarget = $state(null);
  let delOpen = $state(false);

  // `admin` is deliberately NOT offered when choosing a role.
  //
  // The decision (2026-08-21) is that an administrator is an administrator:
  // there is no tier of person who should see the console but not its settings.
  // Rather than dissolve the distinction in code — which would have meant
  // rewriting 24 tests, four of which exist to stop an admin minting a
  // super_admin or deleting accounts — the weaker role is simply no longer
  // handed out. The backend still enforces the split, so the ceiling is intact
  // and a mistake here cannot escalate anyone.
  //
  // Accounts that ALREADY hold `admin` keep working and still render their
  // badge; this list only governs what can be newly chosen. If one is ever
  // needed again, add it back here — no server change is required.
  const ROLES = ['user', 'super_admin'];

  /**
   * The options for ONE row's role dropdown.
   *
   * A `<select>` whose `value` matches none of its options does not render
   * blank — it renders the FIRST option. With `admin` dropped from ROLES, an
   * account that still holds `admin` would therefore have displayed as "user":
   * a demotion that never happened, shown as fact, on the page whose whole job
   * is saying who can do what. So a row always offers its own current role,
   * even one no longer handed out.
   */
  function rolesFor(u) {
    return ROLES.includes(u.role) ? ROLES : [...ROLES, u.role];
  }

  /**
   * A one-line, status-aware reason an ACTION failed, for a toast or the inline
   * message beside a form. Never blames a stopped backend for an answered
   * request — that is the whole point of carrying the status.
   */
  function reason(e, verb) {
    const s = Number(e?.status ?? 0);
    if (s === 401) return `Your sign-in has timed out. Sign in again, then ${verb}.`;
    if (s === 403) return `Your account is not permitted to ${verb}.`;
    if (s > 0) return e?.message || `The backend answered ${s}.`;
    return `No response from the backend — could not ${verb}.`;
  }

  let loading = $state(true);
  let error = $state(null);
  let users = $state([]);

  // Branch (store) pinning. `store_id` null = global scope ("All branches").
  // The option list comes from the outlets the inventory actually has; if that
  // call fails the select degrades to "just the value already on the row" rather
  // than offering a list we cannot vouch for.
  let outlets = $state([]);
  let outletsErr = $state(null);
  let savingStore = $state({});

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
      const data = await getJSON('/admin/users');
      users = Array.isArray(data) ? data : [];
    } catch (e) {
      // Keep the object: 401 (expired token) must not print "backend offline".
      error = e;
      users = [];
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
      // getJSON already surfaces the backend's `detail` as the message.
      await getJSON('/admin/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: em, name: nm, password: password || undefined, role })
      });
      toast(`Created ${em}`);
      email = '';
      name = '';
      password = '';
      role = 'user';
      await load();
    } catch (e) {
      formError = reason(e, 'create this user');
    } finally {
      creating = false;
    }
  }

  async function loadOutlets() {
    try {
      const d = await getJSON('/admin/embed/outlets');
      outlets = (Array.isArray(d) ? d : []).map((o) => o.site_code).filter(Boolean);
      outletsErr = null;
    } catch (e) {
      outlets = [];
      // Hold the error, not a boolean: the note beside the select can then say
      // whether the branch list is missing because the session expired or
      // because nothing answered.
      outletsErr = e;
    }
  }

  // A super_admin is always globally scoped — the backend forces it regardless
  // of what is sent — so the control is rendered read-only there instead of
  // offering a choice that would be silently discarded.
  function storeEditable(u) {
    return u.role !== 'super_admin';
  }

  // Union of the known outlets and whatever this row is already pinned to, so a
  // branch code that no longer appears in inventory is still shown (and kept)
  // instead of quietly snapping the select to "All branches".
  function storeOptions(u) {
    const list = [...outlets];
    if (u.store_id && !list.includes(u.store_id)) list.unshift(u.store_id);
    return list;
  }

  async function setStore(u, raw) {
    const next = raw === '' ? null : raw;
    const prev = u.store_id ?? null;
    if (next === prev) return;
    const idx = users.findIndex((x) => x.id === u.id);
    if (idx < 0) return;
    // optimistic: paint the new value, roll the exact old one back on failure
    users[idx] = { ...users[idx], store_id: next };
    savingStore = { ...savingStore, [u.id]: true };
    try {
      const body = await getJSON(`/admin/users/${encodeURIComponent(u.id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ store_id: next })
      });
      // trust the server's echo over our guess when it sends one
      if (body && typeof body === 'object' && 'store_id' in body) {
        const j = users.findIndex((x) => x.id === u.id);
        if (j >= 0) users[j] = { ...users[j], store_id: body.store_id ?? null };
      }
      toast(next ? `${u.email} pinned to ${next}` : `${u.email} can see all branches`);
    } catch (e) {
      const j = users.findIndex((x) => x.id === u.id);
      if (j >= 0) users[j] = { ...users[j], store_id: prev };
      toast(reason(e, 'change this branch'), 'alert-triangle');
    } finally {
      const { [u.id]: _drop, ...rest } = savingStore;
      savingStore = rest;
    }
  }

  async function patch(id, body) {
    try {
      await getJSON(`/admin/users/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      await load();
    } catch (e) {
      // An action that failed is not a page that failed: keep the list on
      // screen (it is still what the server last told us) and say what broke.
      toast(reason(e, 'update this user'), 'alert-triangle');
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
      await getJSON(`/admin/users/${encodeURIComponent(u.id)}`, { method: 'DELETE' });
      toast(`Deleted ${u.email}`, 'trash-2');
      await load();
    } catch (e) {
      toast(reason(e, 'delete this user'), 'alert-triangle');
    }
  }

  onMount(() => {
    load();
    loadOutlets();
  });
</script>

<PageHeader
  level={2}
  title="Users"
  subtitle="Create and manage admin-panel accounts. No self-signup — you create users here. Email is the identity; LDAP/SSO logins merge into the matching email."
/>

{#if loading}
  <p class="text-body-sm text-ink-2">Loading users…</p>
{:else if error}
  <ErrorState {error} retry={load} what="users" />
{:else}
  <!-- Create user -->
  <div class="mb-4 rounded-panel border border-line bg-surface p-4">
    <div class="mb-2.5 text-body-sm font-medium text-ink">Create user</div>
    <form onsubmit={create} class="flex flex-col gap-2.5 sm:flex-row sm:items-end sm:flex-wrap">
      <label class="flex w-full flex-1 flex-col gap-1 sm:w-auto">
        <span class="text-meta text-ink-3">Email</span>
        <input
          bind:value={email}
          type="email"
          placeholder="user@org.com"
          class="w-full rounded-panel border border-line bg-page px-3 py-2 text-body-sm text-ink outline-none placeholder:text-ink-3 focus:border-accent"
        />
      </label>
      <label class="flex w-full flex-1 flex-col gap-1 sm:w-auto">
        <span class="text-meta text-ink-3">Name</span>
        <input
          bind:value={name}
          placeholder="Full name"
          class="w-full rounded-panel border border-line bg-page px-3 py-2 text-body-sm text-ink outline-none placeholder:text-ink-3 focus:border-accent"
        />
      </label>
      <label class="flex w-full flex-1 flex-col gap-1 sm:w-auto">
        <span class="text-meta text-ink-3">Password</span>
        <input
          bind:value={password}
          type="password"
          placeholder="leave blank for SSO/LDAP-only user"
          class="w-full rounded-panel border border-line bg-page px-3 py-2 text-body-sm text-ink outline-none placeholder:text-ink-3 focus:border-accent"
        />
      </label>
      <label class="flex w-full flex-col gap-1 sm:w-auto">
        <span class="text-meta text-ink-3">Role</span>
        <select
          bind:value={role}
          class="w-full rounded-panel border border-line bg-page px-3 py-2 text-body-sm text-ink outline-none focus:border-accent"
        >
          {#each ROLES as r}
            <option value={r}>{r}</option>
          {/each}
        </select>
      </label>
      <button
        type="submit"
        disabled={creating}
        class="inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-panel bg-accent px-3.5 py-2 text-body-sm font-medium text-on-accent transition-colors hover:bg-accent-hover disabled:opacity-60"
      >
        <Plus size={15} />
        {creating ? 'Creating' : 'Create'}
      </button>
    </form>
    {#if formError}
      <p class="mt-3 text-body-sm text-danger">{formError}</p>
    {/if}
  </div>

  <!-- Users table -->
  <div class="overflow-hidden rounded-panel border border-line bg-surface">
    {#if users.length === 0}
      <div class="px-6 py-10 text-center text-body-sm text-ink-2">
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
              <th>Branch</th>
              <th>Sources</th>
              <th>Access</th>
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
                <td>
                  {#if !storeEditable(u)}
                    <span
                      class="inline-flex items-center rounded-panel bg-surface-2 px-2 py-0.5 text-label text-ink-2"
                      title="A super_admin is always scoped to every branch — the backend forces it."
                      >All branches</span
                    >
                  {:else}
                    <select
                      value={u.store_id ?? ''}
                      onchange={(e) => setStore(u, e.currentTarget.value)}
                      disabled={!!savingStore[u.id]}
                      aria-label={`Branch for ${u.email}`}
                      title="Pin this account to one branch, or give it every branch"
                      class="rounded-panel border border-line bg-page px-2 py-1 text-meta text-ink outline-none focus:border-accent disabled:opacity-60"
                    >
                      <option value="">All branches</option>
                      {#each storeOptions(u) as sc (sc)}
                        <option value={sc}>{sc}</option>
                      {/each}
                    </select>
                    {#if outletsErr && !u.store_id}
                      <span
                        class="ml-1 text-label text-ink-3"
                        title={outletsErr.status
                          ? `GET /admin/embed/outlets answered ${outletsErr.status}`
                          : 'GET /admin/embed/outlets got no response'}
                        >branch list unavailable{outletsErr.status === 401
                          ? ' — session expired'
                          : outletsErr.status === 403
                            ? ' — not permitted'
                            : ''}</span
                      >
                    {/if}
                  {/if}
                </td>
                <td class="text-meta text-ink-3">
                  {(u.auth_sources ?? []).join(', ') || '–'}
                </td>
                <td>
                  {#if u.approved}
                    <button
                      onclick={() => patch(u.id, { approved: false })}
                      disabled={u.role === 'super_admin'}
                      class="inline-flex items-center gap-1 rounded-panel bg-success-soft px-2 py-0.5 text-label font-medium text-success transition-colors disabled:cursor-not-allowed disabled:opacity-60"
                      title={u.role === 'super_admin' ? 'super_admin is always approved' : 'Approved — click to revoke access'}
                    >
                      <Check size={12} /> approved
                    </button>
                  {:else}
                    <button
                      onclick={() => { patch(u.id, { approved: true }); toast(`Approved ${u.email}`); }}
                      class="inline-flex items-center gap-1 rounded-panel bg-accent px-2.5 py-0.5 text-label font-semibold text-on-accent transition-colors hover:bg-accent-hover"
                      title="Grant this account access to the console"
                    >
                      <ShieldCheck size={12} /> Approve
                    </button>
                  {/if}
                </td>
                <td>
                  <button
                    onclick={() => patch(u.id, { active: !u.active })}
                    class={'inline-flex items-center rounded-panel px-2 py-0.5 text-label font-medium transition-colors ' +
                      (u.active
                        ? 'bg-success-soft text-success'
                        : 'bg-surface-2 text-ink-3')}
                    title={u.active ? 'Click to disable' : 'Click to enable'}
                  >
                    {u.active ? 'active' : 'disabled'}
                  </button>
                </td>
                <!-- `when` and not `||`: an account that has never signed in and
                     one whose timestamp we failed to read are different facts, and
                     `||` collapses them into the same dash. It also rendered the
                     raw ISO string, which is the only place in the console that
                     did. See UNKNOWN in $lib/charts/format.js. -->
                <td class="text-ink-2 tnum">{when(u.last_login)}</td>
                <td style="text-align:right">
                  <div class="inline-flex items-center gap-2">
                    <select
                      value={u.role}
                      onchange={(e) => patch(u.id, { role: e.currentTarget.value })}
                      class="rounded-panel border border-line bg-page px-2 py-1 text-meta text-ink outline-none focus:border-accent"
                      aria-label={`Role for ${u.email}`}
                      title="Change role"
                    >
                      {#each rolesFor(u) as r}
                        <option value={r}>{r}</option>
                      {/each}
                    </select>
                    <button
                      onclick={() => remove(u)}
                      disabled={u.role === 'super_admin'}
                      aria-label={`Delete ${u.email}`}
                      title={u.role === 'super_admin'
                        ? 'Cannot delete a super_admin'
                        : 'Delete user'}
                      class="inline-flex items-center rounded-panel p-1.5 text-ink-3 transition-colors hover:text-danger disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:text-ink-3"
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
