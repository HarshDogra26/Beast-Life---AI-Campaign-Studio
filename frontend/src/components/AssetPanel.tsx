import type { Asset, Cost } from '../types'

const TITLES: Record<string, string> = {
  square_1080x1080: '1:1 feed ad',
  vertical_1080x1920: '9:16 story ad',
  video_1080x1920: 'Vertical video',
  master_scene: 'Master scene — shared identity anchor',
  vertical_plate: 'Vertical plate — the video’s copy-free base frame',
}

/** What a user downloads as an ad. The rest are pipeline intermediates. */
const DELIVERABLES = ['square_1080x1080', 'vertical_1080x1920', 'video_1080x1920']

function bytes(n: number): string {
  return n > 1_048_576 ? `${(n / 1_048_576).toFixed(1)} MB` : `${(n / 1024).toFixed(0)} KB`
}

function AssetCard({ asset }: { asset: Asset }) {
  const isVideo = asset.media_type === 'video/mp4'
  const isVertical = asset.height > asset.width
  const gen = asset.generation ?? {}

  return (
    <div className="card">
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
        <h3 style={{ margin: 0 }}>{TITLES[asset.format] ?? asset.format}</h3>
        <span className="badge" style={{ marginLeft: 'auto' }}>
          {asset.width}×{asset.height}
        </span>
      </div>

      <div className={`asset-frame${isVertical ? ' vertical' : ''}`}>
        {isVideo ? (
          <video className="asset-preview" controls preload="metadata" src={asset.preview_url} />
        ) : (
          <img
            className="asset-preview"
            src={asset.preview_url}
            alt={TITLES[asset.format] ?? asset.format}
            loading="lazy"
          />
        )}
      </div>

      <div className="asset-meta">
        <span>{bytes(asset.byte_size)}</span>
        {asset.duration_s != null && <span>{asset.duration_s.toFixed(2)}s</span>}
        {gen.generated_size && (
          <span>
            generated {gen.generated_size.width}×{gen.generated_size.height}
          </span>
        )}
        {gen.operation && <span>{gen.operation}</span>}
        {gen.strategy && <span>{gen.strategy}</span>}
      </div>

      <div style={{ marginTop: 14 }}>
        <a href={asset.download_url} download>
          <button className="small">↓ Download</button>
        </a>
      </div>

      <details>
        <summary>Generation prompt and parameters</summary>
        <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
          Retained verbatim for every asset. <span className="mono">source_asset_id</span>{' '}
          points at the image this one was re-framed from — the mechanism that keeps the
          formats consistent.
        </p>
        <pre>{JSON.stringify(gen, null, 2)}</pre>
      </details>
    </div>
  )
}

export default function AssetPanel({ assets, cost }: { assets: Asset[]; cost: Cost }) {
  const sorted = [...assets].sort(
    (a, b) => DELIVERABLES.indexOf(a.format) - DELIVERABLES.indexOf(b.format),
  )
  const deliverables = sorted.filter((a) => DELIVERABLES.includes(a.format))
  const intermediates = sorted.filter((a) => !DELIVERABLES.includes(a.format))

  return (
    <>
      <div className="grid-2">
        {deliverables.map((a) => (
          <AssetCard key={a.id} asset={a} />
        ))}
      </div>

      {intermediates.length > 0 && (
        <details>
          <summary>Pipeline intermediates ({intermediates.length})</summary>
          <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
            The master scene is generated once, then re-framed into each format through an
            image edit — both ads descend from it, which is what keeps product, palette and
            lighting identical across formats. The plate is the vertical ad before copy is
            composited; the video animates that, so its per-beat text does not
            double-expose over the still’s baked-in headline.
          </p>
          <div className="grid-2" style={{ marginTop: 14 }}>
            {intermediates.map((a) => (
              <AssetCard key={a.id} asset={a} />
            ))}
          </div>
        </details>
      )}

      <h2 className="section-title">Provider usage</h2>

      <div className="card">
        <div className="row" style={{ gap: 12 }}>
          <Metric label="Provider calls" value={String(cost.provider_calls)} />
          <Metric label="Image calls" value={String(cost.image_calls)} />
          <Metric label="Search credits" value={String(cost.search_credits)} />
          <Metric
            label="Est. cost"
            value={
              cost.estimated_cost_usd != null
                ? `$${cost.estimated_cost_usd.toFixed(4)}`
                : 'unknown'
            }
            accent
          />
        </div>

        {(cost.unknown_cost_calls > 0 || cost.unsettled_calls > 0) && (
          <div className="warn-box">
            {cost.unknown_cost_calls > 0 && (
              <div>
                {cost.unknown_cost_calls} call(s) reported no token usage, so their cost is
                recorded as unknown rather than zero.
              </div>
            )}
            {cost.unsettled_calls > 0 && (
              <div>
                <strong>{cost.unsettled_calls} call(s) dispatched but never settled</strong>{' '}
                — possibly billed without a result reaching us.
              </div>
            )}
          </div>
        )}

        <p className="muted" style={{ marginTop: 14, marginBottom: 0 }}>
          {cost.note}
        </p>
      </div>
    </>
  )
}

function Metric({
  label,
  value,
  accent = false,
}: {
  label: string
  value: string
  accent?: boolean
}) {
  return (
    <div
      style={{
        padding: '13px 16px',
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
      <div
        style={{
          fontSize: 21,
          fontWeight: 620,
          marginTop: 4,
          fontVariantNumeric: 'tabular-nums',
          color: accent ? 'var(--accent-soft)' : 'var(--text)',
        }}
      >
        {value}
        {accent && (
          <span className="badge warn" style={{ marginLeft: 8, verticalAlign: 'middle' }}>
            ESTIMATE
          </span>
        )}
      </div>
    </div>
  )
}
