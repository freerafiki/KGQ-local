// One result card. Clicking anywhere on it opens the node page (the full
// detail lives there); only "Show more" toggles the expanded body in place.

import { computed, ref } from '../../vendor/vue.esm-browser.prod.js';
import {
  BADGE_CLASS, BADGE_SHORT, formalBadge, nameOf, truncate,
  previewText, fullFields, scoreParts, prettyDocType, displayTitle,
} from '../dom.js';

export default {
  name: 'ResultCard',
  props: {
    result: { type: Object, required: true },
    // Rendered inside a group under its parent card: the provenance rows
    // would just repeat the card right above them, so they are suppressed.
    nested: { type: Boolean, default: false },
    // Fulltext normalization bounds of the current batch (see ftBounds).
    ftMin: { type: Number, default: 0 },
    ftMax: { type: Number, default: 1 },
  },
  emits: ['open'],
  setup(props) {
    // "Show more" state: per card, transient (never persisted).
    const open = ref(false);

    // Contribution cards use their FormalType badge word; other types use their own.
    const badgeClass = computed(() => {
      const r = props.result;
      const word = r.type === 'Oggetto Informativo' ? formalBadge(r.formalType) : '';
      return word ? word.toLowerCase() : (BADGE_CLASS[r.type] || 'ft');
    });

    const badgeText = computed(() => {
      const r = props.result;
      if (r.type !== 'Oggetto Informativo') return BADGE_SHORT[r.type] || 'FT';
      return formalBadge(r.formalType) || 'Document';
    });

    const name = computed(() => truncate(displayTitle(nameOf(props.result)), 180));
    const preview = computed(() => previewText(props.result));
    const fields = computed(() => fullFields(props.result));

    // Indication / Gap cards: no headline (their "title" is just their body
    // text) and no info line (author, year, ... belong to the parent document).
    const isChild = computed(() =>
      props.result.type === 'Raccomandazione' || props.result.type === 'Lacuna');

    // Info line under the card: First author | Year | Type | Journal, each
    // part only when present. firstAuthor/docType/journal are sent for
    // documents only (see query_util._serialize).
    const metaBits = computed(() => {
      const r = props.result;
      if (isChild.value) return [];
      const bits = [];
      if (r.firstAuthor) bits.push({ label: 'First author:', text: truncate(r.firstAuthor, 80) });
      // Documents always show the year slot ("-" when neither the release
      // year nor the DOI year is known); Projects only when they have one.
      const isDoc = r.type === 'Oggetto Informativo';
      if (r.releaseYear || isDoc) bits.push({ label: 'Year:', text: r.releaseYear ? String(r.releaseYear) : '-' });
      const docType = prettyDocType(r.docType);
      if (docType) bits.push({ label: 'Type:', text: docType });
      if (r.journal) bits.push({ label: 'Journal:', text: truncate(r.journal, 100) });
      return bits;
    });

    // Rec/Gap/Project point back to their source document. On a STANDALONE
    // child card the provenance is shown twice: the parent document at the
    // TOP of the card (which plan/paper the child hangs on, named by its doc
    // type — "Plan: ...") and, at the bottom, the one-line reason the card is
    // in the list at all. Both are suppressed when the card is nested: the
    // card right above already IS the parent. The type word is missing only
    // on payloads saved before the backend sent it, in which case the top row
    // shows the title alone.
    const parentTitle = computed(() => {
      const r = props.result;
      if (r.type === 'Oggetto Informativo' || !r.parent_oi) return '';
      return truncate(displayTitle(r.parent_oi.officialTitle || r.parent_oi.title), 120);
    });
    const parentType = computed(() => {
      const r = props.result;
      if (r.type === 'Oggetto Informativo' || !r.parent_oi) return '';
      return formalBadge(r.parent_oi.formalType);
    });
    const showParent = computed(() => !props.nested && !!parentTitle.value);

    // The bottom line, per child type: it says WHICH relation put the card
    // here (an indication = a Recommendation, or a gap).
    const WHY_TEXT = {
      'Raccomandazione': 'included because of the indication',
      'Lacuna':          'included because of the gap',
    };
    const whyText = computed(() =>
      (showParent.value && WHY_TEXT[props.result.type]) || '');

    const wrrf = computed(() => Number(props.result.wrrf_score).toFixed(4));
    // Cross-encoder score (0-1, the tier decision when present); null when
    // the result was not reranked (author mode, reranker off, beyond top-N).
    const rerankScore = computed(() => {
      const s = props.result.rerank_score;
      return (s === null || s === undefined) ? '' : Number(s).toFixed(3);
    });
    const scoreLine = computed(() => scoreParts(props.result, props.ftMin, props.ftMax));

    return {
      open, badgeClass, badgeText, name, preview, fields, isChild,
      metaBits, showParent, parentTitle, parentType, whyText, wrrf, rerankScore, scoreLine,
    };
  },
  template: `
<div class="card" :class="{ open: open }" @click="$emit('open', result.neo4j_id)">
  <div v-if="showParent" class="parent-row parent-top"><span v-if="parentType" class="plabel">{{ parentType }}:</span> {{ parentTitle }}</div>
  <div v-if="isChild" class="card-head child-head"><div class="preview">{{ preview }}</div><span class="badge" :class="badgeClass"><svg class="bic" aria-hidden="true"><use :href="'#ic-' + badgeClass"></use></svg>{{ badgeText }}</span></div>
  <template v-else>
    <div class="card-head"><span class="name">{{ name }}</span><span class="badge" :class="badgeClass"><svg class="bic" aria-hidden="true"><use :href="'#ic-' + badgeClass"></use></svg>{{ badgeText }}</span></div>
    <div class="preview">{{ preview }}</div>
  </template>
  <div class="full"><div v-for="f in fields" :key="f.label" class="field"><span v-if="f.label" class="flabel">{{ f.label }}</span> {{ f.text }}</div></div>
  <button type="button" class="toggle" @click.stop="open = !open">{{ open ? 'Show less' : 'Show more' }}</button>
  <div v-if="metaBits.length" class="card-meta"><template v-for="(bit, i) in metaBits" :key="bit.label"><span v-if="i" class="msep">|</span><span class="mlabel">{{ bit.label }}</span> {{ bit.text }}</template></div>
  <div class="scores"><template v-if="rerankScore"><b>RERANK {{ rerankScore }}</b> | </template><b>WRRF {{ wrrf }}</b> | <template v-for="(p, i) in scoreLine" :key="i"><template v-if="i">, </template><span :class="{ fail: !p.passed }" :title="p.passed ? 'passed the relevance threshold' : 'below the relevance threshold'">{{ p.text }}</span></template></div>
  <div v-if="whyText" class="why-row">{{ whyText }}</div>
</div>
`,
};
