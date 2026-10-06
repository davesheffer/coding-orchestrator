export type AgentStatusMark = 'running' | 'done' | 'aborted' | 'error'

/** One subagent the session spawned, from `agent.spawn` to its `turn.complete`. */
export type AgentRun = {
  id: string
  type: string
  model: string
  description: string
  isBackground: boolean
  startedAt: number
  endedAt?: number
  status: AgentStatusMark
}

export type Confidence = 'high' | 'medium' | 'low' | 'missing'

/** The RESULT / EVIDENCE / CONFIDENCE / UNVERIFIED card a role agent hands back. */
export type ReportCard = {
  agentId: string
  type: string
  model: string
  at: number
  durationMs: number
  confidence: Confidence
  missing: string[]
  unverified: number
  exitCodes: number[]
  isWeak: boolean
  result: string
}

export type Zone = 'green' | 'amber' | 'red' | 'unknown'

/** The relay gauge: live context tokens against relay/config.json thresholds. */
export type Gauge = {
  tokens?: number
  window: number
  percent?: number
  zone: Zone
  soft: number
  hard: number
  usd?: number
}

/** One Jev decision line, summarized for display. */
export type JevEntry = {
  ts: string
  feature: string
  summary: string
  isAlert: boolean
}

export type JevStatus = {
  /** null while no recent line says either way. */
  isOnline: boolean | null
  lastError?: string
  spendToday: number
  deniesToday: number
  weakToday: number
  recent: JevEntry[]
  note?: string
}

export type HunchLevel = 'info' | 'warn' | 'alert'

export type HunchCallStatus = 'running' | 'ok' | 'error' | 'denied'

/** One Hunch call: an `mcp__hunch__*` tool, or the hunch CLI run through Bash (`task verify`). */
export type HunchCall = {
  /** The tool_use_id. */
  id: string
  /** `context`, `check_constraints`, `verify`, `cli update`, ... */
  name: string
  target: string
  taskId?: string
  /** `main`, or the subagent role that made the call. */
  role: string
  startedAt: number
  durationMs?: number
  status: HunchCallStatus
  summary: string
  level: HunchLevel
}

/** Constraint lines `hunch_check_constraints` returned. */
export type HunchConstraint = {
  id: string
  severity: string
  statement: string
}

export type HunchLog = {
  /** The newest htask_ id seen in a call's arguments or result. */
  taskId?: string
  calls: HunchCall[]
  total: number
  /** Constraint ids already toasted this session, so a `**` invariant toasts once. */
  seenConstraints: string[]
}

/** One open Claude Code session, from its `~/.claude/sessions/<pid>.json` registry file. */
export type SessionRow = {
  pid: number
  sessionId: string
  name: string
  cwd: string
  /** `claude-vscode` (a VS Code tab the bridge can switch to) or `cli` (a terminal). */
  entrypoint: string
  /** `idle` or `busy`, as the session last wrote it; anything else is `unknown`. */
  status: 'idle' | 'busy' | 'unknown'
  updatedAt: number
}

declare module 'claude-code' {
  interface PluginState {
    'mission-control': {
      agents: AgentRun[]
      reports: ReportCard[]
      gauge: Gauge | null
      jev: JevStatus | null
      hunch: HunchLog
      isBandHidden: boolean
      tick: number
      sessions: SessionRow[]
      /** When the session registry was last polled, rounded down to 30 s so the pane's ages redraw. */
      sessionsCheckedAt: number
    }
  }
}
