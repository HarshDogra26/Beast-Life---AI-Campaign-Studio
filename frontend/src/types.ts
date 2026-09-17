export type StageStatus =
  | 'pending' | 'running' | 'completed' | 'failed'
  | 'interrupted' | 'reused' | 'awaiting_input'

export type CampaignStatus =
  | 'draft' | 'researching' | 'awaiting_selection'
  | 'generating' | 'completed' | 'failed' | 'interrupted'

export interface Stage {
  stage: string
  status: StageStatus
  attempt: number
  error: string | null
  error_kind: string | null
  duration_s: number | null
  retryable: boolean
}

export interface Source {
  id: string
  url: string
  title: string
  accessed_at: string
  excerpt: string
  injection_flags: string[]
}

export interface ToolCall {
  tool: string
  arguments: Record<string, unknown>
  latency_ms: number
  ok: boolean
  result_summary: string
  error: string | null
  credits_spent: number
}

export interface AgentStep {
  index: number
  decision_summary: string
  tool_calls: ToolCall[]
}

export interface Angle {
  id: string
  title: string
  audience_insight: string
  insight_source_ids: string[]
  hook: string
  visual_direction: string
  rationale: string
  supporting_source_ids: string[]
}

export interface Research {
  sources: Source[]
  steps: AgentStep[]
  angles: Angle[]
  coverage_gap: string | null
  stop_reason: string | null
  budget: Record<string, number>
  usage: Record<string, number | string | null>
  is_fixture: boolean
}

export interface Asset {
  id: string
  format: string
  width: number
  height: number
  byte_size: number
  media_type: string
  duration_s: number | null
  created_at: string
  preview_url: string
  download_url: string
  generation: Record<string, any>
}

export interface Cost {
  provider_calls: number
  image_calls: number
  search_credits: number
  estimated_cost_usd: number | null
  is_estimate: boolean
  unknown_cost_calls: number
  unsettled_calls: number
  note: string
}

export interface CampaignDetail {
  id: string
  title: string
  status: CampaignStatus
  brief: Record<string, any>
  selected_angle_id: string | null
  provider_mode: string
  size_strategy: string
  image_model: string
  error: string | null
  created_at: string
  updated_at: string
  stages: Stage[]
  research: Research | null
  spec: Record<string, any> | null
  assets: Asset[]
  cost: Cost
  active_job: string | null
}

export interface CampaignSummary {
  id: string
  title: string
  status: CampaignStatus
  product_name: string
  created_at: string
  updated_at: string
  asset_count: number
  provider_mode: string
  size_strategy: string
}

export interface Health {
  status: string
  provider_mode: string
  image_model: string
  image_model_family: string
  size_strategy: string
  ffmpeg: string
  database: string
  worker_epoch: string
  notes: string[]
  failure_injection: string[]
}
