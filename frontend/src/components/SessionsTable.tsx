import { durationMinutes, plateFor, type SessionRecord } from '../lib/api'
import { dateAndTime } from '../lib/time'
import { Or, Row, SessionStatusBadge, Table, Td, Th } from './ui/table'
import { EmptyState } from './ui/states'

/** Recorded visits, mapped onto `SessionResponse` names rather than onto a shape
 * this project once assumed it had.
 *
 * A plate renders as the full number only for a role the server lets reveal it;
 * everyone else sees the mask, and "no plate was read" is shown as its own answer
 * because ANPR is switched off in this configuration.
 */
export function SessionsTable({
  rows,
  showCamera = false,
  emptyDetail,
}: {
  rows: SessionRecord[]
  showCamera?: boolean
  emptyDetail?: string
}) {
  if (rows.length === 0) {
    return (
      <EmptyState
        title="No visits match this view"
        detail={
          emptyDetail ??
          'The query answered successfully with an empty page. Sessions are written by the gate pipeline when it is running, so a lot that has not been driven through a gate has no rows yet.'
        }
      />
    )
  }

  return (
    <Table
      head={
        <>
          <Th>Session</Th>
          <Th>Plate</Th>
          {showCamera && <Th>Camera</Th>}
          <Th>Entry</Th>
          <Th>Exit</Th>
          <Th>Duration</Th>
          <Th>Status</Th>
        </>
      }
    >
      {rows.map((row) => {
        const minutes = durationMinutes(row.duration_seconds)
        return (
          <Row key={row.id}>
            <Td className="font-mono text-xs text-mist-400 whitespace-nowrap">
              <Or>{row.source_session_id}</Or>
            </Td>
            <Td className="font-medium text-mist-50 whitespace-nowrap">
              {plateFor(row) ?? <span className="text-mist-600 italic">not read</span>}
            </Td>
            {showCamera && <Td className="text-mist-400 font-mono text-xs whitespace-nowrap">{row.camera_id}</Td>}
            <Td className="text-mist-400 whitespace-nowrap tabular">
              <Or>{row.entry_time ? dateAndTime(row.entry_time) : null}</Or>
            </Td>
            <Td className="text-mist-400 whitespace-nowrap tabular">
              <Or>{row.exit_time ? dateAndTime(row.exit_time) : null}</Or>
            </Td>
            <Td className="tabular whitespace-nowrap">
              {/* `0` is a measured visit under a minute; null was never measured. */}
              <Or>{minutes === null ? null : `${minutes} min`}</Or>
            </Td>
            <Td>
              <div className="flex flex-col items-start gap-1">
                <SessionStatusBadge status={row.status} />
                {row.review_reason && <span className="text-[0.65rem] text-amber-200/80">{row.review_reason}</span>}
                {row.anomaly_reason && <span className="text-[0.65rem] text-rose-200/80">{row.anomaly_reason}</span>}
              </div>
            </Td>
          </Row>
        )
      })}
    </Table>
  )
}
