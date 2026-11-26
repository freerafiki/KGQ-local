// Search page entry point.
//
// The root component's template is the inline markup in index.html (the full
// Vue build compiles it at mount); this module only wires the state, the API
// call, the three-layer mode switcher (strategy / field / hint) and the two
// child components (cards, contributor facets).

import {
  createApp, computed, onMounted, onBeforeUnmount, nextTick, reactive, ref, toRefs, watch,
} from '../vendor/vue.esm-browser.prod.js';
import {
  store, visibleResults, groupedResults, strongGroups, farGroups, farCount, isStrong, clearFilters,
  saveState, restoreState, pruneFacets,
  FORMAL_ENTRIES, PROJECT_ENTRIES, INCLUDE_ENTRIES, PURPOSE_ENTRIES,
  TAXONOMY_GROUPS,
  STRATEGY_ENTRIES, FIELD_ENTRIES,
  DEFAULT_MODE, isMode, setMode, strategyOf, modeLabel, hintForMode,
  yearDomain, filterCounts, activeFilters,
} from './state.js';
import { fetchSearch } from './api.js';
import { ftBounds, clampInt, queryTerms } from './dom.js';
import ResultCard from './components/result-card.js';
import ActorFacet from './components/actor-facet.js';

// Deep links (?q=...&mode=...&purpose=...&docType=...&include=... plus the
// ?phenomenon=, ?topic= and ?area= taxonomy params): used by the node page
// ("search this author", drill-down from a purpose/doc-type chip) and by
// shared URLs. An unknown mode is ignored, so old links stay safe.
function readUrlQuery() {
  const params = new URLSearchParams(location.search);
  const mode = params.get('mode');
  const q = (params.get('q') || '').trim();
  return {
    q,
    mode: isMode(mode) ? mode : '',
    purposes: readListParam(params, 'purpose', PURPOSE_ENTRIES.map(e => e.value)),
    // Phenomena / Topic / Geographic area: one param per taxonomy group
    // (?phenomenon=, ?topic=, ?area=), values matched against the group's
    // own list — see TAXONOMY_GROUPS in state.js.
    ...Object.fromEntries(
      TAXONOMY_GROUPS.map(g => [g.key, readListParam(params, g.param, g.values)])),
    docTypes: readListParam(params, 'docType',
      [...FORMAL_ENTRIES, ...PROJECT_ENTRIES].map(e => e.value)),
    includes: readListParam(params, 'include', INCLUDE_ENTRIES.map(e => e.value)),
    year: readYearParam(params),
  };
}

// ?year=2021 sets both bounds, ?year=2019-2022 sets each; order-agnostic,
// anything else is ignored (= no constraint). The node page's Release year
// link produces the single-year form.
function readYearParam(params) {
  if (!params.has('year')) return null;
  const m = params.get('year').trim().match(/^(\d{4})(?:-(\d{4}))?$/);
  if (!m) return null;
  const a = Number(m[1]);
  const b = m[2] ? Number(m[2]) : a;
  return { from: Math.min(a, b), to: Math.max(a, b) };
}

// One comma-separated list param: null = absent (no constraint), otherwise
// the listed values that are known to the filter group (garbage dropped).
function readListParam(params, key, known) {
  if (!params.has(key)) return null;
  return params.get(key).split(',').map(s => s.trim()).filter(s => known.includes(s));
}

// The purpose/docType/include params REPLACE their own filter group's
// selection (docType/purpose/...: the listed entries become the ONLY ones
// shown; include: the listed Rec/Gap types are switched on), and a present
// param with no valid value selects nothing. Each param only touches its OWN group — an inbound ?docType= link
// must not silently clear "Include also", which that link never carried.
// People and Institutions are not URL state — their names only exist in the
// result set, so person links go through ?mode=author&q=... instead.
function applyUrlFilters(url) {
  if (url.purposes) {
    for (const e of PURPOSE_ENTRIES) store.purposes[e.value] = url.purposes.includes(e.value);
  }
  for (const g of TAXONOMY_GROUPS) {
    const picked = url[g.key];
    if (!picked) continue;             // absent param: that group stays as it is
    for (const v of g.values) store[g.key][v] = picked.includes(v);
  }
  if (url.docTypes) {
    for (const e of FORMAL_ENTRIES) store.formal[e.value] = url.docTypes.includes(e.value);
    for (const e of PROJECT_ENTRIES) store.types[e.value] = url.docTypes.includes(e.value);
  }
  if (url.includes) {
    for (const e of INCLUDE_ENTRIES) store.types[e.value] = url.includes.includes(e.value);
  }
  if (url.year) {
    store.year.from = url.year.from;
    store.year.to = url.year.to;
  }
}

// Keep the address bar shareable: only non-default values are written, and
// replaceState avoids piling one history entry per search. Called only
// after a successful search, and it writes what the RESULTS on screen came
// from (submittedMode) — a selection made while the request was in flight
// must not leak into the URL.
function syncUrl() {
  const params = new URLSearchParams();
  if (store.submittedQuery) params.set('q', store.submittedQuery);
  if (store.submittedMode !== DEFAULT_MODE) params.set('mode', store.submittedMode);
  // Selected filter entries ride along (nothing selected = nothing written),
  // so a copied URL reopens the same view. (They are rewritten on each
  // search — clicking an entry alone does not touch the URL yet.)
  const purposes = PURPOSE_ENTRIES.filter(e => store.purposes[e.value]).map(e => e.value);
  if (purposes.length) params.set('purpose', purposes.join(','));
  for (const g of TAXONOMY_GROUPS) {
    const picked = g.values.filter(v => store[g.key][v]);
    if (picked.length) params.set(g.param, picked.join(','));
  }
  const docTypes = [
    ...FORMAL_ENTRIES.filter(e => store.formal[e.value]).map(e => e.value),
    ...PROJECT_ENTRIES.filter(e => store.types[e.value]).map(e => e.value),
  ];
  if (docTypes.length) {
    params.set('docType', docTypes.join(','));
  }
  const includes = INCLUDE_ENTRIES.filter(e => store.types[e.value]).map(e => e.value);
  if (includes.length) {
    params.set('include', includes.join(','));
  }
  // Time: only a narrowed range is written (?year=2019-2022; one year as
  // ?year=2021), so a shared URL reopens the same window.
  const dom = yearDomain.value;
  if (dom.min !== null) {
    const from = store.year.from ?? dom.min;
    const to = store.year.to ?? dom.max;
    if (from !== dom.min || to !== dom.max) {
      params.set('year', from === to ? String(from) : from + '-' + to);
    }
  }
  const qs = params.toString();
  history.replaceState(null, '', location.pathname + (qs ? '?' + qs : '') + location.hash);
}

const SearchPage = {
  components: { ResultCard, ActorFacet },
  setup() {
    // Template ref on the search bar, for the empty-query focus.
    const qInput = ref(null);

    // Two searches can overlap (a deep link while a restore is pending, or
    // Search pressed twice quickly); the slower (older) response must not
    // overwrite the newer one.
    let lastRequestId = 0;

    const ft = computed(() => ftBounds(visibleResults.value));

    // Time slider: display bounds fall back to the result set's full range;
    // a bound never crosses the other one (dragging past it stops there).
    const yearFrom = computed(() => store.year.from ?? yearDomain.value.min);
    const yearTo = computed(() => store.year.to ?? yearDomain.value.max);
    const rangeFill = computed(() => {
      const d = yearDomain.value;
      if (d.min === null || d.max === null) return { left: '0%', right: '0%' };
      if (d.max === d.min) return { left: '0%', right: '0%' };
      const span = d.max - d.min;
      const from = Math.min(Math.max(yearFrom.value, d.min), d.max);
      const to = Math.min(Math.max(yearTo.value, d.min), d.max);
      return {
        left: ((from - d.min) / span * 100) + '%',
        right: ((d.max - to) / span * 100) + '%',
      };
    });
    function setYearFrom(ev) {
      const v = Number(ev.target.value);
      store.year.from = Math.min(v, store.year.to ?? yearDomain.value.max);
    }
    function setYearTo(ev) {
      const v = Number(ev.target.value);
      store.year.to = Math.max(v, store.year.from ?? yearDomain.value.min);
    }

    // The count matches the cards above the "possibly unrelated" toggle:
    // strong results only (visible minus the far tier), and "filtered from"
    // is the strong total of the unfiltered set.
    const countText = computed(() => {
      const shown = visibleResults.value.length - farCount.value;
      const total = store.results.filter(isStrong).length;
      const far = farCount.value;
      return shown + ' results' + (total !== shown ? ' (filtered from ' + total + ')' : '') +
        (far ? ', plus ' + far + ' possibly unrelated' : '');
    });
    const embedTime = computed(() => (store.timings.embedding_time_s || 0).toFixed(3));
    const searchTime = computed(() => (store.timings.search_time_s || 0).toFixed(3));
    const rerankTime = computed(() => (store.timings.rerank_time_s || 0).toFixed(3));

    // "searched:" / "searched in Word-Matching Search (authors):" — the
    // mode comes from the server echo, so the summary states what actually
    // ran (a selection made since then is NOT what the results show).
    const searchPrefix = computed(() =>
      store.submittedMode !== DEFAULT_MODE
        ? 'searched in ' + modeLabel(store.submittedMode) + ':'
        : 'searched:');

    // Words to highlight in the cards: only for results of a word mode
    // (the semantic strategy can match by meaning, with no shared word).
    const HIGHLIGHT_MODES = ['title', 'keywords', 'author'];
    const hlTerms = computed(() =>
      HIGHLIGHT_MODES.includes(store.submittedMode) ? queryTerms(store.submittedQuery) : []);

    // The line under the switcher describing the effective selection
    // (layer 3): the selected field's own text under word matching, the
    // semantic strategy's otherwise.
    const modeHint = computed(() => hintForMode(store.mode));

    // The active layer-1 strategy, derived from the effective mode — so a
    // deep link / restore (?mode=nl) opens on the right strategy too.
    const strategy = computed(() => strategyOf(store.mode));

    // Sliding highlight behind the selected button: one absolutely
    // positioned pill PER ROW whose left/width follow that row's selected
    // button, so switching animates the rounded overlay (CSS transition on
    // left/width). `ready` turns the transition on only after the first
    // placement, so a pill does not fly in from the left edge when the page
    // loads. The field row mounts/unmounts with the strategy, so placing it
    // is a no-op while hidden (its ref is null).
    const strategyTabsEl = ref(null);
    const fieldTabsEl = ref(null);
    const strategyIndicator = reactive({ left: 0, width: 0, ready: false });
    const fieldIndicator = reactive({ left: 0, width: 0, ready: false });
    function placeOne(el, indicator) {
      const btn = el && el.querySelector('[aria-selected="true"]');
      if (!btn) return;
      indicator.left = btn.offsetLeft;
      indicator.width = btn.offsetWidth;
      if (!indicator.ready) requestAnimationFrame(() => { indicator.ready = true; });
    }
    function placeIndicators() {
      placeOne(strategyTabsEl.value, strategyIndicator);
      placeOne(fieldTabsEl.value, fieldIndicator);
    }
    watch(() => store.mode, () => nextTick(placeIndicators));
    // Web fonts / window width change the button sizes.
    window.addEventListener('resize', placeIndicators);
    onBeforeUnmount(() => window.removeEventListener('resize', placeIndicators));

    // `queryArg` is passed when the query is already known (deep link);
    // otherwise the search bar is the query.
    async function search(queryArg) {
      const query = (typeof queryArg === 'string' ? queryArg : store.query).trim();
      if (!query) { if (qInput.value) qInput.value.focus(); return; }

      // Always fetch the FULL result set (no server-side filter); the
      // checkbox/facet filters narrow it in memory afterwards.
      store.phase = 'loading';
      store.error = '';
      const requestId = ++lastRequestId;
      try {
        const data = await fetchSearch({
          query,
          mode: store.mode,
          source_k:    clampInt(store.source_k, 10),
          final_k:     clampInt(store.final_k, 20),
          rrf_constant: clampInt(store.rrf_constant, 60),
        });
        if (requestId !== lastRequestId) return;   // superseded: ignore
        store.submittedQuery = query;
        store.submittedMode = data.mode || store.mode;
        store.results = data.results || [];
        store.showFar = false;
        store.timings = {
          embedding_time_s: data.embedding_time_s,
          search_time_s:    data.search_time_s,
          rerank_time_s:    data.rerank_time_s || 0,
        };
        pruneFacets();
        store.phase = 'done';
        saveState();
        syncUrl();
      } catch (err) {
        if (requestId !== lastRequestId) return;
        store.phase = 'error';
        store.error = String((err && err.message) || err);
      }
    }

    // Layer-1 click: pick the strategy. Selecting only changes the
    // selection — the query on screen keeps its own mode until Search is
    // pressed again (deliberately NO auto re-run and NO URL write: the
    // address bar follows a successful search, so it always matches the
    // results). Picking word matching restores the remembered field.
    function selectStrategy(id) {
      const entry = STRATEGY_ENTRIES.find(s => s.id === id);
      if (!entry || strategy.value === id) return;
      setMode(entry.mode || store.wordField);
    }

    // Layer-2 click: pick the field word matching searches. setMode also
    // remembers it as the layer-2 choice for the flip back from semantic.
    function selectField(value) {
      if (store.mode === value) return;
      setMode(value);
    }

    // ARIA tabs pattern PER ROW: arrows/Home/End move the selection (roving
    // tabindex) within the row that has focus. `values` lists that row's
    // selectable ids, `current` is the row's live selection, `pick` applies
    // it (the button focus follows the selection).
    function rowKeydown(e, values, current, pick) {
      const idx = values.indexOf(current);
      let next = null;
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (idx + 1) % values.length;
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (idx - 1 + values.length) % values.length;
      else if (e.key === 'Home') next = 0;
      else if (e.key === 'End') next = values.length - 1;
      if (next === null || next === idx) return;
      e.preventDefault();
      pick(values[next]);
      const tabs = e.currentTarget.querySelectorAll('[role="tab"]');
      if (tabs[next]) tabs[next].focus();
    }
    function onStrategyKeydown(e) {
      rowKeydown(e, STRATEGY_ENTRIES.map(s => s.id), strategy.value, selectStrategy);
    }
    function onFieldKeydown(e) {
      rowKeydown(e, FIELD_ENTRIES.map(f => f.value), store.mode, selectField);
    }

    // Card click: full detail lives on the node page.
    function openNode(eid) {
      location.href = 'node.html?eid=' + encodeURIComponent(eid);
    }

    onMounted(() => {
      nextTick(placeIndicators);
      if (document.fonts && document.fonts.ready) document.fonts.ready.then(placeIndicators);
      const url = readUrlQuery();
      applyUrlFilters(url);            // purpose/docType/include narrow the groups
      if (url.q) {
        // Deep link: run it, skip the session restore (the link is explicit).
        setMode(url.mode || DEFAULT_MODE);
        store.query = url.q;
        search(url.q);
        return;
      }
      restoreState();                 // may restore the mode of the saved results
      if (url.mode) setMode(url.mode);   // ...but the URL wins
    });

    const activeGroups = computed(() => new Set(activeFilters.value.map(f => f.group)));

    return {
      ...toRefs(store),
      formalEntries: FORMAL_ENTRIES,
      // Doc Type renders the formal entries plus Project; "Include also"
      // renders the rest of the type entries (Indications, Gap).
      projectEntries: PROJECT_ENTRIES,
      includeEntries: INCLUDE_ENTRIES,
      purposeEntries: PURPOSE_ENTRIES,
      // The three taxonomy facets (Phenomena, Topic, Geographic area): the
      // template renders them from the group descriptors, `taxonomy` is the
      // store itself so each box v-models store[group.key][value].
      taxonomyGroups: TAXONOMY_GROUPS,
      taxonomy: store,
      strategyEntries: STRATEGY_ENTRIES,
      fieldEntries: FIELD_ENTRIES,
      strategy,
      visible: visibleResults,
      groups: groupedResults,
      strongGroups, farGroups, farCount,
      ft, countText, embedTime, searchTime, rerankTime, searchPrefix,
      modeHint, strategyTabsEl, fieldTabsEl, strategyIndicator, fieldIndicator, hlTerms,
      // Filter entry counts (whole result set) + the Time slider state.
      counts: filterCounts, activeFilters, clearFilters,
      anyActive: names => names.some(n => activeGroups.value.has(n)),
      totalCount: computed(() => store.results.length),
      // Names of the filter sections currently narrowing the results.
      activeGroups,
      yearDomain, yearFrom, yearTo, rangeFill, setYearFrom, setYearTo,
      qInput, search, selectStrategy, selectField, onStrategyKeydown, onFieldKeydown, openNode,
    };
  },
};

createApp(SearchPage).mount('#app');
