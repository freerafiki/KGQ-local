// One linked node card on the node page: parent document ("Source
// Document"), connected Rec/Gap. The whole card opens the node page.
//
// The card is an <a>, so its purpose line stays plain text — nested anchors
// are invalid HTML. The facet links live on the overview's purpose chips
// (Main purpose / Secondary purpose rows).

export default {
  name: 'LinkCard',
  props: {
    // { href, badgeClass, badgeText, title, snippet, rel, purposes: 'A, B' }
    item: { type: Object, required: true },
  },
  template: `
<a class="link-item" :href="item.href">
  <span class="l-badge" :class="item.badgeClass"><svg class="bic" aria-hidden="true"><use :href="'#ic-' + item.badgeClass"></use></svg>{{ item.badgeText }}</span>
  <span class="l-title">{{ item.title }}</span>
  <div class="l-snippet">{{ item.snippet }}</div>
  <div v-if="item.rel" class="l-rel">Linked via {{ item.rel }}</div>
  <div v-if="item.purposes" class="l-rel">Purpose: {{ item.purposes }}</div>
</a>
`,
};
