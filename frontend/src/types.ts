export type Role = 'student' | 'admin'

export interface User {
  id: number
  email: string
  full_name: string | null
  role: Role
  is_active: boolean
  created_at?: string
  last_login_at?: string | null
}

export interface AnswerPoint {
  id: number
  text: string
  marks: number
  is_critical: boolean
  from_examiner_feedback: boolean
  rationale: string | null
  accepted_alternatives: string[] | null
}

export interface QuestionPart {
  id: number
  label: string | null
  position: number
  text: string
  marks: number
  preamble: string | null
  answer_points: AnswerPoint[]
}

export interface Figure {
  id: number
  label: string | null
  caption: string | null
  position: number
  image_id: number | null
  wanted_description: string | null
  image_description: string | null
  source_url: string | null
}

export interface ExaminerFeedback {
  examiner_number: number | null
  common_mistakes: string[] | null
  cohort_impression: string[] | null
}

export interface QuestionSummary {
  id: number
  question_type: 'SEQ' | 'VSAQ' | 'OSCE'
  subspecialty: string | null
  topic: string | null
  total_marks: number
  status: string
  source: string
  exam_period: string | null
  original_number: number | null
  model_answer_status: string
  part_count: number
  figure_count: number
  angoff_expected: number | null
  created_at: string
}

export interface QuestionDetail extends QuestionSummary {
  purpose: string | null
  stem: string
  curriculum_standard_raw: string | null
  curriculum_codes: string[] | null
  angoff_rationale: string | null
  generation_meta: Record<string, unknown> | null
  parts: QuestionPart[]
  figures: Figure[]
  examiner_feedback: ExaminerFeedback[]
}

export interface QuestionPage {
  items: QuestionSummary[]
  total: number
  limit: number
  offset: number
}

export interface SourceDocument {
  id: number
  filename: string
  content_type: string
  size_bytes: number
  page_count: number | null
  exam_period: string | null
  document_kind: string | null
  status: string
  status_detail: string | null
  question_count: number
  created_at: string
}

export interface Job {
  id: number
  job_type: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  total_steps: number
  completed_steps: number
  progress: number
  message: string | null
  error: string | null
  result: Record<string, unknown> | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface SettingItem {
  key: string
  label: string
  group: string
  help_text: string
  choices: string[]
  is_secret: boolean
  value: unknown
  is_set: boolean
}

export interface Invite {
  id: number
  code: string
  email: string | null
  role: Role
  note: string | null
  expires_at: string | null
  used_at: string | null
  created_at: string
}

export interface ErrorEntry {
  id: number
  created_at: string
  level: string
  source: string
  message: string
  detail: string | null
  context: Record<string, unknown> | null
}

export interface AdminStats {
  users: number
  documents: number
  questions_total: number
  questions_by_type: Record<string, number>
  questions_by_subspecialty: Record<string, number>
  questions_by_status: Record<string, number>
  with_model_answers: number
  active_jobs: number
  errors_24h: number
  ai_last_30_days: {
    calls: number
    prompt_tokens: number
    completion_tokens: number
    cost_usd: number
  }
}
