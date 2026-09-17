import type { CampaignDetail, CampaignSummary, Health } from './types'

/** Error carrying the server's structured code, so the UI can react to the cause. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string = 'unknown',
    readonly detail?: unknown,
  ) {
    super(message)
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })

  if (!response.ok) {
    let message = `Request failed (${response.status})`
    let code = 'http_error'
    let detail: unknown
    try {
      const body = await response.json()
      // FastAPI nests our structured errors under `detail` when raised via
      // HTTPException, but returns them flat from our own handlers.
      const payload = body?.detail && typeof body.detail === 'object' ? body.detail : body
      message = payload?.error ?? body?.detail ?? message
      code = payload?.code ?? code
      detail = payload?.detail
    } catch {
      /* non-JSON body: keep the generic message */
    }
    throw new ApiError(message, response.status, code, detail)
  }

  return response.status === 204 ? (undefined as T) : response.json()
}

/** A fresh key per user-initiated action, so a retry replays rather than duplicates. */
export function newIdempotencyKey(): string {
  return crypto.randomUUID()
}

export const api = {
  health: () => request<Health>('/api/health'),

  listCampaigns: () => request<CampaignSummary[]>('/api/campaigns'),

  getCampaign: (id: string) => request<CampaignDetail>(`/api/campaigns/${id}`),

  createCampaign: (brief: Record<string, unknown>, idempotencyKey: string) =>
    request<{ campaign_id: string; job_id: string }>('/api/campaigns', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify({ brief }),
    }),

  selectAngle: (id: string, angleId: string, idempotencyKey: string) =>
    request<{ job_id: string }>(`/api/campaigns/${id}/select`, {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify({ angle_id: angleId }),
    }),

  retryStage: (id: string, stage: string, idempotencyKey: string) =>
    request<{ job_id: string }>(`/api/campaigns/${id}/stages/${stage}/retry`, {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
    }),

  uploadReference: async (file: File) => {
    const form = new FormData()
    form.append('file', file)
    const response = await fetch('/api/campaigns/uploads', { method: 'POST', body: form })
    if (!response.ok) {
      const body = await response.json().catch(() => ({}))
      const payload = body?.detail ?? body
      throw new ApiError(payload?.error ?? 'Upload failed', response.status, payload?.code)
    }
    return response.json() as Promise<{ reference_image_id: string; width: number; height: number }>
  },

  ledger: (id: string) => request<any>(`/api/campaigns/${id}/ledger`),
}

/**
 * Subscribe to live stage updates.
 *
 * The stream is a convenience, not the source of truth: every payload is a full
 * snapshot, and the caller can always recover by re-fetching the campaign. That
 * is why a dropped connection needs no special handling beyond closing.
 */
export function subscribeToCampaign(
  id: string,
  onSnapshot: () => void,
  onDone?: () => void,
): () => void {
  const source = new EventSource(`/api/campaigns/${id}/events`)
  source.addEventListener('snapshot', () => onSnapshot())
  source.addEventListener('done', () => {
    onSnapshot()
    onDone?.()
    source.close()
  })
  source.onerror = () => {
    // EventSource retries on its own; a persistent failure falls back to polling.
    if (source.readyState === EventSource.CLOSED) onDone?.()
  }
  return () => source.close()
}
