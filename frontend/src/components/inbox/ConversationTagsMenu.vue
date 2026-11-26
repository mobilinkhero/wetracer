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
 * The open conversation's tags, in its header: a Tags button opening a menu
 * of every defined tag, ticked where one is on the chat. Picking one toggles
 * it — the parent does the adding and removing.
 */
import { computed, onMounted } from 'vue'
import { navIconSvg } from '@/components/layout/navIcons'
import ActionMenu, { type ActionMenuItem } from '@/components/common/ActionMenu.vue'
import { useConversationTags } from '@/composables/useConversationTags'

const props = defineProps<{
  /** The tags on this conversation (slugs) */
  tags: string[]
  disabled?: boolean
}>()

const emit = defineEmits<{
  (e: 'toggle', slug: string): void
}>()

const { definitions, ensureTags } = useConversationTags()
onMounted(() => void ensureTags())

const items = computed<ActionMenuItem[]>(() =>
  definitions.value.map((def) => ({
    id: def.slug,
    label: def.label,
    checked: props.tags.includes(def.slug),
  })),
)

const applied = computed(() => props.tags.length)
const label = computed(() =>
  applied.value === 0 ? 'Tags' : applied.value === 1 ? '1 tag' : `${applied.value} tags`,
)
</script>

<template>
  <ActionMenu
    :label="applied ? `Tags: ${applied} on this conversation. Change tags` : 'Tags. Add or remove tags'"
    :items="items"
    trigger-class="tags-btn"
    @select="emit('toggle', $event)"
  >
    <span aria-hidden="true" v-html="navIconSvg('tag', 14)"></span>
    <span class="tags-label">{{ label }}</span>
  </ActionMenu>
</template>

<style scoped>
/* The trigger lives inside ActionMenu: reach it with :deep, as ThreadHeader does */
:deep(.tags-btn) {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 10px;
  border-radius: 8px;
  border: 1px solid var(--o10);
  background: var(--o04);
  color: var(--text2);
  font: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
}

:deep(.tags-btn:hover) {
  background: var(--o08);
  color: var(--text);
}

:deep(.tags-btn:focus-visible) {
  outline: 2px solid var(--accent-border);
  outline-offset: 1px;
}

.tags-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
