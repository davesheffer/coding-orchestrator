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

declare module 'claude-code' {
  interface PluginState {
    'mission-control': {
      agents: AgentRun[]
      reports: ReportCard[]
      gauge: Gauge | null
      jev: JevStatus | null
      isBandHidden: boolean
      tick: number
    }
  }
}
