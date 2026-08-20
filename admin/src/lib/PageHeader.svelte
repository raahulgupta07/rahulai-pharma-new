<script>
  /**
   * The header a screen announces itself with.
   *
   * `level` exists for the tabbed screens. Embed, Configuration, People &
   * access and Answer quality are each one PAGE made of panels that used to be
   * pages, and every panel still brought its own `h1`. So the page's name was
   * whichever tab you happened to be on: you clicked "Embed & integration" and
   * landed on something called "Embed widget", and clicking a tab renamed the
   * page again. Panels mount lazily and stay mounted, so the document also
   * accumulated an `h1` per tab visited — four, on Embed. (They sit inside
   * `hidden` panels, which are out of the accessibility tree, so this was a
   * naming problem rather than an announcement one.)
   *
   * The shell owns the `h1` and names the page the way the rail does. A panel
   * inside it passes `level={2}` and keeps its own subtitle, actions and meta.
   */
  let { title = '', subtitle = '', actions, meta, level = 1 } = $props();
</script>

<header class="mb-4 flex flex-wrap items-start gap-4">
  <div class="min-w-0">
    <svelte:element
      this={level === 1 ? 'h1' : 'h2'}
      class="page-title leading-tight text-ink {level === 1 ? 'text-title' : 'text-body font-semibold'}"
      >{title}</svelte:element
    >
    {#if subtitle}
      <p class="mt-1 max-w-xl text-body-sm leading-relaxed text-ink-3">{subtitle}</p>
    {/if}
    {#if meta}<div class="mt-2 flex flex-wrap items-center gap-2">{@render meta()}</div>{/if}
  </div>
  {#if actions}
    <div class="ml-auto flex items-center gap-2">{@render actions()}</div>
  {/if}
</header>
