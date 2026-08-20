<script>
  import { onMount } from 'svelte';
  import PageHeader from '$lib/PageHeader.svelte';
  import { base } from '$app/paths';
  import { ApiError, getJSON } from '$lib/api.js';
  import ErrorState from '$lib/ErrorState.svelte';

  // Categorical relation colours, drawn from the CityCare v2 palette so the
  // canvas does not sit in a different colour world from the page around it.
  // These are CATEGORIES, not a series — they encode which kind of edge this
  // is, so they have to stay distinguishable from each other, and each is also
  // rendered as text on a 12% tint (see `alpha`), so each clears AA on the
  // light surfaces: indigo 10.51, amber 6.86, purple 9.50, slate 5.66.
  // The relation is always labelled as well, so colour is never the only cue.
  const REL = {
    generic: '#2F3293', // indigo (the accent)
    contains: '#8A4A04', // amber (ingredient)
    treats: '#5B2D8E' // purple — the palette carries no green
  };
  // Node fills by type.
  // `cat` appears only when the graph falls back to the structural layer —
  // see `seeded_from` below. Fixed hexes, like the three that were already
  // here: these are SVG presentation attributes, where `var(--token)` does
  // not resolve. Noted rather than silently left as a token that never applies.
  const TYPE_COL = { drug: '#23266F', ing: '#8A4A04', cond: '#5B2D8E', cat: '#008A86' };
  const TYPE_LABEL = { drug: 'Medicine', ing: 'Ingredient', cond: 'Condition', cat: 'Category' };

  const EDGE_META = [
    { key: 'has_generic', label: 'shared generic', color: REL.generic },
    { key: 'contains', label: 'ingredients', color: REL.contains },
    { key: 'in_category', label: 'categories', color: '#61667E' },
    { key: 'treats', label: 'treats', color: REL.treats }
  ];

  let counts = $state(null);
  let countsError = $state(null);
  let panel = $state(null); // node detail payload from /admin/graph/node
  /** Which layer the drawing came from: 'treats' | 'structure' | 'none'.
   *  The page used to render nothing and say "no graph data" while its own
   *  counters showed 16,021 edges — the two disagreed and neither explained
   *  the other. This is what makes the difference visible. */
  let seededFrom = $state(null);
  let panelError = $state(null);
  let panelLoading = $state(false);
  let graphEmpty = $state(false);
  // The error OBJECT (an ApiError carries .status), never a pre-baked string:
  // 401, 403 and a dead backend are three different things to tell someone.
  let graphError = $state(null);

  let svgEl; // bound <svg>

  async function loadCounts() {
    countsError = null;
    try {
      counts = await getJSON('/admin/graph');
    } catch (e) {
      counts = null;
      countsError = e;
    }
  }

  const short = (s) => (s && s.length > 15 ? s.slice(0, 14) + '…' : s ?? '');
  const fmt = (n) => (n === null || n === undefined ? '' : Number(n).toLocaleString());
  const relColor = (rel) => REL[rel] ?? '#C8CBDC';

  // Hex -> rgba for ~12% alpha chip backgrounds.
  function alpha(hex, a) {
    const h = hex.replace('#', '');
    const r = parseInt(h.slice(0, 2), 16);
    const g = parseInt(h.slice(2, 4), 16);
    const b = parseInt(h.slice(4, 6), 16);
    return `rgba(${r},${g},${b},${a})`;
  }

  // d3 handles, kept in module scope for the click handler / cleanup.
  let d3sel = null; // { node, link, label } selections
  let simulation = null;
  let currentCode = $state(null);

  // zoom/fit machinery, populated by buildGraph
  let d3ref = null;
  let zoomBehavior = null;
  let svgSel = null; // d3 selection of <svg>
  let rootSel = null; // d3 selection of the transformed <g>
  let lastData = null;

  let fullscreen = $state(false);
  let nodeLimit = $state(80);
  let loadingGraph = $state(false);

  function zoomBy(k) {
    if (!svgSel || !zoomBehavior) return;
    svgSel.transition().duration(200).call(zoomBehavior.scaleBy, k);
  }

  // Fit the whole graph into view: measure node bounds, compute transform.
  function fitView() {
    if (!svgSel || !zoomBehavior || !rootSel || !lastData?.nodes?.length || !d3ref) return;
    const nodes = lastData.nodes;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of nodes) {
      if (n.x == null) continue;
      if (n.x < minX) minX = n.x;
      if (n.y < minY) minY = n.y;
      if (n.x > maxX) maxX = n.x;
      if (n.y > maxY) maxY = n.y;
    }
    if (!isFinite(minX)) return;
    const w = svgEl.clientWidth || 760;
    const h = svgEl.clientHeight || 520;
    const gw = Math.max(maxX - minX, 1);
    const gh = Math.max(maxY - minY, 1);
    const pad = 40;
    const scale = Math.min(4, Math.max(0.12, Math.min((w - pad * 2) / gw, (h - pad * 2) / gh)));
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    const t = d3ref.zoomIdentity.translate(w / 2 - scale * cx, h / 2 - scale * cy).scale(scale);
    svgSel.transition().duration(400).call(zoomBehavior.transform, t);
  }

  function resetZoom() {
    if (!svgSel || !zoomBehavior || !d3ref) return;
    svgSel.transition().duration(300).call(zoomBehavior.transform, d3ref.zoomIdentity);
  }

  function toggleFullscreen() {
    fullscreen = !fullscreen;
    // Don't rebuild — just resize the viewBox to the new container and re-frame.
    // Rebuilding would re-fling settled nodes; keeping positions is smoother.
    setTimeout(() => {
      if (!svgSel || !svgEl) return;
      const w = svgEl.clientWidth || 760;
      const h = svgEl.clientHeight || 520;
      svgSel.attr('viewBox', `0 0 ${w} ${h}`);
      fitView();
    }, 80);
  }

  async function reloadGraph(limit) {
    nodeLimit = limit;
    loadingGraph = true;
    graphError = null;
    try {
      // Also the Retry path from the failure panel, so it must be able to
      // recover from d3 never having loaded — a bare `if (!d3ref) return`
      // here made Retry a button that silently did nothing.
      if (!d3ref) d3ref = await whenD3Ready();
      const data = await getJSON(`/admin/graph/overview?limit=${limit}`);
      seededFrom = data?.seeded_from ?? null;
      if (!data?.nodes?.length) { graphEmpty = true; return; }
      graphEmpty = false;
      buildGraph(d3ref, data);
    } catch (e) {
      // A d3 timeout is not an HTTP failure; give it status 0 and its own words.
      graphError =
        e instanceof ApiError
          ? e
          : new ApiError(0, e?.message || 'The graph could not be drawn.');
    } finally {
      loadingGraph = false;
    }
  }

  function highlight(centerId) {
    if (!d3sel) return;
    const { node, link, label } = d3sel;
    if (!centerId) {
      node.attr('opacity', 1);
      label.attr('opacity', (d) => (d.type === 'drug' ? 0.8 : 1));
      link.attr('stroke', '#C8CBDC').attr('stroke-opacity', 0.4);
      return;
    }
    const neighbors = new Set([centerId]);
    link.each((l) => {
      const s = typeof l.source === 'object' ? l.source.id : l.source;
      const t = typeof l.target === 'object' ? l.target.id : l.target;
      if (s === centerId) neighbors.add(t);
      if (t === centerId) neighbors.add(s);
    });
    node.attr('opacity', (d) => (neighbors.has(d.id) ? 1 : 0.12));
    label.attr('opacity', (d) => (neighbors.has(d.id) ? (d.type === 'drug' ? 0.85 : 1) : 0.12));
    link.attr('stroke', (l) => {
      const s = typeof l.source === 'object' ? l.source.id : l.source;
      const t = typeof l.target === 'object' ? l.target.id : l.target;
      return s === centerId || t === centerId ? relColor(l.rel) : '#C8CBDC';
    }).attr('stroke-opacity', (l) => {
      const s = typeof l.source === 'object' ? l.source.id : l.source;
      const t = typeof l.target === 'object' ? l.target.id : l.target;
      return s === centerId || t === centerId ? 0.85 : 0.06;
    });
  }

  async function openNode(code) {
    panelLoading = true;
    panelError = null;
    currentCode = code;
    highlight(code);
    try {
      panel = await getJSON(`/admin/graph/node?code=${encodeURIComponent(code)}`);
    } catch (e) {
      // The old fallback built a fake node — empty `contains`/`treats`/
      // `same_generic` arrays, which the footer then totalled as "0 graph
      // links". A medicine with no connections and a medicine we could not
      // load look identical that way. Carry the failure instead.
      panel = null;
      panelError = e;
    } finally {
      panelLoading = false;
    }
  }

  function closePanel() {
    panel = null;
    panelError = null;
    currentCode = null;
    highlight(null);
  }

  function buildGraph(d3, data) {
    d3ref = d3;
    lastData = data;
    const width = svgEl.clientWidth || 760;
    const height = svgEl.clientHeight || 520;

    const svg = d3.select(svgEl).attr('viewBox', `0 0 ${width} ${height}`);
    svg.selectAll('*').remove();

    const root = svg.append('g');

    const zoom = d3
      .zoom()
      .scaleExtent([0.1, 6])
      .on('zoom', (ev) => root.attr('transform', ev.transform));
    svg.call(zoom).on('dblclick.zoom', null);

    svgSel = svg;
    rootSel = root;
    zoomBehavior = zoom;

    const radius = (d) => (d.type === 'drug' ? 6 : d.type === 'ing' ? 10 : 11);  // hubs 10-11

    const sim = d3
      .forceSimulation(data.nodes)
      .force(
        'link',
        d3
          .forceLink(data.links)
          .id((d) => d.id)
          .distance((l) => (l.rel === 'generic' ? 34 : 58))
      )
      .force('charge', d3.forceManyBody().strength(-140))
      .force('center', d3.forceCenter(width / 2, height / 2))
      // gentle pull toward centre keeps disconnected clusters from flying off
      .force('x', d3.forceX(width / 2).strength(0.04))
      .force('y', d3.forceY(height / 2).strength(0.04))
      .force('collide', d3.forceCollide().radius((d) => radius(d) + 6));
    simulation = sim;

    const link = root
      .append('g')
      .attr('stroke', '#C8CBDC')
      .attr('stroke-opacity', 0.4)
      .selectAll('line')
      .data(data.links)
      .join('line')
      .attr('stroke-width', 1.2);

    const node = root
      .append('g')
      .selectAll('circle')
      .data(data.nodes)
      .join('circle')
      .attr('r', radius)
      .attr('fill', (d) => TYPE_COL[d.type] ?? 'var(--color-accent)')
      .attr('stroke', '#fff')
      .attr('stroke-width', 1.5)
      .attr('cursor', (d) => (d.type === 'drug' ? 'pointer' : 'default'));

    const label = root
      .append('g')
      .selectAll('text')
      .data(data.nodes)
      .join('text')
      .text((d) => short(d.label))
      .attr('font-size', (d) => (d.type === 'drug' ? 8.5 : 10))
      .attr('font-weight', (d) => (d.type === 'drug' ? 400 : 600))
      .attr('fill', (d) => (d.type === 'drug' ? 'var(--color-ink-2)' : TYPE_COL[d.type]))
      .attr('opacity', (d) => (d.type === 'drug' ? 0.8 : 1))
      .attr('dx', (d) => radius(d) + 3)
      .attr('dy', 3)
      .attr('pointer-events', 'none');

    d3sel = { node, link, label };

    node
      .on('mouseenter', (_ev, d) => {
        if (!currentCode) highlight(d.id);
      })
      .on('mouseleave', () => {
        highlight(currentCode);
      })
      .on('click', (_ev, d) => {
        if (d.type === 'drug') openNode(d.id);
      });

    node.call(
      d3
        .drag()
        .on('start', (ev, d) => {
          if (!ev.active) sim.alphaTarget(0.3).restart();
          d.fx = d.x;
          d.fy = d.y;
        })
        .on('drag', (ev, d) => {
          d.fx = ev.x;
          d.fy = ev.y;
        })
        .on('end', (ev, d) => {
          if (!ev.active) sim.alphaTarget(0);
          d.fx = null;
          d.fy = null;
        })
    );

    sim.on('tick', () => {
      link
        .attr('x1', (d) => d.source.x)
        .attr('y1', (d) => d.source.y)
        .attr('x2', (d) => d.target.x)
        .attr('y2', (d) => d.target.y);
      node.attr('cx', (d) => d.x).attr('cy', (d) => d.y);
      label.attr('x', (d) => d.x).attr('y', (d) => d.y);
    });

    // once the layout settles, frame everything in view
    sim.on('end', () => fitView());
    setTimeout(() => fitView(), 1200);
  }

  // Wait for the globally-loaded d3 (app.html CDN script) to be ready.
  function whenD3Ready() {
    return new Promise((resolve, reject) => {
      if (window.d3) return resolve(window.d3);
      let tries = 0;
      const t = setInterval(() => {
        if (window.d3) {
          clearInterval(t);
          resolve(window.d3);
        } else if (++tries > 40) {
          clearInterval(t);
          reject(new Error('d3 failed to load'));
        }
      }, 50);
    });
  }

  onMount(() => {
    loadCounts();
    let cleanup = () => {};
    (async () => {
      let d3;
      try {
        d3 = await whenD3Ready();
      } catch {
        // Not a backend failure at all — the drawing library never loaded.
        // Status 0 with its own message keeps it out of the HTTP wording.
        graphError = new ApiError(0, 'The graph drawing library (d3) did not load in this page.');
        return;
      }
      try {
        const data = await getJSON('/admin/graph/overview?limit=80');
        // The mount path, not just the Retry/limit path. Setting `seededFrom`
        // in only one of the two loaders is why the layer note stayed hidden on
        // first load and appeared only after clicking a node-limit button.
        seededFrom = data?.seeded_from ?? null;
        if (!data?.nodes?.length) {
          graphEmpty = true;
          return;
        }
        buildGraph(d3, data);
        cleanup = () => {
          if (simulation) simulation.stop();
        };
      } catch (e) {
        graphError = e;
      }
    })();
    return () => cleanup();
  });
</script>

<PageHeader
  title="Knowledge graph"
  subtitle="The full drug graph behind GraphRAG — every medicine linked to its ingredients and the conditions it treats. Click a medicine to highlight its connections and open its details."
>
  {#snippet meta()}
    {#if counts}
      {#each EDGE_META as e}
        <span class="inline-flex items-center gap-1.5 rounded-full bg-surface-2 px-2.5 py-1 text-meta text-ink-2">
          <span class="h-2 w-2 rounded-full" style="background:{e.color}"></span>
          {e.label}
          <span class="tnum">{fmt(counts[e.key])}</span>
        </span>
      {/each}
    {:else if countsError}
      <!-- Say WHY they are missing. "Unavailable" alone reads as "the graph has
           no edges" to anyone who has not seen this page working. -->
      <span class="text-meta text-ink-3">
        edge counts unavailable{countsError.status === 401
          ? ' — session expired'
          : countsError.status === 403
            ? ' — not permitted'
            : countsError.status
              ? ` — backend answered ${countsError.status}`
              : ' — no response from the backend'}
      </span>
    {/if}
  {/snippet}
</PageHeader>

<div class="mb-2 flex flex-wrap items-center justify-between gap-2">
  <div class="min-w-0">
    <p class="text-meta text-ink-3">scroll to zoom · drag to pan · click a medicine</p>
    <!-- Which layer is on screen. Without this the page draws the structural
         graph while the counters say `treats 0`, and nothing connects the two. -->
    {#if seededFrom === 'structure'}
      <p class="mt-1 text-label text-ink-3">
        Showing the <span class="font-medium text-ink-2">structural</span> graph — ingredients,
        shared generics and categories, built from the catalog. The
        <span class="font-medium text-ink-2">treats</span> layer is read out of indication text
        by the model and has not been built, so conditions are not drawn.
      </p>
    {/if}
  </div>
  <div class="flex items-center gap-1.5">
    <span class="text-meta text-ink-3">nodes</span>
    {#each [80, 200, 500, 1000] as lim}
      <button
        onclick={() => reloadGraph(lim)}
        disabled={loadingGraph}
        class="cursor-pointer rounded-control px-2 py-1 text-meta transition-colors disabled:opacity-50 {nodeLimit === lim ? 'bg-accent text-on-accent' : 'bg-surface-2 text-ink-2 hover:bg-surface'}"
      >{lim}</button>
    {/each}
    {#if loadingGraph}<span class="text-meta text-ink-3">loading…</span>{/if}
  </div>
</div>

<div
  class="flex overflow-hidden rounded-card border border-line bg-surface"
  class:graph-fs={fullscreen}
>
  <!-- LEFT: force graph -->
  <div class="relative flex-1">
    {#if graphError}
      <div class="flex items-center justify-center px-6" style="height:{fullscreen ? '100vh' : '70vh'}">
        <div class="w-full max-w-[520px]">
          <ErrorState
            error={graphError}
            retry={() => reloadGraph(nodeLimit)}
            what="the knowledge graph"
          />
        </div>
      </div>
    {:else if graphEmpty}
      <!-- What is missing, not just that something is. The old copy said "no
           graph data is available yet" beside three non-zero counters, which is
           the one thing a reader cannot reconcile. -->
      <div class="flex items-center justify-center px-6 text-center" style="height:{fullscreen ? '100vh' : '70vh'}">
        <div class="max-w-[420px]">
          <p class="text-body font-semibold text-ink">Nothing to draw</p>
          <p class="mt-1.5 text-body-sm leading-[1.6] text-ink-2">
            No edges have been built yet. They are derived from the catalog on ingest,
            so this fills in once a catalog file has been loaded on
            <a href={base + '/ftp'} class="font-medium text-accent hover:underline">Data pipeline</a>.
          </p>
        </div>
      </div>
    {/if}
    <svg
      bind:this={svgEl}
      class="w-full"
      style="height:{fullscreen ? '100vh' : '70vh'}"
      class:hidden={graphError || graphEmpty}
      role="img"
      aria-label="Force-directed knowledge graph of medicines, ingredients, and conditions"
    ></svg>

    <!-- zoom / fit controls -->
    {#if !graphError && !graphEmpty}
      <div class="absolute right-3 top-3 flex flex-col gap-1 rounded-card border border-line bg-surface/90 p-1 shadow-sm backdrop-blur">
        <button onclick={() => zoomBy(1.4)} aria-label="Zoom in" title="Zoom in"
          class="flex h-8 w-8 cursor-pointer items-center justify-center rounded-panel text-title leading-none text-ink-2 hover:bg-surface-2 hover:text-ink">+</button>
        <button onclick={() => zoomBy(1 / 1.4)} aria-label="Zoom out" title="Zoom out"
          class="flex h-8 w-8 cursor-pointer items-center justify-center rounded-panel text-title leading-none text-ink-2 hover:bg-surface-2 hover:text-ink">−</button>
        <button onclick={fitView} aria-label="Fit to view" title="Fit to view"
          class="flex h-8 w-8 cursor-pointer items-center justify-center rounded-panel text-ink-2 hover:bg-surface-2 hover:text-ink">
          <svg aria-hidden="true" focusable="false" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/></svg>
        </button>
        <button onclick={resetZoom} aria-label="Reset zoom" title="Reset zoom"
          class="flex h-8 w-8 cursor-pointer items-center justify-center rounded-panel text-ink-2 hover:bg-surface-2 hover:text-ink">
          <svg aria-hidden="true" focusable="false" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>
        </button>
        <div class="mx-1 my-0.5 border-t border-line"></div>
        <button onclick={toggleFullscreen} aria-label="Toggle fullscreen" title={fullscreen ? 'Exit fullscreen' : 'Fullscreen'}
          class="flex h-8 w-8 cursor-pointer items-center justify-center rounded-panel text-ink-2 hover:bg-surface-2 hover:text-ink">
          {#if fullscreen}
            <svg aria-hidden="true" focusable="false" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3v3a2 2 0 0 1-2 2H3"/><path d="M21 8h-3a2 2 0 0 1-2-2V3"/><path d="M3 16h3a2 2 0 0 1 2 2v3"/><path d="M16 21v-3a2 2 0 0 1 2-2h3"/></svg>
          {:else}
            <svg aria-hidden="true" focusable="false" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/></svg>
          {/if}
        </button>
      </div>
    {/if}
  </div>

  <!-- RIGHT: slide-in detail panel -->
  <div
    class="shrink-0 overflow-hidden border-l border-line bg-surface-2 transition-[width] duration-200"
    style="width:{panel || panelError ? 320 : 0}px"
  >
    <div class="w-[300px] overflow-y-auto p-4" style="height:{fullscreen ? '100vh' : '70vh'}">
      {#if panelError}
        <div class="flex items-start gap-2">
          <div class="min-w-0">
            <h2 class="truncate text-body font-semibold text-ink">{currentCode}</h2>
          </div>
          <button
            onclick={closePanel}
            class="ml-auto shrink-0 cursor-pointer rounded-control px-1.5 py-0.5 text-meta text-ink-3 hover:bg-surface hover:text-ink"
            aria-label="Close details"
          >× close</button>
        </div>
        <div class="mt-3">
          <ErrorState
            error={panelError}
            retry={() => openNode(currentCode)}
            what="this medicine's connections"
          />
        </div>
      {:else if panel}
        {@const sg = panel.same_generic ?? []}
        {@const cc = panel.contains ?? []}
        {@const tt = panel.treats ?? []}
        {@const total = cc.length + tt.length + sg.length}
        <div class="flex items-start gap-2">
          <span class="mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full" style="background:{TYPE_COL.drug}"></span>
          <div class="min-w-0">
            <h2 class="truncate text-body font-semibold text-ink">{panel.brand_name ?? currentCode}</h2>
            <p class="text-meta text-ink-3">{TYPE_LABEL.drug}{panel.generic_name ? ` · ${panel.generic_name}` : ''}</p>
          </div>
          <button
            onclick={closePanel}
            class="ml-auto shrink-0 cursor-pointer rounded-control px-1.5 py-0.5 text-meta text-ink-3 hover:bg-surface hover:text-ink"
            aria-label="Close details"
          >× close</button>
        </div>

        {#if panelLoading}
          <p class="mt-4 text-body-sm text-ink-3">Loading…</p>
        {:else}
          {#if panel.total_stock !== null && panel.total_stock !== undefined}
            <p class="mt-3 text-meta text-ink-2">
              In stock: <span class="tnum font-medium text-ink">{fmt(panel.total_stock)}</span> units ·
              <span class="tnum font-medium text-ink">{fmt(panel.site_count)}</span> sites
            </p>
          {/if}

          {#if cc.length}
            <div class="mt-4">
              <h3 class="text-label font-medium uppercase tracking-wide text-ink-3">Contains</h3>
              <div class="mt-1.5 flex flex-wrap gap-1.5">
                {#each cc as ing}
                  <span class="rounded-full px-2 py-0.5 text-meta" style="background:{alpha(REL.contains, 0.12)};color:{REL.contains}">{ing}</span>
                {/each}
              </div>
            </div>
          {/if}

          {#if tt.length}
            <div class="mt-4">
              <h3 class="text-label font-medium uppercase tracking-wide text-ink-3">Treats</h3>
              <div class="mt-1.5 flex flex-wrap gap-1.5">
                {#each tt as cond}
                  <span class="rounded-full px-2 py-0.5 text-meta" style="background:{alpha(REL.treats, 0.12)};color:{REL.treats}">{cond}</span>
                {/each}
              </div>
            </div>
          {/if}

          {#if sg.length}
            <div class="mt-4">
              <h3 class="text-label font-medium uppercase tracking-wide text-ink-3">Same generic</h3>
              <div class="mt-1.5 flex flex-wrap gap-1.5">
                {#each sg as g}
                  <button
                    onclick={() => openNode(g.article_code)}
                    class="cursor-pointer rounded-full px-2 py-0.5 text-meta transition-opacity hover:opacity-80"
                    style="background:{alpha(REL.generic, 0.12)};color:{REL.generic}"
                  >{g.brand_name ?? g.article_code}</button>
                {/each}
              </div>
            </div>
          {/if}

          <div class="mt-5 flex items-center justify-between border-t border-line pt-3 text-meta text-ink-3">
            <span><span class="tnum text-ink-2">{total}</span> graph links</span>
            <button onclick={closePanel} class="cursor-pointer hover:text-ink">× close</button>
          </div>
        {/if}
      {/if}
    </div>
  </div>
</div>

<style>
  /* Fullscreen: lift the graph card out of page flow to cover the viewport. */
  .graph-fs {
    position: fixed;
    inset: 0;
    z-index: 50;
    border-radius: 0;
    border: none;
  }
</style>
