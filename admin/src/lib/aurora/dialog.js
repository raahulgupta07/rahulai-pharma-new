/**
 * `use:dialog` — the four things a modal has to do for a keyboard, in one place.
 *
 * Seven dialog-ish surfaces existed in this console and each solved this from
 * scratch. Two got it right. The other five failed in different combinations,
 * and the worst of them was the one guarding "Delete user" and "Delete
 * credential": focus never entered it, so the confirmation appeared while the
 * keyboard was still in the page underneath, and Escape did nothing — its
 * handler was bound to the overlay `<div>`, and a DOM listener cannot see a
 * keystroke that lands outside its own subtree. Tabbing went through controls
 * hidden behind a dimmed backdrop; cancelling left focus on `<body>`.
 *
 * So: one action, applied to the dialog element, doing all four.
 *
 *   1. Move focus in. The first focusable child, or the node itself.
 *   2. Trap Tab. Wrapping at both ends, Shift-Tab included.
 *   3. Escape closes — on `window`, not on the node, so it works before step 1
 *      has taken effect and from anywhere the focus has since drifted.
 *   4. Restore focus to whatever opened it, on teardown.
 *
 * Plus a scroll lock, because a background that scrolls under a modal moves the
 * thing the user will be returned to.
 *
 * NOT done here: making the background `inert`. Every one of these dialogs is
 * rendered inside the shell it would have to mark inert, so doing it properly
 * needs the dialogs hoisted to a portal first. `aria-modal="true"` is the
 * declared mitigation in the meantime, and the Tab trap is what actually keeps
 * a keyboard user out of the background. Recorded so it is a known gap rather
 * than an oversight.
 *
 * `returnTo` exists for ONE case: a dialog that opens from another dialog. In
 * the auth panel, "Continue" on a replace-confirmation opens the provider modal
 * and the Continue button is removed in the same breath — restoring to it would
 * drop focus to `<body>`. That flow already tracks the original row toggle, so
 * it passes a getter and the modal hands focus back there instead. Everything
 * else wants the default and should not pass it.
 *
 * @param {HTMLElement} node
 * @param {{ onclose?: () => void, returnTo?: () => (HTMLElement|null) }} [opts]
 */
export function dialog(node, opts = {}) {
  let onclose = opts.onclose;
  const returnToFn = opts.returnTo;

  // Captured BEFORE focus moves. `document.activeElement` is the trigger at
  // this instant and nothing else knows it — no caller stored it, which is why
  // every dialog that closed left the user somewhere arbitrary.
  const openedFrom = document.activeElement;

  const SELECTOR =
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
    'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  /** Visible focusable descendants, in tab order. */
  function focusables() {
    return [...node.querySelectorAll(SELECTOR)].filter(
      // offsetParent is null for display:none AND for a fixed-position element,
      // so it cannot be the only test — a fixed drawer's own controls would all
      // be filtered out. getClientRects() catches the genuinely hidden.
      (el) => el.getClientRects().length > 0
    );
  }

  function focusFirst() {
    const els = focusables();
    if (els.length) {
      els[0].focus();
      return;
    }
    // Nothing to focus: focus the dialog itself so the reader is at least
    // inside it. Needs tabindex="-1" on the node, which callers set.
    node.focus?.();
  }

  function onKeydown(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      onclose?.();
      return;
    }
    if (e.key !== 'Tab') return;
    const els = focusables();
    if (!els.length) {
      e.preventDefault();
      return;
    }
    const first = els[0];
    const last = els[els.length - 1];
    // `document.activeElement` may be outside the dialog entirely — that is the
    // state this action exists to prevent, but it is reachable if focus was
    // moved programmatically. Pull it back rather than letting Tab escape.
    if (!node.contains(document.activeElement)) {
      e.preventDefault();
      (e.shiftKey ? last : first).focus();
      return;
    }
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  const prevOverflow = document.body.style.overflow;
  document.body.style.overflow = 'hidden';

  // Capture phase: a dialog whose own content stops propagation (a code editor,
  // a menu) must not be able to swallow Escape and strand the user.
  window.addEventListener('keydown', onKeydown, true);

  // After paint, so the node's children exist and are laid out.
  const raf = requestAnimationFrame(focusFirst);

  return {
    update(next = {}) {
      onclose = next.onclose;
    },
    destroy() {
      cancelAnimationFrame(raf);
      window.removeEventListener('keydown', onKeydown, true);
      document.body.style.overflow = prevOverflow;
      // Resolved at teardown, not at mount: in the chained case the caller
      // knows the right target only once the first dialog is closing.
      const returnTo = returnToFn?.() ?? openedFrom;
      // Only if it is still in the document. A row deleted by the very action
      // this dialog confirmed is detached, and focusing a detached node
      // silently sends focus to <body> anyway.
      if (returnTo && returnTo.isConnected && typeof returnTo.focus === 'function') {
        returnTo.focus();
      }
    }
  };
}
