// Page state for the search screen: the request/response, the client-side
// filters and their persistence. A single reactive object shared by the root
// component and the child components (facets), so nothing needs prop drilling.
//
// Nothing here touches the DOM: this module is the page's model only.

import { reactive, computed } from '../vendor/vue.esm-browser.prod.js';
import { formalBadge, FORMAL_TYPES, clampInt } from './dom.js';

// ---- Filter taxonomy -------------------------------------------------------

// Doc Type holds the 5 FormalType entries (these ARE the document type of a
// Contribution — every one has exactly one) plus Project, its own node type;
// Project joins them as the 6th box. Display order = the order the entries
// were specified in (paper, plan, model, law, dataset, project).
export const FORMAL_ENTRIES = [
  { value: 'Paper',   dot: 'paper' },
  { value: 'Plan',    dot: 'plan' },
  { value: 'Model',   dot: 'model' },
  { value: 'Law',     dot: 'law' },   // was "Norm", renamed everywhere
  { value: 'Dataset', dot: 'dataset' },
];

// The plain node types. `group` says which filter group renders the entry:
// Project sits with the documents in Doc Type, while Rec/Gap attach TO a
// document and live in the separate "Include also" group. All three still
// share one `store.types` map — they are ONE axis in the filter chain; only
// the rendering and the URL params (docType vs include) split. `value` is
// the checkbox/URL word (the displayed label, where they differ), `type` =
// the node label the result carries (`r.type`).
export const TYPE_ENTRIES = [
  { value: 'Indications',   dot: 'rec', label: 'Indications', type: 'Raccomandazione', group: 'includeAlso' },
  { value: 'Gap',           dot: 'gap', label: 'Gap',         type: 'Lacuna',          group: 'includeAlso' },
  { value: 'Project',       dot: 'prj', label: 'Project',     type: 'Progetto',        group: 'docType' },
];

// The two groups the type entries render in, pre-split so the template and
// the URL contract can treat "the documents" and "include also" separately.
export const PROJECT_ENTRIES = TYPE_ENTRIES.filter(e => e.group === 'docType');
export const INCLUDE_ENTRIES = TYPE_ENTRIES.filter(e => e.group === 'includeAlso');

// Checkbox values MUST stay the taxonomy labels from query_util.PURPOSE_CATEGORIES.
// `desc` is the second line: Purpose.name with the category prefix stripped
// where the name starts with it.
export const PURPOSE_ENTRIES = [
  { value: 'Valutazione',        dot: 'pv-val', desc: 'i.e. impatti, rischi, conseguenze' },
  { value: 'Monitoraggio',       dot: 'pv-mon', desc: 'e/o descrizione dello stato attuale' },
  { value: 'Ricostruzione',      dot: 'pv-ric', desc: 'e interpretazione del passato del sistema' },
  { value: 'Previsione',         dot: 'pv-pre', desc: 'o rappresentazione del futuro' },
  { value: 'Supporto decisioni', dot: 'pv-sup', desc: 'Supporto diretto alle decisioni' },
  { value: 'Governance',         dot: 'pv-gov', desc: 'Regole, processi o governance' },
  { value: 'Gap di conoscenza',  dot: 'pv-gap', desc: 'Evidenzia un limite o un gap di conoscenza' },
];

// Name order for the People facet: compare the LAST word of each name so
// authors list by surname ("Ada Rossi" sorts under Rossi). The full name is
// the tie-break, keeping same-surname authors in given-name order; the
// last-word rule also copes with particles ("Anna de Marchi" -> Marchi).
export function bySurname(a, b) {
  const last = n => {
    const parts = String(n).trim().split(/\s+/);
    return parts[parts.length - 1];
  };
  return last(a).localeCompare(last(b)) || a.localeCompare(b);
}

// The two contributor facets share one shape: a searchable checkbox list
// (live substring query + OR-ed selections), reading a different payload
// field. They are combined with AND — see visibleResults below. `compare`
// orders the offered list: People by surname, Institutions plain A-Z (an
// organisation has no surname to key on — its whole string is the name).
export const FACET_ENTRIES = [
  { key: 'person',      field: 'people',       title: 'People',      emptyMsg: 'No matching people.',      compare: bySurname },
  { key: 'institution', field: 'institutions', title: 'Institutions', emptyMsg: 'No matching institutions.', compare: (a, b) => a.localeCompare(b) },
];

// The search modes shown as tabs above the search bar, in display order.
// `value` is what the API expects (query_util.SEARCH_MODES), `label` is the
// tab text. The order is presentation only: `nl` stays the default mode.
export const MODE_ENTRIES = [
  { value: 'title',    label: 'Title' },
  { value: 'author',   label: 'Authors' },
  { value: 'nl',       label: 'Natural language' },
  { value: 'keywords', label: 'Keywords' },
];

export function isMode(value) {
  return MODE_ENTRIES.some(m => m.value === value);
}

export function modeLabel(value) {
  const entry = MODE_ENTRIES.find(m => m.value === value);
  return entry ? entry.label : value;
}

// ---- State -----------------------------------------------------------------

export const store = reactive({
  // Request / response
  query: '',            // live content of the search bar
  submittedQuery: '',   // the query the current results answer to
  mode: 'nl',           // selected tab: mode of the NEXT request
  submittedMode: 'nl',  // mode the current results came from (server echo)
  phase: 'idle',        // idle -> loading -> done | error
  error: '',
  results: [],          // FULL result set from the API (filters apply client-side)
  timings: {},          // last successful response: embedding/search seconds

  // Advanced parameters (the inputs are the source of truth; clamped on send)
  source_k: 20,
  final_k: 50,
  rrf_constant: 60,

  // Client-side filters — all default to checked, so unchecking one hides
  // the results carrying that tag.
  formal:   Object.fromEntries(FORMAL_ENTRIES.map(e => [e.value, true])),
  types:    Object.fromEntries(TYPE_ENTRIES.map(e => [e.value, true])),
  purposes: Object.fromEntries(PURPOSE_ENTRIES.map(e => [e.value, true])),
  facets: {
    person:      { query: '', selected: [] },
    institution: { query: '', selected: [] },
  },
  // Time filter: the selected year bounds (null = that side still covers the
  // full range of the current results) and whether docs WITHOUT a year are
  // shown (the "show docs without year" checkbox under the slider).
  year: { from: null, to: null, showMissing: true },
});

// ---- Facets ----------------------------------------------------------------

// The names a facet offers. `people` falls back to the merged list only when
// the split is missing (older stored payloads), never when it is an empty list.
function pick(field, r) {
  const value = field === 'people' ? (r.people || r.actors) : r[field];
  return value || [];
}

function collectNames(entry) {
  const names = [];
  for (const r of store.results) {
    for (const a of pick(entry.field, r)) if (!names.includes(a)) names.push(a);
  }
  return names.sort(entry.compare);
}

// Built once per facet; recomputed only when `store.results` changes.
export const FACET_NAMES = Object.fromEntries(
  FACET_ENTRIES.map(e => [e.key, computed(() => collectNames(e))])
);

// ---- Time filter -----------------------------------------------------------

// releaseYear as a number, or null when the payload has none (no year docs
// are governed by the "show docs without year" checkbox).
function yearOf(r) {
  const raw = r.releaseYear;
  if (raw === null || raw === undefined || raw === '') return null;
  const y = Number(raw);
  return Number.isFinite(y) ? y : null;
}

// The year span of the CURRENT result set — the slider's domain. Both bounds
// are null when no result carries a year; the Time group then stays hidden.
export const yearDomain = computed(() => {
  let min = null;
  let max = null;
  for (const r of store.results) {
    const y = yearOf(r);
    if (y === null) continue;
    if (min === null || y < min) min = y;
    if (max === null || y > max) max = y;
  }
  return { min, max };
});

// ---- Filter entry counts ---------------------------------------------------

// How many results the CURRENT result set holds per entry, for the three
// checkbox groups (Doc Type, Include also, Main purpose). Counted over the
// WHOLE set — the numbers stay stable while other filters are toggled and
// only change with a new search (the user-facing meaning: "we have 33
// Papers" in this result set).
export const filterCounts = computed(() => {
  const formal = Object.fromEntries(FORMAL_ENTRIES.map(e => [e.value, 0]));
  const type = Object.fromEntries(TYPE_ENTRIES.map(e => [e.value, 0]));
  const purpose = Object.fromEntries(PURPOSE_ENTRIES.map(e => [e.value, 0]));
  for (const r of store.results) {
    if (r.type === 'Oggetto Informativo') {
      const word = formalBadge(r.formalType);
      if (word && word in formal) formal[word] += 1;
    } else {
      const entry = TYPE_ENTRIES.find(e => e.type === r.type);
      if (entry) type[entry.value] += 1;
    }
    for (const p of r.purposes || []) if (p in purpose) purpose[p] += 1;
  }
  return { formal, type, purpose };
});

// Both constraints apply when set: a live-typed query (substring) and the
// selected list entries (OR-ed, result must carry at least one of them).
function passesActorFilter(actors, query, selected) {
  if (query && !actors.some(a => a.toLowerCase().includes(query))) return false;
  if (selected.length && !selected.some(s => actors.includes(s))) return false;
  return true;
}

function facetPasses(key, r) {
  const entry = FACET_ENTRIES.find(e => e.key === key);
  const facet = store.facets[key];
  return passesActorFilter(
    pick(entry.field, r),
    facet.query.trim().toLowerCase(),
    facet.selected,
  );
}

// Whitelist semantics per axis: a result survives if it carries at least one
// active tag. Results with NO tags on an axis (e.g. a Rec/Gap with no purpose)
// are "untagged" and always pass that axis.
function passesAxis(tags, active) {
  if (!tags.length) return true;
  if (!active.length) return false;
  return tags.some(t => active.includes(t));
}

// ---- The filter chain ------------------------------------------------------
// Toggling a filter re-filters the ALREADY-FETCHED results locally, no round
// trip to the API.
export const visibleResults = computed(() => {
  const typeActive    = TYPE_ENTRIES.filter(e => store.types[e.value]).map(e => e.type);
  const formalActive  = FORMAL_ENTRIES.filter(e => store.formal[e.value]).map(e => e.value);
  const purposeActive = PURPOSE_ENTRIES.filter(e => store.purposes[e.value]).map(e => e.value);

  // Time: the bounds default to the full year range of this result set;
  // docs with no releaseYear follow the "show docs without year" checkbox.
  const dom = yearDomain.value;
  const yearLo = store.year.from ?? dom.min;
  const yearHi = store.year.to ?? dom.max;

  return store.results.filter(r => {
    // Contributions are filtered through their FormalType badge (the 5 Doc
    // Type entries); every other node type by its own entry — Project through
    // the Doc Type group, Rec/Gap through "Include also". A Contribution
    // without a recognised FormalType only survives while NOTHING is
    // narrowed.
    let typeOk;
    if (r.type === 'Oggetto Informativo') {
      const word = formalBadge(r.formalType);
      typeOk = word ? passesAxis([word], formalActive)
                    : formalActive.length === FORMAL_TYPES.length;
    } else {
      typeOk = passesAxis([r.type], typeActive);
    }
    if (!typeOk) return false;
    if (!passesAxis(r.purposes || [], purposeActive)) return false;
    if (dom.min !== null) {
      const y = yearOf(r);
      if (y !== null) {
        if (y < yearLo || y > yearHi) return false;
      } else if (!store.year.showMissing) return false;
    }
    // People and Institutions are independent axes: a result has to satisfy
    // every facet that has a query or a selection.
    return FACET_ENTRIES.every(e => facetPasses(e.key, r));
  });
});

// ---- Grouped view ----------------------------------------------------------
// A Rec/Gap whose parent document is ALSO among the visible results is shown
// nested under that parent's card instead of as a top-level card of its own
// (the full detail stays one click away on the node page).
//
// Each group takes the position of its FIRST member in rank order — parent or
// child — so a group never lands below its best-ranked member. A child whose
// parent is filtered out (or simply not in the result set) stays a top-level
// card and keeps its "Plan: ..." parent row.
export const groupedResults = computed(() => {
  const visible = visibleResults.value;
  const visibleIds = new Set(visible.map(r => r.neo4j_id));
  const groups = [];
  const byKey = new Map();
  for (const r of visible) {
    const parentId = r.parent_oi ? r.parent_oi.neo4j_id : '';
    const nested = !!parentId && parentId !== r.neo4j_id && visibleIds.has(parentId);
    const key = nested ? parentId : r.neo4j_id;
    let group = byKey.get(key);
    if (!group) {
      group = { key, root: null, children: [] };
      byKey.set(key, group);
      groups.push(group);
    }
    if (nested) group.children.push(r);
    else group.root = r;
  }
  return groups;
});

// Selections that vanished from a fresh result set are dropped — the same
// moment the previous code pruned them (while seeding the lists).
export function pruneFacets() {
  for (const entry of FACET_ENTRIES) {
    const names = FACET_NAMES[entry.key].value;
    const facet = store.facets[entry.key];
    facet.selected = facet.selected.filter(s => names.includes(s));
  }
}

// ---- Persistence -----------------------------------------------------------

const SEARCH_STORE_KEY = 'kgq.search.v1';

// Back/Forward — or a fresh reload after the browser evicted the back-forward
// cache — must not lose the current search. State lives in sessionStorage:
// PER TAB, so other tabs/users can never collide.
export function saveState() {
  if (!store.submittedQuery || !store.results.length) return;
  try {
    sessionStorage.setItem(SEARCH_STORE_KEY, JSON.stringify({
      query: store.submittedQuery,
      // The saved results were produced in this mode, so it is also the tab
      // the restored page must show as active.
      mode: store.submittedMode,
      source_k: clampInt(store.source_k, 10),
      final_k: clampInt(store.final_k, 20),
      rrf_constant: clampInt(store.rrf_constant, 60),
      results: store.results,
      timings: store.timings,
    }));
  } catch (e) { /* quota exceeded / private mode: restore just won't happen */ }
}

// Re-render from the stored results with NO API call, so restoring works even
// while the backend is busy with someone else's search.
export function restoreState() {
  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem(SEARCH_STORE_KEY)); } catch (e) {}
  if (!saved || typeof saved.query !== 'string' || !saved.query ||
      !Array.isArray(saved.results) || !saved.results.length) return false;
  store.query = saved.query;
  if (isMode(saved.mode)) store.mode = saved.mode;
  store.submittedMode = store.mode;
  if (Number.isFinite(saved.source_k)) store.source_k = saved.source_k;
  if (Number.isFinite(saved.final_k)) store.final_k = saved.final_k;
  if (Number.isFinite(saved.rrf_constant)) store.rrf_constant = saved.rrf_constant;
  store.submittedQuery = saved.query;
  store.results = saved.results;
  store.timings = saved.timings || {};
  pruneFacets();
  store.phase = 'done';
  return true;
}
