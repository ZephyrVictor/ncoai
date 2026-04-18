import { useCallback, useContext, useEffect, useRef } from 'react';
import { useRecoilValue, useResetRecoilState, useSetRecoilState } from 'recoil';
import {
  actionState,
  askUserState,
  chatSettingsInputsState,
  chatSettingsValueState,
  configState,
  currentThreadIdState,
  elementState,
  favoriteMessagesState,
  firstUserInteraction,
  IGenerationState,
  generationState,
  messagesState,
  sessionIdState,
  sessionState,
  sideViewState,
  tasklistState,
  threadQuotaState,
  threadIdToResumeState,
  tokenCountState,
  userQuotaState
} from 'src/state';
import { IFileRef, IStep } from 'src/types';
import { addMessage } from 'src/utils/message';
import {
  clearLocalGenerationSnapshot,
  updateLocalGenerationSnapshot
} from 'src/utils/localGeneration';
import { v4 as uuidv4 } from 'uuid';

import { ChainlitContext } from './context';

type PartialBy<T, K extends keyof T> = Omit<T, K> & Partial<Pick<T, K>>;

const useChatInteract = () => {
  const client = useContext(ChainlitContext);
  const session = useRecoilValue(sessionState);
  const askUser = useRecoilValue(askUserState);
  const config = useRecoilValue(configState);
  const generation = useRecoilValue(generationState);
  const sessionId = useRecoilValue(sessionIdState);
  const resetGeneration = useResetRecoilState(generationState);

  const resetChatSettings = useResetRecoilState(chatSettingsInputsState);
  const resetSessionId = useResetRecoilState(sessionIdState);
  const resetChatSettingsValue = useResetRecoilState(chatSettingsValueState);

  const setFirstUserInteraction = useSetRecoilState(firstUserInteraction);
  const setGeneration = useSetRecoilState(generationState);
  const setMessages = useSetRecoilState(messagesState);
  const setElements = useSetRecoilState(elementState);
  const setTasklists = useSetRecoilState(tasklistState);
  const setActions = useSetRecoilState(actionState);
  const setTokenCount = useSetRecoilState(tokenCountState);
  const setIdToResume = useSetRecoilState(threadIdToResumeState);
  const setSideView = useSetRecoilState(sideViewState);
  const setCurrentThreadId = useSetRecoilState(currentThreadIdState);
  const setFavoriteMessages = useSetRecoilState(favoriteMessagesState);
  const setThreadQuota = useSetRecoilState(threadQuotaState);
  const setUserQuota = useSetRecoilState(userQuotaState);
  const currentThreadId = useRecoilValue(currentThreadIdState);
  const generationRef = useRef<IGenerationState>({ phase: 'idle' });

  const syncGenerationState = useCallback(
    (nextState: IGenerationState) => {
      generationRef.current = nextState;
      setGeneration(nextState);
    },
    [setGeneration]
  );

  useEffect(() => {
    generationRef.current = generation;
  }, [generation]);

  useEffect(() => {
    return () => {
      generationRef.current = { phase: 'idle' };
    };
  }, []);

  const clear = useCallback(() => {
    session?.socket.emit('clear_session');
    session?.socket.disconnect();
    generationRef.current = { phase: 'idle' };
    setIdToResume(undefined);
    resetSessionId();
    resetGeneration();
    setFirstUserInteraction(undefined);
    setMessages([]);
    setElements([]);
    setTasklists([]);
    setActions([]);
    setTokenCount(0);
    resetChatSettings();
    resetChatSettingsValue();
    setSideView(undefined);
    setCurrentThreadId(undefined);
    setThreadQuota(undefined);
    setUserQuota(undefined);
    clearLocalGenerationSnapshot();
  }, [resetGeneration, session]);

  const sendMessage = useCallback(
    (
      message: PartialBy<IStep, 'createdAt' | 'id'>,
      fileReferences: IFileRef[] = []
    ) => {
      if (generationRef.current.phase !== 'idle') {
        return;
      }

      if (!message.id) {
        message.id = uuidv4();
      }
      if (!message.createdAt) {
        message.createdAt = new Date().toISOString();
      }
      const assistantMessageId = uuidv4();
      const assistantName = config?.ui?.name || 'Assistant';

      setMessages((oldMessages) => {
        const nextMessages = addMessage(oldMessages, message as IStep);
        return addMessage(nextMessages, {
          id: assistantMessageId,
          threadId: message.threadId || '',
          type: 'assistant_message',
          name: assistantName,
          output: '',
          streaming: false,
          createdAt: new Date().toISOString(),
          metadata: {
            generation_status: 'pending',
            optimistic: true
          }
        });
      });

      syncGenerationState({
        phase: 'pending',
        assistantMessageId
      });
      updateLocalGenerationSnapshot({
        threadId: message.threadId || currentThreadId || '',
        assistantMessageId,
        partialOutput: '',
        generationStatus: 'pending',
        updatedAt: Date.now()
      });

      session?.socket.emit('client_message', {
        message,
        fileReferences,
        assistantMessageId
      });
    },
    [
      config?.ui?.name,
      currentThreadId,
      session?.socket,
      setMessages,
      syncGenerationState
    ]
  );

  const editMessage = useCallback(
    (message: IStep) => {
      session?.socket.emit('edit_message', { message });
    },
    [session?.socket]
  );

  const toggleMessageFavorite = useCallback(
    (message: IStep) => {
      const favorite = !(message.metadata?.favorite ?? false);
      const nextMessage: IStep = {
        ...message,
        metadata: {
          ...(message.metadata || {}),
          favorite
        }
      };

      setMessages((oldMessages) =>
        oldMessages.map((item) => (item.id === message.id ? nextMessage : item))
      );

      setFavoriteMessages((oldFavorites) => {
        if (favorite) {
          const filtered = oldFavorites.filter(
            (step) => step.id !== message.id
          );
          return [nextMessage, ...filtered];
        }
        return oldFavorites.filter((step) => step.id !== message.id);
      });

      session?.socket.emit('message_favorite', { message: nextMessage });
    },
    [session?.socket, setFavoriteMessages, setMessages]
  );

  const windowMessage = useCallback(
    (data: any) => {
      session?.socket.emit('window_message', data);
    },
    [session?.socket]
  );

  const startAudioStream = useCallback(() => {
    session?.socket.emit('audio_start');
  }, [session?.socket]);

  const sendAudioChunk = useCallback(
    (
      isStart: boolean,
      mimeType: string,
      elapsedTime: number,
      data: Int16Array
    ) => {
      session?.socket.emit('audio_chunk', {
        isStart,
        mimeType,
        elapsedTime,
        data
      });
    },
    [session?.socket]
  );

  const endAudioStream = useCallback(() => {
    session?.socket.emit('audio_end');
  }, [session?.socket]);

  const replyMessage = useCallback(
    (message: IStep) => {
      if (askUser) {
        if (askUser.parentId) message.parentId = askUser.parentId;
        setMessages((oldMessages) => addMessage(oldMessages, message));
        askUser.callback(message);
      }
    },
    [askUser]
  );

  const updateChatSettings = useCallback(
    (values: object) => {
      session?.socket.emit('chat_settings_change', values);
    },
    [session?.socket]
  );

  const editChatSettings = useCallback(
    (values: object) => {
      session?.socket.emit('chat_settings_edit', values);
    },
    [session?.socket]
  );

  const stopTask = useCallback(() => {
    const activeAssistantMessageId = generationRef.current.assistantMessageId;
    if (!activeAssistantMessageId || generationRef.current.phase === 'idle') {
      return;
    }

    syncGenerationState({
      phase: 'stopping',
      assistantMessageId: activeAssistantMessageId
    });
    updateLocalGenerationSnapshot({
      assistantMessageId: activeAssistantMessageId,
      generationStatus: 'stopping',
      updatedAt: Date.now()
    });

    setMessages((oldMessages) =>
      oldMessages.map((message) => {
        if (message.id !== activeAssistantMessageId) {
          return message;
        }

        return {
          ...message,
          streaming: false,
          metadata: {
            ...(message.metadata || {}),
            generation_status: 'stopping'
          }
        };
      })
    );

    session?.socket.emit('stop');
  }, [session?.socket, setMessages, syncGenerationState]);

  const uploadFile = useCallback(
    (file: File, onProgress: (progress: number) => void, parentId?: string) => {
      return client.uploadFile(file, onProgress, sessionId, parentId);
    },
    [client, sessionId]
  );

  return {
    uploadFile,
    clear,
    replyMessage,
    sendMessage,
    editMessage,
    windowMessage,
    startAudioStream,
    sendAudioChunk,
    endAudioStream,
    stopTask,
    setIdToResume,
    updateChatSettings,
    editChatSettings,
    toggleMessageFavorite
  };
};

export { useChatInteract };
