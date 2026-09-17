import type { Stage } from '../types'

const LABELS: Record<string, string> = {
  validate_brief: 'Validate brief',
  research: 'Research the web',
  synthesize_angles: 'Synthesise 3 angles',
  await_selection: 'Await your selection',
  build_spec: 'Build creative spec',
  master_scene: 'Master scene',
  render_square: 'Render 1080×1080',
  render_vertical: 'Render 1080×1920',
  render_video: 'Render video',
}

/** Short note on why a stage exists, shown inline so the pipeline explains itself. */
const HINTS: Record<string, string> = {
  research: 'bounded tool loop over MCP',
  master_scene: 'generated once — the shared identity anchor',
  render_square: 're-frame of the master scene',
  render_vertical: 're-frame of the master scene',
  render_video: 'animates the approved 1080×1920 plate',
}

/** These two depend only on the master scene, so they run in one superstep. */
const CONCURRENT = new Set(['render_square', 'render_vertical'])

const STATUS_TEXT: Record<string, string> = {
  reused: 'reused · no provider call',
  awaiting_input: 'waiting for you',
  interrupted: 'interrupted',
  running: 'running',
  pending: 'queued',
}

export default function StageTimeline({
  stages,
  onRetry,
  retrying,
}: {
  stages: Stage[]
  onRetry: (stage: string) => void
  retrying: string | null
}) {
  const done = stages.filter((s) => s.status === 'completed' || s.status === 'reused').length
  const failed = stages.filter((s) => s.error)

  return (
    <div className="card">
      <div style={{ display: 'flex', alignItems: 'center', marginBottom: 14 }}>
        <h2 style={{ margin: 0 }}>Workflow</h2>
        <span className="badge" style={{ marginLeft: 'auto' }}>
          {done}/{stages.length} complete
        </span>
      </div>

      <div className="stages">
        {stages.map((s) => (
          <div key={s.stage} className={`stage ${s.status}`}>
            <span className="dot" />
            <span className="name">
              {LABELS[s.stage] ?? s.stage}
              {CONCURRENT.has(s.stage) && <span className="parallel-tag">∥ PARALLEL</span>}
              {HINTS[s.stage] && (
                <span
                  className="muted"
                  style={{ marginLeft: 9, fontSize: 11.5, fontWeight: 400 }}
                >
                  {HINTS[s.stage]}
                </span>
              )}
            </span>
            <span className="meta">
              {STATUS_TEXT[s.status] ?? s.status}
              {s.duration_s != null && ` · ${s.duration_s.toFixed(1)}s`}
              {s.attempt > 1 && ` · try ${s.attempt}`}
            </span>
            {s.retryable && (
              <button
                className="small"
                onClick={() => onRetry(s.stage)}
                disabled={retrying !== null}
              >
                {retrying === s.stage ? 'Retrying…' : 'Retry'}
              </button>
            )}
          </div>
        ))}
      </div>

      {failed.length > 0 && (
        <div className="error-box">
          {failed.map((s) => (
            <div key={s.stage} style={{ marginBottom: 8 }}>
              <strong>{LABELS[s.stage] ?? s.stage}</strong>
              {s.error_kind && (
                <span className="badge err" style={{ marginLeft: 8 }}>
                  {s.error_kind}
                </span>
              )}
              <div className="mono" style={{ marginTop: 5, color: 'var(--text-dim)' }}>
                {s.error}
              </div>
            </div>
          ))}
          <p className="muted" style={{ margin: '10px 0 0' }}>
            Retrying re-runs only the failed stage. Completed stages are reused from their
            stored output and cost nothing.
          </p>
        </div>
      )}
    </div>
  )
}
