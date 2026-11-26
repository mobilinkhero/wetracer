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

import api from './api'
import type { TagDefinition } from '@/types/tags'

/** The tags and score after an add or remove; null where the server said nothing. */
export interface ConversationTagsResult {
  tags: string[] | null
  priority_score: number | null
}

/**
 * Conversation tags (GET /admin/tags, POST/DELETE
 * /admin/conversations/{sessionId}/tags). The list endpoint answers with the
 * definitions array; an add or remove answers with the conversation's tags
 * and priority score afterwards. A field the server leaves out comes back
 * null, so the caller can fall back to what it already knows.
 */
export const tagsService = {
  async listTags(): Promise<TagDefinition[]> {
    const response = await api.get<TagDefinition[] | { tags: TagDefinition[] }>('/admin/tags')
    const data = response.data
    return Array.isArray(data) ? data : (data?.tags ?? [])
  },

  /** Add one tag; the new tag list and priority score come back. */
  async addConversationTag(sessionId: string, slug: string): Promise<ConversationTagsResult> {
    const response = await api.post<ConversationTagsResult>(
      `/admin/conversations/${encodeURIComponent(sessionId)}/tags`,
      { slug },
    )
    return normalizeTagsResult(response.data)
  },

  /** Remove one tag; the new tag list and priority score come back. */
  async removeConversationTag(sessionId: string, slug: string): Promise<ConversationTagsResult> {
    const response = await api.delete<ConversationTagsResult>(
      `/admin/conversations/${encodeURIComponent(sessionId)}/tags`,
      { data: { slug } },
    )
    return normalizeTagsResult(response.data)
  },
}

function normalizeTagsResult(
  data: Partial<ConversationTagsResult> | null | undefined,
): ConversationTagsResult {
  return {
    tags: Array.isArray(data?.tags) ? data.tags : null,
    priority_score: typeof data?.priority_score === 'number' ? data.priority_score : null,
  }
}
