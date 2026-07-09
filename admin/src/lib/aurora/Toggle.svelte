<script>
  // Accessible pill toggle switch.
  let { checked = $bindable(false), label = '', onchange } = $props();
  function flip() {
    checked = !checked;
    onchange?.(checked);
  }
</script>

<button
  type="button"
  role="switch"
  aria-checked={checked}
  aria-label={label}
  onclick={flip}
  class="relative h-[27px] w-[46px] flex-shrink-0 cursor-pointer rounded-full transition-colors duration-200"
  style="background:{checked ? 'var(--color-success)' : 'var(--color-line-2)'}"
>
  <span
    class="absolute top-[3px] h-[21px] w-[21px] rounded-full bg-white shadow-[0_1px_3px_rgba(0,0,0,.25)] transition-all duration-200"
    style="left:{checked ? '22px' : '3px'}"
  ></span>
</button>
