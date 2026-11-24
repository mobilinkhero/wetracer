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

import { replyExtras } from '../webclient/panel/answer-extras'
import { ref } from 'vue'
import { io, Socket } from 'socket.io-client'
import type { HumanAgent, SocketError } from '../types/widget'
import { getErrorMessage, isReauthorizingRefusal, REAUTHORIZING_REFUSALS } from '../types/widget'
import { widgetEnv } from '../webclient/widget-env'
import { localTokenStore, readTokenClaims } from './useConversationToken'
import { isFramed, reportedPageUrl } from '../webclient/frame-session'
import type { Message } from '@/types/chat'

export type ConnectionStatus = 'connected' | 'connecting' | 'failed'

/** The conversation this connection landed in. */
export interface SessionState {
    session_id: string
    /** True when the conversation belongs to the visitor the host page identified. */
    authenticated: boolean
    /** True when the server created it, false when it resumed an open one. */
    created: boolean
}

/**
 * One token (or chunk) of a live AI reply, and the end / failure of a
 * streamed reply. The wire shape — event names and payload keys — is
 * defined by `docs/STREAMING-PROTOCOL.md` in the repo root; until that
 * protocol lands, nothing subscribes to any streaming event (see
 * [bindStreamingEvents]). The parsers below are deliberately tolerant
 * (delta/token/chunk, full_text/text) so a protocol-conformant payload
 * is accepted whichever of those keys it uses.
 */
export interface WidgetStreamToken {
    /** Stored row id of the reply being streamed, when the server sent one. */
    messageId?: number
    /** The new text in this event (empty when it carries only [fullText]). */
    delta: string
    /** Running total text, when the server sends it instead of (or as well as) deltas. */
    fullText?: string
    agentName?: string
    /** `model-stream`, `kb` or `model-full`. */
    source?: string
}

export interface WidgetStreamDone {
    messageId?: number
    /** The complete reply text, when the server sends it. */
    fullText?: string
    /** The complete reply as a message, when the server sends it. */
    message?: { message?: string; message_id?: number; sources?: string[]; agent_name?: string }
}

/** The streaming event names from `docs/STREAMING-PROTOCOL.md` (v1). */
export interface WidgetStreamingEvents {
    startEvent: string
    tokenEvent: string
    doneEvent: string
    errorEvent: string
}

/** The documented widget streaming events (docs/STREAMING-PROTOCOL.md v1). */
export const WIDGET_STREAMING_EVENTS: WidgetStreamingEvents = {
    startEvent: 'chat_response_stream_start',
    tokenEvent: 'chat_response_stream_token',
    doneEvent: 'chat_response_stream_done',
    errorEvent: 'chat_response_stream_error',
}

export interface WidgetStreamStart {
    messageId?: number
    /** `model-stream`, `kb` or `model-full`. */
    source?: string
}

const _streamIdOf = (data: any): number | undefined => {
    const raw = data?.message_id ?? data?.messageId ?? data?.id
    return typeof raw === 'number' ? raw : undefined
}

const parseStreamToken = (data: any): WidgetStreamToken | null => {
    if (!data || typeof data !== 'object') return null
    const delta = data.delta ?? data.token ?? data.chunk
    const full = data.full_text ?? data.fullText
    return {
        messageId: _streamIdOf(data),
        delta: typeof delta === 'string' ? delta : '',
        fullText: typeof full === 'string' ? full : undefined,
        agentName: typeof data.agent_name === 'string' ? data.agent_name : undefined,
        source: typeof data.source === 'string' ? data.source : undefined,
    }
}

const parseStreamDone = (data: any): WidgetStreamDone | null => {
    if (!data || typeof data !== 'object') return null
    const full = data.full_text ?? data.fullText ?? data.text
    const rawMessage = (data as any).message
    return {
        messageId: _streamIdOf(data),
        fullText: typeof full === 'string' ? full : undefined,
        message: rawMessage && typeof rawMessage === 'object'
            ? {
                message: typeof rawMessage.message === 'string' ? rawMessage.message : undefined,
                message_id: _streamIdOf(rawMessage),
                sources: Array.isArray(rawMessage.sources) ? rawMessage.sources : undefined,
                agent_name: typeof rawMessage.agent_name === 'string' ? rawMessage.agent_name : undefined,
            }
            : undefined,
    }
}

// How long to wait for the server to confirm a chat was closed. Generous enough for
// a slow round trip, short enough that the visitor is not left staring at a button.
const END_CHAT_TIMEOUT_MS = 5000

/** A live AI reply's stored row id (its thumbs refer to it), when the server sent one. */
const replyId = (data: { message_id?: unknown }): { id?: number } =>
    typeof data.message_id === 'number' ? { id: data.message_id } : {}

/** The browser's preferred languages, at most five, most preferred first. */
export const browserLanguages = (nav: Pick<Navigator, 'languages' | 'language'> = navigator): string[] =>
    (nav.languages?.length ? [...nav.languages] : [nav.language]).filter(Boolean).slice(0, 5)

/**
 * `panel`: this socket is the widget panel's, which draws live steps, status cards
 * and the email check; the onboarding test chat says nothing and gets plain replies.
 */
export function useWidgetSocket({ panel = false }: { panel?: boolean } = {}) {
    const messages = ref<Message[]>([])
    const loading = ref(false)
    const errorMessage = ref('')
    const showError = ref(false)
    const loadingHistory = ref(false)
    const hasStartedChat = ref(false)
    const connectionStatus = ref<ConnectionStatus>('connecting')
    const retryCount = ref(0)
    const MAX_RETRIES = 5
    const humanAgent = ref<HumanAgent>({})
    const currentForm = ref<any>(null)
    const currentSessionId = ref<string>('')
    // Listeners other widget features add (see `on`). Re-attached to every new
    // socket, so they survive a reconnect that builds a fresh connection.
    const extraListeners: Array<[string, (...args: any[]) => void]> = []

    // The typing indicator is driven solely by the server's `bot_typing`
    // event. The widget never guesses: a reply it optimistically waited for is
    // exactly how the dots ended up spinning forever whenever nothing was
    // going to answer (AI switched off, a human handling it, a queued chat).
    let botTypingTimeout: ReturnType<typeof setTimeout> | null = null

    /** Last resort: a backend that dies mid-reply must not strand the dots. */
    const BOT_TYPING_TIMEOUT_MS = 60_000

    const stopBotTyping = () => {
        loading.value = false
        if (botTypingTimeout) {
            clearTimeout(botTypingTimeout)
            botTypingTimeout = null
        }
    }

    const startBotTyping = () => {
        loading.value = true
        if (botTypingTimeout) clearTimeout(botTypingTimeout)
        botTypingTimeout = setTimeout(stopBotTyping, BOT_TYPING_TIMEOUT_MS)
    }

    // --- Token-by-token streaming (docs/STREAMING-PROTOCOL.md v1) ---
    // The visitor's own message needs no equivalent: sendMessage is a
    // fire-and-forget socket emit — the message is shown the moment it
    // is sent and the (slow) AI reply never blocks it, so the web
    // widget never had the mobile "Send timed out" bug. What it gets
    // here is the live-reply side: tokens render in the reply bubble
    // as they arrive, the typing indicator runs only until the first
    // token, and a complete `chat_response` finalises the bubble.
    // The documented events are bound by default (see
    // WIDGET_STREAMING_EVENTS); bindStreamingEvents re-binds them.
    let streamingMessage: any = null
    let lastStreamSource: string | undefined = undefined
    let boundStreamingEvents: WidgetStreamingEvents | null =
        WIDGET_STREAMING_EVENTS
    const isLiveStreaming = ref(false)

    /** The stream started: open the in-progress bubble now — the first token may be seconds away. */
    const applyStreamStart = (event: WidgetStreamStart) => {
        if (streamingMessage) return
        stopBotTyping()
        streamingMessage = {
            ...(typeof event.messageId === 'number' ? { id: event.messageId } : {}),
            message: '',
            message_type: 'bot',
            created_at: new Date().toISOString(),
            session_id: '',
            source: event.source,
            liveStream: true,
        }
        messages.value.push(streamingMessage)
        isLiveStreaming.value = true
    }

    /** One token of a live reply. First token replaces the typing dots with the streaming bubble. */
    const applyStreamToken = (event: WidgetStreamToken) => {
        stopBotTyping()
        if (!streamingMessage) {
            streamingMessage = {
                ...(typeof event.messageId === 'number' ? { id: event.messageId } : {}),
                message: event.fullText ?? event.delta,
                message_type: 'bot',
                created_at: new Date().toISOString(),
                session_id: '',
                agent_name: event.agentName,
                source: event.source,
                // Not `stream`: that flag is the client-side typewriter,
                // which reveals a complete message. A live stream renders
                // its growing text directly (see AnswerMessage/HumanMessage).
                liveStream: true,
            }
            messages.value.push(streamingMessage)
            isLiveStreaming.value = true
        } else {
            streamingMessage.message =
                event.fullText ?? `${streamingMessage.message ?? ''}${event.delta}`
            if (typeof event.messageId === 'number') streamingMessage.id = event.messageId
            if (event.source) streamingMessage.source = event.source
        }
    }

    /**
     * The streamed reply finished. The widget `done` event carries only
     * `{session_id, source, message_id}` — the final `chat_response`
     * follows immediately, so the bubble stays live (with its id)
     * until that reply replaces it, instead of finalising into a
     * second bubble. A done that carries the complete text/message
     * (other transports) finalises the bubble in place.
     */
    const completeStream = (event: WidgetStreamDone) => {
        const message = streamingMessage
        if (!message) return
        if (event.message?.message !== undefined || event.fullText !== undefined) {
            streamingMessage = null
            isLiveStreaming.value = false
            stopBotTyping()
            message.liveStream = false
            if (event.message?.message !== undefined) message.message = event.message.message
            else if (event.fullText !== undefined) message.message = event.fullText
            if (typeof event.messageId === 'number') message.id = event.messageId
            if (event.message?.sources) message.sources = event.message.sources
            if (event.message?.agent_name) message.agent_name = event.message.agent_name
            return
        }
        if (typeof event.messageId === 'number') message.id = event.messageId
        // Otherwise: wait for the chat_response that follows.
    }

    /**
     * The stream failed mid-reply. The partial text stays visible (it
     * is real reply content); the typing dots come back while the
     * complete reply is awaited through the normal `chat_response`
     * path, instead of an error banner for a reply that may still
     * arrive in full.
     */
    const failStream = (_error: string) => {
        const message = streamingMessage
        streamingMessage = null
        isLiveStreaming.value = false
        if (message) message.liveStream = false
        startBotTyping()
    }

    /** Drop the streaming placeholder: a complete reply supersedes it. */
    const dropStreamingPlaceholder = () => {
        if (!streamingMessage) return
        // The final chat_response carries no source; the stream that
        // produced it does, so the reply keeps its "from help article"
        // label after finalisation.
        lastStreamSource = streamingMessage.source
        messages.value = messages.value.filter((m: any) => m !== streamingMessage)
        streamingMessage = null
        isLiveStreaming.value = false
    }

    /** Subscribe to the streaming events named by docs/STREAMING-PROTOCOL.md — call only with the documented names. */
    const bindStreamingEvents = (events: WidgetStreamingEvents) => {
        boundStreamingEvents = events
        if (socket) registerStreamingListeners(socket)
    }

    const registerStreamingListeners = (sock: Socket) => {
        if (!boundStreamingEvents) return
        const { startEvent, tokenEvent, doneEvent, errorEvent } = boundStreamingEvents
        sock.on(startEvent, (data: any) => {
            if (!data || typeof data !== 'object') return
            applyStreamStart({
                messageId: _streamIdOf(data),
                source: typeof data.source === 'string' ? data.source : undefined,
            })
        })
        sock.on(tokenEvent, (data: any) => {
            const token = parseStreamToken(data)
            if (token) applyStreamToken(token)
        })
        sock.on(doneEvent, (data: any) => {
            const done = parseStreamDone(data)
            if (done) completeStream(done)
        })
        sock.on(errorEvent, (data: any) => {
            const message = data?.error ?? data?.message
            failStream(typeof message === 'string' ? message : 'Streaming failed')
        })
    }

    let socket: Socket | null = null

    // socket.io retries transport failures on its own, but not a connection the
    // server rejected outright — see the connect_error handler. These drive the
    // attempts we have to make ourselves.
    const MANUAL_RETRY_BASE_MS = 1000
    const MANUAL_RETRY_MAX_MS = 15000
    let manualRetryTimer: ReturnType<typeof setTimeout> | null = null

    // Refusals that retrying the same connection can't change, so retrying is just
    // load: the org has no AI config, or the token needs authorizing again (see
    // REAUTHORIZING_REFUSALS - the caller of connect() does that, see `refusal`).
    // Anything else, including a transient failure inside the connect handler, is
    // worth another attempt.
    const FINAL_REFUSALS = new Set<SocketError['type']>(['ai_config_missing', ...REAUTHORIZING_REFUSALS])
    let refusedForGood = false
    // The server is about to close the connection so the widget starts over
    let startOverRequested = false
    // Why the last attempt was refused for good, for the caller of connect().
    const refusal = ref<SocketError['type'] | null>(null)

    const clearManualRetry = () => {
        if (manualRetryTimer) {
            clearTimeout(manualRetryTimer)
            manualRetryTimer = null
        }
    }

    const scheduleManualRetry = () => {
        if (manualRetryTimer || refusedForGood) return
        const delay = Math.min(
            MANUAL_RETRY_BASE_MS * 2 ** Math.max(0, retryCount.value - 1),
            MANUAL_RETRY_MAX_MS
        )
        manualRetryTimer = setTimeout(() => {
            manualRetryTimer = null
            socket?.connect()
        }, delay)
    }

    let onTakeoverCallback: ((data: { session_id: string, user_name: string }) => void) | null = null
    let onWorkflowStateCallback: ((data: any) => void) | null = null
    let onWorkflowProceededCallback: ((data: any) => void) | null = null
    let onSessionStateCallback: ((data: SessionState) => void) | null = null
    // A refusal while connect() is settling goes to its caller. One after that - a
    // reconnect once the token lapsed in a tab left open - has nobody waiting on
    // it, so it is handed to the widget to authorize again.
    let onReauthorizeCallback: (() => void) | null = null
    let startingUp = false
    // The server announces the conversation the moment the socket connects, which is
    // before the widget has finished starting up and registered its listener - so the
    // last announcement is held and replayed to a late subscriber.
    let lastSessionState: SessionState | null = null
    let widgetToken: string | undefined = undefined
    let widgetIdAuth: string | undefined = undefined
    // The visitor (token `sub`) the live connection authenticated as.
    let connectedVisitor: string | undefined = undefined
    const visitorOf = (token: string | undefined | null) => readTokenClaims(token ?? null)?.sub as string | undefined

    // Store token for socket authentication.
    // A rotated token has to reach the open connection too: the server keeps the
    // token the socket connected with, and that copy is what a reconnect presents.
    const setToken = (token: string | undefined) => {
        widgetToken = token
        if (!token) return
        if (socket?.connected) {
            socket.emit('refresh_token', { conversation_token: token })
        }
    }

    // Store widget ID for socket authentication (for anonymous access)
    const setWidgetId = (widgetId: string | undefined) => {
        widgetIdAuth = widgetId
    }

    /** Auth payload for one connection attempt, read fresh each time. */
    const buildAuth = () => {
        // Use passed token first, then the shared localStorage entry - except in a
        // framed widget, whose own storage is not the host's (the host owns the token)
        const token = widgetToken || (isFramed() ? null : localTokenStore().read())

        // Build auth object with token and widget_id
        const auth: any = {}
        if (token) {
            auth.conversation_token = token
        }
        if (widgetIdAuth) {
            auth.widget_id = widgetIdAuth
        }
        if (panel) auth.panel = true
        // The visitor's languages, most preferred first: status cards are written in the first the server has
        auth.languages = browserLanguages()
        // Host page embedding the widget, recorded server-side on lead capture
        // (lead_source.page_url). A framed widget is told it by the loader; a srcdoc
        // one (older loaders) can read its same-origin parent; else the referrer.
        try {
            auth.page_url = reportedPageUrl()
                || ((window.parent !== window && window.parent.location?.href)
                    ? window.parent.location.href
                    : (document.referrer || window.location.href))
        } catch {
            auth.page_url = document.referrer || ''
        }
        return auth
    }

    const initializeSocket = (sessionId: string) => {
        socket = io(`${widgetEnv.WS_URL}/widget`, {
            transports: ['websocket'],
            reconnection: true,
            // Keep trying. The old cap of 5 attempts was ~15s of backoff, so a
            // backend restart or a laptop waking from sleep left the widget dead
            // until the visitor reloaded the page.
            reconnectionAttempts: Infinity,
            reconnectionDelay: 1000,
            reconnectionDelayMax: 15000,
            // Called before every attempt, so a token refreshed since the socket
            // was created is the one that gets sent.
            auth: (cb: (data: object) => void) => cb(buildAuth())
        })

        // Set up event listeners
        socket.on('connect', () => {
            connectedVisitor = visitorOf(buildAuth().conversation_token)
            connectionStatus.value = 'connected'
            clearManualRetry()
            retryCount.value = 0
        })

        // A reply is actually being produced now.
        socket.on('bot_typing', () => {
            startBotTyping()
        })

        socket.on('disconnect', (reason) => {
            // Nothing can arrive to clear it while we are disconnected.
            stopBotTyping()

            if (connectionStatus.value === 'connected') {
                console.log('Socket disconnected, setting connection status to connecting')
                connectionStatus.value = 'connecting'
            }

            // socket.io doesn't reconnect after the server closes the connection.
            // Only when the server said why first, because the visitor's
            // conversation was deleted: connecting again opens a new one, or is
            // refused with a reason that has the widget authorize a new visitor.
            // Any other server-side close stays closed.
            const startOver = startOverRequested
            startOverRequested = false // only ever for the disconnect that follows it
            if (reason === 'io server disconnect' && startOver) {
                socket?.connect()
            }
        })

        socket.on('error', (error?: SocketError) => {
            if (error?.type === 'conversation_gone') startOverRequested = true
        })

        // A refusal carries its reason in `data`: events the server emits before
        // refusing never reach the client.
        socket.on('connect_error', (err?: Error & { data?: SocketError }) => {
            const reason = err?.data?.type
            if (reason && FINAL_REFUSALS.has(reason)) {
                refusedForGood = true
                refusal.value = reason
                clearManualRetry()
                connectionStatus.value = 'failed'
                // A token to authorize again is ours to fix, not the visitor's to read.
                if (!isReauthorizingRefusal(reason)) {
                    handleError(err!.data)
                } else if (!startingUp) {
                    onReauthorizeCallback?.()
                }
                return
            }

            retryCount.value++
            console.error('Socket connection failed, attempt:', retryCount.value)

            // Offer the manual retry once it has been failing for a while. The
            // automatic attempts carry on underneath — 'connect' puts this back to
            // 'connected' as soon as one of them lands.
            if (retryCount.value >= MAX_RETRIES) {
                connectionStatus.value = 'failed'
            }

            // `active` is false when the server rejected the connection itself
            // rather than the transport failing, and socket.io does not retry
            // those. One transient error inside the connect handler would
            // otherwise wedge the widget on "Connecting..." until a page reload.
            if (socket && !socket.active) {
                scheduleManualRetry()
            }
        })

        socket.on('chat_response', (data) => {
            stopBotTyping() // the reply we were waiting for has arrived
            // A complete reply supersedes a streaming bubble (same
            // reply, delivered whole): drop the placeholder first, or
            // the reply would show twice.
            dropStreamingPlaceholder()
            // Capture session_id from response for file attachments
            if (data.session_id) {
                console.log('Captured session_id from chat_response:', data.session_id)
                currentSessionId.value = data.session_id
            } else {
                console.warn('No session_id in chat_response data:', data)
            }

            if (data.type === 'agent_message') {
                // Handle human agent messages
                const agentMessage: any = {
                    // Found by it when the person edits it (message_edited)
                    ...(typeof data.message_id === 'number' ? { id: data.message_id } : {}),
                    message: data.message,
                    message_type: 'agent',
                    created_at: new Date().toISOString(),
                    session_id: '',
                    agent_name: data.agent_name,
                    stream: true, // live reply → client-side typewriter reveal
                    attributes: {
                        end_chat: data.end_chat,
                        end_chat_reason: data.end_chat_reason,
                        end_chat_description: data.end_chat_description,
                        request_rating: data.request_rating
                    }
                }
                
                // Add attachments if present
                if (data.attachments && Array.isArray(data.attachments)) {
                    agentMessage.id = data.message_id
                    agentMessage.attachments = data.attachments.map((att: any, idx: number) => ({
                        id: data.message_id * 1000 + idx,
                        filename: att.filename,
                        file_url: att.file_url,
                        content_type: att.content_type,
                        file_size: att.file_size
                    }))
                }
                
                messages.value.push(agentMessage)
            // UPDATED CHECK: Look for the shopify_output object and products array
            } else if (data.shopify_output && typeof data.shopify_output === 'object' && data.shopify_output.products) {
                // Handle structured Shopify product data
                messages.value.push({
                    ...replyId(data),
                    message: data.message, // Keep the accompanying text message
                    message_type: 'product', // Use 'product' type for rendering
                    created_at: new Date().toISOString(),
                    session_id: '', 
                    agent_name: data.agent_name,
                    // Assign the whole structured object
                    shopify_output: data.shopify_output, 
                    // Remove the old flattened fields (product_id, product_title, etc.)
                    attributes: { // Keep other attributes if needed
                         end_chat: data.end_chat,
                         request_rating: data.request_rating,
                         ...replyExtras(data),
                    }
                })
            } else {
                // Handle regular bot messages (without Shopify data)
                messages.value.push({
                    ...replyId(data),
                    message: data.message,
                    message_type: 'bot',
                    created_at: new Date().toISOString(),
                    session_id: '',
                    agent_name: data.agent_name,
                    stream: true, // live reply → client-side typewriter reveal
                    // A streamed reply's source (e.g. `kb`) survives
                    // finalisation so the "from help article" label stays.
                    ...(lastStreamSource ? { source: lastStreamSource } : {}),
                    // Knowledge-base citations (display gated by show_citations in the widget)
                    sources: Array.isArray(data.sources) && data.sources.length ? data.sources : undefined,
                    attributes: {
                        end_chat: data.end_chat,
                        end_chat_reason: data.end_chat_reason,
                        end_chat_description: data.end_chat_description,
                        request_rating: data.request_rating,
                        // The agent's choices or follow-ups, the handover card and the steps' receipt
                        ...replyExtras(data),
                    }
                })
            }
            // loading.value = false // Moved to the top
            // Consumed by the branch above (if any): a later,
            // non-streamed reply must not inherit this turn's source.
            lastStreamSource = undefined
        })

        socket.on('handle_taken_over', (data: { session_id: string, user_name: string, profile_picture?: string }) => {
            // Add system message for takeover
            messages.value.push({
                message: `${data.user_name} joined the conversation`,
                message_type: 'system',
                created_at: new Date().toISOString(),
                session_id: data.session_id
            })

            
            humanAgent.value = {
                ...humanAgent.value,
                human_agent_name: data.user_name,
                human_agent_profile_pic: data.profile_picture
            }

            // Whatever we were waiting on, it is not coming: the bot is out of
            // this conversation now. Without this the typing dots spin forever
            // above the "joined the conversation" line.
            stopBotTyping()

            // Call the callback if registered
            if (onTakeoverCallback) {
                onTakeoverCallback(data)
            }
        })

        // The person changed a reply they sent: the bubble shows the new text
        socket.on('message_edited', (data: { message_id?: number; message?: string; edited_at?: string }) => {
            if (typeof data?.message_id !== 'number' || typeof data.message !== 'string') return
            const edited = messages.value.find((m) => m.id === data.message_id)
            if (!edited) return
            edited.message = data.message
            edited.attributes = { ...(edited.attributes || {}), edited_at: data.edited_at }
        })

        socket.on('session_initialized', (data) => {
            // Capture session_id immediately upon connection
            if (data.session_id) {
                currentSessionId.value = data.session_id
                // Report every connection's conversation: the host page needs to know
                // when a new one was created (a visitor starting a new chat used to
                // happen with no signal at all) and whether it is still authenticated.
                lastSessionState = {
                    session_id: data.session_id,
                    authenticated: !!data.authenticated,
                    created: !!data.created
                }
                onSessionStateCallback?.(lastSessionState)
            }
        })

        socket.on('error', handleError)
        for (const [event, listener] of extraListeners) socket.on(event, listener)
        socket.on('chat_history', handleChatHistory)
        socket.on('rating_submitted', handleRatingSubmitted)
        socket.on('display_form', handleDisplayForm)
        socket.on('form_submitted', handleFormSubmitted)
        socket.on('workflow_state', handleWorkflowState)
        socket.on('workflow_proceeded', handleWorkflowProceeded)
        registerStreamingListeners(socket)

        return socket
    }

    // Set when the conversation was ended (endChat): the next connect starts afresh.
    let freshConnectionNext = false

    const connect = async (): Promise<boolean> => {
        try {
            // Keep a live connection that is already this visitor's. Tearing it down
            // loses whatever the server emits before the new one joins the room - a
            // human agent's first reply right after a takeover (which re-runs the
            // widget's authorization) vanished that way, and history is only loaded
            // once. A rotated token for the same visitor already reached the open
            // connection via refresh_token; another visitor still gets a new one.
            if (!freshConnectionNext && socket?.connected && connectedVisitor
                && connectedVisitor === visitorOf(buildAuth().conversation_token)) {
                connectionStatus.value = 'connected'
                return true
            }
            freshConnectionNext = false

            connectionStatus.value = 'connecting'
            retryCount.value = 0

            // Cleanup existing socket if any
            stopBotTyping()
            clearManualRetry()
            refusedForGood = false
            refusal.value = null
            if (socket) {
                socket.removeAllListeners()
                socket.disconnect()
                socket = null
            }

            startingUp = true
            socket = initializeSocket('')

            return new Promise((resolve) => {
                const settle = (connected: boolean) => {
                    startingUp = false
                    resolve(connected)
                }
                socket?.on('connect', () => {
                    settle(true)
                })

                // Registered after initializeSocket's handler, so a refusal is
                // already recorded when this runs. A refused connect has to settle:
                // the widget's startup awaits it.
                socket?.on('connect_error', () => {
                    if (refusedForGood || retryCount.value >= MAX_RETRIES) {
                        settle(false)
                    }
                })
            })
        } catch (error) {
            console.error('Socket initialization failed:', error)
            startingUp = false
            connectionStatus.value = 'failed'
            return false
        }
    }

    // Manual reconnect function
    const reconnect = () => {
        if (socket) {
            socket.disconnect()
        }
        return connect()
    }

    // Register takeover callback
    const onTakeover = (callback: (data: { session_id: string, user_name: string }) => void) => {
        onTakeoverCallback = callback
    }

    // Register the callback that authorizes again after a later refusal
    const onReauthorize = (callback: () => void) => {
        onReauthorizeCallback = callback
    }

    // Register conversation-state callback
    const onSessionState = (callback: (data: SessionState) => void) => {
        onSessionStateCallback = callback
        if (lastSessionState) callback(lastSessionState)
    }

    // Register workflow state callback
    const onWorkflowState = (callback: (data: any) => void) => {
        onWorkflowStateCallback = callback
    }

    // Register workflow proceeded callback
    const onWorkflowProceeded = (callback: (data: any) => void) => {
        onWorkflowProceededCallback = callback
    }

    // Socket event handlers
    const handleError = (error: any) => {
        // A thumb that did not save is put back where it was (useAnswerFeedback);
        // it says nothing about the reply that may be on its way.
        if (error?.type === 'feedback_error') return
        stopBotTyping()
        errorMessage.value = getErrorMessage(error as SocketError)
        showError.value = true

        // Hide error after 5 seconds
        setTimeout(() => {
            showError.value = false
            errorMessage.value = ''
        }, 5000)
    }

    const handleChatHistory = (data: {
        type: string;
        messages: Message[];
    }) => {
        if (data.type === 'chat_history' && Array.isArray(data.messages)) {
            const historyMessages = data.messages.map((msg: Message) => {
                // Base message structure
                const messageObj = {
                    ...(typeof msg.id === 'number' ? { id: msg.id } : {}),
                    message: msg.message,
                    message_type: msg.message_type as "assistant" | "user" | "error" | "bot" | "agent" | "system" | "product",
                    created_at: msg.created_at,
                    session_id: '',
                    agent_name: msg.agent_name || '',
                    user_name: msg.user_name || '',
                    attributes: msg.attributes || {},
                    attachments: msg.attachments || [] // Include attachments
                }

                // Restore knowledge-base citations persisted in attributes
                if (Array.isArray(msg.attributes?.sources) && msg.attributes.sources.length) {
                    ;(messageObj as Message).sources = msg.attributes.sources
                }

                // Check if message has Shopify data in attributes
                if (msg.attributes?.shopify_output && typeof msg.attributes.shopify_output === 'object') {
                    return {
                        ...messageObj,
                        message_type: 'product',
                        shopify_output: msg.attributes.shopify_output
                    }
                }

                return messageObj
            })

            messages.value = [
                ...historyMessages.filter(newMsg => 
                    !messages.value.some(existingMsg => 
                        existingMsg.message === newMsg.message && 
                        existingMsg.created_at === newMsg.created_at
                    )
                ),
                ...messages.value
            ]
        }
    }

    // Add rating submission handler
    const handleRatingSubmitted = (data: { success: boolean, message: string }) => {
        if (data.success) {
            messages.value.push({
                message: 'Thank you for your feedback!',
                message_type: 'system',
                created_at: new Date().toISOString(),
                session_id: ''
            })
        }
    }


    // Form display handler
    const handleDisplayForm = (data: { form_data: any, session_id: string }) => {
        console.log('Form display handler in composable:', data)
        stopBotTyping()
        currentForm.value = data.form_data
        console.log('Set currentForm in handleDisplayForm:', currentForm.value)
        
        // Check if this is a full screen form
        if (data.form_data?.form_full_screen === true) {
            console.log('Full screen form detected, triggering workflow state callback')
            // Trigger workflow state callback for full screen forms
            if (onWorkflowStateCallback) {
                onWorkflowStateCallback({
                    type: 'form',
                    form_data: data.form_data,
                    session_id: data.session_id
                })
            }
        } else {
            // Add form message to chat for regular forms
            messages.value.push({
                message: '',
                message_type: 'form',
                created_at: new Date().toISOString(),
                session_id: data.session_id,
                attributes: {
                    form_data: data.form_data
                }
            })
        }
    }

    // Form submission confirmation handler
    const handleFormSubmitted = (data: { success: boolean, message: string }) => {
        console.log('Form submitted confirmation received, clearing currentForm')
        currentForm.value = null
        if (data.success) {
            // Success message will come through regular chat_response
            console.log('Form submitted successfully')
        }
    }

    // Workflow state handler
    const handleWorkflowState = (data: any) => {
        console.log('Workflow state received in composable:', data)
        
        // Set currentForm for form states to ensure submission works
        if (data.type === 'form' || data.type === 'display_form') {
            console.log('Setting currentForm from workflow state:', data.form_data)
            currentForm.value = data.form_data
        }
        
        if (onWorkflowStateCallback) {
            onWorkflowStateCallback(data)
        }
    }

    // Workflow proceeded handler
    const handleWorkflowProceeded = (data: { success: boolean }) => {
        console.log('Workflow proceeded in composable:', data)
        if (onWorkflowProceededCallback) {
            onWorkflowProceededCallback(data)
        }
    }

    // Add rating submission function
    const submitRating = async (rating: number, feedback?: string | null) => {
        if (!socket || !rating) return
        
        socket.emit('submit_rating', {
            rating,
            feedback
        })
    }

    // Form submission function
    const submitForm = async (formData: Record<string, any>) => {
        console.log('Submitting form in socket:', formData)
        console.log('Current form in socket:', currentForm.value)
        console.log('Socket in socket:', socket)
        
        if (!socket) {
            console.error('No socket available for form submission')
            return
        }
        
        // Allow submission even if currentForm.value is null, as long as we have form data
        if (!formData || Object.keys(formData).length === 0) {
            console.error('No form data to submit')
            return
        }
        
        // Handoff contact-capture forms go to a dedicated handler (updates the customer
        // record) instead of the workflow form pipeline.
        const isContactForm = currentForm.value?.form_type === 'contact'
        const event = isContactForm ? 'submit_contact_info' : 'submit_form'
        console.log(`Emitting ${event} event with data:`, formData)
        socket.emit(event, {
            form_data: formData
        })

        // Clear current form after submission
        currentForm.value = null
    }

    // Get workflow state function
    const getWorkflowState = async () => {
        if (!socket) return
        console.log('Getting workflow state 12')
        socket.emit('get_workflow_state')
    }

    // Proceed workflow function
    const proceedWorkflow = async () => {
        if (!socket) return
        
        socket.emit('proceed_workflow', {})
    }

    // Send message function
    const sendMessage = async (newMessage: string, email: string, files: Array<{content: string, filename: string, content_type: string, size: number}> = [], extra: Record<string, unknown> = {}) => {
        if (!socket || (!newMessage.trim() && files.length === 0)) return
        
        // Add user message to display with temporary blob URLs for images
        const userMessage: any = {
            message: newMessage,
            message_type: 'user',
            created_at: new Date().toISOString(),
            session_id: ''
        }
        
        // Add temporary attachments for immediate display (will be replaced with real URLs from backend)
        if (files.length > 0) {
            userMessage.attachments = files.map((file, idx) => {
                // Create temporary blob URL for images and audio
                let tempUrl = ''
                if (file.content_type.startsWith('image/') || file.content_type.startsWith('audio/')) {
                    // Convert base64 to blob URL for immediate display
                    const byteCharacters = atob(file.content)
                    const byteNumbers = new Array(byteCharacters.length)
                    for (let i = 0; i < byteCharacters.length; i++) {
                        byteNumbers[i] = byteCharacters.charCodeAt(i)
                    }
                    const byteArray = new Uint8Array(byteNumbers)
                    const blob = new Blob([byteArray], { type: file.content_type })
                    tempUrl = URL.createObjectURL(blob)
                }
                
                return {
                    id: Date.now() * 1000 + idx, // Temporary ID
                    filename: file.filename,
                    file_url: tempUrl, // Temporary blob URL, will be replaced
                    content_type: file.content_type,
                    file_size: file.size,
                    _isTemporary: true // Flag to identify temporary attachments
                }
            })
        }
        
        messages.value.push(userMessage)

        // Emit to socket WITH files (files will be uploaded on backend)
        socket.emit('chat', {
            ...extra,
            message: newMessage,
            email: email,
            files: files  // Send files with base64 content
        })

        hasStartedChat.value = true
    }
    

    /**
     * Close the current session server-side and reset local conversation state.
     *
     * History is scoped to the ACTIVE session (see get_chat_history), so closing it
     * is what makes the next conversation genuinely new rather than a reload of the
     * old one. Resolves when the server confirms, or after a short grace period so a
     * dropped confirmation can't leave the visitor staring at a dead button.
     */
    const resetConversationState = () => {
        messages.value = []
        hasStartedChat.value = false
        currentSessionId.value = ''
        // A reply in flight never lands once the session closes, so the typing
        // indicator would spin forever over an empty chat.
        stopBotTyping()
        currentForm.value = null
    }

    // Must be a member of EndChatReasonType (backend/app/models/session_to_agent.py):
    // the column is an enum, and an unknown value makes close_session roll back and
    // silently leave the session open.
    const endChat = (reason = 'CUSTOMER_REQUEST'): Promise<boolean> => {
        return new Promise((resolve) => {
            if (!socket || !socket.connected) {
                // Nothing was closed server-side, so the session is still open and
                // will come back on the next connect. Say so rather than clearing the
                // transcript and calling it a new chat.
                resolve(false)
                return
            }
            let settled = false
            const settle = (closed: boolean) => {
                if (settled) return
                settled = true
                clearTimeout(timer)
                socket?.off('chat_ended', onClosed)
                socket?.off('error', onFailed)
                if (closed) {
                    resetConversationState()
                    // The next connect must be a new connection: kept open, the old
                    // one would reopen the closed conversation on the next message
                    // instead of starting a new one.
                    freshConnectionNext = true
                }
                resolve(closed)
            }
            const onClosed = () => settle(true)
            const onFailed = (error: any) => {
                if (error?.type === 'end_chat_error') settle(false)
            }
            // Nothing confirmed in this long means the close did not happen.
            const timer = setTimeout(() => settle(false), END_CHAT_TIMEOUT_MS)
            socket.on('chat_ended', onClosed)
            socket.on('error', onFailed)
            socket.emit('end_chat', { reason })
        })
    }

    // Chat history functions
    const loadChatHistory = async () => {
        if (!socket) return

        try {
            loadingHistory.value = true
            socket.emit('get_chat_history')
        } catch (error) {
            console.error('Failed to load chat history:', error)
        } finally {
            loadingHistory.value = false
        }
    }

    const cleanup = () => {
        // Before removeAllListeners, or the disconnect handler that would have
        // done it never fires and the timer outlives the widget.
        stopBotTyping()
        clearManualRetry()
        if (socket) {
            socket.removeAllListeners()
            socket.disconnect()
            socket = null
        }
        onTakeoverCallback = null
        onWorkflowStateCallback = null
        onWorkflowProceededCallback = null
    }

    /** Listen to a socket event, now and on every later connection. */
    const on = (event: string, listener: (...args: any[]) => void) => {
        extraListeners.push([event, listener])
        socket?.on(event, listener)
    }

    /** Send an event on the current connection, if there is one. */
    const emit = (event: string, data: unknown) => {
        socket?.emit(event, data)
    }

    return {
        messages,
        loading,
        errorMessage,
        showError,
        loadingHistory,
        hasStartedChat,
        connectionStatus,
        sendMessage,
        endChat,
        loadChatHistory,
        connect,
        reconnect,
        cleanup,
        humanAgent,
        onTakeover,
        onSessionState,
        onReauthorize,
        refusal,
        submitRating,
        currentForm,
        submitForm,
        getWorkflowState,
        proceedWorkflow,
        onWorkflowState,
        onWorkflowProceeded,
        currentSessionId,
        setToken,
        setWidgetId,
        on,
        emit,
        // Token-by-token streaming (docs/STREAMING-PROTOCOL.md): wire
        // with bindStreamingEvents once the protocol names the events.
        isLiveStreaming,
        applyStreamStart,
        applyStreamToken,
        completeStream,
        failStream,
        bindStreamingEvents,
    }
}

/** The widget's socket connection, as the composables built on it see it. */
export type WidgetSocket = ReturnType<typeof useWidgetSocket>
