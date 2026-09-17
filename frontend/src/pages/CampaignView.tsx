import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import { ApiError, api, newIdempotencyKey, subscribeToCampaign } from '../api'
import StageTimeline from '../components/StageTimeline'
import ResearchReview from '../components/ResearchReview'
import AssetPanel from '../components/AssetPanel'

const ACTIVE = new Set(['researching', 'generating', 'draft'])

export default function CampaignView() {
  const { id = '' } = useParams()
  const queryClient = useQueryClient()
  const [retrying, setRetrying] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const { data: campaign, isLoading } = useQuery({
    queryKey: ['campaign', id],
    queryFn: () => api.getCampaign(id),
    // Polling is the fallback; SSE below is the fast path. Both read the same
    // server state, so whichever arrives first is equally correct.
    refetchInterval: (query) =>
      ACTIVE.has(query.state.data?.status ?? '') ? 3000 : false,
  })

  useEffect(() => {
    if (!id) return
    return subscribeToCampaign(id, () => {
      queryClient.invalidateQueries({ queryKey: ['campaign', id] })
    })
  }, [id, queryClient])

  const select = useMutation({
    mutationFn: (angleId: string) => api.selectAngle(id, angleId, newIdempotencyKey()),
    onSuccess: () => {
      setActionError(null)
      queryClient.invalidateQueries({ queryKey: ['campaign', id] })
    },
    onError: (e: ApiError) => setActionError(e.message),
  })

  const retry = useMutation({
    mutationFn: (stage: string) => api.retryStage(id, stage, newIdempotencyKey()),
    onMutate: (stage) => setRetrying(stage),
    onSettled: () => {
      setRetrying(null)
      queryClient.invalidateQueries({ queryKey: ['campaign', id] })
    },
    onError: (e: ApiError) => setActionError(e.message),
  })

  if (isLoading) {
    return (
      <div className="card">
        <div className="skeleton skeleton-line" style={{ width: '38%', height: 19 }} />
        <div className="skeleton skeleton-line" style={{ width: '62%' }} />
        <div className="skeleton skeleton-line" style={{ width: '48%' }} />
        <div className="skeleton" style={{ height: 150, marginTop: 18 }} />
      </div>
    )
  }
  if (!campaign) return <div className="error-box">Campaign not found.</div>

  const working = ACTIVE.has(campaign.status)
  const deliverables = campaign.assets.filter((a) => a.format !== 'master_scene')

  return (
    <div>
      <div className="card">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div>
            <h2 style={{ marginBottom: 4, fontSize: 21, letterSpacing: '-0.02em' }}>
              {campaign.title}
            </h2>
            <div className="mono" style={{ color: 'var(--text-faint)' }}>
              {campaign.id}
            </div>
          </div>
          <span
            className={`badge ${
              campaign.status === 'completed' ? 'ok'
              : campaign.status === 'failed' ? 'err'
              : campaign.status === 'interrupted' ? 'warn' : ''
            }`}
            style={{ marginLeft: 'auto' }}
          >
            {campaign.status.replace('_', ' ')}
          </span>
        </div>
        <p className="muted" style={{ marginTop: 10 }}>
          {campaign.provider_mode === 'fixture' ? 'Fixture providers' : 'Live providers'} ·{' '}
          image model <span className="mono">{campaign.image_model}</span> · size strategy{' '}
          <span className="mono">{campaign.size_strategy}</span>
          {campaign.size_strategy === 'bands' &&
            ' (this deployment does not accept custom sizes, so 9:16 is reached by extending the canvas)'}
        </p>
        {campaign.error && <div className="error-box">{campaign.error}</div>}
        {campaign.status === 'interrupted' && (
          <div className="warn-box">
            This campaign was interrupted by a server restart. The affected stage is
            marked below and can be retried; completed work was preserved.
          </div>
        )}
      </div>

      {actionError && <div className="error-box">{actionError}</div>}

      <StageTimeline
        stages={campaign.stages}
        onRetry={(stage) => retry.mutate(stage)}
        retrying={retrying}
      />

      {working && (
        <div className="info-box">
          <strong>Working…</strong> this view updates live over SSE. Refreshing the page or
          restarting the server loses nothing — every stage result is on disk.
        </div>
      )}

      {campaign.research && (
        <ResearchReview
          research={campaign.research}
          selectedAngleId={campaign.selected_angle_id}
          onSelect={(angleId) => select.mutate(angleId)}
          busy={select.isPending}
        />
      )}

      {campaign.spec && (
        <div className="card">
          <h2>Creative specification</h2>
          <p className="muted">
            Version {campaign.spec.version} ·{' '}
            <span className="mono">{campaign.spec.spec_id}</span> · the single shared input
            to both image ads and the video.
          </p>
          <table>
            <tbody>
              <tr><th>Headline</th><td>{campaign.spec.headline}</td></tr>
              {campaign.spec.subhead && <tr><th>Subhead</th><td>{campaign.spec.subhead}</td></tr>}
              <tr><th>CTA</th><td>{campaign.spec.cta_text}</td></tr>
              <tr><th>Scene</th><td>{campaign.spec.scene?.setting}</td></tr>
              <tr>
                <th>Palette</th>
                <td>
                  {['primary', 'secondary', 'accent', 'background'].map((k) => (
                    <span
                      key={k}
                      title={`${k}: ${campaign.spec!.palette[k]}`}
                      style={{
                        display: 'inline-block', width: 22, height: 22, borderRadius: 4,
                        marginRight: 6, verticalAlign: 'middle',
                        background: campaign.spec!.palette[k],
                        border: '1px solid var(--border)',
                      }}
                    />
                  ))}
                </td>
              </tr>
              <tr>
                <th>Video outline</th>
                <td>
                  {campaign.spec.video?.beats
                    ?.map((b: any) => `${b.label} ${b.seconds}s`)
                    .join(' → ')}
                </td>
              </tr>
            </tbody>
          </table>
          <details style={{ marginTop: 10 }}>
            <summary>Full specification JSON</summary>
            <pre>{JSON.stringify(campaign.spec, null, 2)}</pre>
          </details>
        </div>
      )}

      {deliverables.length > 0 && (
        <>
          <h2 className="section-title">Assets</h2>
          <AssetPanel assets={campaign.assets} cost={campaign.cost} />
        </>
      )}
    </div>
  )
}
