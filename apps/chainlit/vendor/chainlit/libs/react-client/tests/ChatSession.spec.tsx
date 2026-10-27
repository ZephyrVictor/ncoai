import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { useEffect } from 'react';
import { RecoilRoot, useRecoilValue } from 'recoil';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ChainlitContext } from '../src/context';
import {
  currentThreadIdState,
  generationState,
  threadIdToResumeState,
  userQuotaState
} from '../src/state';
import { useChatSession } from '../src/useChatSession';
import { readLocalGenerationSnapshot } from '../src/utils/localGeneration';

const handlers = new Map<string, (...args: any[]) => void>();
const socket = {
  auth: {} as Record<string, string>,
  on: vi.fn((event: string, callback: (...args: any[]) => void) => {
    handlers.set(event, callback);
  }),
  emit: vi.fn(),
  close: vi.fn(),
  removeAllListeners: vi.fn()
};

vi.mock('socket.io-client', () => ({ default: vi.fn(() => socket) }));

const client = {
  httpEndpoint: 'http://localhost:8000/chat',
  type: 'webapp',
  stickyCookie: vi.fn(async () => {})
};

function Wrapper({ children }: { children: React.ReactNode }) {
  return (
    <ChainlitContext.Provider value={client as any}>
      <RecoilRoot>{children}</RecoilRoot>
    </ChainlitContext.Provider>
  );
}

afterEach(() => {
  cleanup();
  handlers.clear();
  window.sessionStorage.clear();
  vi.clearAllMocks();
});

describe('Chat socket lifecycle', () => {
  it('restores stop controls and becomes idle when a resumed generation settles', async () => {
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <ChainlitContext.Provider value={client as any}>
        <RecoilRoot
          initializeState={({ set }) => set(threadIdToResumeState, 'thread-1')}
        >
          {children}
        </RecoilRoot>
      </ChainlitContext.Provider>
    );
    const { result } = renderHook(
      () => {
        const session = useChatSession();
        const generation = useRecoilValue(generationState);
        useEffect(() => {
          session.connect({ userEnv: {} });
        }, [session.connect]);
        return generation;
      },
      { wrapper }
    );
    await waitFor(() => expect(handlers.has('resume_thread')).toBe(true));
    const thread = {
      id: 'thread-1',
      createdAt: new Date().toISOString(),
      steps: [
        {
          id: 'assistant-1',
          type: 'assistant_message',
          name: 'Assistant',
          output: 'partial answer',
          streaming: true,
          metadata: { generation_status: 'streaming' }
        }
      ]
    };
    act(() => handlers.get('resume_thread')!(thread));
    expect(result.current).toEqual({
      phase: 'streaming',
      assistantMessageId: 'assistant-1'
    });
    expect(readLocalGenerationSnapshot()?.threadId).toBe('thread-1');

    act(() =>
      handlers.get('resume_thread')!({
        ...thread,
        steps: [
          {
            ...thread.steps[0],
            output: 'partial answer stopped',
            streaming: false,
            metadata: { generation_status: 'stopped' }
          }
        ]
      })
    );
    expect(result.current).toEqual({ phase: 'idle' });
    expect(readLocalGenerationSnapshot()).toBeNull();
  });

  it('keeps the connection on first interaction and tracks the assigned thread', async () => {
    const { result } = renderHook(
      () => {
        const session = useChatSession();
        const threadId = useRecoilValue(currentThreadIdState);
        const quota = useRecoilValue(userQuotaState);
        // The application connects whenever this callback changes.
        useEffect(() => {
          session.connect({ userEnv: {} });
        }, [session.connect]);
        return { ...session, threadId, quota };
      },
      { wrapper: Wrapper }
    );

    await waitFor(() => expect(handlers.has('first_interaction')).toBe(true));
    const connect = result.current.connect;
    act(() =>
      handlers.get('first_interaction')!({
        interaction: 'hello',
        thread_id: 'thread-1'
      })
    );

    expect(result.current.threadId).toBe('thread-1');
    expect(result.current.connect).toBe(connect);
    expect(socket.auth.threadId).toBe('thread-1');
    act(() =>
      handlers.get('stream_start')!({
        id: 'assistant-1',
        type: 'assistant_message',
        name: 'Assistant',
        output: 'hello',
        createdAt: new Date().toISOString(),
        streaming: true,
        metadata: { generation_status: 'streaming' }
      })
    );
    expect(readLocalGenerationSnapshot()?.threadId).toBe('thread-1');

    act(() =>
      handlers.get('update_message')!({
        id: 'assistant-1',
        type: 'assistant_message',
        name: 'Assistant',
        output: 'hello world',
        createdAt: new Date().toISOString(),
        streaming: false,
        metadata: {
          token_usage: {
            user_used_tokens: 78,
            user_limit_tokens: 20000,
            user_policy_version: 11,
            updated_at: '2026-10-27T10:00:00Z'
          }
        }
      })
    );
    expect(result.current.quota).toMatchObject({
      used_tokens: 78,
      policy_version: 11
    });
    expect(socket.close).not.toHaveBeenCalled();
  });
});
