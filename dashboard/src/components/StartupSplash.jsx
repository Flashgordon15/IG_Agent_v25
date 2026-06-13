import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'

const POLL_MS = 2000
const WARM_STEP_MS = 5000
const WARM_TOTAL_MS = 25000

const WARM_MILESTONES = [
  { pct: 20, label: 'Verifying Broker Session Handshake...' },
  { pct: 40, label: 'Auditing Database Integrity...' },
  { pct: 60, label: 'Arming Trading Gate Arrays...' },
  { pct: 80, label: 'Indexing Learning Plane Records...' },
  { pct: 100, label: 'System Operational Baseline Ready.' },
]

function isBackendBootComplete(boot) {
  if (!boot || typeof boot !== 'object') return false
  const pct = Number(boot.percent)
  return pct === 100 && boot.stage === 'ready' && boot.ready === true
}

function warmMilestoneForElapsed(elapsedMs) {
  const idx = Math.min(
    WARM_MILESTONES.length - 1,
    Math.floor(elapsedMs / WARM_STEP_MS),
  )
  return WARM_MILESTONES[idx]
}

/**
 * Stage 2 — progress bar.
 * COLD: 1:1 binding to /api/startup/status boot_metrics (real multi-minute boot).
 * WARM: mandatory 25s integrity sequence when backend is already at 100% ready.
 */
export default function StartupSplash({ onComplete }) {
  const [percent, setPercent] = useState(0)
  const [label, setLabel] = useState('Detecting system state…')
  const [error, setError] = useState(null)
  const [mode, setMode] = useState(null) // null | 'cold' | 'warm'
  const completedRef = useRef(false)
  const warmStartRef = useRef(null)

  // Detect warm vs cold on first startup status read.
  useEffect(() => {
    let cancelled = false

    const detect = async () => {
      try {
        const status = await api.getStartupStatus()
        if (cancelled || completedRef.current) return
        const boot = status?.boot_metrics || {}
        if (boot.error) {
          setError(boot.error)
          return
        }
        if (isBackendBootComplete(boot)) {
          setMode('warm')
          warmStartRef.current = Date.now()
          const first = WARM_MILESTONES[0]
          setPercent(first.pct)
          setLabel(first.label)
        } else {
          setMode('cold')
          const pct = Number(boot.percent) || 0
          setPercent(Math.min(100, Math.max(0, pct)))
          setLabel(boot.label || 'Broker Handshake')
        }
      } catch {
        if (!cancelled) {
          setError('Cannot reach agent API — is the server running on :8080?')
        }
      }
    }

    detect()
    return () => {
      cancelled = true
    }
  }, [])

  // Warm path: 5 seconds per milestone, 25 seconds total.
  useEffect(() => {
    if (mode !== 'warm' || error) return undefined

    let cancelled = false
    let timer = null

    const tick = () => {
      if (cancelled || completedRef.current) return
      const start = warmStartRef.current ?? Date.now()
      warmStartRef.current = start
      const elapsed = Date.now() - start
      const frame = warmMilestoneForElapsed(elapsed)
      setPercent(frame.pct)
      setLabel(frame.label)

      if (elapsed >= WARM_TOTAL_MS) {
        completedRef.current = true
        setPercent(100)
        setLabel(WARM_MILESTONES[WARM_MILESTONES.length - 1].label)
        onComplete?.()
        return
      }
      timer = window.setTimeout(tick, 200)
    }

    tick()
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [mode, error, onComplete])

  // Cold path: poll real backend boot_metrics until ready.
  useEffect(() => {
    if (mode !== 'cold' || error) return undefined

    let cancelled = false
    let timer = null

    const poll = async () => {
      try {
        const status = await api.getStartupStatus()
        if (cancelled) return

        const boot = status?.boot_metrics || {}
        const pct = Number(boot.percent)
        setPercent(Number.isFinite(pct) ? Math.min(100, Math.max(0, pct)) : 0)
        setLabel(boot.label || 'Starting up…')

        if (boot.error) {
          setError(boot.error)
          return
        }

        if (isBackendBootComplete(boot)) {
          if (!completedRef.current) {
            completedRef.current = true
            onComplete?.()
          }
          return
        }
      } catch {
        if (!cancelled) {
          setError('Cannot reach agent API — is the server running on :8080?')
        }
      }

      if (!cancelled && !completedRef.current) {
        timer = window.setTimeout(poll, POLL_MS)
      }
    }

    poll()
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [mode, error, onComplete])

  return (
    <div className="startup-splash" role="status" aria-live="polite">
      <div className="startup-splash__card">
        <div className="startup-splash__brand">
          <span className="startup-splash__logo">IG</span>
          <div>
            <p className="startup-splash__stage">Stage 2 of 3</p>
            <h1 className="startup-splash__title">IG Agent</h1>
            <p className="startup-splash__version">v29.1</p>
          </div>
        </div>

        <p className="startup-splash__label">{label}</p>

        <div className="startup-splash__track" aria-hidden="true">
          <div
            className="startup-splash__bar"
            style={{
              width: `${percent}%`,
              transition: 'width 0.4s ease-out',
            }}
          />
        </div>

        <p className="startup-splash__pct">{percent}%</p>

        {mode === 'warm' && (
          <p className="startup-splash__hint">System integrity validation (warm session)</p>
        )}
        {mode === 'cold' && (
          <p className="startup-splash__hint">Live initialization — this may take several minutes</p>
        )}

        {error && (
          <p className="startup-splash__error" role="alert">{error}</p>
        )}
      </div>
    </div>
  )
}
