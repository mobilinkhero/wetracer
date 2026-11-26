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

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { nextTick } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { defaultSort, inboxViewGroups, selectedViewItem } from '@/components/inbox/inboxViews'
import InboxList from '@/components/inbox/InboxList.vue'
import ConversationRow from '@/components/inbox/ConversationRow.vue'
import TagFilterChips from '@/components/inbox/TagFilterChips.vue'
import ConversationTagsMenu from '@/components/inbox/ConversationTagsMenu.vue'
import ThreadHeader from '@/components/inbox/ThreadHeader.vue'
import { visiblePillCount } from '@/components/inbox/tagPillFit'
import { tagsService } from '@/services/tags'
import { ensureTags, resetTagsForTests, useConversationTags } from '@/composables/useConversationTags'
import type { TagDefinition } from '@/types/tags'
import type { ChatDetail, Conversation } from '@/types/chat'

const apiMock = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }))
vi.mock('@/services/api', () => ({ default: apiMock }))
vi.mock('@/composables/useEnterpriseFeatures', () => ({
  useEnterpriseFeatures: () => ({
    hasEnterpriseModule: false,
    moduleImports: {},
    enterpriseComponent: () => ({ render: () => null }),
  }),
}))

const NOW = new Date('2026-11-13T12:00:00Z')
const ago = (m: number) => new Date(NOW.getTime() - m * 60_000).toISOString()

const DEFS: TagDefinition[] = [
  { slug: 'payment-received', label: 'Payment received', group: 'payment', weight: 90, color: '#2e7d32' },
  { slug: 'urgent', label: 'Urgent', group: 'priority', weight: 80, color: '#c62828' },
  { slug: 'vip', label: 'VIP', group: 'customer', weight: 40, color: '#6a1b9a' },
  { slug: 'sim-owner-details', label: 'SIM Owner Details', group: 'service', weight: 10, color: '#1565c0' },
]

const conversation = (fields: Partial<Conversation> = {}): Conversation => ({
  customer: { id: 'c1', email: 'maya@example.com', full_name: 'Maya Richter', phone: '+49 30 1234' },
  agent: { id: 'a1', name: 'Shop bot', display_name: 'Shop assistant', ai_replies_enabled: true },
  last_message: 'Hello, I need my SIM details',
  updated_at: ago(6),
  message_count: 3,
  session_id: 's1',
  user_id: null,
  status: 'open',
  channel: 'whatsapp',
  ...fields,
})

const chat = (fields: Partial<ChatDetail> = {}): ChatDetail => ({
  session_id: 's1',
  customer: { id: 'c1', email: 'maya@example.com', full_name: 'Maya Richter', phone: '+49 30 1234' },
  agent: { id: 'a1', name: 'Shop bot', display_name: 'Shop assistant', ai_replies_enabled: true },
  messages: [],
  created_at: ago(30),
  updated_at: ago(6),
  user_id: null,
  group_id: null,
  status: 'transferred',
  channel: 'whatsapp',
  handed_over_at: ago(6),
  ...fields,
})

beforeEach(() => {
  vi.clearAllMocks()
  resetTagsForTests()
  apiMock.get.mockResolvedValue({ data: DEFS })
  apiMock.post.mockResolvedValue({ data: { tags: ['urgent'], priority_score: 80 } })
  apiMock.delete.mockResolvedValue({ data: { tags: [], priority_score: 0 } })
})

describe('tagsService', () => {
  it('reads the definitions, as an array or wrapped', async () => {
    expect(await tagsService.listTags()).toEqual(DEFS)
    expect(apiMock.get).toHaveBeenCalledWith('/admin/tags')

    apiMock.get.mockResolvedValueOnce({ data: { tags: DEFS } })
    resetTagsForTests()
    expect(await tagsService.listTags()).toEqual(DEFS)
  })

  it('adds one tag with its slug, and removes one the same way', async () => {
    await tagsService.addConversationTag('s1', 'urgent')
    expect(apiMock.post).toHaveBeenCalledWith('/admin/conversations/s1/tags', { slug: 'urgent' })

    await tagsService.removeConversationTag('s1', 'urgent')
    expect(apiMock.delete).toHaveBeenCalledWith('/admin/conversations/s1/tags', { data: { slug: 'urgent' } })
  })

  it('leaves a field the server omitted as null, for the caller to work out', async () => {
    apiMock.post.mockResolvedValueOnce({ data: {} })
    expect(await tagsService.addConversationTag('s1', 'urgent')).toEqual({ tags: null, priority_score: null })
  })
})

describe('useConversationTags', () => {
  it('loads the definitions once, heaviest first, and reuses them', async () => {
    const first = await ensureTags()
    const second = await ensureTags()
    expect(apiMock.get).toHaveBeenCalledTimes(1)
    expect(first).toBe(second)
    expect(first.map((d) => d.slug)).toEqual(['payment-received', 'urgent', 'vip', 'sim-owner-details'])
  })

  it('labels a tag, or falls back to the slug when it is unknown', async () => {
    const { tagLabel, tagPillStyle } = useConversationTags()
    await ensureTags()
    expect(tagLabel('urgent')).toBe('Urgent')
    expect(tagLabel('gone-tag')).toBe('gone-tag')
    expect(tagPillStyle('urgent').background).toContain('#c62828')
    expect(tagPillStyle('gone-tag')).toEqual({})
  })
})

describe('TagFilterChips', () => {
  const mountChips = (selected: string[] = []) =>
    mount(TagFilterChips, { props: { tags: DEFS, selected } })

  it('is collapsed by default, with a Filter by tag button and no chips', () => {
    const wrapper = mountChips()
    expect(wrapper.find('.filter-toggle').text()).toContain('Filter by tag')
    expect(wrapper.find('.tag-panel').exists()).toBe(false)
    expect(wrapper.find('.tag-chip').exists()).toBe(false)
  })

  it('expands to one label line per group, with Service behind More tags', async () => {
    const wrapper = mountChips()
    await wrapper.find('.filter-toggle').trigger('click')
    expect(wrapper.findAll('.group-label').map((g) => g.text())).toEqual([
      'Payment', 'Priority', 'Customer',
    ])
    expect(wrapper.findAll('.tag-chip').map((c) => c.text())).toEqual([
      'Payment received', 'Urgent', 'VIP',
    ])
    expect(wrapper.find('.more-tags').text()).toContain('More tags')
    await wrapper.find('.more-tags').trigger('click')
    expect(wrapper.findAll('.group-label').map((g) => g.text())).toEqual([
      'Payment', 'Priority', 'Customer', 'Service',
    ])
    expect(wrapper.findAll('.tag-chip').map((c) => c.text())).toEqual([
      'Payment received', 'Urgent', 'VIP', 'SIM Owner Details',
    ])
  })

  it('collapses again when the toggle is clicked', async () => {
    const wrapper = mountChips()
    await wrapper.find('.filter-toggle').trigger('click')
    expect(wrapper.find('.tag-panel').exists()).toBe(true)
    await wrapper.find('.filter-toggle').trigger('click')
    expect(wrapper.find('.tag-panel').exists()).toBe(false)
  })

  it('shows how many tags are selected, and clears them', async () => {
    const wrapper = mountChips(['urgent', 'vip'])
    expect(wrapper.find('.count-badge').text()).toBe('2 selected')
    expect(wrapper.find('.clear').exists()).toBe(true)
    await wrapper.find('.clear').trigger('click')
    expect(wrapper.emitted('update:selected')).toEqual([[[]]])
  })

  it('toggles one chip on and off', async () => {
    const wrapper = mountChips()
    await wrapper.find('.filter-toggle').trigger('click')
    await wrapper.findAll('.tag-chip')[1].trigger('click')
    expect(wrapper.emitted('update:selected')).toEqual([[['urgent']]])

    const selected = mountChips(['urgent', 'vip'])
    await selected.find('.filter-toggle').trigger('click')
    await selected.findAll('.tag-chip')[1].trigger('click')
    expect(selected.emitted('update:selected')).toEqual([[['vip']]])
  })
})

describe('visiblePillCount', () => {
  it('keeps every pill that fits', () => {
    expect(visiblePillCount([100, 180, 240], 260, 3)).toBe(3)
  })

  it('drops pills past the edge, leaving room for +N', () => {
    expect(visiblePillCount([200, 300, 420], 260, 4)).toBe(1)
    expect(visiblePillCount([100, 200, 280], 260, 3)).toBe(2)
  })

  it('shows none when even one pill does not fit', () => {
    expect(visiblePillCount([300], 260, 1)).toBe(0)
    expect(visiblePillCount([], 260, 0)).toBe(0)
  })
})

describe('ConversationRow tags', () => {
  it('shows the tags as pills beside the AI badge, three at most', async () => {
    const wrapper = mount(ConversationRow, {
      props: {
        conversation: conversation({ tags: ['payment-received', 'urgent', 'sim-owner-details', 'vip'] }),
        active: false,
        currentUserId: 'me',
        now: NOW,
      },
    })
    await flushPromises()
    expect(wrapper.find('.chip-ai').text()).toBe('AI · Shop assistant')
    const pills = wrapper.findAll('.tag-pill')
    expect(pills.map((p) => p.text())).toEqual(['Payment received', 'Urgent', 'SIM Owner Details', '+1'])
  })

  it('shows only the pills that fit, and the rest behind +N', async () => {
    const rect = vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      if (this.classList.contains('chips')) return { right: 260 } as DOMRect
      if (this.classList.contains('tag-pill')) {
        const right: Record<string, number> = {
          'Payment received': 200,
          Urgent: 300,
          'SIM Owner Details': 420,
        }
        return { right: right[this.textContent?.trim() ?? ''] ?? 0 } as DOMRect
      }
      return { right: 0 } as DOMRect
    })
    try {
      const wrapper = mount(ConversationRow, {
        props: {
          conversation: conversation({ tags: ['payment-received', 'urgent', 'sim-owner-details', 'vip'] }),
          active: false,
          currentUserId: 'me',
          now: NOW,
        },
      })
      await flushPromises()
      await nextTick()
      await nextTick()
      expect(wrapper.findAll('.tag-pill').map((p) => p.text())).toEqual(['Payment received', '+3'])
    } finally {
      rect.mockRestore()
    }
  })

  it('shows nothing extra on an untagged conversation, and the slug for an unknown tag', async () => {
    const plain = mount(ConversationRow, {
      props: { conversation: conversation(), active: false, currentUserId: 'me', now: NOW },
    })
    expect(plain.find('.tag-pill').exists()).toBe(false)

    const unknown = mount(ConversationRow, {
      props: {
        conversation: conversation({ tags: ['old-tag'] }),
        active: false,
        currentUserId: 'me',
        now: NOW,
      },
    })
    await flushPromises()
    expect(unknown.find('.tag-pill').text()).toBe('old-tag')
  })
})

describe('ConversationTagsMenu', () => {
  it('ticks the tags already on the chat, and toggles the one picked', async () => {
    const wrapper = mount(ConversationTagsMenu, { props: { tags: ['urgent'] } })
    await flushPromises()

    const trigger = wrapper.find('button[aria-haspopup="menu"]')
    expect(trigger.text()).toBe('1 tag')
    await trigger.trigger('click')
    const items = wrapper.findAll('[role="menuitemradio"]')
    expect(items.map((i) => i.text())).toEqual([
      'Payment received', '✓Urgent', 'VIP', 'SIM Owner Details',
    ])
    expect(items[1].attributes('aria-checked')).toBe('true')
    expect(items[0].attributes('aria-checked')).toBe('false')

    await items[0].trigger('click')
    expect(wrapper.emitted('toggle')).toEqual([['payment-received']])
  })
})

describe('ThreadHeader tags', () => {
  it('forwards a tag picked in the header menu', async () => {
    const wrapper = mount(ThreadHeader, {
      props: {
        chat: chat({ tags: ['urgent'] }),
        currentUserId: 'me',
        detailsOpen: false,
        assignees: null,
        moreActions: [],
        canClose: false,
        showBack: false,
      },
    })
    await flushPromises()
    await wrapper.find('button[aria-haspopup="menu"]').trigger('click')
    await wrapper.findAll('[role="menuitemradio"]')[2].trigger('click')
    expect(wrapper.emitted('toggle-tag')).toEqual([['vip']])
  })
})

describe('InboxList tags and priority sort', () => {
  const groups = inboxViewGroups({ open: 3, waiting_for_human: 1, ai_handling: 0, resolved_by_ai_today: 0, negative: 0, assigned_to_me: 0, mentions: 0, snoozed: 0, channels: {} })
  const mountList = (props: Record<string, unknown> = {}) =>
    mount(InboxList, {
      props: {
        view: selectedViewItem(groups, { view: 'open', channel: null }),
        groups,
        conversations: [conversation({ tags: ['urgent'], priority_score: 80 })],
        selectedSessionId: null,
        unread: {},
        currentUserId: 'me',
        now: NOW,
        loading: false,
        loadingMore: false,
        hasMore: false,
        error: '',
        showViewPicker: false,
        canRestoreViews: false,
        canStartConversation: false,
        showNavButton: false,
        narrowed: false,
        filterAgents: [],
        filterTeammates: [],
        sort: 'priority',
        search: '',
        filters: { agentId: '', assigneeId: '' },
        ...props,
      },
    })

  it('offers Priority first in the sort, and reports a change back', async () => {
    const wrapper = mountList()
    expect(wrapper.find('.sort-select').findAll('option').map((o) => o.attributes('value'))).toEqual([
      'priority', 'newest', 'waiting_longest',
    ])
    expect(wrapper.find('.sort-select').findAll('option').map((o) => o.text())).toEqual([
      'Sort: Priority', 'Sort: Newest', 'Sort: Waiting longest',
    ])
    await wrapper.find('.sort-select').setValue('newest')
    expect(wrapper.emitted('update:sort')).toEqual([['newest']])
  })

  it('filters by tag chips, and reports the selection', async () => {
    const wrapper = mountList()
    await flushPromises()
    expect(wrapper.findAll('.tag-chip').length).toBe(0)
    await wrapper.find('.filter-toggle').trigger('click')
    expect(wrapper.findAll('.tag-chip').length).toBe(3)
    await wrapper.findAll('.tag-chip')[0].trigger('click')
    expect(wrapper.emitted('update:selectedTags')).toEqual([[['payment-received']]])
  })

  it('shows the row tags without a filter when there are no definitions', async () => {
    apiMock.get.mockResolvedValue({ data: [] })
    const wrapper = mountList()
    await flushPromises()
    expect(wrapper.find('.tag-filter').exists()).toBe(false)
    expect(wrapper.find('.tag-pill').text()).toBe('urgent')
  })
})

describe('defaultSort', () => {
  it('lists by priority first, the waiting queue longest-waiting first', () => {
    expect(defaultSort({ view: 'open', channel: null })).toBe('priority')
    expect(defaultSort({ view: 'mine', channel: null })).toBe('priority')
    expect(defaultSort({ view: 'waiting', channel: null })).toBe('waiting_longest')
  })
})
