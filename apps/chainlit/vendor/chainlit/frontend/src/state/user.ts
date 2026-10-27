import { atom, type RecoilState } from 'recoil';

const localUserEnv = localStorage.getItem('userEnv');

export const userEnvState: RecoilState<Record<string, string>> = atom<Record<string, string>>({
  key: 'UserEnv',
  default: localUserEnv ? JSON.parse(localUserEnv) : {}
});
