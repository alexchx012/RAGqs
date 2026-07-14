export interface KnowledgeSpace {
  space_id: string;
  name: string;
  spaceId?: string;
  id?: string;
}

export interface DocumentRecord {
  document_id: string;
  file_name: string;
  status: string;
  indexed_chunks: number;
  total_chunks: number;
}

export interface IndexJob {
  job_id: string;
  status: string;
  document_id?: string;
  source_path?: string;
}

export interface AuditRecord {
  id?: string;
  traceId?: string;
  question?: string;
  sources: Array<{ content: string; score: number }>;
  createdAt?: string;
}

export interface ChatMessage {
  type: 'user' | 'assistant';
  content: string;
  role?: string;
}

export interface ChatSession {
  id?: string;
  session_id?: string;
  title: string;
  messageCount?: number;
  message_count?: number;
  updatedAt?: string;
  updated_at?: string;
  lastMessage?: string;
  last_message?: string;
}

export interface ApiResponse<T = unknown> {
  code: number;
  message?: string;
  detail?: string;
  data?: T;
  history?: ChatMessage[];
}

export interface ChatData {
  success?: boolean;
  answer?: string;
  errorMessage?: string;
}

export interface KnowledgeSpacesData {
  spaces?: KnowledgeSpace[];
}

export interface DocumentsData {
  documents?: DocumentRecord[];
}

export interface IndexJobsData {
  jobs?: IndexJob[];
}

export interface AuditData {
  audits?: AuditRecord[];
}

export interface SessionsData {
  sessions?: ChatSession[];
}

export type PanelState<T> =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'empty' }
  | { status: 'ready'; items: T[] };
