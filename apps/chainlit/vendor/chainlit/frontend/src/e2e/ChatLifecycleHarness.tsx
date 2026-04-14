import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { RecoilRoot, useRecoilValue, useSetRecoilState } from 'recoil';

import {
  ChainlitAPI,
  ChainlitContext,
  configState,
  findMessageById,
  generationState,
  IGenerationState,
  loadingState,
  messagesState,
  sessionState,
  updateMessageById,
  updateMessageContentById,
  useChatData,
  useChatInteract,
  useChatMessages
} from '@chainlit/react-client';
import type { IStep } from '@chainlit/react-client';

import MessagesContainer from '@/components/chat/MessagesContainer';
import SubmitButton from '@/components/chat/MessageComposer/SubmitButton';
import { ThemeProvider } from '@/components/ThemeProvider';
import { Badge } from '@/components/ui/badge';
import { Textarea } from '@/components/ui/textarea';
import { Toaster } from '@/components/ui/sonner';
import i18n from 'i18next';

const START_DELAY_MS = 1400;
const TOKEN_DELAY_MS = 220;
const TOKENS = [
  '这是',
  '一条',
  '用于',
  '验证',
  'pending',
  '切换',
  '与',
  '停止生成',
  '行为',
  '的',
  '模拟回复。'
];

const TRANSLATIONS = {
  chat: {
    input: {
      actions: {
        send: '发送消息',
        stop: '停止任务'
      }
    },
    messages: {
      status: {
        pendingResponse: '正在等待响应',
        stoppingResponse: '正在停止',
        stoppedResponse: '已停止'
      }
    }
  }
};

const HARNESS_CONFIG = {
  chatProfiles: [],
  ui: {
    name: 'NCOAI',
    cot: 'hidden'
  },
  features: {
    edit_message: false,
    unsafe_allow_html: false,
    latex: false,
    user_message_markdown: true
  }
};

type HarnessSnapshot = {
  emitCounts: {
    clientMessage: number;
    stop: number;
  };
  generation: IGenerationState;
  messages: Array<{
    id: string;
    type: string;
    output?: string;
    metadata?: Record<string, unknown>;
  }>;
};

declare global {
  interface Window {
    __CHAINLIT_E2E__?: {
      snapshot: () => HarnessSnapshot;
    };
  }
}

type MockSocket = {
  auth: Record<string, string>;
  connected: boolean;
  emit: (event: string, payload?: any) => void;
  close: () => void;
  removeAllListeners: () => void;
};

function ChatLifecycleHarnessInner() {
  const [prompt, setPrompt] = useState(
    '请帮我处理一个上下文很大的问题，并且慢一点再开始回复。'
  );
  const [emitCounts, setEmitCounts] = useState({
    clientMessage: 0,
    stop: 0
  });

  const { generation } = useChatData();
  const { messages } = useChatMessages();
  const { sendMessage } = useChatInteract();

  const setConfig = useSetRecoilState(configState);
  const setGeneration = useSetRecoilState(generationState);
  const setLoading = useSetRecoilState(loadingState);
  const setMessages = useSetRecoilState(messagesState);
  const setSession = useSetRecoilState(sessionState);

  const timersRef = useRef<number[]>([]);
  const activeAssistantMessageIdRef = useRef<string | null>(null);
  const stoppedRef = useRef(false);

  const clearTimers = useCallback(() => {
    timersRef.current.forEach((timerId) => window.clearTimeout(timerId));
    timersRef.current = [];
  }, []);

  const clearTransportState = useCallback(() => {
    clearTimers();
    activeAssistantMessageIdRef.current = null;
    stoppedRef.current = false;
  }, [clearTimers]);

  const resetLifecycle = useCallback(() => {
    clearTransportState();
    setLoading(false);
    setGeneration({ phase: 'idle' });
  }, [clearTransportState, setGeneration, setLoading]);

  const updateAssistantMessage = useCallback(
    (messageId: string, updater: (message: IStep) => IStep) => {
      setMessages((oldMessages) => {
        const currentMessage = findMessageById(oldMessages, messageId);
        if (!currentMessage) {
          return oldMessages;
        }

        return updateMessageById(
          oldMessages,
          messageId,
          updater(currentMessage)
        );
      });
    },
    [setMessages]
  );

  const finishRequest = useCallback(
    (messageId: string) => {
      updateAssistantMessage(messageId, (message) => ({
        ...message,
        streaming: false,
        metadata: undefined
      }));
      resetLifecycle();
    },
    [resetLifecycle, updateAssistantMessage]
  );

  const scheduleReply = useCallback(
    (assistantMessageId: string) => {
      clearTransportState();
      activeAssistantMessageIdRef.current = assistantMessageId;
      setLoading(true);

      const startTimer = window.setTimeout(() => {
        if (
          stoppedRef.current ||
          activeAssistantMessageIdRef.current !== assistantMessageId
        ) {
          return;
        }

        updateAssistantMessage(assistantMessageId, (message) => ({
          ...message,
          streaming: true,
          metadata: undefined
        }));
        setGeneration({
          phase: 'streaming',
          assistantMessageId
        });

        TOKENS.forEach((token, index) => {
          const tokenTimer = window.setTimeout(() => {
            if (
              stoppedRef.current ||
              activeAssistantMessageIdRef.current !== assistantMessageId
            ) {
              return;
            }

            setMessages((oldMessages) =>
              updateMessageContentById(
                oldMessages,
                assistantMessageId,
                token,
                false,
                false
              )
            );

            if (index === TOKENS.length - 1) {
              finishRequest(assistantMessageId);
            }
          }, TOKEN_DELAY_MS * (index + 1));

          timersRef.current.push(tokenTimer);
        });
      }, START_DELAY_MS);

      timersRef.current.push(startTimer);
    },
    [
      clearTransportState,
      finishRequest,
      setGeneration,
      setLoading,
      setMessages,
      updateAssistantMessage
    ]
  );

  const confirmStop = useCallback(() => {
    const assistantMessageId = activeAssistantMessageIdRef.current;
    stoppedRef.current = true;
    clearTimers();

    if (assistantMessageId) {
      updateAssistantMessage(assistantMessageId, (message) => ({
        ...message,
        streaming: false,
        metadata: {
          ...(message.metadata || {}),
          generation_status: 'stopped'
        }
      }));
    }

    const stopTimer = window.setTimeout(() => {
      resetLifecycle();
    }, 180);

    timersRef.current.push(stopTimer);
  }, [clearTimers, resetLifecycle, updateAssistantMessage]);

  const socket = useMemo<MockSocket>(
    () => ({
      auth: {},
      connected: true,
      emit: (event: string, payload?: any) => {
        if (event === 'client_message') {
          setEmitCounts((prev) => ({
            ...prev,
            clientMessage: prev.clientMessage + 1
          }));
          scheduleReply(String(payload?.assistantMessageId || ''));
          return;
        }

        if (event === 'stop') {
          setEmitCounts((prev) => ({
            ...prev,
            stop: prev.stop + 1
          }));
          confirmStop();
        }
      },
      close: () => undefined,
      removeAllListeners: () => undefined
    }),
    [confirmStop, scheduleReply]
  );

  const harnessApiClient = useMemo(() => {
    const client = new ChainlitAPI('http://127.0.0.1:4173', 'webapp');

    client.get = async (endpoint: string) => {
      let payload: unknown = {};

      if (endpoint.startsWith('/auth/config')) {
        payload = { requireLogin: false };
      } else if (endpoint.startsWith('/user')) {
        payload = null;
      } else if (endpoint.startsWith('/project/settings')) {
        payload = HARNESS_CONFIG;
      }

      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: {
          'Content-Type': 'application/json'
        }
      });
    };

    client.setFeedback = async () => ({ success: true, feedbackId: 'e2e-feedback' });
    client.deleteFeedback = async () => ({ success: true });

    return client;
  }, []);

  useEffect(() => {
    if (!i18n.hasResourceBundle('zh-CN', 'translation')) {
      i18n.addResourceBundle('zh-CN', 'translation', TRANSLATIONS, true, true);
    }
    void i18n.changeLanguage('zh-CN');

    setConfig(HARNESS_CONFIG as any);
    setSession({ socket } as any);
    setMessages([]);
    setLoading(false);
    setGeneration({ phase: 'idle' });

    return () => {
      clearTransportState();
      setSession(undefined as any);
      setMessages([]);
      setLoading(false);
      setGeneration({ phase: 'idle' });
    };
  }, [
    clearTransportState,
    setConfig,
    setGeneration,
    setLoading,
    setMessages,
    setSession,
    socket
  ]);

  useEffect(() => {
    window.__CHAINLIT_E2E__ = {
      snapshot: () => ({
        emitCounts,
        generation,
        messages: messages.map((message) => ({
          id: String(message.id),
          type: message.type,
          output: message.output,
          metadata: message.metadata as Record<string, unknown> | undefined
        }))
      })
    };

    return () => {
      delete window.__CHAINLIT_E2E__;
    };
  }, [emitCounts, generation, messages]);

  const handleSubmit = useCallback(() => {
    if (!prompt.trim()) {
      return;
    }

    void sendMessage({
      name: 'acceptance-user',
      type: 'user_message',
      output: prompt
    });
  }, [prompt, sendMessage]);

  return (
    <ChainlitContext.Provider value={harnessApiClient}>
      <ThemeProvider storageKey="chainlit-e2e-theme" defaultTheme="light">
        <Toaster richColors className="toast" position="top-right" />
        <div className="min-h-screen bg-background text-foreground px-6 py-8">
          <div className="mx-auto flex max-w-5xl flex-col gap-6">
            <section className="rounded-3xl border border-border bg-card/80 p-6 shadow-sm">
              <div className="flex flex-wrap items-center gap-3">
                <Badge variant="secondary">
                  <span data-testid="phase">{generation.phase}</span>
                </Badge>
                <Badge variant="outline">
                  请求数:
                  <span className="ml-2" data-testid="client-message-count">
                    {emitCounts.clientMessage}
                  </span>
                </Badge>
                <Badge variant="outline">
                  停止数:
                  <span className="ml-2" data-testid="stop-count">
                    {emitCounts.stop}
                  </span>
                </Badge>
              </div>
              <p className="mt-3 text-sm text-muted-foreground">
                这个页面直接复用当前聊天消息渲染和发送/停止逻辑，用来做真实浏览器验收。
              </p>
            </section>

            <section className="rounded-3xl border border-border bg-background shadow-sm">
              <div className="border-b border-border px-6 py-4">
                <h1 className="text-lg font-semibold">聊天区验收</h1>
              </div>
              <div className="h-[60vh] overflow-y-auto px-6 py-4">
                <MessagesContainer />
              </div>
              <div className="border-t border-border px-6 py-4">
                <div className="flex items-end gap-3">
                  <Textarea
                    data-testid="prompt-input"
                    value={prompt}
                    disabled={generation.phase !== 'idle'}
                    onChange={(event) => setPrompt(event.target.value)}
                    className="min-h-[96px]"
                  />
                  <SubmitButton
                    disabled={!prompt.trim()}
                    onSubmit={handleSubmit}
                  />
                </div>
              </div>
            </section>
          </div>
        </div>
      </ThemeProvider>
    </ChainlitContext.Provider>
  );
}

export default function ChatLifecycleHarness() {
  return (
    <RecoilRoot>
      <ChatLifecycleHarnessInner />
    </RecoilRoot>
  );
}
