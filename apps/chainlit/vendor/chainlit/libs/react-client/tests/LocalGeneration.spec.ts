import { afterEach, describe, expect, it } from 'vitest';

import { IThread } from '../src/types';
import {
  appendLocalGenerationToken,
  clearLocalGenerationSnapshot,
  mergeThreadWithLocalGeneration,
  readLocalGenerationSnapshot,
  trackLocalGenerationFromStep,
  updateLocalGenerationSnapshot
} from '../src/utils/localGeneration';

const baseThread = (): IThread => ({
  id: 'thread-1',
  createdAt: new Date().toISOString(),
  steps: [
    {
      id: 'assistant-1',
      type: 'assistant_message',
      name: 'Assistant',
      output: 'hello',
      createdAt: new Date().toISOString(),
      streaming: true,
      metadata: {
        generation_status: 'streaming'
      }
    }
  ]
});

afterEach(() => {
  window.sessionStorage.clear();
});

describe('local generation recovery', () => {
  it('restores fresher local partial output over an older thread snapshot', () => {
    updateLocalGenerationSnapshot({
      threadId: 'thread-1',
      assistantMessageId: 'assistant-1',
      partialOutput: 'hello world',
      generationStatus: 'streaming',
      updatedAt: Date.now()
    });

    const merged = mergeThreadWithLocalGeneration(baseThread());

    expect(merged.steps[0].output).toBe('hello world');
    expect(merged.steps[0].metadata?.local_generation_resyncing).toBe(true);
    expect(merged.steps[0].streaming).toBe(true);
  });

  it('creates a synthetic assistant message when no db snapshot exists yet', () => {
    updateLocalGenerationSnapshot({
      threadId: 'thread-1',
      assistantMessageId: 'assistant-2',
      partialOutput: 'partial answer',
      generationStatus: 'pending',
      updatedAt: Date.now()
    });

    const merged = mergeThreadWithLocalGeneration({
      ...baseThread(),
      steps: []
    });

    expect(merged.steps).toHaveLength(1);
    expect(merged.steps[0].id).toBe('assistant-2');
    expect(merged.steps[0].output).toBe('partial answer');
    expect(merged.steps[0].metadata?.local_generation_resyncing).toBe(true);
  });

  it('tracks token appends and clears when the generation is finalized', () => {
    updateLocalGenerationSnapshot({
      threadId: 'thread-1',
      assistantMessageId: 'assistant-1',
      partialOutput: 'hel',
      generationStatus: 'streaming',
      updatedAt: Date.now()
    });

    appendLocalGenerationToken({
      assistantMessageId: 'assistant-1',
      token: 'lo',
      isSequence: false
    });

    expect(readLocalGenerationSnapshot()?.partialOutput).toBe('hello');

    trackLocalGenerationFromStep({
      id: 'assistant-1',
      type: 'assistant_message',
      name: 'Assistant',
      output: 'hello',
      createdAt: new Date().toISOString(),
      streaming: false,
      metadata: {
        generation_status: 'stopped'
      }
    });

    expect(readLocalGenerationSnapshot()).toBeNull();
    clearLocalGenerationSnapshot();
  });
});
