// One contributor facet: a live-filtered checkbox list plus the selected
// chips. The page has two of them (People, Institutions) with identical
// behaviour, so they are one component parameterised by facet key.

import { computed } from '../../vendor/vue.esm-browser.prod.js';
import { store, FACET_ENTRIES, FACET_NAMES } from '../state.js';

export default {
  name: 'ActorFacet',
  props: {
    // Key into store.facets / FACET_ENTRIES ("person" | "institution").
    facet: { type: String, required: true },
  },
  setup(props) {
    const entry = FACET_ENTRIES.find(e => e.key === props.facet);
    const state = store.facets[props.facet];
    const names = FACET_NAMES[props.facet];

    // The live-typed query only narrows WHICH entries are listed; it is also
    // applied to the results themselves (see visibleResults in state.js).
    const visibleNames = computed(() => {
      const q = state.query.trim().toLowerCase();
      return q ? names.value.filter(n => n.toLowerCase().includes(q)) : names.value;
    });

    // v-model on the checkboxes does the add/remove: `selected` is an array.
    function remove(name) {
      state.selected = state.selected.filter(s => s !== name);
    }

    return { entry, state, visibleNames, remove };
  },
  template: `
<details class="filter-group">
  <summary>{{ entry.title }}</summary>
  <div class="group-body">
    <input type="text" class="actor-input" placeholder="Type to filter…" autocomplete="off" v-model="state.query">
    <div class="actor-list">
      <div v-if="!visibleNames.length" class="actor-empty">{{ entry.emptyMsg }}</div>
      <label v-for="n in visibleNames" :key="n" class="actor-item"><input type="checkbox" :value="n" v-model="state.selected"> {{ n }}</label>
    </div>
    <div class="actor-chips"><span v-for="n in state.selected" :key="n" class="chip">{{ n }}<button type="button" title="Remove" @click="remove(n)">&#215;</button></span></div>
  </div>
</details>
`,
};
