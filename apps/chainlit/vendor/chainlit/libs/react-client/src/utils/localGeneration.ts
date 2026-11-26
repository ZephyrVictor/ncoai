import { IThread } from 'src/types';

import { IStep } from '../types/step';

const STORAGE_KEY = 'chainlit.active-generation';

export interface ILocalGenerationSnapshot {
  threadId: string;
  assistantMessageId: string;
  partialOutput: string;
  generationStatus: string;
  updatedAt: number;
}

const getSessionStorage = (): Storage | undefined => {
  if (typeof window === 'undefined') {
    return undefined;
  }

  try {
    return window.sessionStorage;
  } catch {
    return undefined;
  }
};

const isTerminalStatus = (status?: string) =>
  ['stopped', 'completed', 'done', 'error', 'failed'].includes(status || '');

const cloneStep = (step: IStep): IStep => ({
  ...step,
  steps: step.steps?.map(cloneStep)
});

const stripLocalResyncingMetadata = (
  metadata?: Record<string, any>
): Record<string, any> | undefined => {
  if (!metadata?.local_generation_resyncing) {
    return metadata;
  }

  const nextMetadata = { ...metadata };
  delete nextMetadata.local_generation_resyncing;
  return Object.keys(nextMetadata).length ? nextMetadata : undefined;
};

export const readLocalGenerationSnapshot =
  (): ILocalGenerationSnapshot | null => {
    const storage = getSessionStorage();
    if (!storage) {
      return null;
    }

    try {
      const raw = storage.getItem(STORAGE_KEY);
      if (!raw) {
        return null;
      }
      const parsed = JSON.parse(raw) as ILocalGenerationSnapshot;
      if (!parsed?.assistantMessageId) {
        return null;
      }
      return parsed;
    } catch {
      return null;
    }
  };

export const writeLocalGenerationSnapshot = (
  snapshot: ILocalGenerationSnapshot
) => {
  const storage = getSessionStorage();
  if (!storage) {
    return;
  }

  storage.setItem(STORAGE_KEY, JSON.stringify(snapshot));
};

export const updateLocalGenerationSnapshot = (
  patch: Partial<ILocalGenerationSnapshot> &
    Pick<ILocalGenerationSnapshot, 'assistantMessageId'>
) => {
  const current = readLocalGenerationSnapshot();
  const nextSnapshot: ILocalGenerationSnapshot = {
    threadId: patch.threadId ?? current?.threadId ?? '',
    assistantMessageId: patch.assistantMessageId,
    partialOutput: patch.partialOutput ?? current?.partialOutput ?? '',
    generationStatus:
      patch.generationStatus ?? current?.generationStatus ?? 'pending',
    updatedAt: patch.updatedAt ?? Date.now()
  };

  writeLocalGenerationSnapshot(nextSnapshot);
};

export const clearLocalGenerationSnapshot = (assistantMessageId?: string) => {
  const storage = getSessionStorage();
  if (!storage) {
    return;
  }

  const current = readLocalGenerationSnapshot();
  if (
    assistantMessageId &&
    current?.assistantMessageId &&
    current.assistantMessageId !== assistantMessageId
  ) {
    return;
  }

  storage.removeItem(STORAGE_KEY);
};

export const trackLocalGenerationFromStep = (
  step: IStep,
  fallbackThreadId?: string
) => {
  if (step.type !== 'assistant_message') {
    return;
  }

  const generationStatus = String(step.metadata?.generation_status || '');
  const shouldClear =
    Boolean(step.isError) ||
    (!step.streaming && (!generationStatus || isTerminalStatus(generationStatus)));

  if (shouldClear) {
    clearLocalGenerationSnapshot(step.id);
    return;
  }

  updateLocalGenerationSnapshot({
    threadId: step.threadId || fallbackThreadId || '',
    assistantMessageId: step.id,
    partialOutput: step.output || '',
    generationStatus: generationStatus || (step.streaming ? 'streaming' : 'pending'),
    updatedAt: Date.now()
  });
};

export const appendLocalGenerationToken = ({
  assistantMessageId,
  token,
  isSequence
}: {
  assistantMessageId: string;
  token: string;
  isSequence: boolean;
}) => {
  const current = readLocalGenerationSnapshot();
  if (!current || current.assistantMessageId !== assistantMessageId) {
    return;
  }

  writeLocalGenerationSnapshot({
    ...current,
    partialOutput: isSequence
      ? token
      : `${current.partialOutput || ''}${token}`,
    generationStatus: 'streaming',
    updatedAt: Date.now()
  });
};

const withLocalRecovery = (
  step: IStep,
  snapshot: ILocalGenerationSnapshot
): IStep => {
  const status = String(step.metadata?.generation_status || '');
  if (!step.streaming && (!status || isTerminalStatus(status))) {
    clearLocalGenerationSnapshot(step.id);
    return { ...step, metadata: stripLocalResyncingMetadata(step.metadata) };
  }
  const nextMetadata = {
    ...(step.metadata || {}),
    generation_status: snapshot.generationStatus,
    local_generation_resyncing: true
  };

  return {
    ...step,
    output:
      snapshot.partialOutput.length >= (step.output || '').length
        ? snapshot.partialOutput
        : step.output,
    streaming: !isTerminalStatus(snapshot.generationStatus),
    metadata: nextMetadata
  };
};

const mergeStepsWithLocalGeneration = (
  steps: IStep[],
  snapshot: ILocalGenerationSnapshot
): { steps: IStep[]; matched: boolean } => {
  let matched = false;

  const nextSteps = steps.map((step) => {
    const nextStep = cloneStep(step);
    if (nextStep.id === snapshot.assistantMessageId) {
      matched = true;
      return withLocalRecovery(nextStep, snapshot);
    }

    if (nextStep.steps?.length) {
      const nested = mergeStepsWithLocalGeneration(nextStep.steps, snapshot);
      if (nested.matched) {
        matched = true;
        nextStep.steps = nested.steps;
      }
    }

    return {
      ...nextStep,
      metadata: stripLocalResyncingMetadata(nextStep.metadata)
    };
  });

  return { steps: nextSteps, matched };
};

export const mergeThreadWithLocalGeneration = (thread: IThread): IThread => {
  const snapshot = readLocalGenerationSnapshot();
  if (!snapshot) {
    return thread;
  }

  const canApplyToThread =
    snapshot.threadId === thread.id ||
    (!snapshot.threadId &&
      thread.steps.some((step) => step.id === snapshot.assistantMessageId));
  if (!canApplyToThread) {
    return thread;
  }

  const merged = mergeStepsWithLocalGeneration(thread.steps || [], snapshot);
  if (merged.matched) {
    return {
      ...thread,
      steps: merged.steps
    };
  }

  return {
    ...thread,
    steps: [
      ...thread.steps,
      {
        id: snapshot.assistantMessageId,
        threadId: snapshot.threadId || thread.id,
        type: 'assistant_message',
        name: 'Assistant',
        output: snapshot.partialOutput,
        createdAt: new Date(snapshot.updatedAt).toISOString(),
        streaming: !isTerminalStatus(snapshot.generationStatus),
        metadata: {
          generation_status: snapshot.generationStatus,
          local_generation_resyncing: true
        }
      }
    ]
  };
};

export const clearLocalResyncingFromMessage = (step: IStep): IStep => ({
  ...step,
  metadata: stripLocalResyncingMetadata(step.metadata)
});
