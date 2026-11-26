import { debounce } from 'lodash';
import { useCallback, useContext, useEffect, useRef } from 'react';
import {
  useRecoilState,
  useRecoilValue,
  useResetRecoilState,
  useSetRecoilState
} from 'recoil';
import io from 'socket.io-client';
import { toast } from 'sonner';
import {
  actionState,
  askUserState,
  audioConnectionState,
  callFnState,
  chatProfileState,
  chatSettingsInputsState,
  chatSettingsValueState,
  commandsState,
  currentThreadIdState,
  elementState,
  favoriteMessagesState,
  firstUserInteraction,
  generationState,
  isAiSpeakingState,
  loadingState,
  mcpState,
  messagesState,
  modesState,
  resumeThreadErrorState,
  sessionIdState,
  sessionState,
  sideViewState,
  tasklistState,
  threadQuotaState,
  threadIdToResumeState,
  tokenCountState,
  type IGenerationState,
  userQuotaState,
  wavRecorderState,
  wavStreamPlayerState
} from 'src/state';
import {
  IAction,
  ICommand,
  IElement,
  IMessageElement,
  IMode,
  IQuotaSummary,
  IStep,
  ITasklistElement,
  ITokenUsageMetadata,
  IThread
} from 'src/types';
import {
  addMessage,
  deleteMessageById,
  findMessageById,
  updateMessageById,
  updateMessageContentById
} from 'src/utils/message';
import {
  appendLocalGenerationToken,
  clearLocalGenerationSnapshot,
  clearLocalResyncingFromMessage,
  mergeThreadWithLocalGeneration,
  readLocalGenerationSnapshot,
  trackLocalGenerationFromStep,
  updateLocalGenerationSnapshot
} from 'src/utils/localGeneration';

import { OutputAudioChunk } from './types/audio';

import { ChainlitContext } from './context';
import type { IToken } from './useChatData';

const quotaSummaryFromTokenUsage = (
  tokenUsage?: Partial<ITokenUsageMetadata>
): { threadQuota?: IQuotaSummary; userQuota?: IQuotaSummary } => {
  if (!tokenUsage) {
    return {};
  }

  const updatedAt = tokenUsage.updated_at;
  const sessionLimit = tokenUsage.session_limit_tokens;
  const userLimit = tokenUsage.user_limit_tokens;

  return {
    threadQuota:
      typeof sessionLimit === 'number'
        ? {
            used_tokens: tokenUsage.session_used_tokens || 0,
            limit_tokens: sessionLimit,
            remaining_tokens: Math.max(
              sessionLimit - (tokenUsage.session_used_tokens || 0),
              0
            ),
            updated_at: updatedAt
          }
        : undefined,
    userQuota:
      typeof userLimit === 'number'
        ? {
            used_tokens: tokenUsage.user_used_tokens || 0,
            limit_tokens: userLimit,
            policy_version: tokenUsage.user_policy_version,
            remaining_tokens: Math.max(
              userLimit - (tokenUsage.user_used_tokens || 0),
              0
            ),
            updated_at: updatedAt
          }
        : undefined
  };
};

const mergeQuotaSummary = (
  preferred?: IQuotaSummary,
  fallback?: IQuotaSummary
): IQuotaSummary | undefined => {
  if (!preferred) {
    return fallback;
  }
  if (!fallback) {
    return preferred;
  }

  const preferredVersion = preferred.policy_version || 0;
  const fallbackVersion = fallback.policy_version || 0;
  if (preferredVersion !== fallbackVersion) {
    return preferredVersion >= fallbackVersion ? preferred : fallback;
  }

  const preferredTime = preferred.updated_at
    ? Date.parse(preferred.updated_at)
    : 0;
  const fallbackTime = fallback.updated_at ? Date.parse(fallback.updated_at) : 0;

  if (preferredTime !== fallbackTime) {
    return preferredTime >= fallbackTime ? preferred : fallback;
  }

  if (preferred.status === 'blocked' && fallback.status !== 'blocked') {
    return preferred;
  }
  if (fallback.status === 'blocked' && preferred.status !== 'blocked') {
    return fallback;
  }

  return preferred;
};

const applyQuotaFromStep = (
  step: IStep,
  setThreadQuota: (value: IQuotaSummary | undefined) => void,
  setUserQuota: (value: IQuotaSummary | undefined) => void
) => {
  const tokenUsage = step.metadata?.token_usage as
    | Partial<ITokenUsageMetadata>
    | undefined;
  const { threadQuota, userQuota } = quotaSummaryFromTokenUsage(tokenUsage);
  if (threadQuota) {
    setThreadQuota(threadQuota);
  }
  if (userQuota) {
    setUserQuota(userQuota);
  }
};

const findActiveGenerationStep = (steps: IStep[] = []): IStep | undefined => {
  for (const step of [...steps].reverse()) {
    const status = String(step.metadata?.generation_status || '');
    if (
      step.type === 'assistant_message' &&
      (step.streaming ||
        ['pending', 'queued', 'streaming', 'stopping'].includes(status))
    ) {
      return step;
    }
    const nested = findActiveGenerationStep(step.steps || []);
    if (nested) return nested;
  }
};

const hasStreamingStep = (steps: IStep[] = []) =>
  Boolean(findActiveGenerationStep(steps));

const applyQuotaFromSteps = (
  steps: IStep[],
  setThreadQuota: (value: IQuotaSummary | undefined) => void,
  setUserQuota: (value: IQuotaSummary | undefined) => void
) => {
  steps.forEach((step) => {
    applyQuotaFromStep(step, setThreadQuota, setUserQuota);
    if (step.steps?.length) {
      applyQuotaFromSteps(step.steps, setThreadQuota, setUserQuota);
    }
  });
};

const toIdleGenerationState = (): IGenerationState => ({
  phase: 'idle'
});

const useChatSession = () => {
  const client = useContext(ChainlitContext);
  const sessionId = useRecoilValue(sessionIdState);
  const generation = useRecoilValue(generationState);

  const [session, setSession] = useRecoilState(sessionState);
  const setIsAiSpeaking = useSetRecoilState(isAiSpeakingState);
  const setAudioConnection = useSetRecoilState(audioConnectionState);
  const resetChatSettingsValue = useResetRecoilState(chatSettingsValueState);
  const setChatSettingsValue = useSetRecoilState(chatSettingsValueState);
  const setFirstUserInteraction = useSetRecoilState(firstUserInteraction);
  const setLoading = useSetRecoilState(loadingState);
  const setMcps = useSetRecoilState(mcpState);
  const wavStreamPlayer = useRecoilValue(wavStreamPlayerState);
  const wavRecorder = useRecoilValue(wavRecorderState);
  const setMessages = useSetRecoilState(messagesState);
  const setAskUser = useSetRecoilState(askUserState);
  const setCallFn = useSetRecoilState(callFnState);
  const setCommands = useSetRecoilState(commandsState);
  const setModes = useSetRecoilState(modesState);
  const setSideView = useSetRecoilState(sideViewState);
  const setElements = useSetRecoilState(elementState);
  const setTasklists = useSetRecoilState(tasklistState);
  const setActions = useSetRecoilState(actionState);
  const setChatSettingsInputs = useSetRecoilState(chatSettingsInputsState);
  const setTokenCount = useSetRecoilState(tokenCountState);
  const setThreadQuota = useSetRecoilState(threadQuotaState);
  const setUserQuota = useSetRecoilState(userQuotaState);
  const setGeneration = useSetRecoilState(generationState);
  const [chatProfile, setChatProfile] = useRecoilState(chatProfileState);
  const idToResume = useRecoilValue(threadIdToResumeState);
  const setThreadResumeError = useSetRecoilState(resumeThreadErrorState);
  const setFavoriteMessages = useSetRecoilState(favoriteMessagesState);

  const [currentThreadId, setCurrentThreadId] =
    useRecoilState(currentThreadIdState);
  const currentThreadIdRef = useRef(currentThreadId);
  const streamingResumeRefreshId = useRef(0);
  const generationRef = useRef<IGenerationState>(generation);

  useEffect(() => {
    generationRef.current = generation;
  }, [generation]);

  const syncGenerationState = useCallback(
    (nextState: IGenerationState) => {
      generationRef.current = nextState;
      setGeneration(nextState);
    },
    [setGeneration]
  );

  // Use currentThreadId as thread id in websocket header
  useEffect(() => {
    currentThreadIdRef.current = currentThreadId;
    if (session?.socket) {
      session.socket.auth['threadId'] = currentThreadId || '';
    }
  }, [currentThreadId]);

  const _connect = useCallback(
    async ({
      transports,
      userEnv
    }: {
      transports?: string[];
      userEnv: Record<string, string>;
    }) => {
      const { protocol, host, pathname } = new URL(client.httpEndpoint);
      const uri = `${protocol}//${host}`;
      const path =
        pathname && pathname !== '/'
          ? `${pathname}/ws/socket.io`
          : '/ws/socket.io';

      try {
        await client.stickyCookie(sessionId);
      } catch (err) {
        console.error(`Failed to set sticky session cookie: ${err}`);
      }

      const socket = io(uri, {
        path,
        withCredentials: true,
        transports,
        auth: {
          clientType: client.type,
          sessionId,
          threadId: idToResume || '',
          userEnv: JSON.stringify(userEnv),
          chatProfile: chatProfile ? encodeURIComponent(chatProfile) : ''
        }
      });
      setSession((old) => {
        old?.socket?.removeAllListeners();
        old?.socket?.close();
        return {
          socket
        };
      });

      socket.on('connect', () => {
        socket.emit('connection_successful');
        setSession((s) => ({ ...s!, error: false }));
        socket.emit('fetch_favorites');
        setMcps((prev) =>
          prev.map((mcp) => {
            let promise;
            if (mcp.isUserProvided && mcp.url && mcp.clientType) {
              // User-provided MCP (SSE or streamable-http)
              promise = client.connectUserMcp(
                sessionId,
                mcp.name,
                mcp.clientType,
                mcp.url,
                mcp.headers
              );
            } else {
              // Named (developer-configured) MCP
              promise = client.connectMcp(sessionId, mcp.name);
            }
            promise
              .then(async ({ success, mcp }) => {
                setMcps((prev) =>
                  prev.map((existingMcp) => {
                    if (existingMcp.name === mcp.name) {
                      return {
                        ...existingMcp,
                        status: success ? 'connected' : 'failed',
                        tools: mcp ? mcp.tools : existingMcp.tools
                      };
                    }
                    return existingMcp;
                  })
                );
              })
              .catch(() => {
                setMcps((prev) =>
                  prev.map((existingMcp) => {
                    if (existingMcp.name === mcp.name) {
                      return {
                        ...existingMcp,
                        status: 'failed'
                      };
                    }
                    return existingMcp;
                  })
                );
              });
            return { ...mcp, status: 'connecting' };
          })
        );
      });

      socket.on('connect_error', (_) => {
        setSession((s) => ({ ...s!, error: true }));
      });

      socket.on('task_start', () => {
        setLoading(true);
      });

      socket.on('task_end', () => {
        setLoading(false);
        const activeGeneration = generationRef.current;
        const assistantMessageId = activeGeneration.assistantMessageId;

        if (!assistantMessageId) {
          syncGenerationState(toIdleGenerationState());
          clearLocalGenerationSnapshot();
          return;
        }

        setMessages((oldMessages) => {
          const assistantMessage = findMessageById(
            oldMessages,
            assistantMessageId
          );

          if (!assistantMessage) {
            return oldMessages;
          }

          const isOptimistic = Boolean(assistantMessage.metadata?.optimistic);
          if (isOptimistic && !assistantMessage.output) {
            return deleteMessageById(oldMessages, assistantMessageId);
          }

          const nextMetadata = assistantMessage.metadata
            ? Object.fromEntries(
                Object.entries(assistantMessage.metadata).filter(
                  ([key]) =>
                    key !== 'optimistic' &&
                    key !== 'local_generation_resyncing'
                )
              )
            : undefined;

          return updateMessageById(oldMessages, assistantMessageId, {
            ...assistantMessage,
            streaming: false,
            metadata:
              nextMetadata && Object.keys(nextMetadata).length
                ? nextMetadata
                : undefined
          });
        });

        clearLocalGenerationSnapshot(assistantMessageId);
        syncGenerationState(toIdleGenerationState());
      });

      socket.on('reload', () => {
        socket.emit('clear_session');
        window.location.reload();
      });

      socket.on('audio_connection', async (state: 'on' | 'off') => {
        if (state === 'on') {
          let isFirstChunk = true;
          const startTime = Date.now();
          const mimeType = 'pcm16';
          try {
            await wavRecorder.begin();
            await wavStreamPlayer.connect();
            await wavRecorder.record(async (data) => {
              const elapsedTime = Date.now() - startTime;
              socket.emit('audio_chunk', {
                isStart: isFirstChunk,
                mimeType,
                elapsedTime,
                data: data.mono
              });
              isFirstChunk = false;
            });
            wavStreamPlayer.onStop = () => setIsAiSpeaking(false);
          } catch {
            try {
              await wavRecorder.end();
            } catch {
              // ignored
            }
            await wavStreamPlayer.interrupt();
            socket.emit('audio_end');
            setAudioConnection('off');
            return;
          }
        } else {
          await wavRecorder.end();
          await wavStreamPlayer.interrupt();
        }
        setAudioConnection(state);
      });

      socket.on('audio_chunk', (chunk: OutputAudioChunk) => {
        wavStreamPlayer.add16BitPCM(chunk.data, chunk.track);
        setIsAiSpeaking(true);
      });

      socket.on('audio_interrupt', () => {
        wavStreamPlayer.interrupt();
      });

      const hydrateThread = (thread: IThread) => {
        const hydratedThread = mergeThreadWithLocalGeneration(thread);
        const isReadOnlyView = Boolean(
          (hydratedThread as any)?.metadata?.viewer_read_only
        );
        if (!isReadOnlyView && idToResume && hydratedThread.id !== idToResume) {
          window.location.href = `/thread/${hydratedThread.id}`;
          return;
        }
        if (!isReadOnlyView && idToResume) {
          currentThreadIdRef.current = hydratedThread.id;
          setCurrentThreadId(hydratedThread.id);
        }
        const activeStep = !isReadOnlyView
          ? findActiveGenerationStep(hydratedThread.steps)
          : undefined;
        if (activeStep) {
          const status = String(activeStep.metadata?.generation_status || '');
          const stopping =
            generationRef.current.phase === 'stopping' &&
            generationRef.current.assistantMessageId === activeStep.id;
          syncGenerationState({
            phase:
              stopping || status === 'stopping'
                ? 'stopping'
                : ['pending', 'queued'].includes(status)
                  ? 'pending'
                  : 'streaming',
            assistantMessageId: activeStep.id
          });
          trackLocalGenerationFromStep(activeStep, hydratedThread.id);
        } else {
          syncGenerationState(toIdleGenerationState());
        }

        let messages: IStep[] = [];
        for (const step of hydratedThread.steps) {
          messages = addMessage(messages, step);
        }
        if (hydratedThread.metadata?.chat_profile) {
          setChatProfile(hydratedThread.metadata?.chat_profile);
        }
        if (hydratedThread.metadata?.chat_settings) {
          setChatSettingsValue(hydratedThread.metadata?.chat_settings);
        }
        const threadTokenSummary = hydratedThread.metadata?.token_usage_summary as
          | IQuotaSummary
          | undefined;
        setThreadQuota(undefined);
        applyQuotaFromSteps(
          hydratedThread.steps || [],
          setThreadQuota,
          setUserQuota
        );
        if (threadTokenSummary) {
          setThreadQuota((prev) => mergeQuotaSummary(threadTokenSummary, prev));
        }
        setMessages(messages);
        const elements = hydratedThread.elements || [];
        setTasklists(
          (elements as ITasklistElement[]).filter((e) => e.type === 'tasklist')
        );
        setElements(
          (elements as IMessageElement[]).filter(
            (e) => ['avatar', 'tasklist'].indexOf(e.type) === -1
          )
        );
      };

      const refreshStreamingThread = async (
        threadId: string,
        refreshId: number,
        remainingAttempts = 60
      ) => {
        if (
          remainingAttempts <= 0 ||
          streamingResumeRefreshId.current !== refreshId
        ) {
          return;
        }

        try {
          const response = await client.get(`/project/thread/${threadId}`);
          const thread = (await response.json()) as IThread;
          if (streamingResumeRefreshId.current !== refreshId) {
            return;
          }

          hydrateThread(thread);
          if (hasStreamingStep(thread.steps || [])) {
            window.setTimeout(() => {
              void refreshStreamingThread(
                threadId,
                refreshId,
                remainingAttempts - 1
              );
            }, 1500);
          }
        } catch {
          if (streamingResumeRefreshId.current !== refreshId) {
            return;
          }
          window.setTimeout(() => {
            void refreshStreamingThread(
              threadId,
              refreshId,
              remainingAttempts - 1
            );
          }, 2000);
        }
      };

      socket.on('resume_thread', (thread: IThread) => {
        streamingResumeRefreshId.current += 1;
        const refreshId = streamingResumeRefreshId.current;
        hydrateThread(thread);
        if (hasStreamingStep(thread.steps || [])) {
          window.setTimeout(() => {
            void refreshStreamingThread(thread.id, refreshId);
          }, 1500);
        }
      });

      socket.on('resume_thread_error', (error?: string) => {
        setThreadResumeError(error);
      });

      socket.on('new_message', (message: IStep) => {
        applyQuotaFromStep(message, setThreadQuota, setUserQuota);
        trackLocalGenerationFromStep(message, currentThreadIdRef.current);
        setMessages((oldMessages) => addMessage(oldMessages, message));
      });

      socket.on(
        'first_interaction',
        (event: { interaction: string; thread_id: string }) => {
          setFirstUserInteraction(event.interaction);
          currentThreadIdRef.current = event.thread_id;
          setCurrentThreadId(event.thread_id);
          const localGeneration = readLocalGenerationSnapshot();
          if (localGeneration?.assistantMessageId) {
            updateLocalGenerationSnapshot({
              assistantMessageId: localGeneration.assistantMessageId,
              threadId: event.thread_id,
              updatedAt: Date.now()
            });
          }
        }
      );

      socket.on('update_message', (message: IStep) => {
        applyQuotaFromStep(message, setThreadQuota, setUserQuota);
        trackLocalGenerationFromStep(message, currentThreadIdRef.current);
        setMessages((oldMessages) => {
          const activeGeneration = generationRef.current;
          const activeAssistantMessageId = activeGeneration.assistantMessageId;

          if (
            activeGeneration.phase === 'stopping' &&
            activeAssistantMessageId === message.id
          ) {
            const existingMessage = findMessageById(oldMessages, message.id);
            if (!existingMessage) {
              return oldMessages;
            }

            return updateMessageById(oldMessages, message.id, {
              ...message,
              output: existingMessage.output,
              streaming: false
            });
          }

          return updateMessageById(
            oldMessages,
            message.id,
            clearLocalResyncingFromMessage(message)
          );
        });
      });

      socket.on('delete_message', (message: IStep) => {
        setMessages((oldMessages) =>
          deleteMessageById(oldMessages, message.id)
        );
      });

      socket.on('stream_start', (message: IStep) => {
        applyQuotaFromStep(message, setThreadQuota, setUserQuota);
        trackLocalGenerationFromStep(message, currentThreadIdRef.current);
        const activeGeneration = generationRef.current;
        if (
          activeGeneration.phase === 'stopping' &&
          activeGeneration.assistantMessageId === message.id
        ) {
          return;
        }

        setMessages((oldMessages) => addMessage(oldMessages, message));
        if (activeGeneration.assistantMessageId === message.id) {
          syncGenerationState({
            phase: 'streaming',
            assistantMessageId: message.id
          });
        }
      });

      socket.on(
        'stream_token',
        ({ id, token, isSequence, isInput }: IToken) => {
          const activeGeneration = generationRef.current;
          if (
            activeGeneration.phase === 'stopping' &&
            activeGeneration.assistantMessageId === id
          ) {
            return;
          }

          appendLocalGenerationToken({
            assistantMessageId: String(id),
            token,
            isSequence
          });
          setMessages((oldMessages) => {
            const nextMessages = updateMessageContentById(
              oldMessages,
              id,
              token,
              isSequence,
              isInput
            );
            const existingMessage = findMessageById(nextMessages, String(id));
            if (!existingMessage) {
              return nextMessages;
            }

            return updateMessageById(nextMessages, String(id), {
              ...existingMessage,
              streaming: true,
              metadata: {
                ...(existingMessage.metadata || {}),
                generation_status: 'streaming'
              }
            });
          });

          if (activeGeneration.assistantMessageId === id) {
            syncGenerationState({
              phase: 'streaming',
              assistantMessageId: id as string
            });
          }
        }
      );

      socket.on('ask', ({ msg, spec }, callback) => {
        setAskUser({ spec, callback, parentId: msg.parentId });
        setMessages((oldMessages) => addMessage(oldMessages, msg));

        setLoading(false);
      });

      socket.on('ask_timeout', () => {
        setAskUser(undefined);
        setLoading(false);
      });

      socket.on('clear_ask', () => {
        setAskUser(undefined);
      });

      socket.on('call_fn', ({ name, args }, callback) => {
        setCallFn({ name, args, callback });
      });

      socket.on('clear_call_fn', () => {
        setCallFn(undefined);
      });

      socket.on('call_fn_timeout', () => {
        setCallFn(undefined);
      });

      socket.on('chat_settings', (inputs: any) => {
        setChatSettingsInputs(inputs);
        resetChatSettingsValue();
      });

      socket.on('set_commands', (commands: ICommand[]) => {
        setCommands(commands);
      });

      socket.on('set_modes', (modes: IMode[]) => {
        setModes(modes);
      });

      socket.on('set_favorites', (steps: IStep[]) => {
        setFavoriteMessages(steps);
      });

      socket.on('set_sidebar_title', (title: string) => {
        setSideView((prev) => {
          if (prev?.title === title) return prev;
          return { title, elements: prev?.elements || [] };
        });
      });

      socket.on(
        'set_sidebar_elements',
        ({ elements, key }: { elements: IMessageElement[]; key?: string }) => {
          if (!elements.length) {
            setSideView(undefined);
          } else {
            elements.forEach((element) => {
              if (!element.url && element.chainlitKey) {
                element.url = client.getElementUrl(
                  element.chainlitKey,
                  sessionId
                );
              }
            });
            setSideView((prev) => {
              if (prev?.key === key) return prev;
              return { title: prev?.title || '', elements: elements, key };
            });
          }
        }
      );

      socket.on('element', (element: IElement) => {
        if (!element.url && element.chainlitKey) {
          element.url = client.getElementUrl(element.chainlitKey, sessionId);
        }

        if (element.type === 'tasklist') {
          setTasklists((old) => {
            const index = old.findIndex((e) => e.id === element.id);
            if (index === -1) {
              return [...old, element];
            } else {
              return [...old.slice(0, index), element, ...old.slice(index + 1)];
            }
          });
        } else {
          setElements((old) => {
            const index = old.findIndex((e) => e.id === element.id);
            if (index === -1) {
              return [...old, element];
            } else {
              return [...old.slice(0, index), element, ...old.slice(index + 1)];
            }
          });
        }
      });

      socket.on('remove_element', (remove: { id: string }) => {
        setElements((old) => {
          return old.filter((e) => e.id !== remove.id);
        });
        setTasklists((old) => {
          return old.filter((e) => e.id !== remove.id);
        });
      });

      socket.on('action', (action: IAction) => {
        setActions((old) => [...old, action]);
      });

      socket.on('remove_action', (action: IAction) => {
        setActions((old) => {
          const index = old.findIndex((a) => a.id === action.id);
          if (index === -1) return old;
          return [...old.slice(0, index), ...old.slice(index + 1)];
        });
      });

      socket.on('token_usage', (count: number) => {
        setTokenCount((old) => old + count);
      });

      socket.on('window_message', (data: any) => {
        if (window.parent) {
          window.parent.postMessage(data, '*');
        }
      });

      socket.on('toast', (data: { message: string; type: string }) => {
        if (!data.message) {
          console.warn('No message received for toast.');
          return;
        }

        switch (data.type) {
          case 'info':
            toast.info(data.message);
            break;
          case 'error':
            toast.error(data.message);
            break;
          case 'success':
            toast.success(data.message);
            break;
          case 'warning':
            toast.warning(data.message);
            break;
          default:
            toast(data.message);
            break;
        }
      });
    },
    [
      chatProfile,
      client,
      idToResume,
      sessionId,
      setGeneration,
      setSession,
      syncGenerationState
    ]
  );

  const connect = useCallback(debounce(_connect, 200), [_connect]);

  const disconnect = useCallback(() => {
    if (session?.socket) {
      session.socket.removeAllListeners();
      session.socket.close();
    }
    syncGenerationState(toIdleGenerationState());
  }, [session, syncGenerationState]);

  return {
    connect,
    disconnect,
    session,
    sessionId,
    chatProfile,
    idToResume,
    setChatProfile
  };
};

export { useChatSession };
