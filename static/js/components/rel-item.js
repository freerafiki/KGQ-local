// One row of a related list on the node page: the neighbour's label plus an
// optional caption line. `item.href` decides how the label links:
//   external (http/doi URL) -> opens in a new tab,
//   internal (node.html?eid=...) -> same window,
//   empty                   -> plain text (no usable URL / no eid yet).

export default {
  name: 'RelItem',
  props: {
    item: { type: Object, required: true },
  },
  template: `
<li class="rel-item">
  <a v-if="item.external" :href="item.href" target="_blank" rel="noopener">{{ item.label }}</a>
  <a v-else-if="item.href" :href="item.href">{{ item.label }}</a>
  <template v-else>{{ item.label }}</template>
  <div v-if="item.extra" class="rel-extra">{{ item.extra }}</div>
</li>
`,
};
