import { useRecoilValue } from 'recoil';

import {
  actionState,
  askUserState,
  callFnState,
  chatSettingsDefaultValueSelector,
  chatSettingsInputsState,
  chatSettingsValueState,
  elementState,
  generationState,
  loadingState,
  sessionState,
  tasklistState
} from './state';

export interface IToken {
  id: number | string;
  token: string;
  isSequence: boolean;
  isInput: boolean;
}

const useChatData = () => {
  const loading = useRecoilValue(loadingState);
  const generation = useRecoilValue(generationState);
  const elements = useRecoilValue(elementState);
  const tasklists = useRecoilValue(tasklistState);
  const actions = useRecoilValue(actionState);
  const session = useRecoilValue(sessionState);
  const askUser = useRecoilValue(askUserState);
  const callFn = useRecoilValue(callFnState);
  const chatSettingsInputs = useRecoilValue(chatSettingsInputsState);
  const chatSettingsValue = useRecoilValue(chatSettingsValueState);
  const chatSettingsDefaultValue = useRecoilValue(
    chatSettingsDefaultValueSelector
  );

  const connected = session?.socket.connected && !session?.error;
  const isGenerating = generation.phase !== 'idle';
  const disabled =
    !connected ||
    isGenerating ||
    askUser?.spec.type === 'file' ||
    askUser?.spec.type === 'action' ||
    askUser?.spec.type === 'element';

  return {
    actions,
    askUser,
    callFn,
    chatSettingsDefaultValue,
    chatSettingsInputs,
    chatSettingsValue,
    connected,
    disabled,
    elements,
    error: session?.error,
    generation,
    isGenerating,
    loading,
    tasklists
  };
};

export { useChatData };
