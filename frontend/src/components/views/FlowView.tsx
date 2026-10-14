import { CircleAlert, CircleCheck, CircleSlash, Clock, Pause } from 'lucide-react'
import { Chip } from '../ui/chip'
import { Panel, PanelHeader, SectionTitle, type Tone } from '../ui/panel'
import { BRANCH_KIND_LABEL, FLOW_EDGE_CASES, FLOW_STEPS, type BranchKind } from '../../lib/flow'

const KIND_TONE: Record<BranchKind, Tone> = {
  continue: 'good',
  hold: 'info',
  stop: 'neutral',
  abort: 'warn',
}

const KIND_ICON: Record<BranchKind, typeof CircleCheck> = {
  continue: CircleCheck,
  hold: Pause,
  stop: CircleSlash,
  abort: CircleAlert,
}

export function FlowView() {
  return (
    <div className="space-y-5">
      <SectionTitle
        title="Working Flow"
        subtitle="One frame of video, from the file on disk to a bay changing colour on this screen. Nine steps, and every branch is the condition the code actually tests rather than the condition the design would prefer."
        actions={
          <div className="flex items-center gap-2 flex-wrap">
            {(Object.keys(BRANCH_KIND_LABEL) as BranchKind[]).map((kind) => (
              <Chip key={kind} tone={KIND_TONE[kind]} label={BRANCH_KIND_LABEL[kind]} />
            ))}
          </div>
        }
      />

      <Panel>
        <PanelHeader title="The timings that decide a state" icon={<Clock className="w-4 h-4" />} meta="configs/app.yaml:22" />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {[
            { k: 'Evidence to occupy', v: '0.6 s', d: 'a claim must hold continuously' },
            { k: 'Absence to free', v: '8.0 s', d: 'a false FREE double-books a bay' },
            { k: 'Containment to claim', v: '0.45', d: 'of the vehicle footprint inside the polygon' },
            { k: 'Straddle violation', v: '0.20 ×2', d: 'on two bays at once publishes a violation' },
          ].map((item) => (
            <div key={item.k} className="rounded-xl border border-line bg-ink-950/60 p-3.5">
              <p className="text-[0.68rem] uppercase tracking-[0.12em] text-mist-600">{item.k}</p>
              <p className="text-lg font-semibold text-volt-300 tabular mt-1">{item.v}</p>
              <p className="text-[0.7rem] text-mist-400 mt-1 leading-relaxed">{item.d}</p>
            </div>
          ))}
        </div>
      </Panel>

      <ol className="space-y-4">
        {FLOW_STEPS.map((step) => (
          <li key={step.id}>
            <Panel padded={false} className="overflow-hidden">
              <div className="flex items-start gap-3 p-5 pb-3">
                <span className="shrink-0 mt-0.5 inline-flex h-7 w-7 items-center justify-center rounded-full border border-volt-500/40 bg-volt-500/12 text-[0.78rem] font-semibold text-volt-300 tabular">
                  {step.n}
                </span>
                <div className="min-w-0">
                  <h2 className="text-[0.98rem] font-semibold text-mist-50 tracking-tight">{step.title}</h2>
                  <p className="text-sm text-mist-400 leading-relaxed mt-1">{step.what}</p>
                  <p className="text-[0.7rem] text-mist-600 font-mono mt-1.5 break-all">{step.file}</p>
                </div>
              </div>
              {step.branches.length > 0 && (
                <div className="px-5 pb-5 space-y-2">
                  {step.branches.map((branch, index) => {
                    const Icon = KIND_ICON[branch.kind]
                    return (
                      <div
                        key={`${branch.when}-${index}`}
                        className="rounded-lg border border-line bg-ink-950/50 p-3 flex gap-3"
                      >
                        <span className={`shrink-0 mt-0.5 text-mist-600 ${TONE_TEXT[branch.kind]}`}>
                          <Icon className="w-4 h-4" />
                        </span>
                        <div className="min-w-0">
                          <p className="text-xs font-medium text-mist-200 leading-relaxed">
                            <span className="text-mist-600">if </span>
                            {branch.when}
                          </p>
                          <p className="text-xs text-mist-400 leading-relaxed mt-1">{branch.outcome}</p>
                          <div className="flex flex-wrap items-center gap-2 mt-1.5">
                            <Chip tone={KIND_TONE[branch.kind]} label={BRANCH_KIND_LABEL[branch.kind]} />
                            <span className="text-[0.68rem] text-mist-600 font-mono break-all">{branch.line}</span>
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </Panel>
          </li>
        ))}
      </ol>

      <Panel>
        <PanelHeader title="The four questions this flow gets asked directly" meta="answered from the source" />
        <div className="grid gap-3 lg:grid-cols-2">
          {FLOW_EDGE_CASES.map((case_) => (
            <div key={case_.question} className="rounded-xl border border-line bg-ink-950/60 p-4">
              <p className="text-sm font-medium text-mist-50">{case_.question}</p>
              <p className="text-xs text-mist-400 leading-relaxed mt-1.5">{case_.answer}</p>
              <p className="text-[0.7rem] text-mist-600 font-mono mt-2 pt-2 border-t border-line break-all">
                {case_.evidence}
              </p>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  )
}

const TONE_TEXT: Record<BranchKind, string> = {
  continue: 'text-[#6ee7a8]',
  hold: 'text-volt-300',
  stop: 'text-mist-400',
  abort: 'text-amber-200',
}
