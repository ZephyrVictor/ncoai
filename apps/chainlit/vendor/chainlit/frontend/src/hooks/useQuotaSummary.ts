import { useEffect, useMemo } from 'react';
import { useSetRecoilState } from 'recoil';

import {
  IQuotaSummary,
  threadQuotaState,
  useApi,
  useChatQuota,
  userQuotaState
} from '@chainlit/react-client';

interface IQuotaResponse {
  user_identifier: string;
  quota: IQuotaSummary;
}

const pickNewestQuota = (
  preferred?: IQuotaSummary,
  fallback?: IQuotaSummary
): IQuotaSummary | undefined => {
  if (!preferred) return fallback;
  if (!fallback) return preferred;
  const preferredTime = preferred.updated_at
    ? Date.parse(preferred.updated_at)
    : 0;
  const fallbackTime = fallback.updated_at ? Date.parse(fallback.updated_at) : 0;
  return preferredTime >= fallbackTime ? preferred : fallback;
};

export const useQuotaSummary = () => {
  const setUserQuota = useSetRecoilState(userQuotaState);
  const setThreadQuota = useSetRecoilState(threadQuotaState);
  const { threadQuota, userQuota } = useChatQuota();
  const { data, mutate } = useApi<IQuotaResponse>('/api/quota/me');

  useEffect(() => {
    if (data?.quota) {
      setUserQuota((prev) => pickNewestQuota(prev, data.quota));
    }
  }, [data?.quota, setUserQuota]);

  const resolvedUserQuota = useMemo(
    () => pickNewestQuota(userQuota, data?.quota),
    [userQuota, data?.quota]
  );

  return {
    threadQuota,
    userQuota: resolvedUserQuota,
    refreshUserQuota: mutate,
    setThreadQuota,
  };
};
