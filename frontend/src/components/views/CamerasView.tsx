import { useQuery } from '@tanstack/react-query'
import { Camera, Cctv, Clapperboard, RefreshCw, VideoOff } from 'lucide-react'
import { cameraOverlay, health, type CameraInfo } from '../../lib/api'
import { cameraRows } from '../../lib/health'
import { useLive } from '../../lib/live'
import { dateAndTime, isoToMs, timeAgo } from '../../lib/time'
import { useNow } from '../../lib/useNow'
import { CameraPreview } from '../CameraPreview'
import { Chip } from '../ui/chip'
import { ErrorState, LoadingState, PlannedState } from '../ui/states'
import { Panel, PanelHeader, SectionTitle, TONE } from '../ui/panel'
import { Row, Table, Td, Th } from '../ui/table'

/** Every camera the server knows about, and what this process is doing with it.
 *
 * The registry and the running pipelines are two different things: a row is
 * configuration from `configs/app.yaml`, while `live` is whichever camera this API
 * process actually started. The page shows both per camera, so a demo can say
 * truthfully that two of four cameras are not being analysed right now.
 */
export function CamerasView() {
  const live = useLive()
  const now = useNow(2000)
  const selected = live.cameras.find((entry) => entry.camera_id === live.cameraId)

  return (
    <div className="space-y-5">
      <SectionTitle
        title="Camera monitoring"
        subtitle="The annotated picture the pipeline draws, what it says it is reading, and which cameras this process runs at all."
        actions={
          <button
            type="button"
            onClick={() => live.refresh()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-line-strong px-2.5 py-1.5 text-xs text-mist-400 hover:text-mist-50 hover:border-volt-500/50 transition-colors"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            Re-read bays
          </button>
        }
      />

      {live.cameras.length === 0 ? (
        <Panel>
          <LoadingState label="Reading the camera registry…" />
        </Panel>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {live.cameras.map((camera) => (
            <CameraTile
              key={camera.camera_id}
              camera={camera}
              selected={camera.camera_id === live.cameraId}
              onSelect={() => live.setCameraId(camera.camera_id)}
              now={now}
            />
          ))}
        </div>
      )}

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
        <CameraPreview cameraId={live.cameraId} live={selected?.live} />
        <OverlayTable cameraId={live.cameraId} />
      </div>

      <Panel rise={false}>
        <PanelHeader title="Physical camera input" icon={<VideoOff className="w-4 h-4" />} meta="not connected" />
        <PlannedState
          title="No RTSP or USB camera is wired to this system"
          detail="Both feeds come from video files under data/samples, replayed by the pipeline. Connecting a camera means adding its source to configs/app.yaml and restarting the API process; nothing in this UI starts or stops a reader, and no camera has been opened without that configuration."
          evidence="GET /cameras -> live.source is a file path or an index; live.streaming is only true for cameras listed under api.pipeline.cameras."
        />
      </Panel>
    </div>
  )
}

function CameraTile({
  camera,
  selected,
  onSelect,
  now,
}: {
  camera: CameraInfo
  selected: boolean
  onSelect: () => void
  now: number
}) {
  const rows = cameraRows(camera)
  const streaming = camera.live.streaming
  const seenAt = isoToMs(camera.last_seen_at)
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`panel panel-hover p-4 text-left transition-[border-color] ${selected ? 'border-volt-500/60' : ''}`}
      title={selected ? 'This camera is shown below' : 'Show this camera below'}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-mist-50 truncate">{camera.name || camera.camera_id}</p>
          <p className="text-[0.68rem] text-mist-600 font-mono truncate">{camera.camera_id}</p>
        </div>
        <Chip
          tone={streaming ? 'good' : camera.live.frames_available ? 'warn' : 'neutral'}
          label={streaming ? 'Streaming' : camera.live.frames_available ? 'Idle' : 'Not running'}
          pulse={streaming}
        />
      </div>

      <dl className="mt-3 space-y-1.5 text-[0.7rem]">
        <Fact label="Role" value={camera.role} />
        <Fact label="Source" value={camera.source ?? 'none configured'} mono />
        <Fact
          label="Frames"
          value={camera.live.frames_processed === null ? 'not counted' : `${camera.live.frames_processed} over ${camera.live.passes} pass(es)`}
        />
        <Fact
          label="Last stored event"
          value={seenAt === null ? 'none yet' : `${timeAgo(seenAt, now)} · ${dateAndTime(camera.last_seen_at ?? '')}`}
        />
      </dl>

      <div className="mt-3 flex flex-wrap gap-1.5">
        {camera.live.synthetic_replay && (
          <span className="inline-flex items-center gap-1 rounded-md border border-amber-400/30 bg-amber-400/10 px-1.5 py-0.5 text-[0.62rem] font-medium text-amber-200">
            <Clapperboard className="w-3 h-3" />
            Synthetic replay
          </span>
        )}
        {camera.live.loop && (
          <span className="rounded-md border border-line-strong bg-ink-850 px-1.5 py-0.5 text-[0.62rem] text-mist-400">looping</span>
        )}
        <span className={`rounded-md px-1.5 py-0.5 text-[0.62rem] ${camera.live.read_plates ? TONE.good.chip : TONE.warn.chip}`}>
          {camera.live.read_plates ? 'plate reading on' : 'plate reading off'}
        </span>
      </div>

      {rows[1].detail && <p className="mt-3 text-[0.66rem] leading-relaxed text-mist-600">{rows[1].detail}</p>}
    </button>
  )
}

function Fact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <dt className="text-mist-600">{label}</dt>
      <dd className={`truncate text-mist-200 ${mono ? 'font-mono text-[0.65rem]' : ''}`}>{value}</dd>
    </div>
  )
}

/** The JSON half of the newest frame, which is what the picture's boxes are made of.
 *
 * `GET /cameras/{id}/overlay` answers 409 `no_frame` for a camera this process has
 * not run, and that is shown as its own state instead of an empty table.
 */
function OverlayTable({ cameraId }: { cameraId: string }) {
  const { data, error, isFetching, refetch } = useQuery({
    queryKey: ['overlay', cameraId],
    queryFn: () => cameraOverlay(cameraId),
    refetchInterval: 3000,
    retry: false,
  })

  const refused = error instanceof Error && /no_frame|409/.test(error.message)

  return (
    <Panel rise={false} padded={false} className="overflow-hidden">
      <div className="px-4 pt-4">
        <PanelHeader
          title="Detection overlay"
          icon={<Cctv className="w-4 h-4" />}
          meta={data ? `frame #${data.seq}` : refused ? 'no frame for this camera' : 'reading'}
          actions={
            <button
              type="button"
              onClick={() => void refetch()}
              className="inline-flex items-center gap-1.5 rounded-lg border border-line-strong px-2 py-1 text-[0.7rem] text-mist-400 hover:text-mist-50"
              title="Ask for the newest processed frame's geometry"
            >
              <Camera className="w-3.5 h-3.5" />
              Now
            </button>
          }
        />
      </div>

      <div className="p-4 pt-0">
        {refused ? (
          <p className="text-sm text-mist-600 leading-relaxed">
            This process holds no frame for <span className="font-mono text-mist-400">{cameraId}</span>. The overlay only
            exists for a camera whose pipeline has read at least one frame.
          </p>
        ) : error ? (
          <ErrorState error={error} />
        ) : !data ? (
          <LoadingState label="Reading the newest frame…" />
        ) : (
          <>
            <p className="text-[0.7rem] text-mist-600 mb-3 leading-relaxed">
              Same geometry the annotated JPEG burns into the picture, sampled every three seconds while this page is
              open{isFetching ? ' · refreshing' : ''}.
            </p>
            {data.tracks.length === 0 ? (
              <p className="text-sm text-mist-600">This frame holds no tracked vehicles.</p>
            ) : (
              <Table
                head={
                  <>
                    <Th>Track</Th>
                    <Th>Label</Th>
                    <Th>Confidence</Th>
                    <Th>Bounding box</Th>
                  </>
                }
              >
                {data.tracks.map((track) => (
                  <Row key={track.track_id}>
                    <Td className="font-mono text-xs text-mist-50">{track.track_id}</Td>
                    <Td className="text-mist-200">{track.label}</Td>
                    <Td className="tabular">{(track.confidence * 100).toFixed(1)}%</Td>
                    <Td className="font-mono text-[0.7rem] text-mist-400 tabular">
                      {track.bbox.map((n) => Math.round(n)).join(', ')}
                    </Td>
                  </Row>
                ))}
              </Table>
            )}
            <div className="mt-3 border-t border-line pt-3">
              <HealthNumbers cameraId={cameraId} />
            </div>
          </>
        )}
      </div>
    </Panel>
  )
}

/** The server's own counters, so a camera page is not only about pictures. */
function HealthNumbers({ cameraId }: { cameraId: string }) {
  const { data } = useQuery({ queryKey: ['health'], queryFn: health, refetchInterval: 30_000, staleTime: 30_000 })
  const state = data?.cameras.find((entry) => entry.camera_id === cameraId)
  if (!data) return <p className="text-[0.68rem] text-mist-600">No health counters read yet.</p>
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[0.68rem]">
      <Fact label="Frames into the hub" value={String(data.frames_in)} />
      <Fact label="Events published" value={String(data.events_out)} />
      <Fact label="Dropped events" value={String(data.dropped_events)} />
      <Fact label="Passes for this camera" value={String(state?.passes ?? 0)} />
    </dl>
  )
}
