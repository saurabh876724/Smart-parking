import { useEffect, useState } from 'react'
import { streamUrl, type CameraLive } from '../lib/api'
import { Loader2, RefreshCw, Video, VideoOff } from 'lucide-react'
import { Chip } from './ui/chip'
import type { Tone } from './ui/panel'
import { timeAgo } from '../lib/time'

interface CameraPreviewProps {
  cameraId: string
  /** What the process says about this camera, so the caption can name a replay as
   * a replay instead of letting every picture read as a field camera. */
  live?: CameraLive
  /** Kept by the page so a re-render on a socket event cannot reopen the stream. */
  className?: string
}

/** What the picture element is doing, as far as a browser can tell.
 *
 * An `<img>` reports failure without a status code, so a stream refused for
 * credentials and a stream with no frames yet look the same here. Both causes are
 * named rather than guessed at.
 */
type Phase = 'connecting' | 'live' | 'stopped' | 'signed_out'

export function CameraPreview({ cameraId, live, className = '' }: CameraPreviewProps) {
  const [attempt, setAttempt] = useState(0)
  const [url, setUrl] = useState<string | null>(null)
  const [phase, setPhase] = useState<Phase>('connecting')
  const [seenAt, setSeenAt] = useState<number | null>(null)

  useEffect(() => {
    // Read once per connect, not on every render: this dashboard re-renders on
    // every socket event, and a `src` recomputed there would close and reopen the
    // stream under the operator. The token is whatever the session holds right now,
    // so a reconnect after a refresh sends the new credential.
    const next = streamUrl(cameraId)
    setUrl(next)
    setPhase(next === null ? 'signed_out' : 'connecting')
  }, [cameraId, attempt])

  return (
    <section className={`panel overflow-hidden ${className}`}>
      <header className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 border-b border-line">
        <div className="flex items-center gap-2 min-w-0">
          <Video className="w-4 h-4 text-volt-400 shrink-0" />
          <h2 className="text-sm font-semibold text-mist-50 truncate">Processed feed</h2>
          <span className="text-xs text-mist-600 font-mono truncate">{cameraId}</span>
        </div>
        <PhaseChip phase={phase} cameraId={cameraId} />
      </header>

      <div className="relative bg-ink-950 h-[420px] sm:h-[520px] flex items-center justify-center overflow-hidden">
        {/* Only while there is a chance of a picture: a failed element stays in the
            tree otherwise, and the browser would retry it on its own. */}
        {url !== null && (phase === 'connecting' || phase === 'live') && (
          <img
            // A new key is what actually reconnects: the URL string can be the
            // same one the browser just failed on, and only a remounted element
            // re-opens the multipart request.
            key={attempt}
            src={url}
            alt={`Processed video from ${cameraId}`}
            // The credential is in this URL, so it must not also travel as the
            // referer of a request to some other host.
            referrerPolicy="no-referrer"
            onLoad={() => {
              setPhase('live')
              setSeenAt(Date.now())
            }}
            onError={() => setPhase('stopped')}
            // Sized to the frame, never stretched to the card: these sources are
            // portrait, and a full-width `img` blew a 478x850 frame up to a
            // thousand pixels tall.
            className="max-h-full max-w-full w-auto object-contain"
          />
        )}

        {phase !== 'live' && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center">
            <span className="text-mist-600">
              {phase === 'connecting' ? <Loader2 className="w-6 h-6 animate-spin" /> : <VideoOff className="w-6 h-6" />}
            </span>
            <p className="text-sm text-mist-400 max-w-xl leading-relaxed">{PHASE_TEXT[phase]}</p>
            {phase !== 'connecting' && url !== null && (
              <button
                type="button"
                onClick={() => setAttempt((a) => a + 1)}
                className="inline-flex items-center gap-2 rounded-lg border border-volt-500/40 bg-volt-500/12 px-3 py-1.5 text-sm text-volt-300 hover:bg-volt-500/20 transition-colors"
              >
                <RefreshCw className="w-4 h-4" />
                Reconnect
              </button>
            )}
          </div>
        )}

        {phase === 'live' && live?.synthetic_replay && (
          <span className="absolute left-3 top-3 rounded-md border border-amber-400/40 bg-ink-950/80 px-2 py-1 text-[0.68rem] font-semibold uppercase tracking-wider text-amber-200">
            Synthetic replay
          </span>
        )}
      </div>

      <footer className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 border-t border-line text-[0.7rem] text-mist-600 leading-snug">
        <span className="max-w-3xl">
          {live?.streaming ? `Reader active · ${live.frames_processed ?? 0} frame(s) over ${live.passes} pass(es) · ` : ''}
          Detections, track ids, bay polygons and the occupancy HUD are drawn into these JPEGs by
          <code className="mx-1 text-mist-400">parking/render.py</code>
          - the same drawing the pipeline writes into its annotated clips.
          {seenAt !== null && ` Frame received ${timeAgo(seenAt)}.`}
        </span>
        <button
          type="button"
          onClick={() => setAttempt((a) => a + 1)}
          className="shrink-0 inline-flex items-center gap-1.5 rounded-lg border border-line-strong px-2 py-1 hover:text-mist-50 hover:border-volt-500/50 transition-colors"
          title="Reopen the stream with the current access token"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          Reconnect
        </button>
      </footer>
    </section>
  )
}

const PHASE_TEXT: Record<Exclude<Phase, 'live'>, string> = {
  connecting: 'Opening the stream…',
  stopped:
    'The picture stopped. An image element reports no status, so the two real causes are the ' +
    'pipeline having produced no frame for this camera - it only runs when `api.pipeline` is ' +
    'enabled and lists it - and this session no longer being accepted. Reconnect rebuilds the ' +
    'request with the current access token.',
  signed_out:
    'This browser holds no access token, so there is nothing to send with the picture. Sign in again.',
}

const PHASE_FACE: Record<Phase, { label: string; tone: Tone }> = {
  connecting: { label: 'Connecting', tone: 'warn' },
  live: { label: 'Streaming', tone: 'good' },
  stopped: { label: 'Not streaming', tone: 'bad' },
  signed_out: { label: 'Not signed in', tone: 'bad' },
}

/** The state of this one stream, kept apart from the header's event-channel chip:
 * `/ws/live` carries bay statuses and is a different connection with different
 * failure modes.
 */
function PhaseChip({ phase, cameraId }: { phase: Phase; cameraId: string }) {
  const face = PHASE_FACE[phase]
  return (
    <Chip
      tone={face.tone}
      label={face.label}
      pulse={phase === 'live'}
      hint={`MJPEG stream for ${cameraId}`}
    />
  )
}
