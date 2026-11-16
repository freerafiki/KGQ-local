// Presentation helpers for the result cards.
// Pure functions (no DOM, no Vue) so both pages can reuse them and so they
// stay testable on their own.

export const BADGE_CLASS = {
  'Oggetto Informativo': 'oi',
  'Raccomandazione':     'rec',
  'Lacuna':              'gap',
  'Progetto':            'prj',
};

export const BADGE_SHORT = {
  'Oggetto Informativo': 'Document',
  'Raccomandazione':     'Indication',
  'Lacuna':              'Gap',
  'Progetto':            'Project',
};

// The 5 FormalType badge words = the FormalType entries of the Doc Type
// filter (Project is a plain node type and lives in state.js TYPE_ENTRIES).
export const FORMAL_TYPES = ['Paper', 'Plan', 'Model', 'Law', 'Dataset'];

// FormalType raw text ("Documento / report --> rapporti, ...") -> short badge
// word. Only the part before "-->" is matched; '' means "no match" and the
// caller keeps the generic "Document" badge.
export function formalBadge(ft) {
  if (!ft) return '';
  const head = String(ft).split('-->')[0].toLowerCase();
  if (head.includes('docum'))                             return 'Paper';
  if (head.includes('modell'))                            return 'Model';
  if (head.includes('piano') || head.includes('program')) return 'Plan';
  if (head.includes('normativ') || head.includes('atto')) return 'Law';
  if (head.includes('dataset') || head.includes('dati'))  return 'Dataset';
  return '';
}

// Purpose taxonomy label -> pastel chip class on the node page (chip colors
// live in node.css; labels come from query_util.PURPOSE_CATEGORIES and match
// the search page's PURPOSE_ENTRIES values, so a chip can deep-link to the
// matching Purpose facet: index.html?purpose=...).
export const PURPOSE_CLASS = {
  'Valutazione':        'pv-val',
  'Monitoraggio':       'pv-mon',
  'Ricostruzione':      'pv-ric',
  'Previsione':         'pv-pre',
  'Supporto decisioni': 'pv-sup',
  'Governance':         'pv-gov',
  'Gap di conoscenza':  'pv-gap',
};

// Card headline = C2 (English) when present; Rec/Gap/Project have no
// officialTitle, so they fall back to their body text.
export function nameOf(r) {
  return r.officialTitle || r.title || r.content || r.description || 'Unnamed result';
}

// ---- ALL-CAPS titles -> Title Case -----------------------------------------
// Many legacy titles are stored in capitals ("THE ORIGIN OF THE SAND IN THE
// VENICE LAGOON"). Sentence case would destroy the proper nouns (venice,
// adriatic, ...), which cannot be told apart from ordinary words once the
// text is all caps, so we use Title Case instead (John Gruber's titlecase
// rules): every word capitalised except small words, which stay lowercase
// unless they open the title or follow ':' '.' '–'.
// Only "shouting" titles are touched (most words in capitals), and in
// those only the fully capitalised words: mixed-case words such as species
// names ("Hexaplex trunculus") or "PCBs" are kept exactly as written.
// Acronyms cannot be detected from capitals alone; known ones are listed
// below (extend the list when a new one shows up wrongly cased).
const ACRONYMS = new Map(Object.entries({
  ARPAV: 'ARPAV', CNR: 'CNR', CORILA: 'CORILA', CVM: 'CVM', DEM: 'DEM',
  DNA: 'DNA', ELGIRA: 'ELGIRA', ESA: 'ESA', EU: 'EU', GIS: 'GIS', GPS: 'GPS', HF: 'HF',
  ICZM: 'ICZM', IPCC: 'IPCC', ISPRA: 'ISPRA', IUCN: 'IUCN', LIDAR: 'LiDAR',
  MDDS: 'MDDS', MOSE: 'MOSE', MSFD: 'MSFD', NASA: 'NASA', PAHS: 'PAHs',
  PCBS: 'PCBs', POPS: 'POPs', SAR: 'SAR', SIDEV: 'SIDEV', SLR: 'SLR',
  UE: 'UE', UNEP: 'UNEP', UNESCO: 'UNESCO', VELFEEM: 'VELFEEM', WFD: 'WFD',
}));
const SMALL_WORDS = new Set([
  // English
  'a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'from', 'in', 'into',
  'nor', 'of', 'on', 'onto', 'or', 'per', 'the', 'to', 'via', 'vs', 'with',
  'et', // Latin, in species authorities ("Tomlinson et Posluzny")
  // Italian
  'ai', 'al', 'alla', 'alle', 'con', 'da', 'dai', 'dal', 'dalla', 'dei',
  'degli', 'del', 'della', 'delle', 'di', 'e', 'ed', 'fra', 'gli', 'il',
  'la', 'le', 'lo', 'nei', 'nel', 'nella', 'nelle', 'su', 'sul', 'sulla', 'tra',
]);
// Italian elisions keep a capital after the apostrophe (Sant'Erasmo,
// Dell'Ambiente); English possessives do not (Lagoon's).
const ELIDED = new Set(['all', 'dall', 'dell', 'nell', 'sull', 'sant', 'l', 'd', 'un']);
// Roman numerals of 2+ letters (II, XVI, LXIII); D and M are left out so
// that words like "DI" are never mistaken for one.
const ROMAN = /^(?=[IVXLC]{2,}$)C{0,3}(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$/;

const isCaps = w => /[A-ZÀ-Þ]/.test(w) && w === w.toUpperCase();
const cap = w => w.charAt(0).toUpperCase() + w.slice(1);

function isShouting(text) {
  const words = String(text).match(/[A-Za-zÀ-ÿ]{2,}/g) || [];
  return words.length > 0 && words.filter(isCaps).length / words.length >= 0.6;
}

// One letters-only chunk (no hyphens/apostrophes) of an all-caps word.
function caseChunk(chunk, smallAllowed) {
  if (ACRONYMS.has(chunk)) return ACRONYMS.get(chunk);
  if (ROMAN.test(chunk)) return chunk;
  if (/\d/.test(chunk)) return chunk;                  // 3D, PM10, CO2
  const lower = chunk.toLowerCase();
  if (smallAllowed && SMALL_WORDS.has(lower)) return lower;  // a, e, the, di
  if (chunk.length === 1) return chunk;
  return cap(lower);
}

// An all-caps word, possibly hyphenated (Two-Dimensional, Re-Use) or with an
// apostrophe (Microlayer's, Sant'Erasmo).
function caseWord(word, smallAllowed) {
  return word.split('-').map((part, i) => {
    const [head, ...rest] = part.split(/(['’])/);
    let out = caseChunk(head, smallAllowed && i === 0);
    for (let j = 0; j < rest.length; j += 2) {
      const tail = rest[j + 1] || '';
      out += rest[j] + (ELIDED.has(head.toLowerCase()) ? cap(tail.toLowerCase()) : tail.toLowerCase());
    }
    return out;
  }).join('-');
}

export function displayTitle(text) {
  const str = String(text == null ? '' : text);
  if (!isShouting(str)) return str;
  let sentenceStart = true;
  return str.split(/(\s+)/).map(tok => {
    if (/^\s+$/.test(tok) || tok === '') return tok;
    // Split off leading/trailing punctuation: ("VENICE, ...  'VENICE
    const m = tok.match(/^([^A-Za-zÀ-ÿ0-9]*)(.*?)([^A-Za-zÀ-ÿ0-9]*)$/);
    const [, lead, core, trail] = m;
    let out = core;
    // Initials stay as written: "A." (Tomasin A.), "J.D" (J.D. Paduan),
    // "S." (S. Erasmo). A word glued to a hyphen ("DI-") is a prefix, not
    // a small word.
    const initials = /^[A-Z](\.[A-Z])*$/.test(core) && trail.startsWith('.');
    if (core && isCaps(core) && !initials) out = caseWord(core, !sentenceStart && !trail.startsWith('-'));
    if (core) sentenceStart = /[:.?!]$/.test(trail);
    else sentenceStart = sentenceStart || /^[–—:-]$/.test(tok);
    return lead + out + trail;
  }).join('');
}

export function truncate(s, maxLen) {
  const str = String(s == null ? '' : s);
  return str.length > maxLen ? str.slice(0, maxLen - 1) + '\u2026' : str;
}

// DOI document type (Crossref-style 'journal-article', DataCite-style
// 'JournalArticle', ...) -> readable label. Unknown values are split on
// dashes/camelCase and sentence-cased; '' when absent.
const DOC_TYPE_LABEL = {
  'journal-article':     'Journal article',
  'journalarticle':      'Journal article',
  'book-chapter':        'Book chapter',
  'edited-book':         'Edited book',
  'posted-content':      'Preprint',
  'proceedings-article': 'Proceedings article',
};
export function prettyDocType(raw) {
  if (!raw) return '';
  const s = String(raw).trim();
  const known = DOC_TYPE_LABEL[s.toLowerCase()];
  if (known) return known;
  const words = s.replace(/([a-z])([A-Z])/g, '$1 $2').split(/[-_\s]+/).filter(Boolean);
  const text = words.join(' ').toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// Preview is a truncated (CSS-clamped) glance; `.full` shows the whole text.
export function previewText(r) {
  if (r.type === 'Oggetto Informativo') return r.abstract || '';
  if (r.type === 'Raccomandazione')    return r.content || r.title || '';
  if (r.type === 'Lacuna')             return r.description || r.title || '';
  if (r.type === 'Progetto')           return r.description || r.title || '';
  return r.title || '';
}

// The expanded body, one labelled field per section. Label includes the colon;
// an empty label means "plain text field" (unknown node types).
export function fullFields(r) {
  if (r.type === 'Oggetto Informativo') {
    return [
      { label: 'Abstract:',  text: r.abstract || '' },
      { label: 'Findings:',  text: r.findings || '' },
    ];
  }
  if (r.type === 'Raccomandazione') {
    const fields = [{ label: 'Contenuto:', text: r.content || '' }];
    if (r.motivation) fields.push({ label: 'Motivazione:', text: r.motivation });
    return fields;
  }
  if (r.type === 'Lacuna' || r.type === 'Progetto') {
    return [{ label: 'Descrizione:', text: r.description || '' }];
  }
  return [{ label: '', text: nameOf(r) }];
}

// Fulltext raw scores are BM25-like and unbounded; normalize to 0-1 relative
// to the best/worst in this batch (mirrors query_util.format_scores).
// Only `nl` and `keywords` results carry a 'fulltext' source; `title` and
// `author` results don't, which falls back to the neutral [0, 1] bounds and
// their raw scores are shown as-is.
export function ftBounds(results) {
  const scores = [];
  results.forEach(r => r.sources.forEach((s, i) => {
    if (s === 'fulltext') scores.push(r.rawScores[i]);
  }));
  return scores.length ? [Math.min(...scores), Math.max(...scores)] : [0, 1];
}

// One entry per source: its text plus whether the hit passed that branch's
// relevance threshold (`passed` from the API; absent on old saved results =>
// treated as passed). The card greys out the failing ones, which shows at a
// glance why a result landed in the "possibly unrelated" tier.
export function scoreParts(r, ftMin, ftMax) {
  return r.sources.map((src, i) => {
    const rank = r.sourceRanks[i];
    const score = r.rawScores[i];
    let text;
    if (src === 'fulltext' && ftMax > ftMin) {
      const norm = (score - ftMin) / (ftMax - ftMin);
      text = src + '#' + rank + ': ' + norm.toFixed(4) + ' (' + score.toFixed(4) + ')';
    } else {
      text = src + '#' + rank + ': ' + score.toFixed(4);
    }
    return { text, passed: !r.passed || r.passed[i] !== false };
  });
}

export function scoreText(r, ftMin, ftMax) {
  return scoreParts(r, ftMin, ftMax).map(p => p.text).join(', ');
}

// Numeric parameter from a (possibly emptied/typed-over) input: rounded when
// it is a finite number >= 1, the given default otherwise.
export function clampInt(value, fallback) {
  const v = Number(value);
  return Number.isFinite(v) && v >= 1 ? Math.round(v) : fallback;
}
