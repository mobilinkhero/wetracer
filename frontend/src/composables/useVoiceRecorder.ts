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

import { computed, onBeforeUnmount, ref } from 'vue'
import { PANEL_COPY } from '../webclient/copy'

export interface VoiceRecorderOptions {
  /** Called with the finished recording once it is encoded. */
  onRecorded: (file: File, durationSeconds: number) => void
}

// Preferred recording formats, best first. Chrome/Edge/Firefox record Opus in
// WebM; Safari only supports MP4 (AAC), so it falls through to audio/mp4.
const CANDIDATE_MIME_TYPES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/mp4',
  'audio/ogg;codecs=opus',
  'audio/ogg',
]

const EXTENSION_FOR_MIME: Record<string, string> = {
  'audio/webm': 'webm',
  'audio/mp4': 'mp4',
  'audio/ogg': 'ogg',
}

/** The first container this browser's MediaRecorder can produce, or ''. */
function pickMimeType(): string {
  if (typeof MediaRecorder === 'undefined') return ''
  for (const mimeType of CANDIDATE_MIME_TYPES) {
    try {
      if (MediaRecorder.isTypeSupported(mimeType)) return mimeType
    } catch {
      // isTypeSupported throwing means "not supported here".
    }
  }
  return ''
}

/**
 * Records a voice note with the microphone (MediaRecorder + getUserMedia) and
 * hands the finished recording back as a File, ready to stage like any other
 * attachment. The view owns the mic/stop/cancel buttons; this owns the stream,
 * the timer and the permission errors.
 */
export function useVoiceRecorder({ onRecorded }: VoiceRecorderOptions) {
  // MediaRecorder exists in all modern browsers; getUserMedia needs a secure
  // context (https or localhost) — without it the mic button stays hidden.
  const isSupported = computed(() =>
    typeof MediaRecorder !== 'undefined'
    && typeof navigator !== 'undefined'
    && !!navigator.mediaDevices?.getUserMedia
  )

  const isRecording = ref(false)
  const elapsedSeconds = ref(0)
  /** User-visible reason the last recording could not start. */
  const error = ref('')

  let recorder: MediaRecorder | null = null
  let stream: MediaStream | null = null
  let chunks: Blob[] = []
  let timer: ReturnType<typeof setInterval> | null = null
  let mimeType = ''
  let discarded = false

  const stopTracks = () => {
    stream?.getTracks().forEach((track) => track.stop())
    stream = null
  }

  const stopTimer = () => {
    if (timer !== null) clearInterval(timer)
    timer = null
  }

  const reset = () => {
    recorder = null
    chunks = []
    stopTimer()
    stopTracks()
    isRecording.value = false
    elapsedSeconds.value = 0
  }

  // Start recording. Asks the browser for the microphone; a refusal surfaces as
  // a message instead of failing silently.
  const start = async () => {
    if (isRecording.value) return
    error.value = ''

    if (!isSupported.value) {
      error.value = PANEL_COPY.micUnavailable
      return
    }

    mimeType = pickMimeType()

    let micStream: MediaStream
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (err) {
      const name = (err as Error)?.name
      if (name === 'NotAllowedError' || name === 'SecurityError') {
        error.value = PANEL_COPY.micDenied
      } else if (name === 'NotFoundError' || name === 'OverconstrainedError') {
        error.value = 'No microphone was found on this device.'
      } else {
        error.value = 'Could not start the microphone. Please try again.'
      }
      return
    }

    stream = micStream
    chunks = []
    discarded = false

    try {
      recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream)
    } catch {
      // The chosen container was rejected after all; retry with the default.
      try {
        recorder = new MediaRecorder(stream)
        mimeType = ''
      } catch {
        error.value = PANEL_COPY.micUnavailable
        reset()
        return
      }
    }

    recorder.ondataavailable = (event: BlobEvent) => {
      if (event.data.size > 0) chunks.push(event.data)
    }
    recorder.onstop = () => {
      const duration = elapsedSeconds.value
      const type = recorder?.mimeType || mimeType || 'audio/webm'
      const base = type.split(';')[0]
      const extension = EXTENSION_FOR_MIME[base] || 'webm'
      const blob = new Blob(chunks, { type: base })
      const wasDiscarded = discarded
      reset()
      if (!wasDiscarded && blob.size > 0) {
        onRecorded(new File([blob], `voice-note-${Date.now()}.${extension}`, { type: base }), duration)
      }
    }
    recorder.onerror = () => {
      error.value = 'Recording failed. Please try again.'
      reset()
    }

    recorder.start()
    isRecording.value = true
    elapsedSeconds.value = 0
    timer = setInterval(() => { elapsedSeconds.value += 1 }, 1000)
  }

  // Stop and keep the recording: onRecorded fires from the recorder's onstop.
  const stop = () => {
    if (!isRecording.value || !recorder) return
    if (recorder.state !== 'inactive') recorder.stop()
  }

  // Stop and throw the recording away; onRecorded does not fire.
  const cancel = () => {
    if (!isRecording.value) return
    discarded = true
    if (recorder && recorder.state !== 'inactive') recorder.stop()
    else reset()
  }

  // Dismiss a shown error (the view clears it when the visitor retries).
  const clearError = () => { error.value = '' }

  onBeforeUnmount(() => {
    if (recorder && recorder.state !== 'inactive') {
      discarded = true
      recorder.stop()
    }
    reset()
  })

  return {
    isSupported,
    isRecording,
    elapsedSeconds,
    error,
    start,
    stop,
    cancel,
    clearError,
  }
}
