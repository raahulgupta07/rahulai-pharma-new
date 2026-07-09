<script>
  import { onMount, tick } from 'svelte';
  import {
    Plus,
    Search,
    MessageSquare,
    Star,
    Pill,
    Copy,
    RefreshCw,
    ThumbsUp,
    ThumbsDown,
    ArrowUp,
    Paperclip,
    Store,
    Languages,
    PanelLeft,
    PackageSearch,
    Replace,
    TrendingUp,
    Trash2,
    Loader2,
    Check,
    Wrench,
    ChevronRight,
    Clock,
    Zap,
    X,
    FileText,
    ExternalLink,
    DollarSign,
    ChevronDown,
    Sparkles,
    Pencil
  } from '@lucide/svelte';
  import { base as appBase } from '$app/paths';
  import { renderMarkdown } from '$lib/aurora/markdown.js';
  import { toast } from '$lib/aurora/toast.js';

  const base = 'http://localhost:8088';
  const LS = 'citcare_chat_threads';
  const LS_MODEL = 'citcare_chat_model';

  // Stable per-page-load id so the backend can tie feedback/learning to this conversation.
  const sessionId = crypto.randomUUID();

  // ---- model picker (A/B test Gemini variants) ----
  let models = $state([]); // [{id,name,price_in,price_out,note}]
  let selectedModel = $state('');
  let modelOpen = $state(false);
  let curModel = $derived(models.find((m) => m.id === selectedModel) ?? null);

  async function loadModels() {
    try {
      const r = await fetch(base + '/api/embed/models');
      const d = await r.json();
      models = d.models ?? [];
      const saved = localStorage.getItem(LS_MODEL);
      selectedModel = (saved && models.some((m) => m.id === saved) && saved) || d.default || models[0]?.id || '';
    } catch {
      models = [];
    }
  }
  function pickModel(id) {
    selectedModel = id;
    modelOpen = false;
    localStorage.setItem(LS_MODEL, id);
    toast('Model: ' + (models.find((m) => m.id === id)?.name ?? id));
  }

  // ---- source drawer (clickable article codes) ----
  let drawerOpen = $state(false);
  let drawerCode = $state('');
  let drawerData = $state(null);
  let drawerLoading = $state(false);

  async function openSource(code) {
    drawerCode = code;
    drawerOpen = true;
    drawerData = null;
    drawerLoading = true;
    try {
      const r = await fetch(`${base}/admin/catalog/${encodeURIComponent(code)}`);
      drawerData = r.ok ? await r.json() : null;
    } catch {
      drawerData = null;
    } finally {
      drawerLoading = false;
    }
  }
  // delegate clicks from rendered markdown (article-code chips) via an action,
  // so there's no inline handler on a static element (a11y-clean).
  function codeChips(node) {
    const h = (e) => {
      const chip = e.target.closest?.('[data-code]');
      if (chip) openSource(chip.getAttribute('data-code'));
    };
    node.addEventListener('click', h);
    return { destroy: () => node.removeEventListener('click', h) };
  }

  const FOLLOWUPS = [
    { icon: Store, label: 'Stock at a branch', q: 'stock at branch ' },
    { icon: DollarSign, label: 'Compare prices', q: 'compare prices of ' },
    { icon: Languages, label: 'မြန်မာ', q: 'အထက်ပါ အချက်အလက်ကို မြန်မာလို ပြန်ပြောပြပါ' }
  ];

  function copyMsg(text) {
    navigator.clipboard?.writeText(text);
    toast('Copied');
  }
  function retryLast() {
    if (busy) return;
    const msgs = active?.messages ?? [];
    for (let k = msgs.length - 1; k >= 0; k--) {
      if (msgs[k].role === 'user') {
        send(msgs[k].text);
        return;
      }
    }
  }

  // Derive the tool names used for a bot message from its steps/results trace.
  function toolsFromMsg(msg) {
    const names = new Set();
    for (const s of msg?.steps ?? []) if (s?.label) names.add(s.label);
    for (const r of msg?.results ?? []) if (r?.tool) names.add(r.tool);
    return [...names];
  }

  // Send thumbs up/down (and optional correction) for one bot message.
  // Goes through the app-wide fetch wrapper, which injects the admin Bearer
  // header for any '/admin/' URL — so we must NOT set Authorization here.
  async function sendFeedback(msg, verdict, correction = '') {
    const msgs = active?.messages ?? [];
    const i = msgs.indexOf(msg);
    if (i < 0) return;

    // optimistic UI: mark verdict / correction state on the message
    const next = { ...msg, feedback: verdict };
    if (correction) {
      next.correcting = false;
      next.correctionSaved = true;
    }
    msgs[i] = next;
    threads = threads;

    // paired question = nearest preceding user message
    let question = '';
    for (let k = i - 1; k >= 0; k--) {
      if (msgs[k].role === 'user') {
        question = msgs[k].text ?? '';
        break;
      }
    }

    const body = {
      session_id: sessionId,
      store_id: null,
      model: msg.modelName ?? '',
      question,
      answer: msg.text ?? '',
      tools: toolsFromMsg(msg),
      verdict,
      correction
    };

    try {
      const r = await fetch(base + '/admin/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      if (!r.ok) throw new Error('feedback ' + r.status);
    } catch {
      // soft failure: roll back the optimistic flags and tell the user quietly
      const cur = active?.messages ?? [];
      const j = cur.indexOf(next);
      if (j >= 0) {
        cur[j] = { ...next, feedback: null, correctionSaved: false, feedbackError: true };
        threads = threads;
      }
      toast('Could not save feedback');
    }
  }

  function startCorrection(msg) {
    const msgs = active?.messages ?? [];
    const i = msgs.indexOf(msg);
    if (i < 0) return;
    msgs[i] = { ...msg, correcting: !msg.correcting, feedbackError: false };
    threads = threads;
  }

  function setCorrectionText(msg, val) {
    const msgs = active?.messages ?? [];
    const i = msgs.indexOf(msg);
    if (i < 0) return;
    msgs[i] = { ...msg, correctionText: val };
    threads = threads;
  }

  function submitCorrection(msg) {
    const text = (msg.correctionText ?? '').trim();
    if (!text) return;
    sendFeedback(msg, 'down', text);
  }

  let token = $state(null);
  let status = $state('connecting…');
  let input = $state('');
  let busy = $state(false);
  let scroller;
  let ta;
  let sideOpen = $state(true);

  // ---- threads (persisted) ----
  let threads = $state([]); // [{id, title, ts, messages:[{role,text}]}]
  let activeId = $state(null);
  let active = $derived(threads.find((t) => t.id === activeId) ?? null);
  let messages = $derived(active?.messages ?? []);

  const suggestions = [
    { icon: PackageSearch, title: 'Check stock', q: 'total stock of 1000000024029 across all sites' },
    { icon: Replace, title: 'Find substitutes', q: 'substitutes for ALAXAN' },
    { icon: TrendingUp, title: 'Top sellers', q: 'top 5 by stock at CCBHSC' },
    { icon: Languages, title: 'Burmese query', q: 'ဖျားနာ အတွက် ဘာဆေး ရှိလဲ' }
  ];

  function save() {
    try {
      localStorage.setItem(LS, JSON.stringify(threads.slice(0, 50)));
    } catch {}
  }

  function load() {
    try {
      const raw = localStorage.getItem(LS);
      threads = raw ? JSON.parse(raw) : [];
    } catch {
      threads = [];
    }
  }

  function newChat() {
    activeId = null;
    input = '';
    tick().then(() => ta?.focus());
  }

  function openChat(id) {
    activeId = id;
    scrollDown();
  }

  function deleteChat(id, e) {
    e.stopPropagation();
    threads = threads.filter((t) => t.id !== id);
    if (activeId === id) activeId = null;
    save();
  }

  // group threads by day for the Claude-style sidebar
  let groups = $derived(
    (() => {
      const now = new Date();
      const dayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
      const buckets = { Today: [], Yesterday: [], 'Previous 7 days': [], Earlier: [] };
      for (const t of [...threads].sort((a, b) => b.ts - a.ts)) {
        const age = dayStart - new Date(t.ts).setHours(0, 0, 0, 0);
        if (age <= 0) buckets['Today'].push(t);
        else if (age <= 86400000) buckets['Yesterday'].push(t);
        else if (age <= 7 * 86400000) buckets['Previous 7 days'].push(t);
        else buckets['Earlier'].push(t);
      }
      return Object.entries(buckets).filter(([, v]) => v.length);
    })()
  );

  async function makeSession() {
    try {
      const r = await fetch(base + '/api/embed/session/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ embed_id: 'admin-chat', public_key: 'admin' })
      });
      token = (await r.json()).session_token;
      status = 'online';
    } catch {
      status = 'offline';
    }
  }

  // Prettify raw Agno tool names into human step labels.
  const TOOL_LABELS = {
    get_article_info: 'Looking up article info',
    summarize_article: 'Summarizing article',
    get_stock: 'Checking stock levels',
    top_by_stock: 'Ranking by stock',
    filter_by_price: 'Filtering by price',
    get_substitutes: 'Finding substitutes',
    search_by_name: 'Searching by name',
    search_by_meaning: 'Searching by meaning',
    related_drugs: 'Tracing the drug graph',
    drugs_for_same_condition: 'Finding drugs for the condition',
    find_at_other_stores: 'Checking other branches'
  };
  function stepLabel(raw) {
    if (!raw) return 'Searching the data';
    return TOOL_LABELS[raw] ?? raw.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
  }

  async function scrollDown() {
    await tick();
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  }

  function grow() {
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 200) + 'px';
  }

  async function send(text) {
    const msg = (text ?? input).trim();
    if (!msg || busy || !token) return;

    // start a new thread if none active
    if (!active) {
      const id = 'c' + Date.now();
      threads = [
        { id, title: msg.slice(0, 48), ts: Date.now(), messages: [] },
        ...threads
      ];
      activeId = id;
    }
    const thread = threads.find((t) => t.id === activeId);

    input = '';
    grow();
    busy = true;
    const t0 = Date.now();
    thread.messages = [
      ...thread.messages,
      { role: 'user', text: msg },
      { role: 'bot', text: '', steps: [], results: [], modelName: curModel?.name ?? '' }
    ];
    threads = threads; // trigger
    const idx = thread.messages.length - 1;
    await scrollDown();

    let full = '';
    try {
      // Fire the stream request; on a 401 (expired/invalid session token) mint a
      // fresh token via makeSession() and retry the same request ONCE.
      const doFetch = () =>
        fetch(base + '/api/embed/chat/stream', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_token: token, session_id: sessionId, message: msg, model: selectedModel })
        });
      let r = await doFetch();
      if (r.status === 401) {
        await makeSession(); // re-mint; updates `token`
        r = await doFetch(); // retry once with the refreshed token
      }
      if (!r.ok) throw new Error('session expired (' + r.status + ') — please try again');
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const frame = buf.slice(0, i);
          buf = buf.slice(i + 2);
          let data = '';
          let evt = 'message';
          for (const line of frame.split('\n')) {
            if (line.startsWith('event:')) evt = line.slice(6).trim();
            else if (line.startsWith('data:')) data += line.slice(5).trim();
          }
          if (!data || data === '[DONE]') continue;
          try {
            const j = JSON.parse(data);
            if (evt === 'step') {
              // agent tool-use trace (Claude-style "working" steps)
              const steps = [...(thread.messages[idx].steps ?? []), { label: j.label, icon: j.icon }];
              thread.messages[idx] = { ...thread.messages[idx], steps };
              threads = threads;
              await scrollDown();
            } else if (evt === 'result') {
              // structured rows the agent's tool returned
              const results = [...(thread.messages[idx].results ?? []), { tool: j.tool, rows: j.rows }];
              thread.messages[idx] = { ...thread.messages[idx], results };
              threads = threads;
            } else if (j.delta) {
              full += j.delta;
              thread.messages[idx] = { ...thread.messages[idx], role: 'bot', text: full };
              threads = threads;
              await scrollDown();
            }
          } catch {}
        }
      }
      if (!full)
        thread.messages[idx] = {
          ...thread.messages[idx],
          role: 'bot',
          text: 'I looked up the data but didn’t produce a written answer — please try rephrasing.'
        };
    } catch (e) {
      thread.messages[idx] = {
        ...thread.messages[idx],
        role: 'bot',
        text: 'Error: ' + (e.message || 'request failed')
      };
    } finally {
      const ms = Date.now() - t0;
      thread.messages[idx] = {
        ...thread.messages[idx],
        latencyMs: ms,
        toolCount: (thread.messages[idx].steps ?? []).length
      };
      busy = false;
      threads = threads;
      save();
      await scrollDown();
    }
  }

  function onKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  onMount(() => {
    load();
    makeSession();
    loadModels();
  });
</script>

<div class="flex h-full overflow-hidden">
  <!-- HISTORY SIDEBAR -->
  {#if sideOpen}
    <aside class="flex w-[268px] flex-shrink-0 flex-col border-r border-line bg-surface-2">
      <div class="p-3">
        <button
          onclick={newChat}
          class="flex w-full items-center gap-2.5 rounded-[11px] border border-line-2 bg-surface px-3 py-2.5 text-[14px] font-semibold text-ink transition-colors hover:bg-surface-2"
        >
          <Plus size={17} /> New chat
        </button>
        <div
          class="mt-2 flex items-center gap-2 rounded-[11px] border border-line bg-surface px-3 py-2.5 text-[13.5px] text-ink-3"
        >
          <Search size={15} /> Search chats…
        </div>
      </div>

      <div class="flex-1 overflow-y-auto px-2 pb-3">
        {#if threads.length === 0}
          <p class="px-3 py-6 text-center text-[13px] text-ink-3">No conversations yet.</p>
        {:else}
          {#each groups as [label, items]}
            <div class="px-2.5 pb-1.5 pt-3.5 text-[11px] font-bold uppercase tracking-[0.04em] text-ink-3">
              {label}
            </div>
            {#each items as t (t.id)}
              <div
                role="button"
                tabindex="0"
                onclick={() => openChat(t.id)}
                onkeydown={(e) => e.key === 'Enter' && openChat(t.id)}
                class="group flex cursor-pointer items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-[13.5px] transition-colors
                  {t.id === activeId ? 'bg-accent-soft text-ink' : 'text-ink-2 hover:bg-surface hover:text-ink'}"
              >
                <MessageSquare size={15} class="flex-shrink-0 opacity-60" />
                <span class="flex-1 truncate">{t.title}</span>
                <button
                  onclick={(e) => deleteChat(t.id, e)}
                  aria-label="Delete chat"
                  class="flex-shrink-0 text-ink-3 opacity-0 hover:text-danger group-hover:opacity-100"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            {/each}
          {/each}
        {/if}
      </div>

      <div class="flex items-center gap-2.5 border-t border-line px-3 py-2.5">
        <span
          class="flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-accent to-accent-hover text-[12px] font-bold text-white"
          >AD</span
        >
        <div class="flex-1 leading-tight">
          <div class="text-[13.5px] font-semibold text-ink">admin</div>
          <div class="flex items-center gap-1.5 text-[11px] text-ink-3">
            <span class="h-1.5 w-1.5 rounded-full {status === 'online' ? 'bg-success' : 'bg-ink-3'}"
            ></span>
            {status}
          </div>
        </div>
      </div>
    </aside>
  {/if}

  <!-- CONVERSATION -->
  <div class="flex min-w-0 flex-1 flex-col">
    <!-- chat top bar -->
    <div class="flex items-center gap-2 px-4 py-2.5">
      <button
        onclick={() => (sideOpen = !sideOpen)}
        aria-label="Toggle history"
        class="flex h-9 w-9 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 hover:text-ink"
      >
        <PanelLeft size={18} />
      </button>
      <span class="text-[14px] font-semibold text-ink">City Pharma Agent</span>

      <!-- model picker (A/B test Gemini variants) -->
      <div class="relative">
        <button
          onclick={() => (modelOpen = !modelOpen)}
          class="flex items-center gap-1.5 rounded-lg border border-line bg-surface px-2.5 py-1.5 text-[12.5px] font-medium text-ink hover:bg-surface-2"
        >
          <Sparkles size={13} class="text-accent" />
          {curModel?.name ?? 'Model'}
          <ChevronDown size={13} class="text-ink-3" />
        </button>
        {#if modelOpen}
          <button class="fixed inset-0 z-30 cursor-default" aria-label="Close menu" onclick={() => (modelOpen = false)}></button>
          <div class="absolute left-0 top-full z-40 mt-1.5 w-[280px] overflow-hidden rounded-xl border border-line bg-surface shadow-[var(--shadow-pop)]">
            <div class="border-b border-line px-3 py-2 text-[10.5px] font-bold uppercase tracking-[0.05em] text-ink-3">
              Chat model · A/B test
            </div>
            {#each models as m}
              <button
                onclick={() => pickModel(m.id)}
                class="flex w-full items-start gap-2.5 px-3 py-2.5 text-left transition-colors hover:bg-surface-2
                  {m.id === selectedModel ? 'bg-accent-soft/50' : ''}"
              >
                <Check size={15} class={m.id === selectedModel ? 'mt-0.5 text-accent' : 'mt-0.5 text-transparent'} />
                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-2">
                    <span class="text-[13px] font-semibold text-ink">{m.name}</span>
                    <span class="text-[10.5px] text-ink-3">{m.note}</span>
                  </div>
                  <div class="mt-0.5 font-mono text-[11px] text-ink-3">{m.id}</div>
                  <div class="mt-1 flex items-center gap-2 text-[11px]">
                    <span class="rounded bg-surface-2 px-1.5 py-0.5 text-ink-2">in ${m.price_in}/M</span>
                    <span class="rounded bg-surface-2 px-1.5 py-0.5 text-ink-2">out ${m.price_out}/M</span>
                  </div>
                </div>
              </button>
            {/each}
          </div>
        {/if}
      </div>
    </div>

    <!-- messages / greeting -->
    <div bind:this={scroller} class="flex-1 overflow-y-auto">
      {#if messages.length === 0}
        <div class="flex h-full flex-col items-center justify-center px-6 text-center">
          <h1 class="page-title text-[30px] text-ink">How can I help with the pharmacy?</h1>
          <p class="mt-2.5 text-[15px] text-ink-2">
            Ask about stock, prices, substitutes or indications — English or မြန်မာ.
          </p>
          <div class="mt-7 grid w-full max-w-[560px] grid-cols-1 gap-3 sm:grid-cols-2">
            {#each suggestions as s}
              <button
                onclick={() => send(s.q)}
                class="elev rounded-[13px] border-[0.5px] border-line bg-surface p-4 text-left transition-transform hover:-translate-y-0.5 hover:border-accent"
              >
                <div class="flex items-center gap-2">
                  <s.icon size={16} class="text-accent" />
                  <span class="text-[14px] font-semibold text-ink">{s.title}</span>
                </div>
                <div class="mt-1.5 text-[12.5px] text-ink-3">{s.q}</div>
              </button>
            {/each}
          </div>
        </div>
      {:else}
        <div class="mx-auto max-w-[740px] px-6 pb-10 pt-2">
          {#each messages as m, i}
            {#if m.role === 'user'}
              <div class="my-6 flex justify-end">
                <div
                  class="max-w-[78%] whitespace-pre-wrap rounded-2xl rounded-br-md border border-line bg-surface-2 px-4 py-3 text-[15px] leading-relaxed text-ink"
                >
                  {m.text}
                </div>
              </div>
            {:else}
              <div class="group my-6 flex gap-3.5">
                <div
                  class="mt-0.5 flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-accent to-accent-hover text-white"
                >
                  <Pill size={15} />
                </div>
                <div class="min-w-0 flex-1">
                  <div class="mb-1.5 text-[13px] font-semibold text-ink-2">City Pharma Agent</div>

                  {#if m.steps?.length}
                    {#if m.text === ''}
                      <!-- live tool-use trace while the agent works -->
                      <div class="mb-2 rounded-xl border-[0.5px] border-line bg-surface-2 p-2.5">
                        <div class="mb-1 flex items-center gap-1.5 px-1 text-[11px] font-bold uppercase tracking-[0.05em] text-ink-3">
                          <Wrench size={12} /> Working
                        </div>
                        {#each m.steps as s, i}
                          <div class="flex items-center gap-2 px-1 py-1 text-[13px] text-ink">
                            {#if i === m.steps.length - 1}
                              <Loader2 size={14} class="animate-spin text-accent" />
                            {:else}
                              <Check size={14} class="text-success" />
                            {/if}
                            <span>{stepLabel(s.label)}</span>
                          </div>
                        {/each}
                      </div>
                    {:else}
                      <!-- collapsed trace once the answer is in -->
                      <details class="group/d mb-2.5 rounded-xl border-[0.5px] border-line bg-surface-2">
                        <summary class="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-[12.5px] font-medium text-ink-2">
                          <ChevronRight size={14} class="transition-transform group-open/d:rotate-90" />
                          <Wrench size={13} class="text-accent" />
                          Looked up {m.steps.length} source{m.steps.length > 1 ? 's' : ''}
                        </summary>
                        <div class="px-3 pb-2.5">
                          {#each m.steps as s}
                            <div class="flex items-center gap-2 py-0.5 text-[12.5px] text-ink-2">
                              <Check size={13} class="text-success" />
                              {stepLabel(s.label)}
                            </div>
                          {/each}
                          {#if m.results?.length}
                            {#each m.results as res}
                              <div class="mt-2 overflow-hidden rounded-lg border-[0.5px] border-line bg-surface">
                                <div class="border-b border-line px-2.5 py-1.5 text-[10.5px] font-bold uppercase tracking-[0.04em] text-ink-3">
                                  {stepLabel(res.tool)} · {res.rows.length} row{res.rows.length > 1 ? 's' : ''}
                                </div>
                                <table class="w-full text-[12px]">
                                  <tbody>
                                    {#each res.rows.slice(0, 5) as row}
                                      <tr class="border-t border-line first:border-0">
                                        {#each Object.entries(row).slice(0, 4) as [k, v]}
                                          <td class="px-2.5 py-1.5 text-ink-2">
                                            {#if /code/i.test(k) && /^\d{10,14}$/.test(String(v))}
                                              <button
                                                type="button"
                                                onclick={() => openSource(String(v))}
                                                class="rounded bg-accent-soft px-1.5 font-mono text-[11px] font-semibold text-accent hover:bg-accent hover:text-white"
                                                >{v}</button
                                              >
                                            {:else}
                                              <span class="text-ink">{v}</span>
                                            {/if}
                                          </td>
                                        {/each}
                                      </tr>
                                    {/each}
                                  </tbody>
                                </table>
                              </div>
                            {/each}
                          {/if}
                        </div>
                      </details>
                    {/if}
                  {/if}

                  {#if m.text === '' && !m.steps?.length}
                    <div class="flex gap-1 py-1.5">
                      <span class="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-3" style="animation-delay:0ms"></span>
                      <span class="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-3" style="animation-delay:150ms"></span>
                      <span class="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-3" style="animation-delay:300ms"></span>
                    </div>
                  {:else if m.text !== ''}
                    <div class="md" use:codeChips>{@html renderMarkdown(m.text)}</div>

                    <!-- toolbar: meta + actions -->
                    <div class="mt-3 flex items-center gap-3">
                      <div class="flex items-center gap-3 text-[11.5px] text-ink-3">
                        {#if m.latencyMs}
                          <span class="flex items-center gap-1"><Clock size={12} />{(m.latencyMs / 1000).toFixed(1)}s</span>
                        {/if}
                        {#if m.toolCount}
                          <span class="flex items-center gap-1"><Wrench size={12} />{m.toolCount} tool{m.toolCount > 1 ? 's' : ''}</span>
                        {/if}
                        {#if m.modelName}
                          <span class="flex items-center gap-1"><Sparkles size={12} class="text-accent" />{m.modelName}</span>
                        {/if}
                      </div>
                      <div class="ml-auto flex gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
                        <button onclick={() => copyMsg(m.text)} aria-label="Copy" class="flex h-8 w-8 cursor-pointer items-center justify-center rounded-lg text-ink-3 transition-colors duration-200 hover:bg-surface-2 hover:text-ink focus-visible:ring-2 focus-visible:ring-accent"><Copy size={15} /></button>
                        <button onclick={retryLast} aria-label="Retry" class="flex h-8 w-8 cursor-pointer items-center justify-center rounded-lg text-ink-3 transition-colors duration-200 hover:bg-surface-2 hover:text-ink focus-visible:ring-2 focus-visible:ring-accent"><RefreshCw size={15} /></button>
                        <button
                          onclick={() => sendFeedback(m, 'up')}
                          disabled={m.feedback != null}
                          aria-label="Good answer"
                          aria-pressed={m.feedback === 'up'}
                          class="flex h-8 w-8 cursor-pointer items-center justify-center rounded-lg transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-default
                            {m.feedback === 'up' ? 'bg-accent-soft text-accent' : 'text-ink-3 hover:bg-surface-2 hover:text-ink disabled:opacity-40'}"
                        ><ThumbsUp size={15} /></button>
                        <button
                          onclick={() => sendFeedback(m, 'down')}
                          disabled={m.feedback != null}
                          aria-label="Bad answer"
                          aria-pressed={m.feedback === 'down'}
                          class="flex h-8 w-8 cursor-pointer items-center justify-center rounded-lg transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-default
                            {m.feedback === 'down' ? 'bg-accent-soft text-accent' : 'text-ink-3 hover:bg-surface-2 hover:text-ink disabled:opacity-40'}"
                        ><ThumbsDown size={15} /></button>
                        <button
                          onclick={() => startCorrection(m)}
                          aria-label="Suggest a correction"
                          aria-pressed={!!m.correcting}
                          class="flex h-8 w-8 cursor-pointer items-center justify-center rounded-lg transition-colors duration-200 focus-visible:ring-2 focus-visible:ring-accent
                            {m.correcting ? 'bg-accent-soft text-accent' : 'text-ink-3 hover:bg-surface-2 hover:text-ink'}"
                        ><Pencil size={15} /></button>
                      </div>
                    </div>

                    <!-- inline correction editor -->
                    {#if m.correcting}
                      <div class="mt-2.5 rounded-xl border-[0.5px] border-line bg-surface-2 p-2.5">
                        <label class="mb-1.5 block px-0.5 text-[11px] font-bold uppercase tracking-[0.05em] text-ink-3" for="corr-{i}">
                          What's the correct answer?
                        </label>
                        <div class="flex items-end gap-2">
                          <textarea
                            id="corr-{i}"
                            value={m.correctionText ?? ''}
                            oninput={(e) => setCorrectionText(m, e.currentTarget.value)}
                            rows="2"
                            placeholder="Describe the correct answer…"
                            class="max-h-[140px] min-h-[40px] w-full resize-none rounded-lg border border-line bg-surface px-2.5 py-2 text-[12px] leading-relaxed text-ink outline-none transition-colors duration-200 placeholder:text-ink-3 focus:border-accent/50"
                          ></textarea>
                          <button
                            onclick={() => submitCorrection(m)}
                            disabled={!(m.correctionText ?? '').trim()}
                            aria-label="Save correction"
                            class="flex h-9 w-9 flex-shrink-0 cursor-pointer items-center justify-center rounded-[10px] bg-accent text-white transition-colors duration-200 hover:bg-accent-hover focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-default disabled:bg-line-2"
                          >
                            <Check size={16} />
                          </button>
                        </div>
                      </div>
                    {/if}

                    <!-- feedback status line -->
                    {#if m.correctionSaved}
                      <div class="mt-1.5 flex items-center gap-1.5 text-[12px] text-ink-3"><Check size={12} class="text-success" />Correction saved</div>
                    {:else if m.feedback}
                      <div class="mt-1.5 flex items-center gap-1.5 text-[12px] text-ink-3"><Check size={12} class="text-success" />Thanks — noted</div>
                    {:else if m.feedbackError}
                      <div class="mt-1.5 text-[12px] text-danger">Couldn't save feedback — try again.</div>
                    {/if}

                    <!-- follow-up chips on the latest answer -->
                    {#if !busy && m === messages[messages.length - 1]}
                      <div class="mt-3.5 flex flex-wrap gap-2 border-t border-line pt-3.5">
                        {#each FOLLOWUPS as f}
                          <button
                            onclick={() => {
                              input = f.q;
                              tick().then(() => ta?.focus());
                            }}
                            class="flex items-center gap-1.5 rounded-[9px] border border-line bg-surface px-3 py-1.5 text-[13px] text-ink-2 transition-colors hover:border-accent hover:bg-accent-soft hover:text-accent"
                          >
                            <f.icon size={14} />{f.label}
                          </button>
                        {/each}
                      </div>
                    {/if}
                  {/if}
                </div>
              </div>
            {/if}
          {/each}
        </div>
      {/if}
    </div>

    <!-- composer -->
    <div class="px-6 pb-5">
      <div
        class="mx-auto max-w-[740px] rounded-[22px] border border-line-2 bg-surface p-2 shadow-[var(--shadow-pop)] focus-within:border-accent/50"
      >
        <textarea
          bind:this={ta}
          bind:value={input}
          oninput={grow}
          onkeydown={onKey}
          rows="1"
          aria-label="Message the pharmacy assistant"
          placeholder="Ask about stock, prices, substitutes — English or မြန်မာ…"
          class="max-h-[200px] w-full resize-none border-0 bg-transparent px-3 pb-1 pt-2.5 text-[15.5px] leading-relaxed text-ink outline-none placeholder:text-ink-3"
        ></textarea>
        <div class="flex items-center gap-1.5 px-1 pb-0.5">
          <button aria-label="Attach" class="flex items-center gap-1.5 rounded-[10px] px-2.5 py-2 text-[13px] text-ink-2 hover:bg-surface-2">
            <Paperclip size={16} />
          </button>
          <button class="flex items-center gap-1.5 rounded-[10px] px-2.5 py-2 text-[13px] text-ink-2 hover:bg-surface-2">
            <Store size={16} /> All stores
          </button>
          <button class="flex items-center gap-1.5 rounded-[10px] px-2.5 py-2 text-[13px] text-ink-2 hover:bg-surface-2">
            <Languages size={16} /> Auto
          </button>
          <button
            onclick={() => send()}
            disabled={busy || !token || !input.trim()}
            aria-label="Send"
            class="ml-auto flex h-9 w-9 items-center justify-center rounded-[11px] bg-accent text-white transition-colors hover:bg-accent-hover disabled:cursor-default disabled:bg-line-2"
          >
            <ArrowUp size={18} />
          </button>
        </div>
      </div>
      <p class="mx-auto mt-2 max-w-[740px] text-center text-[11.5px] text-ink-3">
        City Pharma can make mistakes. Verify stock &amp; dosage with a licensed pharmacist.
      </p>
    </div>
  </div>

  <!-- SOURCE DRAWER (opens on clicking an article-code chip) -->
  {#if drawerOpen}
    <div
      class="fixed inset-0 z-40 bg-black/30"
      onclick={() => (drawerOpen = false)}
      aria-hidden="true"
    ></div>
  {/if}
  <aside
    class="fixed bottom-0 right-0 top-0 z-50 flex w-[360px] max-w-[90vw] flex-col border-l border-line bg-surface shadow-[var(--shadow-pop)] transition-transform duration-200
      {drawerOpen ? 'translate-x-0' : 'translate-x-full'}"
  >
    <div class="flex items-center gap-2.5 border-b border-line px-[18px] py-4">
      <span class="flex h-8 w-8 items-center justify-center rounded-lg bg-accent-soft text-accent">
        <FileText size={15} />
      </span>
      <div class="min-w-0 flex-1">
        <div class="truncate text-[14px] font-semibold text-ink">Source · {drawerCode}</div>
        <div class="text-[11.5px] text-ink-3">where this answer came from</div>
      </div>
      <button
        onclick={() => (drawerOpen = false)}
        aria-label="Close"
        class="flex h-8 w-8 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2"
      >
        <X size={18} />
      </button>
    </div>
    <div class="flex-1 overflow-y-auto p-[18px]">
      {#if drawerLoading}
        <div class="space-y-2">
          {#each Array(5) as _}<div class="skel" style="height:16px"></div>{/each}
        </div>
      {:else if drawerData}
        {#each Object.entries(drawerData.article ?? {}) as [k, v]}
          {#if v !== null && v !== undefined && v !== ''}
            <div class="flex justify-between gap-3 border-b border-line py-2 text-[13.5px]">
              <span class="text-ink-2">{k.replace(/_/g, ' ')}</span>
              <span class="text-right font-semibold text-ink">{v}</span>
            </div>
          {/if}
        {/each}
        <div class="flex justify-between gap-3 border-b border-line py-2 text-[13.5px]">
          <span class="text-ink-2">total stock</span>
          <span class="text-right font-semibold text-ink tnum">{(drawerData.total_stock ?? 0).toLocaleString()}</span>
        </div>
        <div class="flex justify-between gap-3 border-b border-line py-2 text-[13.5px]">
          <span class="text-ink-2">in stock at</span>
          <span class="text-right font-semibold text-ink tnum">{drawerData.site_count ?? 0} sites</span>
        </div>

        {#if drawerData.sites?.length}
          <div class="mb-1.5 mt-4 text-[10.5px] font-bold uppercase tracking-[0.05em] text-ink-3">
            Top branches
          </div>
          {#each drawerData.sites.slice(0, 6) as s}
            <div class="flex justify-between gap-3 border-b border-line py-1.5 text-[13px]">
              <span class="font-mono text-ink-2">{s.site_code}</span>
              <span class="font-semibold text-ink tnum">{(s.stock_qty ?? 0).toLocaleString()}</span>
            </div>
          {/each}
        {/if}

        <a
          href="{appBase}/data"
          class="mt-4 flex items-center justify-center gap-2 rounded-[11px] border border-line bg-surface px-4 py-2.5 text-[13px] font-semibold text-ink hover:bg-surface-2"
        >
          Open in Data <ExternalLink size={14} />
        </a>
      {:else}
        <p class="text-[13.5px] text-ink-2">No catalog record found for {drawerCode}.</p>
      {/if}
    </div>
  </aside>
</div>
