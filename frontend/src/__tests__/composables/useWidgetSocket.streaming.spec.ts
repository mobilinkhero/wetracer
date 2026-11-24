/*
 * Widget streaming display — regression spec for the live-reply path
 * (docs/STREAMING-PROTOCOL.md). Drives the real useWidgetSocket handlers
 * through a mocked socket.io client.
 *
 * Status when written (2026-11-25): 5 of 7 fail — the live bubble is never
 * dropped when the final chat_response arrives (it renders twice), a second
 * turn sent mid-stream merges into the first bubble, and a disconnect
 * mid-stream leaves a stale live bubble. See the defects in the test names.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

describe('widget streaming display', () => {
  type Handler = (...args: any[]) => void

  const handlers = new Map<string, Handler[]>()
  let socket: any

  const fire = (event: string, ...args: any[]) =>
    (handlers.get(event) || []).forEach((h) => h(...args))

  vi.mock('socket.io-client', () => ({
    io: (...args: any[]) => (globalThis as any).__ioFactory(...args),
  }))

  beforeEach(() => {
    vi.useFakeTimers()
    handlers.clear()
    localStorage.clear()

    socket = {
      active: true,
      connected: false,
      connect: vi.fn(),
      disconnect: vi.fn(),
      emit: vi.fn(),
      off: vi.fn(),
      removeAllListeners: vi.fn(() => handlers.clear()),
      on: vi.fn((event: string, handler: Handler) => {
        handlers.set(event, [...(handlers.get(event) || []), handler])
      }),
    }
    ;(globalThis as any).__ioFactory = () => socket
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.resetModules()
  })

  async function connectWidget() {
    const { useWidgetSocket } = await import('@/composables/useWidgetSocket')
    const widget = useWidgetSocket()
    widget.setToken('token-1')
    widget.connect()
    socket.connected = true
    fire('connect')
    return widget
  }

  const botMessages = (widget: any) =>
    widget.messages.value.filter((m: any) => m.message_type === 'bot')

  it('renders tokens live into one bubble and stops the typing dots at the first token', async () => {
    const widget = await connectWidget()

    fire('bot_typing')
    expect(widget.loading.value).toBe(true)

    fire('chat_response_stream_start', { session_id: 's1', source: 'model-stream', message_id: null })
    expect(widget.loading.value).toBe(false) // dots gone once the bubble opens
    expect(widget.isLiveStreaming.value).toBe(true)
    expect(botMessages(widget)).toHaveLength(1)
    expect(botMessages(widget)[0].liveStream).toBe(true)
    expect(botMessages(widget)[0].message).toBe('')

    fire('chat_response_stream_token', { session_id: 's1', delta: 'Hello ', source: 'model-stream' })
    fire('chat_response_stream_token', { session_id: 's1', delta: 'world', source: 'model-stream' })
    expect(botMessages(widget)[0].message).toBe('Hello world')
    expect(botMessages(widget)).toHaveLength(1) // still one bubble, not one per token

    fire('chat_response_stream_done', { session_id: 's1', source: 'model-stream', message_id: null })
    // Done without text: bubble stays live until the final chat_response.
    expect(botMessages(widget)).toHaveLength(1)
    expect(botMessages(widget)[0].liveStream).toBe(true)

    fire('chat_response', { session_id: 's1', message_id: 7, message: 'Hello world', agent_name: 'Support' })
    expect(botMessages(widget)).toHaveLength(1) // placeholder replaced, not duplicated
    expect(botMessages(widget)[0].id).toBe(7)
    expect(botMessages(widget)[0].liveStream).toBeFalsy()
    expect(widget.isLiveStreaming.value).toBe(false)
  })

  it('opens a bubble when a token arrives before any start event', async () => {
    const widget = await connectWidget()
    fire('chat_response_stream_token', { session_id: 's1', delta: 'Hi there', source: 'model-stream' })
    expect(botMessages(widget)).toHaveLength(1)
    expect(botMessages(widget)[0].message).toBe('Hi there')
    expect(botMessages(widget)[0].liveStream).toBe(true)
  })

  it('a full_text token replaces the text instead of appending', async () => {
    const widget = await connectWidget()
    fire('chat_response_stream_token', { session_id: 's1', full_text: 'Hello world', source: 'model-stream' })
    fire('chat_response_stream_token', { session_id: 's1', full_text: 'Hello world!', source: 'model-stream' })
    expect(botMessages(widget)[0].message).toBe('Hello world!')
  })

  it('a KB instant answer renders once, keeping its source label', async () => {
    const widget = await connectWidget()
    fire('chat_response_stream_start', { session_id: 's1', source: 'kb', message_id: 42 })
    fire('chat_response_stream_token', { session_id: 's1', delta: 'Reset it from the login page.', source: 'kb' })
    fire('chat_response_stream_done', { session_id: 's1', source: 'kb', message_id: 42 })
    fire('chat_response', {
      session_id: 's1',
      message_id: 42,
      message: 'Reset it from the login page.',
      sources: [{ name: 'How to reset your password', type: 'article' }],
    })
    expect(botMessages(widget)).toHaveLength(1)
    expect(botMessages(widget)[0].source).toBe('kb')
    expect(botMessages(widget)[0].sources).toHaveLength(1)
  })

  it('a stream error keeps the partial text and brings the typing dots back', async () => {
    const widget = await connectWidget()
    fire('chat_response_stream_start', { session_id: 's1', source: 'model-stream', message_id: null })
    fire('chat_response_stream_token', { session_id: 's1', delta: 'Partial ', source: 'model-stream' })
    fire('chat_response_stream_error', { session_id: 's1', error: 'boom' })
    expect(botMessages(widget)).toHaveLength(1)
    expect(botMessages(widget)[0].message).toBe('Partial ')
    expect(botMessages(widget)[0].liveStream).toBe(false)
    expect(widget.loading.value).toBe(true) // waiting on the fallback chat_response

    fire('chat_response', { session_id: 's1', message_id: 9, message: 'Partial answer.' })
    expect(botMessages(widget)).toHaveLength(1)
    expect(botMessages(widget)[0].message).toBe('Partial answer.')
  })

  it('DEFECT — a second turn sent mid-stream must not merge into the first bubble', async () => {
    const widget = await connectWidget()
    fire('bot_typing')
    fire('chat_response_stream_start', { session_id: 's1', source: 'model-stream', message_id: null })
    fire('chat_response_stream_token', { session_id: 's1', delta: 'First reply ', source: 'model-stream' })

    // The composer gate (useComposer.canSendWhenIdle) reads ONLY `loading`,
    // which the first token already cleared — so a send goes out mid-stream.
    expect(widget.loading.value).toBe(false)
    expect(widget.isLiveStreaming.value).toBe(true)

    // Turn 2 starts while turn 1 is still streaming.
    fire('chat_response_stream_start', { session_id: 's1', source: 'model-stream', message_id: null })
    fire('chat_response_stream_token', { session_id: 's1', delta: 'SECOND TURN', source: 'model-stream' })

    // Turn 1's bubble must keep only turn 1's text; turn 2 gets its own bubble.
    expect(botMessages(widget)).toHaveLength(2)
    expect(botMessages(widget)[0].message).toBe('First reply ')
    expect(botMessages(widget)[1].message).toBe('SECOND TURN')
  })

  it('DEFECT — a disconnect mid-stream must not leave a stale live bubble', async () => {
    const widget = await connectWidget()
    fire('chat_response_stream_start', { session_id: 's1', source: 'model-stream', message_id: null })
    fire('chat_response_stream_token', { session_id: 's1', delta: 'Partial ', source: 'model-stream' })

    fire('disconnect', 'transport close')
    vi.advanceTimersByTime(120_000)
    // The dead turn's bubble must be gone or finalised — never live forever.
    expect(widget.messages.value.filter((m: any) => m.liveStream)).toHaveLength(0)
    expect(widget.isLiveStreaming.value).toBe(false)

    // Reconnect + a brand-new turn: its tokens must start a fresh bubble.
    fire('connect')
    fire('chat_response_stream_start', { session_id: 's1', source: 'model-stream', message_id: null })
    fire('chat_response_stream_token', { session_id: 's1', delta: 'NEW TURN', source: 'model-stream' })
    expect(botMessages(widget)).toHaveLength(1)
    expect(botMessages(widget)[0].message).toBe('NEW TURN')
  })
})
