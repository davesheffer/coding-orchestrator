export type Role = 'scout' | 'runner' | 'builder' | 'critic'

export type CriticVerdict = 'SHIP' | 'FIX FIRST' | 'RETHINK' | 'unknown'

/** What the guard has seen this session, against the rules in CLAUDE.md. */
export type GuardLedger = {
  /** Code files edited since the last passing check (docs excluded). */
  uncheckedEdits: string[]
  /** Risky files edited since the last critic SHIP. */
  riskyPending: string[]
  lastCheck: { at: number; command: string; isPassing: boolean } | null
  critic: { at: number; verdict: CriticVerdict } | null
  /** The person's `/orch-guard waive`, cleared by the next edit. */
  waiver: { at: number; reason: string } | null
  /** Calls the guard refused (or warned about) this session. */
  blocked: number
}

/** A subagent the session spawned, by agentId, so its turn end knows its role. */
export type SpawnedAgent = { type: string; model?: string; isBackground: boolean }

declare module 'claude-code' {
  interface PluginState {
    'orch-guard': {
      ledger: GuardLedger
      spawned: Record<string, SpawnedAgent>
    }
  }
}
