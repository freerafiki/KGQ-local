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
  'Raccomandazione':     'Rec',
  'Lacuna':              'Gap',
  'Progetto':            'Project',
};

// The 5 FormalType badge words = the document entries of the Doc Type filter.
export const FORMAL_TYPES = ['Paper', 'Model', 'Plan', 'Norm', 'Dataset'];

// FormalType raw text ("Documento / report --> rapporti, ...") -> short badge
// word. Only the part before "-->" is matched; '' means "no match" and the
// caller keeps the generic "Document" badge.
export function formalBadge(ft) {
  if (!ft) return '';
  const head = String(ft).split('-->')[0].toLowerCase();
  if (head.includes('docum'))                             return 'Paper';
  if (head.includes('modell'))                            return 'Model';
  if (head.includes('piano') || head.includes('program')) return 'Plan';
  if (head.includes('normativ') || head.includes('atto')) return 'Norm';
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

export function truncate(s, maxLen) {
  const str = String(s == null ? '' : s);
  return str.length > maxLen ? str.slice(0, maxLen - 1) + '\u2026' : str;
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

export function scoreText(r, ftMin, ftMax) {
  const parts = [];
  r.sources.forEach((src, i) => {
    const rank = r.sourceRanks[i];
    const score = r.rawScores[i];
    if (src === 'fulltext' && ftMax > ftMin) {
      const norm = (score - ftMin) / (ftMax - ftMin);
      parts.push(src + '#' + rank + ': ' + norm.toFixed(4) + ' (' + score.toFixed(4) + ')');
    } else {
      parts.push(src + '#' + rank + ': ' + score.toFixed(4));
    }
  });
  return parts.join(', ');
}

// Numeric parameter from a (possibly emptied/typed-over) input: rounded when
// it is a finite number >= 1, the given default otherwise.
export function clampInt(value, fallback) {
  const v = Number(value);
  return Number.isFinite(v) && v >= 1 ? Math.round(v) : fallback;
}
