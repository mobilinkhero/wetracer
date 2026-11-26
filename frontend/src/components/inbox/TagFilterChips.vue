<!--
Copyright 2024-2026 ChatterMate

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

<script setup lang="ts">
/**
 * The tag filter above the inbox list: collapsed to one button by default,
 * so the list shows the chats, not the filter. Open it for one toggle chip
 * per defined tag, in the tag's colour. Several chips on means a
 * conversation carrying any of them; Clear takes them all off. The Service
 * group stays behind "More tags" — it is ten chips on its own.
 */
import { computed, ref } from 'vue'
import type { TagDefinition } from '@/types/tags'
import { navIconSvg } from '@/components/layout/navIcons'

const props = defineProps<{
  tags: TagDefinition[]
  selected: string[]
}>()

const emit = defineEmits<{
  (e: 'update:selected', selected: string[]): void
}>()

/** The groups in the order the filter shows them; Service always last. */
const GROUP_ORDER = ['payment', 'priority', 'status', 'customer']
function groupRank(group: string): number {
  if (group === 'service') return Number.MAX_SAFE_INTEGER
  const at = GROUP_ORDER.indexOf(group)
  return at === -1 ? GROUP_ORDER.length : at
}

/** The chips, grouped by what the tag is about, heaviest first. */
const groups = computed(() => {
  const byGroup = new Map<string, TagDefinition[]>()
  for (const def of props.tags) {
    const list = byGroup.get(def.group) ?? []
    list.push(def)
    byGroup.set(def.group, list)
  }
  return [...byGroup.entries()]
    .map(([group, defs]) => ({
      group,
      defs: [...defs].sort((a, b) => b.weight - a.weight || a.label.localeCompare(b.label)),
    }))
    .sort((a, b) => groupRank(a.group) - groupRank(b.group))
})

/** Everything but Service, which has its own expander. */
const mainGroups = computed(() => groups.value.filter((g) => g.group !== 'service'))
const serviceGroup = computed(() => groups.value.find((g) => g.group === 'service'))

const groupLabel = (group: string) => group.charAt(0).toUpperCase() + group.slice(1)

/** Collapsed by default: the list shows the chats, not the filter. */
const open = ref(false)
/** The Service group stays behind "More tags" until asked for. */
const showServices = ref(false)

function toggleOpen(): void {
  open.value = !open.value
}

function toggleServices(): void {
  showServices.value = !showServices.value
}

/** The chip's colours: the tag's colour as a tint, readable text. */
const pillStyle = (def: TagDefinition): Record<string, string> => ({
  background: `color-mix(in srgb, ${def.color} 14%, transparent)`,
  borderColor: `color-mix(in srgb, ${def.color} 45%, transparent)`,
  '--tag-dot': def.color,
})

function toggle(slug: string) {
  const selected = props.selected.includes(slug)
    ? props.selected.filter((s) => s !== slug)
    : [...props.selected, slug]
  emit('update:selected', selected)
}

function clear() {
  emit('update:selected', [])
}
</script>

<template>
  <div class="tag-filter" role="group" aria-label="Filter by tag">
    <div class="filter-bar">
      <button
        type="button"
        class="filter-toggle"
        :aria-expanded="open"
        @click="toggleOpen"
      >
        <span class="filter-icon" aria-hidden="true" v-html="navIconSvg('filter', 14)"></span>
        Filter by tag
        <span v-if="selected.length" class="count-badge">{{ selected.length }} selected</span>
        <span class="toggle-chevron" :class="{ open }" aria-hidden="true" v-html="navIconSvg('chevronDown', 12)"></span>
      </button>
      <button v-if="selected.length" type="button" class="clear" @click="clear">Clear</button>
    </div>
    <div v-if="open" class="tag-panel">
      <div v-for="({ group, defs }) in mainGroups" :key="group" class="tag-group">
        <span class="group-label">{{ groupLabel(group) }}</span>
        <div class="group-chips">
          <button
            v-for="def in defs"
            :key="def.slug"
            type="button"
            class="tag-chip"
            :class="{ on: selected.includes(def.slug) }"
            :style="pillStyle(def)"
            :aria-pressed="selected.includes(def.slug)"
            :title="`Filter by ${def.label}`"
            @click="toggle(def.slug)"
          >
            <span class="tag-dot" aria-hidden="true"></span>
            {{ def.label }}
          </button>
        </div>
      </div>
      <div v-if="serviceGroup" class="tag-group">
        <button
          type="button"
          class="more-tags"
          :aria-expanded="showServices"
          @click="toggleServices"
        >
          {{ showServices ? 'Fewer tags' : 'More tags' }}
          <span class="toggle-chevron" :class="{ open: showServices }" aria-hidden="true" v-html="navIconSvg('chevronDown', 12)"></span>
        </button>
        <template v-if="showServices">
          <span class="group-label">Service</span>
          <div class="group-chips">
            <button
              v-for="def in serviceGroup.defs"
              :key="def.slug"
              type="button"
              class="tag-chip"
              :class="{ on: selected.includes(def.slug) }"
              :style="pillStyle(def)"
              :aria-pressed="selected.includes(def.slug)"
              :title="`Filter by ${def.label}`"
              @click="toggle(def.slug)"
            >
              <span class="tag-dot" aria-hidden="true"></span>
              {{ def.label }}
            </button>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tag-filter {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 2px 0 0;
}

.filter-bar {
  display: flex;
  align-items: center;
  gap: 6px;
}

.filter-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 8px;
  border-radius: 8px;
  border: 1px solid var(--o10);
  background: var(--o04);
  color: var(--text3);
  font: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
}

.filter-toggle:hover {
  background: var(--o08);
  color: var(--text);
}

.filter-icon {
  display: inline-flex;
}

.count-badge {
  display: inline-flex;
  align-items: center;
  height: 16px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--accent-solid);
  color: var(--on-accent-solid);
  font-size: 11px;
  font-weight: 700;
}

.toggle-chevron {
  display: inline-flex;
  transition: transform 120ms ease;
}

.toggle-chevron.open {
  transform: rotate(180deg);
}

.tag-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 2px;
}

/* One group per block: its label on a line of its own, chips below it. */
.tag-group {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.group-label {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--muted2);
}

.group-chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}

.more-tags {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent-ink);
  font: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}

.tag-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 22px;
  padding: 0 8px;
  border-radius: 999px;
  border: 1px solid var(--o10);
  background: var(--o04);
  color: var(--text2);
  font: inherit;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
}

.tag-chip:hover {
  background: var(--o08);
}

.tag-chip.on {
  color: var(--accent-ink);
  box-shadow: inset 0 0 0 1px var(--accent-border);
}

.tag-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--tag-dot, currentColor);
}

.clear {
  padding: 0 4px;
  border: 0;
  background: none;
  color: var(--accent-ink);
  font: inherit;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
}
</style>
