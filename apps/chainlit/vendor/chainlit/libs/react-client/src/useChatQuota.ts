import { useRecoilValue } from 'recoil';

import { threadQuotaState, userQuotaState } from './state';

const useChatQuota = () => {
  const threadQuota = useRecoilValue(threadQuotaState);
  const userQuota = useRecoilValue(userQuotaState);

  return {
    threadQuota,
    userQuota,
  };
};

export { useChatQuota };
