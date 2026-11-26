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
 * The tag filter above the inbox list: one toggle chip per defined tag, in
 * the tag's colour. Several chips on means a conversation carrying any of
 * them; Clear takes them all off.
 */
import { computed } from 'vue'
import type { TagDefinition } from '@/types/tags'

const props = defineProps<{
  tags: TagDefinition[]
  selected: string[]
}>()

const emit = defineEmits<{
  (e: 'update:selected', selected: string[]): void
}>()

/** The chips, grouped by what the tag is about, heaviest first. */
const groups = computed(() => {
  const byGroup = new Map<string, TagDefinition[]>()
  for (const def of props.tags) {
    const list = byGroup.get(def.group) ?? []
    list.push(def)
    byGroup.set(def.group, list)
  }
  return [...byGroup.entries()].map(([group, defs]) => ({
    group,
    defs: [...defs].sort((a, b) => b.weight - a.weight || a.label.localeCompare(b.label)),
  }))
})

const groupLabel = (group: string) => group.charAt(0).toUpperCase() + group.slice(1)

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
    <template v-for="({ group, defs }) in groups" :key="group">
      <span class="group-label" aria-hidden="true">{{ groupLabel(group) }}</span>
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
    </template>
    <button v-if="selected.length" type="button" class="clear" @click="clear">Clear</button>
  </div>
</template>

<style scoped>
.tag-filter {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  padding: 2px 0 0;
}

.group-label {
  margin-left: 6px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--muted2);
}

.group-label:first-child {
  margin-left: 0;
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
