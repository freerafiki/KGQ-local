// Node detail page entry point.
//
// The root component's template is the inline markup in node.html (the full
// Vue build compiles it at mount); this module turns the /node/{eid} payload
// into the page's view model and builds the cross-page drill-down links:
//   people/institutions -> index.html?mode=author&q=NAME  (Authors search)
//   purposes            -> index.html?purpose=LABEL       (Main purpose facet)
//   document types      -> index.html?docType=WORD        (Doc Type facet)
//   Indications/Gap     -> index.html?include=WORD        (Include also facet)
//   release year        -> index.html?year=YYYY           (Time filter)
//   entities            -> node.html?eid=...              (their node page)

import { createApp, computed, onMounted, ref } from '../vendor/vue.esm-browser.prod.js';
import { BADGE_CLASS, BADGE_SHORT, formalBadge, truncate, PURPOSE_CLASS } from './dom.js';
// The type taxonomy (value <-> graph type pairing, and which filter group
// owns the entry) is the search page's filter data; importing it keeps the
// facet links from drifting out of sync.
import { TYPE_ENTRIES } from './state.js';
import { fetchNode } from './api.js';
import LinkCard from './components/link-card.js';
import RelList from './components/rel-list.js';
import UrlItem from './components/url-item.js';

// How many rows each list shows before the "Show N more" disclosure.
// C3 URLs are an overview row, the related lists are their own sections.
const OVERVIEW_URL_LIMIT = 10;
const REL_LIST_LIMIT = 12;

const PROP_LABELS = {
  officialTitle: 'Official Title',
  title: 'Title',
  subtitle: 'Subtitle',
  description: 'Description',
  findings: 'Findings',
  content: 'Content',
  motivation: 'Motivation',
  name: 'Name',
  id_num: 'Project ID',
  funding_source: 'Funding source',
  output_types: 'Output types',
  start_year: 'Start year',
  end_year: 'End year',
  start_month: 'Start month',
  end_month: 'End month',
  notes: 'Notes',
  decision_type: 'Decision type',
  conditions: 'Conditions',
  knowledge: 'Knowledge',
  policy: 'Policy',
  id: 'ID',
  // Release year links to a search bounded to that year — the Time filter's
  // single-year ?year=YYYY form (built in extraRows below).
  releaseYear: 'Release year',
  releaseDate: 'Release date',
  grant: 'Grant',
  version: 'Version',
  versionDetails: 'Version details',
  submitted: 'Submitted',
  dateType: 'Date type',
  temporalType: 'Temporal type',
  temporalReference: 'Temporal reference',
  temporalResolution_0: 'Temporal resolution',
  spatial_type: 'Spatial type',
  spatial_scale: 'Spatial scale',
  spatial_resolution: 'Spatial resolution',
  coveredAreaDescription: 'Covered area',
  discrete_spatial_type: 'Discrete spatial type',
  discrete_points_insertion_mode: 'Discrete points insertion',
  validityPeriod: 'Validity period',
  forDecision: 'For decision',
  forRecommendation: 'For recommendation',
  expert_evaluation: 'Expert evaluation',
  internal_note: 'Internal note',
  internal_record_state: 'Internal record state',
  note: 'Note',
  future_work: 'Future work',
  intervals_year_month_1: 'Time intervals',
  // Related-node fields (they arrive in `related`, not in `properties`)
  URL: 'URL',
  url: 'URL',
  physicalArchive: 'Physical archive',
  email: 'Email',
  institution: 'Institution',
  surname: 'Surname',
  referencePerson: 'Reference person',
  acronym: 'Acronym',
  type: 'Type',
  action: 'Action',
  example: 'Example',
  full_text: 'Full text',
  details: 'Details',
  other_name: 'Other name',
};

// Own property keys the Details table claims (see the field order below);
// everything else of ours renders under "Additional Properties".
const OVERVIEW_KEYS = new Set(['officialTitle', 'title', 'name', 'subtitle',
  'description', 'findings', 'content', 'grant', 'validityPeriod']);

// ---- Cross-page link builders ----------------------------------------------

const nodeHref = eid => 'node.html?eid=' + encodeURIComponent(eid);
const authorHref = name => 'index.html?mode=author&q=' + encodeURIComponent(name);
const purposeHref = label => 'index.html?purpose=' + encodeURIComponent(label);
const docTypeHref = word => 'index.html?docType=' + encodeURIComponent(word);
const includeHref = word => 'index.html?include=' + encodeURIComponent(word);

// Only http(s) links are clickable; anything else renders as plain text.
function safeHref(url) {
  const u = String(url == null ? '' : url).trim();
  return /^https?:\/\//i.test(u) ? u : '';
}

function plainValue(v) {
  if (Array.isArray(v)) return v.join(', ');
  if (v && typeof v === 'object') return JSON.stringify(v);
  return String(v == null ? '' : v);
}

// The caption under a related/url item: the Reference name (for rows whose
// heading is the URL itself) plus the leftover neighbour properties.
function extraLine(item, showLabel) {
  const bits = [];
  if (showLabel && item.label && item.label !== item.url) bits.push(item.label);
  Object.keys(item.props || {}).forEach(k => {
    bits.push((PROP_LABELS[k] || k) + ': ' + truncate(plainValue(item.props[k]), 160));
  });
  return bits.join(' \u00B7 ');
}

// One related item -> the RelItem model. An http(s) URL wins (the referenced
// document is worth opening); otherwise a graph entity links to its own node
// page; without either it stays plain text.
function relModel(item) {
  const external = safeHref(item.url);
  return {
    label: item.label || '',
    href: external || (item.eid ? nodeHref(item.eid) : ''),
    external: !!external,
    extra: extraLine(item, false),
  };
}

// One C3 URL/DOI row. Same priority; the Reference name is shown as the
// caption only when the URL itself is the row heading.
function urlModel(item) {
  const external = safeHref(item.url);
  return {
    main: external ? item.url : (item.label || ''),
    href: external || (item.eid ? nodeHref(item.eid) : ''),
    external: !!external,
    extra: extraLine(item, !!external),
  };
}

// The "Formal type" section drills into the Doc Type filter instead of into
// the FormalType node: its badge word IS the filter entry.
function ftModel(item) {
  const word = formalBadge(item.label);
  return word
    ? { label: item.label, href: docTypeHref(word), external: false, extra: extraLine(item, false) }
    : relModel(item);
}

// Purpose taxonomy label -> clickable chip (Purpose facet on the search page).
const tagModel = p => ({ label: p, cls: PURPOSE_CLASS[p] || '', href: purposeHref(p) });

// The row label already says people/institutions, so no type suffix here.
const actorModel = a => ({ name: a.name, href: a.name ? authorHref(a.name) : '' });

// One linked node card (parent document, connected Rec/Gap). Its purpose
// line stays plain text: the card itself is an <a>, and nested anchors are
// invalid HTML — the overview's purpose chips carry the facet links.
function briefCard(brief) {
  const word = brief.type === 'Oggetto Informativo' ? formalBadge(brief.formalType) : '';
  return {
    href: nodeHref(brief.eid),
    badgeClass: word ? word.toLowerCase() : (BADGE_CLASS[brief.type] || 'ft'),
    badgeText: word || (BADGE_SHORT[brief.type] || 'FT'),
    title: brief.title || brief.content || brief.description || 'Node',
    snippet: truncate(brief.title || brief.content || brief.description || 'No text', 180),
    rel: brief.rel || '',
    purposes: (brief.purposes || []).join(', '),
  };
}

// Where the header badge drills into the filters: the 5 formal words and
// Project open Doc Type (?docType=), Rec/Gap open Include also (?include=).
function filterHref(type, word) {
  if (word) return docTypeHref(word);
  const entry = TYPE_ENTRIES.find(e => e.type === type);
  if (!entry) return '';
  return entry.group === 'includeAlso' ? includeHref(entry.value) : docTypeHref(entry.value);
}

const NodePage = {
  components: { LinkCard, RelList, UrlItem },
  setup() {
    const phase = ref('loading');   // loading -> done | error
    const error = ref('');
    const data = ref(null);

    // Badge + headline. The badge links into the matching filter group when
    // the document type is known (formal word/Project -> Doc Type, Rec/Gap
    // -> Include also); an unknown or unmapped type keeps a plain badge.
    const header = computed(() => {
      const d = data.value;
      if (!d) return null;
      const props = d.properties || {};
      const related = d.related || {};
      const name = props.officialTitle || props.title || props.content || props.name
        || props.description || 'Unnamed node';
      const ftBucket = related.formalType;
      const ftRaw = d.formalType
        || (ftBucket && ftBucket.items && ftBucket.items[0] ? ftBucket.items[0].label : '')
        || '';
      const word = d.type === 'Oggetto Informativo' ? formalBadge(ftRaw) : '';
      return {
        name,
        badgeClass: word ? word.toLowerCase() : (BADGE_CLASS[d.type] || 'ft'),
        badgeText: word || (BADGE_SHORT[d.type] || 'FT'),
        badgeHref: filterHref(d.type, word),
      };
    });

    // Field order for the detail page: C2 title, A1 (Italian), A2 subtitle,
    // A3a abstract, A3b findings, grant, validity period, C3 URL/DOI, A4 main
    // purpose, secondary purpose, contributors split into people /
    // institutions - and only then everything else.
    const detailsRows = computed(() => {
      const d = data.value;
      if (!d) return [];
      const props = d.properties || {};
      const related = d.related || {};
      const isDoc = (d.labels || []).includes('Contribution');
      const rows = [];

      const overviewFields = [
        ['officialTitle', 'Official title (C2)'],
        ['title',         'Title (A1) \u00B7 Italian'],
        ['name',          'Name'],
        ['subtitle',      'Subtitle (A2)'],
        ['description',   isDoc ? 'Abstract (A3a)' : 'Description'],
        ['findings',      'Findings (A3b)'],
        ['content',       'Content'],
        ['grant',         'Grant'],
        ['validityPeriod','Validity period'],
      ];
      overviewFields.forEach(([k, label]) => {
        const v = props[k];
        if (v == null || (typeof v === 'string' && !v.trim())) return;
        rows.push({ label, kind: 'text', value: plainValue(v) });
      });

      // C3 URL/DOI: relationship-linked Reference nodes (not a property).
      const refs = (related.references && related.references.items) || [];
      if (refs.length) {
        rows.push({
          label: 'URL/DOI (C3)', kind: 'urls',
          items: refs.slice(0, OVERVIEW_URL_LIMIT).map(urlModel),
          more: refs.slice(OVERVIEW_URL_LIMIT).map(urlModel),
        });
      }

      // A4 main purpose (has_main_function) + its secondary functions.
      const mainPurposes = (d.mainPurposes && d.mainPurposes.length)
        ? d.mainPurposes
        : (d.purposes || []);
      if (mainPurposes.length) {
        rows.push({ label: 'Main purpose (A4)', kind: 'tags', items: mainPurposes.map(tagModel) });
      }
      if (d.secPurposes && d.secPurposes.length) {
        rows.push({ label: 'Secondary purpose', kind: 'tags', items: d.secPurposes.map(tagModel) });
      }

      // contributionActors, split by their ContributionActor.type.
      const actors = d.actors || [];
      const isInst = a => String(a.type || '').toLowerCase() === 'institution';
      const people = actors.filter(a => !isInst(a));
      const insts = actors.filter(isInst);
      if (people.length) {
        rows.push({ label: 'Contributors (people)', kind: 'actors', items: people.map(actorModel) });
      }
      if (insts.length) {
        rows.push({ label: 'Contributors (institutions)', kind: 'actors', items: insts.map(actorModel) });
      }
      return rows;
    });

    // Remaining own properties: below the connected Recs/Gaps, above
    // "Assisted by / Reviewed by". (Same emptiness rule as before: no rows
    // at all shows "No stored properties." instead of the section.)
    const extraRows = computed(() => {
      const d = data.value;
      if (!d) return [];
      const rows = Object.entries(d.properties || {})
        .filter(([k]) => !OVERVIEW_KEYS.has(k))
        .map(([k, v]) => {
          const row = { label: PROP_LABELS[k] || k, value: plainValue(v) };
          // Release year drills into a Time-bounded search for that year.
          if (k === 'releaseYear' && /^\d{4}$/.test(row.value)) {
            row.href = 'index.html?year=' + row.value;
          }
          return row;
        });
      Object.entries(d.vectorProps || {}).forEach(([k, dim]) => {
        rows.push({ label: PROP_LABELS[k] || k, value: dim + '-dim vector' });
      });
      return rows;
    });

    // Remaining relationship-linked fields, in the backend's display order.
    // (`references` is the C3 row, `user` renders at the bottom as
    // "Assisted by" / "Reviewed by".)
    const relatedSections = computed(() => {
      const d = data.value;
      if (!d) return [];
      const out = [];
      Object.keys(d.related || {}).forEach(key => {
        if (key === 'references' || key === 'user') return;
        const group = d.related[key];
        if (!group || !group.items || !group.items.length) return;
        const items = group.items.map(item => (key === 'formalType' ? ftModel(item) : relModel(item)));
        out.push({
          key,
          title: group.label,
          items: items.slice(0, REL_LIST_LIMIT),
          more: items.slice(REL_LIST_LIMIT),
        });
      });
      return out;
    });

    // Parent document (for Rec/Gap) and connected Recs/Gaps (for
    // Contributions), as linked-node cards.
    const linkSections = computed(() => {
      const d = data.value;
      if (!d) return [];
      const out = [];
      if (d.parents && d.parents.length) {
        out.push({ key: 'parents', title: 'Source Document', items: d.parents.map(briefCard) });
      }
      if (d.children && d.children.length) {
        const recs = d.children.filter(c => c.type === 'Raccomandazione');
        const gaps = d.children.filter(c => c.type === 'Lacuna');
        if (recs.length) {
          out.push({ key: 'recs', title: 'Connected Recommendations (' + recs.length + ')',
                     items: recs.map(briefCard) });
        }
        if (gaps.length) {
          out.push({ key: 'gaps', title: 'Connected Gaps (' + gaps.length + ')',
                     items: gaps.map(briefCard) });
        }
        if (!recs.length && !gaps.length) {
          out.push({ key: 'children', title: 'Connected Recs / Gaps', items: [],
                     empty: 'No Rec/Gap linked to this document.' });
        }
      }
      return out;
    });

    // Assisted by / Reviewed by, at the very bottom of the page (after the
    // connected Recs/Gaps). Embedding-status flags never appear here: the API
    // strips them as internal bookkeeping.
    const assisted = computed(() => {
      const d = data.value;
      const related = (d && d.related) || {};
      const items = ((related.user && related.user.items) || []).map(relModel);
      return { items: items.slice(0, REL_LIST_LIMIT), more: items.slice(REL_LIST_LIMIT) };
    });

    // Back/Escape: back to where the user came from, the search page when
    // there is no history (opened directly).
    function goBack() {
      if (history.length > 1) history.back();
      else location.href = 'index.html';
    }
    function onKeydown(e) {
      if (e.key === 'Escape') goBack();
    }

    async function load() {
      const eid = new URLSearchParams(location.search).get('eid');
      if (!eid) {
        error.value = 'Missing node id (eid query param).';
        phase.value = 'error';
        return;
      }
      try {
        const d = await fetchNode(eid);
        data.value = d;
        phase.value = 'done';
        document.title = truncate(header.value.name, 120) + ' \u00B7 KGQ Search';
      } catch (err) {
        error.value = 'Failed to load node: ' + String((err && err.message) || err);
        phase.value = 'error';
      }
    }

    onMounted(() => {
      document.addEventListener('keydown', onKeydown);
      load();
    });

    return {
      phase, error, data,
      header, detailsRows, extraRows, relatedSections, linkSections, assisted,
      goBack,
    };
  },
};

createApp(NodePage).mount('#app');
