import { atom, type RecoilState } from 'recoil';

export const chatSettingsOpenState: RecoilState<boolean> = atom<boolean>({
  key: 'chatSettingsOpen',
  default: false
});

export const chatSettingsSidebarOpenState: RecoilState<boolean> = atom<boolean>({
  key: 'chatSettingsSidebarOpen',
  default: false
});
