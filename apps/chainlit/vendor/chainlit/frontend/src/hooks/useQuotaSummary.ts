import { useCallback, useContext, useEffect, useMemo } from 'react';
import { useSetRecoilState } from 'recoil';

import {
  IQuotaSummary,
  ChainlitContext,
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
  const preferredVersion = preferred.policy_version || 0;
  const fallbackVersion = fallback.policy_version || 0;
  if (preferredVersion !== fallbackVersion) {
    return preferredVersion > fallbackVersion ? preferred : fallback;
  }
  const preferredTime = preferred.updated_at
    ? Date.parse(preferred.updated_at)
    : 0;
  const fallbackTime = fallback.updated_at ? Date.parse(fallback.updated_at) : 0;
  return preferredTime >= fallbackTime ? preferred : fallback;
};

export const useQuotaSummary = () => {
  const client = useContext(ChainlitContext);
  const setUserQuota = useSetRecoilState(userQuotaState);
  const setThreadQuota = useSetRecoilState(threadQuotaState);
  const { threadQuota, userQuota } = useChatQuota();
  const { data, mutate } = useApi<IQuotaResponse>('/api/quota/me', {
    revalidateOnFocus: false,
    revalidateOnReconnect: false,
    revalidateIfStale: false
  });

  useEffect(() => {
    if (data?.quota) {
      setUserQuota((prev) => pickNewestQuota(data.quota, prev));
    }
  }, [data?.quota, setUserQuota]);

  const refreshUserQuota = useCallback(async () => {
    const response = await client.get('/api/quota/me');
    const payload = (await response.json()) as IQuotaResponse;
    if (payload?.quota) {
      setUserQuota((prev) => pickNewestQuota(payload.quota, prev));
    }
    await mutate(payload, { revalidate: false });
    return payload?.quota;
  }, [client, mutate, setUserQuota]);

  const resolvedUserQuota = useMemo(
    () => pickNewestQuota(data?.quota, userQuota),
    [userQuota, data?.quota]
  );

  return {
    threadQuota,
    userQuota: resolvedUserQuota,
    refreshUserQuota,
    setThreadQuota,
  };
};
