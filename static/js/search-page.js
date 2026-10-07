// Search page entry point.
//
// The root component's template is the inline markup in index.html (the full
// Vue build compiles it at mount); this module only wires the state, the API
// call, the mode tabs and the two child components (cards, contributor facets).

import {
  createApp, computed, onMounted, ref, toRefs,
} from '../vendor/vue.esm-browser.prod.js';
import {
  store, visibleResults, groupedResults, saveState, restoreState, pruneFacets,
  FORMAL_ENTRIES, PROJECT_ENTRIES, INCLUDE_ENTRIES, PURPOSE_ENTRIES,
  MODE_ENTRIES, isMode, modeLabel, yearDomain, filterCounts,
} from './state.js';
import { fetchSearch } from './api.js';
import { ftBounds, clampInt } from './dom.js';
import ResultCard from './components/result-card.js';
import ActorFacet from './components/actor-facet.js';

// Deep links (?q=...&mode=...&purpose=...&docType=...&include=...): used by
// the node page ("search this author", drill-down from a purpose/doc-type
// chip) and by shared URLs. An unknown mode is ignored, so old links stay
// safe.
function readUrlQuery() {
  const params = new URLSearchParams(location.search);
  const mode = params.get('mode');
  const q = (params.get('q') || '').trim();
  return {
    q,
    mode: isMode(mode) ? mode : '',
    purposes: readListParam(params, 'purpose', PURPOSE_ENTRIES.map(e => e.value)),
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
// selection: merely ticking one box among the default all-ticked ones would
// be a no-op, and a present param with no valid value therefore ticks
// nothing. Each param only touches its OWN group — an inbound ?docType= link
// must not silently clear "Include also", which that link never carried.
// People and Institutions are not URL state — their names only exist in the
// result set, so person links go through ?mode=author&q=... instead.
function applyUrlFilters(url) {
  if (url.purposes) {
    for (const e of PURPOSE_ENTRIES) store.purposes[e.value] = url.purposes.includes(e.value);
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
// replaceState avoids piling one history entry per search.
function syncUrl() {
  const params = new URLSearchParams();
  if (store.submittedQuery) params.set('q', store.submittedQuery);
  if (store.mode !== 'nl') params.set('mode', store.mode);
  // Filter selections that differ from the all-ticked default ride along, so
  // a copied URL reopens the same view. (They are rewritten on each search —
  // toggling a box alone does not touch the URL yet.)
  const purposes = PURPOSE_ENTRIES.filter(e => store.purposes[e.value]).map(e => e.value);
  if (purposes.length !== PURPOSE_ENTRIES.length) params.set('purpose', purposes.join(','));
  const docTypes = [
    ...FORMAL_ENTRIES.filter(e => store.formal[e.value]).map(e => e.value),
    ...PROJECT_ENTRIES.filter(e => store.types[e.value]).map(e => e.value),
  ];
  if (docTypes.length !== FORMAL_ENTRIES.length + PROJECT_ENTRIES.length) {
    params.set('docType', docTypes.join(','));
  }
  const includes = INCLUDE_ENTRIES.filter(e => store.types[e.value]).map(e => e.value);
  if (includes.length !== INCLUDE_ENTRIES.length) {
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

    // Switching a tab fires a new search while the previous one may still be
    // in flight; the slower (older) response must not overwrite the newer one.
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

    const countText = computed(() => {
      const total = store.results.length;
      const shown = visibleResults.value.length;
      return shown + ' results' + (total !== shown ? ' (filtered from ' + total + ')' : '');
    });
    const embedTime = computed(() => (store.timings.embedding_time_s || 0).toFixed(3));
    const searchTime = computed(() => (store.timings.search_time_s || 0).toFixed(3));

    // "searched:" / "searched in Title:" — the mode comes from the server
    // echo, so the summary states what actually ran.
    const searchPrefix = computed(() =>
      store.submittedMode !== 'nl'
        ? 'searched in ' + modeLabel(store.submittedMode) + ':'
        : 'searched:');

    // `queryArg` is passed when re-running a query that is already on screen
    // (tab switch, deep link); otherwise the search bar is the query.
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
        store.timings = {
          embedding_time_s: data.embedding_time_s,
          search_time_s:    data.search_time_s,
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

    // Tab click. The results on screen were made by the previous mode, so the
    // shown query is re-run right away — the selected tab and the displayed
    // results never disagree. With nothing searched yet it just selects.
    function selectMode(value) {
      if (store.mode === value) return;
      store.mode = value;
      syncUrl();
      if (store.submittedQuery) search(store.submittedQuery);
    }

    // ARIA tabs pattern: arrows/Home/End move the selection (roving tabindex).
    function onTabKeydown(e) {
      const values = MODE_ENTRIES.map(m => m.value);
      const current = values.indexOf(store.mode);
      let next = null;
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (current + 1) % values.length;
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (current - 1 + values.length) % values.length;
      else if (e.key === 'Home') next = 0;
      else if (e.key === 'End') next = values.length - 1;
      if (next === null) return;
      e.preventDefault();
      selectMode(values[next]);
      const tabs = e.currentTarget.querySelectorAll('[role="tab"]');
      if (tabs[next]) tabs[next].focus();
    }

    // Card click: full detail lives on the node page.
    function openNode(eid) {
      location.href = 'node.html?eid=' + encodeURIComponent(eid);
    }

    onMounted(() => {
      const url = readUrlQuery();
      applyUrlFilters(url);            // purpose/docType/include narrow the groups
      if (url.q) {
        // Deep link: run it, skip the session restore (the link is explicit).
        if (url.mode) store.mode = url.mode;
        store.query = url.q;
        search(url.q);
        return;
      }
      restoreState();                 // may restore the mode of the saved results
      if (url.mode) store.mode = url.mode;   // ...but the URL wins
    });

    return {
      ...toRefs(store),
      formalEntries: FORMAL_ENTRIES,
      // Doc Type renders the formal entries plus Project; "Include also"
      // renders the rest of the type entries (Indications, Gap).
      projectEntries: PROJECT_ENTRIES,
      includeEntries: INCLUDE_ENTRIES,
      purposeEntries: PURPOSE_ENTRIES,
      modeEntries: MODE_ENTRIES,
      visible: visibleResults,
      groups: groupedResults,
      ft, countText, embedTime, searchTime, searchPrefix,
      // Filter entry counts (whole result set) + the Time slider state.
      counts: filterCounts,
      yearDomain, yearFrom, yearTo, rangeFill, setYearFrom, setYearTo,
      qInput, search, selectMode, onTabKeydown, openNode,
    };
  },
};

createApp(SearchPage).mount('#app');
