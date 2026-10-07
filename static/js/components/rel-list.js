// A related list on the node page (Environment, Geographic area, ... and the
// "Assisted by" table). The "Show N more" split happens in the page: it
// renders this component once for the head slice and once inside the
// <details> for the rest, so the limit lives with the page's other limits.

import RelItem from './rel-item.js';

export default {
  name: 'RelList',
  components: { RelItem },
  props: {
    items: { type: Array, required: true },
  },
  template: `
<ul class="rel-list">
  <rel-item v-for="(item, i) in items" :key="i" :item="item"></rel-item>
</ul>
`,
};
