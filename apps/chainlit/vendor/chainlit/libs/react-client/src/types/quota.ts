export interface IQuotaSummary {
  used_tokens: number;
  reserved_tokens?: number;
  limit_tokens: number;
  remaining_tokens: number;
  policy_version?: number;
  updated_at?: string;
  window_start?: string | null;
  window_end?: string | null;
  status?: 'ok' | 'blocked' | 'window_closed' | 'missing_policy';
  message?: string | null;
}

export interface ITokenUsageMetadata {
  request_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  session_used_tokens: number;
  session_limit_tokens: number;
  user_used_tokens: number;
  user_limit_tokens: number;
  user_policy_version?: number;
  updated_at?: string;
}
