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

/**
 * Conversation tags: the AI (or the team) labels a chat — what it is about,
 * whether money changed hands, how urgent it is — and the inbox sorts by
 * them. Definitions come from the server (GET /admin/tags); a conversation
 * only carries the slugs.
 */
export interface TagDefinition {
  slug: string
  label: string
  /** priority | payment | service | status | customer */
  group: string
  /** Higher weighs more in the conversation's priority score */
  weight: number
  /** The pill's colour, a CSS colour */
  color: string
}

/** What adding or removing one tag answers with. */
export interface ConversationTagsResponse {
  tags: string[]
  priority_score: number
}
