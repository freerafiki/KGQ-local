// One C3 URL/DOI row on the node page: the link (external document, or the
// entity's own node page) or plain text when there is nothing clickable,
// plus the caption line (Reference name + leftover properties).

export default {
  name: 'UrlItem',
  props: {
    // { main, href, external, extra }
    item: { type: Object, required: true },
  },
  template: `
<div class="url-item">
  <a v-if="item.href" :href="item.href" :target="item.external ? '_blank' : null" :rel="item.external ? 'noopener' : null">{{ item.main }}</a>
  <template v-else>{{ item.main }}</template>
  <div v-if="item.extra" class="rel-extra">{{ item.extra }}</div>
</div>
`,
};
