/*
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
*/

import { computed, ref } from 'vue'
import { tagsService } from '@/services/tags'
import type { TagDefinition } from '@/types/tags'

/** The tag definitions, loaded once per page and shared by every inbox pane. */
const definitions = ref<TagDefinition[]>([])
const loaded = ref(false)
let inFlight: Promise<TagDefinition[]> | null = null

/** Read the definitions; a failure leaves them empty rather than breaking the inbox. */
export async function ensureTags(): Promise<TagDefinition[]> {
  if (loaded.value) return definitions.value
  if (!inFlight) {
    inFlight = tagsService
      .listTags()
      .then((tags) => {
        definitions.value = [...tags].sort((a, b) => b.weight - a.weight || a.label.localeCompare(b.label))
        loaded.value = true
        return definitions.value
      })
      .catch((err) => {
        console.error('Failed to load conversation tags:', err)
        return []
      })
      .finally(() => {
        inFlight = null
      })
  }
  return inFlight
}

/** For tests: forget the cached definitions. */
export function resetTagsForTests(): void {
  definitions.value = []
  loaded.value = false
  inFlight = null
}

export function useConversationTags() {
  /** A definition by slug; an unknown slug (an old tag, a deleted one) is nothing. */
  const tagBySlug = (slug: string): TagDefinition | undefined =>
    definitions.value.find((t) => t.slug === slug)

  /** What a pill shows: the definition's label, or the slug itself when unknown. */
  const tagLabel = (slug: string): string => tagBySlug(slug)?.label ?? slug

  /** The pill's colours: the definition's colour as a tint, readable text. */
  const tagPillStyle = (slug: string): Record<string, string> => {
    const color = tagBySlug(slug)?.color
    if (!color) return {}
    return {
      background: `color-mix(in srgb, ${color} 14%, transparent)`,
      borderColor: `color-mix(in srgb, ${color} 45%, transparent)`,
      '--tag-dot': color,
    }
  }

  /** The definitions, in the groups the filter shows them in. */
  const grouped = computed(() => {
    const groups = new Map<string, TagDefinition[]>()
    for (const def of definitions.value) {
      const list = groups.get(def.group) ?? []
      list.push(def)
      groups.set(def.group, list)
    }
    return [...groups.entries()]
  })

  return { definitions, loaded, ensureTags, tagBySlug, tagLabel, tagPillStyle, grouped }
}
