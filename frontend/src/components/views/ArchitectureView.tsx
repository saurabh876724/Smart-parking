import { ArrowDown, ArrowRight, Boxes, CircleGauge, Cpu, GitBranch } from 'lucide-react'
import { Chip } from '../ui/chip'
import { Panel, PanelHeader, SectionTitle, type Tone } from '../ui/panel'
import { ARCH_STAGES, PLANNED_COMPONENTS, STATUS_LABEL, type ArchNode, type NodeStatus } from '../../lib/architecture'

const STATUS_TONE: Record<NodeStatus, Tone> = {
  running: 'good',
  configured: 'warn',
  planned: 'unknown',
}

/** One box of the diagram.
 *
 * The file reference is part of the box, not a footnote: the claim is only worth
 * making if a reader can open the source beside it.
 */
function NodeBox({ node }: { node: ArchNode }) {
  return (
    <div className="rounded-xl border border-line bg-ink-900/70 p-4 flex flex-col gap-2.5">
      <div className="flex items-start justify-between gap-3">
        <h3 className="text-sm font-semibold text-mist-50 leading-snug">{node.title}</h3>
        <Chip tone={STATUS_TONE[node.status]} label={STATUS_LABEL[node.status]} />
      </div>
      <p className="text-[0.7rem] uppercase tracking-[0.12em] text-volt-300 font-medium">{node.tech}</p>
      <p className="text-xs text-mist-400 leading-relaxed">{node.detail}</p>
      <p className="text-[0.7rem] text-mist-600 font-mono mt-auto pt-1.5 border-t border-line break-all">
        {node.file}
      </p>
    </div>
  )
}

function Connector() {
  return (
    <div className="flex justify-center py-1" aria-hidden>
      <ArrowDown className="w-5 h-5 text-volt-500/70" />
    </div>
  )
}

export function ArchitectureView() {
  return (
    <div className="space-y-5">
      <SectionTitle
        title="Project Architecture"
        subtitle="Every box below exists in this repository. The direction of data is video, then inference, then geometry, then state, then the bus that feeds both the socket and the database, then this browser."
        actions={
          <div className="flex items-center gap-2">
            <Chip tone="good" label="Running here" />
            <Chip tone="warn" label="In the code, off" />
            <Chip tone="unknown" label="Not implemented" />
          </div>
        }
      />

      <Panel className="flex flex-wrap items-center gap-2 text-xs text-mist-400">
        <GitBranch className="w-4 h-4 text-volt-400" />
        <span className="font-mono text-[0.72rem] text-mist-200">
          clip → capture → detect → track → geometry → assign → FSM ─┬→ bus ─┬→ ws → browser
        </span>
        <span className="font-mono text-[0.72rem] text-mist-200">└→ persistence → SQLite → REST → browser</span>
        <span className="w-full sm:w-auto flex items-center gap-1.5 text-[0.7rem] text-mist-600">
          <ArrowRight className="w-3.5 h-3.5" />
          annotated frame → FrameHub → MJPEG → the picture on Camera Monitoring
        </span>
      </Panel>

      {ARCH_STAGES.map((stage, index) => (
        <div key={stage.id}>
          <Panel padded={false} className="overflow-hidden">
            <div className="p-5 pb-3">
              <PanelHeader
                title={`${stage.label} · ${stage.nodes.length} ${stage.nodes.length === 1 ? 'component' : 'components'}`}
                icon={<Boxes className="w-4 h-4" />}
                meta={stage.caption}
              />
            </div>
            <div className="grid gap-3 px-5 pb-5 sm:grid-cols-2 xl:grid-cols-3">
              {stage.nodes.map((node) => (
                <NodeBox key={node.id} node={node} />
              ))}
            </div>
          </Panel>
          {index < ARCH_STAGES.length - 1 && <Connector />}
        </div>
      ))}

      <Panel>
        <PanelHeader
          title="Planned, or switched off - kept out of the main diagram"
          icon={<Cpu className="w-4 h-4" />}
          meta="nothing in this box may be described as running"
        />
        <div className="grid gap-3 sm:grid-cols-2">
          {PLANNED_COMPONENTS.map((node) => (
            <NodeBox key={node.id} node={node} />
          ))}
        </div>
        <div className="mt-4 rounded-xl border border-line bg-ink-950/60 p-4">
          <h3 className="text-sm font-semibold text-mist-50 mb-2 flex items-center gap-2">
            <CircleGauge className="w-4 h-4 text-volt-400" />
            Four sentences to say out loud in a viva
          </h3>
          <ul className="text-xs text-mist-400 space-y-1.5 leading-relaxed list-disc pl-4">
            <li>The system today reads recorded video, not a field camera.</li>
            <li>It does not read plates on the streams it serves, so the two plate tables are empty by configuration, not by failure.</li>
            <li>It cannot bill; the revenue endpoint answers 501 on purpose.</li>
            <li>It is not deployed through nginx on this machine - the dashboard reaches the API through the Vite dev proxy.</li>
          </ul>
        </div>
      </Panel>
    </div>
  )
}
