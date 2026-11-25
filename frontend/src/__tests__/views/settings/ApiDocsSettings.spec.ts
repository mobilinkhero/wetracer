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

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import ApiDocsSettings from '../../../views/settings/ApiDocsSettings.vue'

const apiMock = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))

vi.mock('@/services/api', () => ({ default: apiMock }))
vi.mock('@/services/widget', () => ({
  widgetService: {
    getWidgets: vi.fn().mockResolvedValue([
      { id: 'widget-1', name: 'Support', organization_id: 'org-1', agent_id: 'agent-1' },
    ]),
  },
}))
vi.mock('@/services/agent', () => ({
  agentService: {
    getAgentRoster: vi.fn().mockResolvedValue([
      { id: 'agent-1', name: 'Support', display_name: 'Support' },
    ]),
  },
}))
vi.mock('@/config/api', () => ({ getApiUrl: () => 'https://app.chatcati.com/api/v1' }))
vi.mock('vue-sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

// Clearly fake service-account JSON — never a real key.
const fakeKey = {
  type: 'service_account',
  project_id: 'test-project',
  private_key: '-----BEGIN PRIVATE KEY-----\nFAKE-KEY-FOR-TESTS-ONLY\n-----END PRIVATE KEY-----\n',
  client_email: 'firebase-adminsdk@test-project.iam.gserviceaccount.com',
}

const unconfigured = { configured: false, project_id: null, updated_at: null }
const configured = { configured: true, project_id: 'test-project', updated_at: '2026-11-25T10:00:00' }

describe('ApiDocsSettings — FCM service-account key', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiMock.get.mockResolvedValue({ data: unconfigured })
    apiMock.post.mockResolvedValue({ data: { configured: true, project_id: 'test-project' } })
  })

  const mountPage = async (fcm = unconfigured) => {
    apiMock.get.mockResolvedValueOnce({ data: fcm })
    const wrapper = mount(ApiDocsSettings, {
      global: {
        stubs: {
          DashboardLayout: { template: '<div class="dashboard-layout"><slot /></div>' },
          CopyField: {
            props: ['fieldId', 'label', 'name', 'value'],
            template: '<div class="copy-field-stub" />',
          },
        },
      },
    })
    await flushPromises()
    await flushPromises()
    return wrapper
  }

  const saveButton = (wrapper: VueWrapper) => wrapper.find('.fcm-key-actions .action-btn')
  const textarea = (wrapper: VueWrapper) => wrapper.find('#fcm-service-account')

  it('shows the key form next to the Test mode status', async () => {
    const wrapper = await mountPage()
    expect(wrapper.find('.status-badge.is-test').text()).toContain('Test mode')
    expect(textarea(wrapper).exists()).toBe(true)
    expect(saveButton(wrapper).text()).toBe('Save FCM key')
  })

  it('rejects invalid JSON without calling the API', async () => {
    const wrapper = await mountPage()
    await textarea(wrapper).setValue('{not json')
    await saveButton(wrapper).trigger('click')
    await flushPromises()
    expect(wrapper.find('.fcm-key-form .error').text()).toContain('not valid JSON')
    expect(apiMock.post).not.toHaveBeenCalled()
  })

  it.each([
    ['wrong type', { ...fakeKey, type: 'authorized_user' }, 'service_account'],
    ['missing project_id', { ...fakeKey, project_id: '' }, 'project_id'],
    ['missing private_key', { ...fakeKey, private_key: '' }, 'private_key'],
  ])('rejects a key with %s without calling the API', async (_name, key, fragment) => {
    const wrapper = await mountPage()
    await textarea(wrapper).setValue(JSON.stringify(key))
    await saveButton(wrapper).trigger('click')
    await flushPromises()
    expect(wrapper.find('.fcm-key-form .error').text()).toContain(fragment)
    expect(apiMock.post).not.toHaveBeenCalled()
  })

  it('saves a valid key, clears the field and refreshes the status to Live', async () => {
    const wrapper = await mountPage()
    apiMock.get.mockResolvedValue({ data: configured })
    await textarea(wrapper).setValue(JSON.stringify(fakeKey))
    await saveButton(wrapper).trigger('click')
    await flushPromises()
    await flushPromises()
    expect(apiMock.post).toHaveBeenCalledWith('/admin/mobile/fcm-config', {
      service_account_json: fakeKey,
    })
    expect((textarea(wrapper).element as HTMLTextAreaElement).value).toBe('')
    expect(wrapper.find('.fcm-key-form .success').text()).toContain('test-project')
    expect(wrapper.find('.status-badge.is-live').text()).toContain('Live')
    expect(wrapper.find('.status-note').text()).toContain('test-project')
    // The stored key is never echoed back.
    expect(wrapper.text()).not.toContain('FAKE-KEY-FOR-TESTS-ONLY')
  })

  it('surfaces the server 422 message and keeps the pasted key', async () => {
    const wrapper = await mountPage()
    apiMock.post.mockRejectedValueOnce({
      response: { data: { detail: 'service_account_json must be a Firebase service-account JSON object' } },
    })
    await textarea(wrapper).setValue(JSON.stringify(fakeKey))
    await saveButton(wrapper).trigger('click')
    await flushPromises()
    expect(wrapper.find('.fcm-key-form .error').text()).toContain(
      'service_account_json must be a Firebase service-account JSON object',
    )
    expect((textarea(wrapper).element as HTMLTextAreaElement).value).toContain('test-project')
  })

  it('lets an already-configured org replace the key', async () => {
    const wrapper = await mountPage(configured)
    expect(wrapper.find('.status-badge.is-live').text()).toContain('Live')
    expect(saveButton(wrapper).text()).toBe('Replace FCM key')
    await textarea(wrapper).setValue(JSON.stringify({ ...fakeKey, project_id: 'test-project-2' }))
    await saveButton(wrapper).trigger('click')
    await flushPromises()
    expect(apiMock.post).toHaveBeenCalledWith('/admin/mobile/fcm-config', {
      service_account_json: { ...fakeKey, project_id: 'test-project-2' },
    })
  })
})
