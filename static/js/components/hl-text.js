// Text with the matched query words wrapped in <mark class="hl">. Rendered
// from segments with plain interpolation (never v-html), so any text is safe.

import { computed } from '../../vendor/vue.esm-browser.prod.js';
import { markParts } from '../dom.js';

export default {
  name: 'HlText',
  props: {
    text: { type: String, default: '' },
    terms: { type: Array, default: () => [] },
  },
  setup(props) {
    const parts = computed(() => markParts(props.text, props.terms));
    return { parts };
  },
  template: `<template v-for="(p, i) in parts" :key="i"><mark v-if="p.hit" class="hl">{{ p.t }}</mark><template v-else>{{ p.t }}</template></template>`,
};
