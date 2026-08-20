<!--
  One honest failure panel for every admin page.

  Every page used to render the same "Backend offline — start the backend and
  reload" block for any thrown error. On 2026-08-17 a 12-hour token simply
  expired; the Stores page reported a healthy container as offline and told the
  user to restart it. The status code was known at the throw site and thrown
  away.

  Pages now pass the status through: `err = { status, message }`, or a plain
  string / Error for a genuine transport failure (status 0).

  Usage:
    import ErrorState from '$lib/ErrorState.svelte';
    ...
    <ErrorState {error} retry={load} what="stores" />
-->
<script>
  import { API_BASE } from '$lib/apiBase.js';

  let { error, retry = null, what = 'this page' } = $props();

  // `error` may be an ApiError ({status, message}), an Error, or a string.
  let status = $derived(Number(error?.status ?? 0));
  let detail = $derived(
    typeof error === 'string' ? error : error?.message || ''
  );

  let title = $derived(
    status === 401
      ? 'Session expired'
      : status === 403
        ? 'Not permitted'
        : status === 404
          ? 'Not available'
          : status >= 500
            ? 'The server failed'
            : status > 0
              ? `Request failed (${status})`
              : 'Cannot reach the backend'
  );

  // Never tell someone to restart a server that answered us.
  let body = $derived(
    status === 401
      ? 'Your sign-in has timed out. Sign in again — nothing was lost.'
      : status === 403
        ? `Your account does not have access to ${what}. Ask a super admin to widen your role or store scope.`
        : status === 404
          ? `The backend has no endpoint for ${what}. It is running a build that predates this page.`
          : status >= 500
            ? `The backend is running but returned ${status} while loading ${what}. The server log has the traceback.`
            : status > 0
              ? `The backend answered with ${status} while loading ${what}.`
              : `No response from ${API_BASE}. The backend may be stopped, or the network path to it is blocked.`
  );

  let canRetry = $derived(retry !== null && status !== 401 && status !== 403);

  // DESIGN.md gives this one panel a flat `danger-soft`. It is split here on
  // purpose, and the console's own rule is why: a refusal is not a failure.
  // 401/403/404 are all facts about who you are or what this build has — an
  // expired session, a role that does not reach this page, a backend older
  // than this console. Painting those red says the server broke, which is a
  // lie the reader then has to un-learn. Only a 5xx or a dead connection is a
  // failure, and only those are red.
  let broke = $derived(status === 0 || status >= 500);
</script>

<div
  role="alert"
  class="rounded-card border px-5 py-6 text-meta
         {broke
    ? 'border-danger bg-danger-soft text-ink-2'
    : 'border-warning bg-warning-soft text-ink-2'}"
>
  <p class="text-body-sm font-bold text-ink">{title}</p>
  <p class="mt-1">{body}</p>
  {#if detail && status > 0}
    <p class="mt-2 font-mono text-meta text-ink-2 opacity-70">{detail}</p>
  {/if}

  <div class="mt-4 flex flex-wrap gap-2">
    {#if status === 401}
      <a
        href="/admin/"
        class="cursor-pointer rounded-control border border-line bg-surface px-3 py-1.5 text-body-sm font-medium text-ink transition-colors hover:bg-surface-2"
      >
        Sign in again
      </a>
    {:else if canRetry}
      <button
        onclick={retry}
        class="cursor-pointer rounded-control border border-line bg-surface px-3 py-1.5 text-body-sm font-medium text-ink transition-colors hover:bg-surface-2"
      >
        Retry
      </button>
    {/if}
  </div>
</div>
