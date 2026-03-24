import { IQuotaSummary, ITokenUsageMetadata } from '@chainlit/react-client';

export const formatTokenCount = (value?: number) => {
  return new Intl.NumberFormat('en-US').format(value || 0);
};

export const getQuotaBlockedReason = (
  threadQuota?: IQuotaSummary,
  userQuota?: IQuotaSummary
) => {
  if (threadQuota && threadQuota.remaining_tokens <= 0) {
    return '当前对话额度已用尽';
  }
  if (userQuota && userQuota.remaining_tokens <= 0) {
    return '用户总额度已用尽';
  }
  return undefined;
};

export const hasTokenUsageMetadata = (
  tokenUsage?: Partial<ITokenUsageMetadata>
): tokenUsage is ITokenUsageMetadata => {
  return (
    typeof tokenUsage?.request_tokens === 'number' &&
    typeof tokenUsage?.session_limit_tokens === 'number'
  );
};
