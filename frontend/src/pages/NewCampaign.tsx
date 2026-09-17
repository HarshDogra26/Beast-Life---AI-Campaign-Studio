import { useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ApiError, api, newIdempotencyKey } from '../api'

const OBJECTIVES = [
  ['introduce_product', 'Introduce the product'],
  ['build_awareness', 'Build awareness'],
  ['drive_consideration', 'Drive consideration'],
  ['drive_traffic', 'Drive traffic'],
  ['retain_customers', 'Retain customers'],
] as const

const EXAMPLE = {
  product_name: 'Meridian Daily Protein',
  product_description:
    'A powdered protein supplement sold in a 1kg matte charcoal tub with a wide screw lid. ' +
    'Each serving is a single scoop mixed with water or milk. It is unflavoured and intended ' +
    'to be taken after training or with a meal.',
  target_audience:
    'Adults aged 25-40 who train at a commercial gym three or more times a week and fit ' +
    'training around full-time work.',
  campaign_objective: 'introduce_product',
  tone: 'practical, energetic, understated',
  call_to_action: 'Explore the range',
}

export default function NewCampaign() {
  const navigate = useNavigate()
  const [form, setForm] = useState(EXAMPLE)
  const [claims, setClaims] = useState('')
  const [referenceId, setReferenceId] = useState<string | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  // One key per form instance. Re-submitting the same filled-in form replays the
  // original response instead of creating a second campaign.
  const idempotencyKey = useRef(newIdempotencyKey())

  const upload = useMutation({
    mutationFn: (file: File) => api.uploadReference(file),
    onSuccess: (data) => {
      setReferenceId(data.reference_image_id)
      setUploadError(null)
    },
    onError: (error: ApiError) => setUploadError(error.message),
  })

  const create = useMutation({
    mutationFn: () =>
      api.createCampaign(
        {
          ...form,
          verified_claims: claims
            .split('\n')
            .map((line) => line.trim())
            .filter(Boolean)
            .map((text) => ({ text })),
          reference_image_id: referenceId,
        },
        idempotencyKey.current,
      ),
    onSuccess: (data) => navigate(`/campaigns/${data.campaign_id}`),
  })

  const set = (key: keyof typeof form) => (e: React.ChangeEvent<any>) =>
    setForm({ ...form, [key]: e.target.value })

  return (
    <div>
      <div
        style={{
          padding: '10px 0 32px',
          maxWidth: 680,
        }}
      >
        <h2
          style={{
            fontSize: 38,
            lineHeight: 1.1,
            letterSpacing: '-0.035em',
            fontWeight: 680,
            margin: '0 0 14px',
          }}
        >
          One brief in.{' '}
          <span
            style={{
              background: 'linear-gradient(96deg, var(--accent-soft), var(--violet))',
              WebkitBackgroundClip: 'text',
              backgroundClip: 'text',
              color: 'transparent',
            }}
          >
            A whole campaign out.
          </span>
        </h2>
        <p className="muted" style={{ fontSize: 15.5, lineHeight: 1.6 }}>
          A research agent searches the live web within a bounded tool budget, proposes
          three source-backed angles, and turns the one you pick into a 1080×1080 ad, a
          1080×1920 ad and a short vertical video — all from a single shared specification.
        </p>
      </div>

      <div className="card">
        <h2>Product brief</h2>
        <p className="muted">
          Only what you enter here is treated as product truth. The generator will not
          invent benefits, certifications, discounts or performance figures — copy that
          does is rejected by a validator before any asset is made.
        </p>

        <label htmlFor="pn">Product name</label>
        <input id="pn" value={form.product_name} onChange={set('product_name')} />

        <label htmlFor="pd">Factual product description</label>
        <textarea id="pd" rows={4} value={form.product_description} onChange={set('product_description')} />

        <label htmlFor="ta">Target audience</label>
        <textarea id="ta" rows={2} value={form.target_audience} onChange={set('target_audience')} />

        <div className="row">
          <div>
            <label htmlFor="co">Campaign objective</label>
            <select id="co" value={form.campaign_objective} onChange={set('campaign_objective')}>
              {OBJECTIVES.map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="tone">Tone</label>
            <input id="tone" value={form.tone} onChange={set('tone')} />
          </div>
          <div>
            <label htmlFor="cta">Call to action</label>
            <input id="cta" value={form.call_to_action} onChange={set('call_to_action')} />
          </div>
        </div>

        <label htmlFor="vc">Verified claims (optional, one per line)</label>
        <textarea
          id="vc"
          rows={2}
          value={claims}
          onChange={(e) => setClaims(e.target.value)}
          placeholder="Each tub contains 1kg of powder."
        />

        <label htmlFor="ref">Reference packshot (optional, PNG/JPEG/WebP, max 5 MB)</label>
        <input
          id="ref"
          ref={fileRef}
          type="file"
          accept="image/png,image/jpeg,image/webp"
          onChange={(e) => {
            const file = e.target.files?.[0]
            if (file) upload.mutate(file)
          }}
        />
        {upload.isPending && <p className="muted">Uploading…</p>}
        {referenceId && (
          <p className="muted">
            <span className="badge ok">attached</span>{' '}
            The product identity will be anchored to this image via an image edit with
            high input fidelity, rather than to a text description.
          </p>
        )}
        {uploadError && <div className="error-box">{uploadError}</div>}
      </div>

      {create.isError && (
        <div className="error-box">
          <strong>Could not start the campaign.</strong>{' '}
          {(create.error as ApiError).message}
          {(create.error as ApiError).detail ? (
            <pre>{JSON.stringify((create.error as ApiError).detail, null, 2)}</pre>
          ) : null}
        </div>
      )}

      <button
        className="primary"
        onClick={() => create.mutate()}
        disabled={create.isPending || create.isSuccess}
      >
        {create.isPending ? 'Starting research…' : 'Research creative angles'}
      </button>
      <p className="muted" style={{ marginTop: 10 }}>
        The research agent will search the web, read selected pages within a bounded tool
        budget, and propose three angles for you to choose between.
      </p>
    </div>
  )
}
