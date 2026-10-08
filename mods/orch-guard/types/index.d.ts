export type Role = 'scout' | 'runner' | 'builder' | 'critic'

export type CriticVerdict = 'SHIP' | 'FIX FIRST' | 'RETHINK' | 'unknown'

/** A path (or `bash: <command>` for a shell write) and when it was last changed. */
export type Touched = { path: string; at: number }

/** What the guard has seen this session, against the rules in CLAUDE.md. */
export type GuardLedger = {
  /** Code changes with no passing check started after them (docs excluded). */
  uncheckedEdits: Touched[]
  /** Risky files changed since a critic that saw them answered SHIP. */
  riskyPending: Touched[]
  lastCheck: { at: number; command: string; isPassing: boolean } | null
  critic: { at: number; verdict: CriticVerdict } | null
  /** The person's `/orch-guard waive`, cleared by the next edit. */
  waiver: { at: number; reason: string } | null
  /** Calls the guard refused (or warned about) this session. */
  blocked: number
}

/** A subagent the session spawned, by agentId, so its turn end knows its role. */
export type SpawnedAgent = {
  type: string
  model?: string
  isBackground: boolean
  at: number
  /** For a critic: the risky paths pending when it started, the ones its SHIP clears. */
  reviews?: string[]
}

declare module 'claude-code' {
  interface PluginState {
    'orch-guard': {
      ledger: GuardLedger
      spawned: Record<string, SpawnedAgent>
    }
  }
}
