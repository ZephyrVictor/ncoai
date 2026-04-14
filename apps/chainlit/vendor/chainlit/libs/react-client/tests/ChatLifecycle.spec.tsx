import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { RecoilRoot } from 'recoil';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useChatData } from '../src/useChatData';
import { useChatInteract } from '../src/useChatInteract';
import { useChatMessages } from '../src/useChatMessages';
import { configState, sessionState } from '../src/state';

type MockSocket = {
  connected: boolean;
  emit: ReturnType<typeof vi.fn>;
};

const renderHarness = () => {
  const socket: MockSocket = {
    connected: true,
    emit: vi.fn()
  };

  render(
    <RecoilRoot
      initializeState={({ set }) => {
        set(sessionState, { socket } as any);
        set(
          configState,
          {
            ui: {
              name: 'NCOAI'
            }
          } as any
        );
      }}
    >
      <ChatLifecycleHarness />
    </RecoilRoot>
  );

  return { socket };
};

function ChatLifecycleHarness() {
  const { sendMessage, stopTask } = useChatInteract();
  const { messages } = useChatMessages();
  const { generation } = useChatData();

  return (
    <div>
      <button
        onClick={() =>
          sendMessage({
            name: 'tester',
            type: 'user_message',
            output: 'hello world'
          })
        }
      >
        send
      </button>
      <button onClick={stopTask}>stop</button>
      <div data-testid="phase">{generation.phase}</div>
      <pre data-testid="messages">{JSON.stringify(messages)}</pre>
    </div>
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('chat lifecycle', () => {
  it('inserts the user message and pending assistant placeholder immediately', () => {
    const { socket } = renderHarness();

    fireEvent.click(screen.getByRole('button', { name: 'send' }));

    const messages = JSON.parse(screen.getByTestId('messages').textContent || '[]');
    expect(screen.getByTestId('phase').textContent).toBe('pending');
    expect(messages).toHaveLength(2);
    expect(messages[0].type).toBe('user_message');
    expect(messages[1].type).toBe('assistant_message');
    expect(messages[1].name).toBe('NCOAI');
    expect(messages[1].metadata.generation_status).toBe('pending');
    expect(messages[1].metadata.optimistic).toBe(true);
    expect(socket.emit).toHaveBeenCalledTimes(1);
    expect(socket.emit).toHaveBeenCalledWith(
      'client_message',
      expect.objectContaining({
        assistantMessageId: expect.any(String),
        fileReferences: [],
        message: expect.objectContaining({
          output: 'hello world',
          type: 'user_message'
        })
      })
    );
  });

  it('ignores repeated submits while the current generation is pending', () => {
    const { socket } = renderHarness();

    fireEvent.click(screen.getByRole('button', { name: 'send' }));
    fireEvent.click(screen.getByRole('button', { name: 'send' }));

    const messages = JSON.parse(screen.getByTestId('messages').textContent || '[]');
    expect(messages).toHaveLength(2);
    expect(socket.emit).toHaveBeenCalledTimes(1);
  });

  it('switches to stopping immediately and freezes the assistant placeholder', () => {
    const { socket } = renderHarness();

    fireEvent.click(screen.getByRole('button', { name: 'send' }));
    fireEvent.click(screen.getByRole('button', { name: 'stop' }));

    const messages = JSON.parse(screen.getByTestId('messages').textContent || '[]');
    expect(screen.getByTestId('phase').textContent).toBe('stopping');
    expect(messages[1].streaming).toBe(false);
    expect(messages[1].metadata.generation_status).toBe('stopping');
    expect(socket.emit).toHaveBeenNthCalledWith(2, 'stop');
  });
});
