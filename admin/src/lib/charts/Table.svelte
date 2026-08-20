<script>
  // Every table on this page. Rows are keyboard-operable buttons when they open
  // something, because "click any row for the full turn" has to work without a
  // mouse. The cells themselves come from the caller as a snippet, so a column
  // can render a pill, a mono id or an em-dash without this file knowing about
  // any of them.
  let {
    cols = [],
    rows = [],
    onpick = null,
    rowKey = (r, i) => r.id ?? i,
    rowClass = () => '',
    empty = 'Nothing recorded in this range.',
    row
  } = $props();
</script>

{#if rows.length === 0}
  <p class="rounded-card border border-dashed border-line-2 bg-surface px-4 py-6 text-center text-body-sm text-ink-3">
    {empty}
  </p>
{:else}
  <div class="overflow-x-auto rounded-card border border-line">
    <table class="w-full border-collapse bg-surface">
      <thead>
        <tr>
          {#each cols as c (c.key)}
            <th
              class="border-b border-line bg-surface-2 px-3.5 py-2.5 text-label font-semibold tracking-[0.05em] text-ink-3 uppercase
                     {c.align === 'right' ? 'text-right' : 'text-left'}"
            >{c.label}</th>
          {/each}
        </tr>
      </thead>
      <tbody>
        {#each rows as r, i (rowKey(r, i))}
          <tr
            class="border-b border-line-2 last:border-b-0 {rowClass(r)}
                   {onpick ? 'cursor-pointer hover:bg-surface-2' : ''}"
            role={onpick ? 'button' : undefined}
            tabindex={onpick ? 0 : undefined}
            onclick={onpick ? () => onpick(r) : undefined}
            onkeydown={onpick ? (e) => (e.key === 'Enter' || e.key === ' ') && (e.preventDefault(), onpick(r)) : undefined}
          >
            {@render row(r)}
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

<style>
  /* Cells are supplied by the caller's snippet, so the padding and type scale
     live here rather than being repeated on every <td> at every call site. */
  tbody :global(td) {
    padding: 10px 14px;
    font-size: 12px;
    color: var(--color-ink-2);
  }
  tbody :global(td.r) {
    text-align: right;
  }
</style>
