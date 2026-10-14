/** One frame's journey, written against the source rather than the intention.
 *
 * Each step names the file it came from, and each branch names the condition the
 * code actually tests, so a reader can open `pipeline/lot_pipeline.py` beside this
 * page and check the line. Nothing here is aspirational: the states a bay can hold
 * are the five the FSM has, and the timings are the ones `configs/app.yaml` sets.
 */

export type BranchKind = 'continue' | 'hold' | 'stop' | 'abort'

export interface FlowBranch {
  /** The test, in the code's own terms. */
  when: string
  /** What happens when the test is true. */
  outcome: string
  kind: BranchKind
  /** Where to check it. */
  line: string
}

export interface FlowStep {
  id: string
  n: number
  title: string
  what: string
  file: string
  branches: FlowBranch[]
}

export const BRANCH_KIND_LABEL: Record<BranchKind, string> = {
  continue: 'moves on',
  hold: 'waits, no new state',
  stop: 'ends this loop',
  abort: 'reports a problem',
}

export const FLOW_STEPS: readonly FlowStep[] = [
  {
    id: 'read',
    n: 1,
    title: 'Read a frame',
    what: 'The camera loop asks its source for the next picture. A file clip is read with OpenCV and paced by the frame rate authored into the clip; a live source would be served by a grabber thread that keeps only the newest frame.',
    file: 'pipeline/lot_pipeline.py:123-208 · video/sources.py:39 · video/grabber.py',
    branches: [
      {
        when: 'cap.read() returns False and the clip is configured to loop',
        outcome: 'Rewind to frame 0 and read again. This is what the gate clip does about every four seconds.',
        kind: 'continue',
        line: 'lot_pipeline.py:203-205',
      },
      {
        when: 'cap.read() returns False at the end of a clip that does not loop',
        outcome: 'The run returns its statistics and the loop is over.',
        kind: 'stop',
        line: 'lot_pipeline.py:203-208',
      },
      {
        when: 'a live source has produced no frame for OFFLINE_AFTER_S',
        outcome: 'Every bay of that camera is marked UNKNOWN. A dead camera is never guessed to be empty.',
        kind: 'abort',
        line: 'lot_pipeline.py:188-195',
      },
      {
        when: 'frame_stride is greater than 1 and this frame is skipped',
        outcome: 'Only the dwell timers tick. Time advances without new evidence, which is a real way to reach FREE.',
        kind: 'hold',
        line: 'lot_pipeline.py:210',
      },
    ],
  },
  {
    id: 'detect',
    n: 2,
    title: 'Detect vehicles',
    what: 'YOLO11s runs the frame at 640 px with confidence 0.35 and keeps the four COCO vehicle classes: car, motorcycle, bus, truck. Detection answers where a vehicle is and nothing about bays.',
    file: 'detection/vehicle_detector.py',
    branches: [
      {
        when: 'no vehicle is found',
        outcome: 'An empty track list is still handed to the engine. Absence is evidence, and it is the only way an occupied bay can later become free.',
        kind: 'continue',
        line: 'lot_pipeline.py:217',
      },
      {
        when: 'the source is the gate fixture',
        outcome: 'Labelled boxes with their track ids are replayed from a JSON file and YOLO never runs; health marks the camera synthetic_replay: true.',
        kind: 'continue',
        line: 'pipeline/gate_pipeline.py · data/fixtures/gate_visit_tracks.json',
      },
    ],
  },
  {
    id: 'track',
    n: 3,
    title: 'Track and re-identify',
    what: 'ByteTrack carries the same track id across frames, so a parked car stays one object instead of becoming a new one every frame.',
    file: 'tracking/tracker.py (persist=True)',
    branches: [
      {
        when: 'a track moves faster than the velocity gate (30 px/s)',
        outcome: 'It is eligible for assignment but contributes no bay evidence: a car driving past is not parking.',
        kind: 'hold',
        line: 'lot_pipeline.py velocity_gating.max',
      },
    ],
  },
  {
    id: 'associate',
    n: 4,
    title: 'Associate with bays',
    what: 'The bottom 40 % of a box is treated as the footprint on the tarmac, clipped against each bay polygon with Sutherland-Hodgman and scored as containment - how much of the car is inside the bay, not how much of the bay the car covers.',
    file: 'parking/geometry.py:160 · configs/slots/lot_a_cam1.json',
    branches: [
      {
        when: 'best containment is at least 0.45',
        outcome: 'The track claims that bay; the engine assigns greedily, one car per bay.',
        kind: 'continue',
        line: 'parking/occupancy_engine.py',
      },
      {
        when: 'containment is below 0.20 for every bay',
        outcome: 'The vehicle is in the aisle and contributes no evidence.',
        kind: 'hold',
        line: 'parking/occupancy_engine.py',
      },
      {
        when: 'containment reaches 0.20 or more on two bays at once',
        outcome: 'A violation event is published - the car is straddling the line - rather than the engine silently choosing one bay.',
        kind: 'abort',
        line: 'parking/occupancy_engine.py',
      },
    ],
  },
  {
    id: 'decide',
    n: 5,
    title: 'Decide occupied or free',
    what: 'No bay changes state on one frame. The proposal for each bay this frame is either a claim or an absence, and that is the input to the state machine.',
    file: 'parking/occupancy_engine.py:163,231',
    branches: [
      {
        when: 'the same bay is claimed by two tracks',
        outcome: 'The stronger evidence wins the bay this frame; the loser is unassigned rather than double-booked.',
        kind: 'continue',
        line: 'parking/occupancy_engine.py',
      },
    ],
  },
  {
    id: 'fsm',
    n: 6,
    title: 'State machine and debounce',
    what: 'Each bay is a small FSM holding evidence against the frame clock: FREE to PENDING_OCCUPIED to OCCUPIED needs 0.6 s of continuous evidence, OCCUPIED to PENDING_FREE to FREE needs 8.0 s with nothing in it.',
    file: 'parking/slot_fsm.py · configs/app.yaml:22 occupancy',
    branches: [
      {
        when: 'evidence has not yet held long enough',
        outcome: 'The bay stays in its PENDING state. Those two states exist only in memory - GET /slots documents that they are never stored.',
        kind: 'hold',
        line: 'api/routers/slots.py:32-38',
      },
      {
        when: '0.6 s of claim elapses',
        outcome: 'Commit OCCUPIED with the track id and score, and publish slot_occupied.',
        kind: 'continue',
        line: 'parking/slot_fsm.py',
      },
      {
        when: '8.0 s of nothing elapses on an occupied bay',
        outcome: 'Commit FREE and publish slot_freed. The delay is deliberately long: a false FREE double-books a slot.',
        kind: 'continue',
        line: 'parking/slot_fsm.py · configs/app.yaml:22',
      },
    ],
  },
  {
    id: 'bus',
    n: 7,
    title: 'Publish on the event bus',
    what: 'The engine publishes on an in-memory, bounded bus and returns immediately. slot_snapshot carries the first committed reading of every bay, then slot_occupied and slot_freed on transitions, and a summary counter every frame.',
    file: 'core/events.py · core/schemas.py:278-290',
    branches: [
      {
        when: 'a bay has never held a car',
        outcome: 'slot_snapshot still delivers its first committed reading, otherwise it would sit at UNKNOWN forever.',
        kind: 'continue',
        line: 'core/schemas.py:281-285',
      },
    ],
  },
  {
    id: 'persist',
    n: 8,
    title: 'Persist, only when asked',
    what: 'A daemon thread drains up to 200 events at a time and writes them in one transaction: parking_slots.current_status, slot_occupancy_history, detection_logs, parking_sessions, cameras.',
    file: 'services/persistence.py · db/repositories/event_log.py:34',
    branches: [
      {
        when: 'serve.py was started without --persist',
        outcome: 'Nothing is written to the database at all, and the startup banner says so. The API still answers from what is already there.',
        kind: 'abort',
        line: 'serve.py --persist',
      },
      {
        when: 'the event is the every-frame summary',
        outcome: 'It is skipped deliberately, because otherwise history would be one row per frame per camera.',
        kind: 'stop',
        line: 'db/repositories/event_log.py:34 skip_types',
      },
      {
        when: 'one write fails for a single event',
        outcome: 'That event is retried on the next pass and at most 200 failure samples are kept; a bad row cannot stall the eyes.',
        kind: 'abort',
        line: 'services/persistence.py',
      },
    ],
  },
  {
    id: 'dashboard',
    n: 9,
    title: 'Deliver to this dashboard',
    what: 'The socket hub relays every event with a channel field and each client has its own 200-message queue; the annotated frame goes a separate way through FrameHub to the MJPEG response.',
    file: 'api/live.py:168-234 · routers/live_ws.py · routers/cameras.py:232',
    branches: [
      {
        when: 'a client cannot keep up with its queue',
        outcome: 'It drops its own messages and dropped_events in /health increments. The pipeline never blocks on a browser.',
        kind: 'abort',
        line: 'api/live.py per-client queue 200',
      },
      {
        when: 'the message is a slot event naming a bay this tab shows and the status differs',
        outcome: 'applySlotMessage folds it into the bay list and every count is re-derived from that list.',
        kind: 'continue',
        line: 'frontend/src/lib/slots.ts:40',
      },
      {
        when: 'the message names a bay elsewhere, or repeats what is already shown',
        outcome: 'It is ignored, so a burst of summary frames cannot cause a re-render storm.',
        kind: 'hold',
        line: 'frontend/src/lib/slots.ts:40',
      },
    ],
  },
]

/** The three answers that are easy to lose in a diagram, kept as their own table
 * because each one is a question a viva asks directly. */
export const FLOW_EDGE_CASES: readonly {
  question: string
  answer: string
  evidence: string
}[] = [
  {
    question: 'The camera is unavailable - what does the screen show?',
    answer: 'For a live source, every bay of that camera becomes UNKNOWN and is shown as Not Reported. For a file replay there is no staleness at all: the read either rewinds or ends the run.',
    evidence: 'lot_pipeline.py:188-195 "Never guess FREE for a dead camera"',
  },
  {
    question: 'A vehicle is not detected - is that a problem?',
    answer: 'No. An empty track list is still applied to the engine every frame, and the dwell timers advance. That is exactly how a bay reaches FREE.',
    evidence: 'lot_pipeline.py:217-218',
  },
  {
    question: 'What is UNKNOWN and is it the same as available?',
    answer: 'UNKNOWN means the camera is not telling us. It is never folded into the available count, and the dashboard gives it its own card.',
    evidence: 'frontend/src/lib/slots.ts:13-17',
  },
  {
    question: 'Can a PENDING state reach the browser?',
    answer: 'Over the socket, sometimes as the in-between picture. Through REST, never - GET /slots only returns committed states.',
    evidence: 'api/routers/slots.py:32-38',
  },
]
