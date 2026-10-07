// One result card. Clicking anywhere on it opens the node page (the full
// detail lives there); only "Show more" toggles the expanded body in place.

import { computed, ref } from '../../vendor/vue.esm-browser.prod.js';
import {
  BADGE_CLASS, BADGE_SHORT, formalBadge, nameOf, truncate,
  previewText, fullFields, scoreText,
} from '../dom.js';

export default {
  name: 'ResultCard',
  props: {
    result: { type: Object, required: true },
    // Rendered inside a group under its parent card: the parent row would
    // just repeat the card right above it, so it is suppressed.
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

    const name = computed(() => truncate(nameOf(props.result), 180));
    const preview = computed(() => previewText(props.result));
    const fields = computed(() => fullFields(props.result));

    // Reference person = the first contributing person (a result may have
    // none: institutions only, or no actors at all). The `r.people || r.actors`
    // fallback only fires when the split is missing (older stored payloads),
    // never when `people` is an empty list.
    const refPerson = computed(() => {
      const r = props.result;
      return (r.people || r.actors || [])[0] || '';
    });
    const year = computed(() => props.result.releaseYear || '');

    const metaBits = computed(() => {
      const bits = [];
      if (refPerson.value) bits.push({ label: 'Reference person:', text: truncate(refPerson.value, 80) });
      if (year.value) bits.push({ label: 'Year:', text: String(year.value) });
      return bits;
    });

    // Rec/Gap/Project point back to their source document. When the document
    // itself is NOT in the result list, the row names it by its doc type
    // ("Plan: ...") — the type word is missing only on payloads saved before
    // the backend sent it, in which case the row shows the title alone.
    const parentTitle = computed(() => {
      const r = props.result;
      if (r.type === 'Oggetto Informativo' || !r.parent_oi) return '';
      return truncate(r.parent_oi.officialTitle || r.parent_oi.title, 120);
    });
    const parentType = computed(() => {
      const r = props.result;
      if (r.type === 'Oggetto Informativo' || !r.parent_oi) return '';
      return formalBadge(r.parent_oi.formalType);
    });

    const wrrf = computed(() => Number(props.result.wrrf_score).toFixed(4));
    const scoreLine = computed(() => scoreText(props.result, props.ftMin, props.ftMax));

    return {
      open, badgeClass, badgeText, name, preview, fields,
      metaBits, parentTitle, parentType, wrrf, scoreLine,
    };
  },
  template: `
<div class="card" :class="{ open: open }" @click="$emit('open', result.neo4j_id)">
  <div class="card-head"><span class="name">{{ name }}</span><span class="badge" :class="badgeClass"><svg class="bic" aria-hidden="true"><use :href="'#ic-' + badgeClass"></use></svg>{{ badgeText }}</span></div>
  <div class="preview">{{ preview }}</div>
  <div class="full"><div v-for="f in fields" :key="f.label" class="field"><span v-if="f.label" class="flabel">{{ f.label }}</span> {{ f.text }}</div></div>
  <button type="button" class="toggle" @click.stop="open = !open">{{ open ? 'Show less' : 'Show more' }}</button>
  <div v-if="metaBits.length" class="card-meta"><template v-for="(bit, i) in metaBits" :key="bit.label"><span v-if="i" class="msep">|</span><span class="mlabel">{{ bit.label }}</span> {{ bit.text }}</template></div>
  <div class="scores"><b>WRRF {{ wrrf }}</b> | {{ scoreLine }}</div>
  <div v-if="!nested && parentTitle" class="parent-row"><span v-if="parentType" class="plabel">{{ parentType }}:</span> {{ parentTitle }}</div>
</div>
`,
};
