import { useState } from 'react'
import type { Research } from '../types'

export default function ResearchReview({
  research,
  selectedAngleId,
  onSelect,
  busy,
}: {
  research: Research
  selectedAngleId: string | null
  onSelect: (angleId: string) => void
  busy: boolean
}) {
  const [choice, setChoice] = useState<string | null>(selectedAngleId)
  const locked = selectedAngleId !== null
  const sourcesById = Object.fromEntries(research.sources.map((s) => [s.id, s]))
  const credits = research.steps
    .flatMap((s) => s.tool_calls)
    .reduce((n, c) => n + c.credits_spent, 0)

  return (
    <>
      {research.is_fixture && (
        <div className="warn-box">
          <strong>Fixture research.</strong> These sources were replayed from recorded
          fixtures for offline review. They are <em>not</em> live browsing results.
        </div>
      )}

      {research.coverage_gap && (
        <div className="warn-box">
          <strong>Evidence gap.</strong> {research.coverage_gap}
        </div>
      )}

      <h2 className="section-title">Agent trace</h2>

      <div className="card">
        <div className="row" style={{ gap: 10, marginBottom: 18 }}>
          <Stat
            label="Searches"
            value={`${research.usage.search_calls ?? 0}/${research.budget.max_search_calls}`}
          />
          <Stat
            label="Pages read"
            value={`${research.usage.fetch_calls ?? 0}/${research.budget.max_fetch_calls}`}
          />
          <Stat
            label="Elapsed"
            value={`${research.usage.elapsed_s ?? 0}s / ${research.budget.wall_clock_s}s`}
          />
          <Stat label="Credits" value={String(credits)} />
        </div>

        {research.stop_reason && (
          <p className="muted" style={{ marginTop: 0 }}>
            Stopped because: <strong>{research.stop_reason}</strong>
          </p>
        )}

        {research.steps.map((step) => (
          <div key={step.index} className="trace-step">
            <div className="decision">
              <span className="turn-label">turn {step.index}</span>
              {step.decision_summary}
            </div>
            {step.tool_calls.map((call, i) => (
              <div key={i} className="toolcall">
                <div>
                  <span className="tool-name">{call.tool}</span>{' '}
                  <span className="args">{JSON.stringify(call.arguments)}</span>
                </div>
                <div className="muted" style={{ marginTop: 5, fontSize: 12 }}>
                  <span style={{ color: call.ok ? 'var(--ok)' : 'var(--err)' }}>
                    {call.ok ? '✓' : '✗'}
                  </span>{' '}
                  {call.result_summary} · {call.latency_ms}ms
                  {call.credits_spent > 0 && ` · ${call.credits_spent} credit`}
                </div>
                {call.error && (
                  <div style={{ color: 'var(--err)', marginTop: 4 }}>{call.error}</div>
                )}
              </div>
            ))}
          </div>
        ))}
      </div>

      <h2 className="section-title">Sources · {research.sources.length}</h2>

      <div className="card">
        {research.sources.map((s) => (
          <div
            key={s.id}
            className={`source-card${s.injection_flags.length ? ' flagged' : ''}`}
          >
            <div className="src-title">
              <span className="src-id">{s.id}</span>
              <a href={s.url} target="_blank" rel="noreferrer noopener">
                {s.title}
              </a>
            </div>
            <div className="mono" style={{ color: 'var(--text-faint)', marginTop: 3 }}>
              {s.url}
            </div>
            <div className="muted" style={{ fontSize: 11.5, marginTop: 2 }}>
              accessed {new Date(s.accessed_at).toLocaleString()}
            </div>

            {s.injection_flags.length > 0 && (
              <div className="warn-box" style={{ marginBottom: 0 }}>
                <span className="badge warn">prompt injection detected</span>{' '}
                <span className="mono">{s.injection_flags.join(', ')}</span>
                <div className="muted" style={{ marginTop: 6 }}>
                  This page tried to issue instructions. It is quoted as evidence only —
                  the agent has just two read-only tools, and angle synthesis runs with no
                  tools bound at all.
                </div>
              </div>
            )}

            <div className="src-excerpt">{s.excerpt}</div>
          </div>
        ))}
      </div>

      <h2 className="section-title">Choose an angle</h2>

      <div className="card">
        <div className="legend">
          <span>
            <i style={{ background: 'var(--ok)' }} />
            Sourced — what a cited page actually says
          </span>
          <span>
            <i style={{ background: 'var(--violet)' }} />
            Interpreted — creative judgement, no evidence claim
          </span>
        </div>

        <div className="grid-2" style={{ marginTop: 18 }}>
          {research.angles.map((a) => (
            <button
              key={a.id}
              type="button"
              className={`angle-card${choice === a.id ? ' selected' : ''}${locked ? ' locked' : ''}`}
              onClick={() => !locked && setChoice(a.id)}
              disabled={locked}
              style={{ textAlign: 'left', font: 'inherit', color: 'inherit' }}
            >
              <span className="badge">{a.id}</span>
              {selectedAngleId === a.id && (
                <span className="badge ok" style={{ marginLeft: 7 }}>
                  selected
                </span>
              )}

              <div className="hook">{a.hook}</div>
              <div className="muted" style={{ fontSize: 12.5 }}>
                {a.title}
              </div>

              <div className="angle-field sourced">
                <div className="label">Audience insight</div>
                <p>{a.audience_insight}</p>
                <div className="cite">
                  {a.insight_source_ids.map((id) => (
                    <a
                      key={id}
                      href={sourcesById[id]?.url}
                      target="_blank"
                      rel="noreferrer noopener"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {id}
                    </a>
                  ))}
                </div>
              </div>

              <div className="angle-field interpreted">
                <div className="label">Visual direction</div>
                <p>{a.visual_direction}</p>
              </div>

              <div className="angle-field interpreted">
                <div className="label">Rationale</div>
                <p>{a.rationale}</p>
              </div>
            </button>
          ))}
        </div>

        {locked ? (
          <p className="muted" style={{ marginTop: 18 }}>
            Angle <strong>{selectedAngleId}</strong> selected — generation has begun.
          </p>
        ) : (
          <button
            className="primary"
            style={{ marginTop: 20 }}
            disabled={!choice || busy}
            onClick={() => choice && onSelect(choice)}
          >
            {busy ? 'Starting generation…' : `Generate campaign from ${choice ?? '…'}`}
          </button>
        )}
      </div>
    </>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div
      style={{
        padding: '11px 14px',
        borderRadius: 'var(--radius-sm)',
        background: 'var(--bg)',
        border: '1px solid var(--border)',
      }}
    >
      <div
        style={{
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: '0.1em',
          textTransform: 'uppercase',
          color: 'var(--text-faint)',
        }}
      >
        {label}
      </div>
      <div style={{ fontSize: 17, fontWeight: 600, marginTop: 3, fontVariantNumeric: 'tabular-nums' }}>
        {value}
      </div>
    </div>
  )
}
