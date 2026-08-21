<!--
  The Shown / Hidden switch in the detail panel.

  This replaces the ⋮ menu item. A menu item said "Hide from customers" and
  nothing said what the branch is doing right now; a switch is the state AND the
  control in one place, which is what the approved design shows.

  It does not flip itself. `onrequest` opens the confirmation flow and the state
  changes only once the backend has agreed — so a refused change (a 403 from a
  non-super-admin, the case the audit trail below it is full of) leaves the
  switch reading what is actually true, rather than showing the change and
  quietly reverting.

  Three signals, never colour on its own:

    * the WORD beside it — "Shown" / "Hidden";
    * the knob's POSITION, and the track's fill;
    * `role="switch"` + `aria-checked`, which is what a screen reader reads.

  `aria-label` names the branch. "Shown to customers" alone would be the same
  accessible name on every panel, and the panel is the only thing on the page
  that says which branch you are looking at.

  No `transition-colors` anywhere in here. That utility animates `outline-color`
  along with everything else, so the global focus ring fades in over 120ms
  instead of appearing — on the one control on this page whose whole job is to
  be found by keyboard. The two properties that actually move are named
  explicitly instead, and both sit on non-focusable spans.
-->
<script>
  let {
    /** 'active' or 'disabled' — the branch's real status. */
    status = 'active',
    /** The branch code, for the accessible name. */
    code = '',
    /** Called with the status being asked for. Does NOT change `status`. */
    onrequest,
    /** No control at all — the reader is not a super admin, or a change is in flight. */
    disabled = false
  } = $props();

  let on = $derived(status !== 'disabled');
</script>

<button
  type="button"
  role="switch"
  aria-checked={on}
  aria-label={`Show ${code || 'this branch'} to customers`}
  {disabled}
  onclick={() => onrequest?.(on ? 'disabled' : 'active')}
  class="inline-flex cursor-pointer items-center gap-2.5 rounded-card p-1
         disabled:cursor-default disabled:opacity-60"
>
  <span
    class="relative h-[21px] w-9 flex-none rounded-full duration-150 ease-out
           motion-safe:transition-[background-color] {on ? 'bg-accent' : 'bg-ink-3'}"
  >
    <!-- The knob is `bg-surface` rather than white: on the hidden track in dark
         mode a hard white puck is the brightest thing in the panel and pulls
         the eye to the control that is switched OFF. -->
    <span
      class="absolute top-[3px] h-[15px] w-[15px] rounded-full bg-surface shadow-sm
             duration-150 ease-out motion-safe:transition-[left] {on
        ? 'left-[18px]'
        : 'left-[3px]'}"
    ></span>
  </span>
  <span class="text-body-sm font-medium text-ink">{on ? 'Shown' : 'Hidden'}</span>
</button>
