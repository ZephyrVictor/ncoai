import {
  MutableRefObject,
  useCallback,
  useEffect,
  useRef,
  useState
} from 'react';
import { useRecoilState, useRecoilValue, useSetRecoilState } from 'recoil';
import { v4 as uuidv4 } from 'uuid';

import {
  FileSpec,
  IQuotaSummary,
  IStep,
  commandsState,
  useAuth,
  useChatData,
  useChatInteract,
  useChatMessages,
  useConfig
} from '@chainlit/react-client';
import type { IMode, IModeOption } from '@chainlit/react-client';
import { modesState } from '@chainlit/react-client';

import { Settings } from '@/components/icons/Settings';
import { Button } from '@/components/ui/button';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger
} from '@/components/ui/tooltip';
import { useTranslation } from 'components/i18n/Translator';

import { useQuery } from '@/hooks/query';
import { useQuotaSummary } from '@/hooks/useQuotaSummary';
import { useIsMobile } from '@/hooks/use-mobile';
import { formatTokenCount, getQuotaBlockedReason } from '@/lib/quota';

import { chatSettingsOpenState } from '@/state/project';
import {
  IAttachment,
  attachmentsState,
  persistentCommandState
} from 'state/chat';

import { Attachments } from './Attachments';
import CommandButtons from './CommandButtons';
import CommandButton from './CommandPopoverButton';
import FavoriteButton from './FavoriteButton';
import Input, { InputMethods } from './Input';
import McpButton from './Mcp';
import ModePicker from './ModePicker';
import SubmitButton from './SubmitButton';
import UploadButton from './UploadButton';
import VoiceButton from './VoiceButton';

interface Props {
  fileSpec: FileSpec;
  onFileUpload: (payload: File[]) => void;
  onFileUploadError: (error: string) => void;
  autoScrollRef: MutableRefObject<boolean>;
}

interface QuotaBlockedState {
  reason: string;
  scope: 'thread' | 'user';
  updatedAt?: string;
}

const findLastQuotaBlockedState = (
  steps: IStep[]
): QuotaBlockedState | undefined => {
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const step = steps[index];
    if (step.steps?.length) {
      const nestedBlockedState = findLastQuotaBlockedState(step.steps);
      if (nestedBlockedState) {
        return nestedBlockedState;
      }
    }

    const blockedScope = step.metadata?.quota_status?.blocked_scope;
    if (blockedScope === 'thread') {
      return {
        reason:
          step.metadata?.quota_status?.reason || '当前对话额度已用尽',
        scope: 'thread',
        updatedAt: step.metadata?.token_usage?.updated_at
      };
    }
    if (blockedScope === 'user') {
      return {
        reason:
          step.metadata?.quota_status?.reason || '用户总额度已用尽',
        scope: 'user',
        updatedAt: step.metadata?.token_usage?.updated_at
      };
    }
    if (blockedScope === 'window' || blockedScope === 'config') {
      return {
        reason:
          step.metadata?.quota_status?.reason ||
          (blockedScope === 'window'
            ? '当前不在配额生效时间内'
            : '当前没有可用配额配置'),
        scope: 'user',
        updatedAt: step.metadata?.token_usage?.updated_at
      };
    }
  }

  return undefined;
};

const isBlockedStateResolved = (
  blockedState: QuotaBlockedState | undefined,
  threadQuota?: IQuotaSummary,
  userQuota?: IQuotaSummary
) => {
  if (!blockedState) {
    return true;
  }

  const activeBlockedReason = getQuotaBlockedReason(threadQuota, userQuota);
  if (activeBlockedReason) {
    return false;
  }

  const relevantQuota =
    blockedState.scope === 'thread' ? threadQuota : userQuota;
  if (!relevantQuota?.updated_at || !blockedState.updatedAt) {
    return false;
  }

  return Date.parse(relevantQuota.updated_at) > Date.parse(blockedState.updatedAt);
};

export default function MessageComposer({
  fileSpec,
  onFileUpload,
  onFileUploadError,
  autoScrollRef
}: Props) {
  const inputRef = useRef<InputMethods>(null);
  const [value, setValue] = useState('');
  const [selectedCommand, setSelectedCommand] = useRecoilState(
    persistentCommandState
  );
  const commands = useRecoilValue(commandsState);
  const setChatSettingsOpen = useSetRecoilState(chatSettingsOpenState);

  // Pre-select the command marked as selected by the backend
  useEffect(() => {
    const defaultSelected = commands.find((c) => c.selected);
    if (defaultSelected && !selectedCommand) {
      setSelectedCommand(defaultSelected);
    }
  }, [commands]);
  const [attachments, setAttachments] = useRecoilState(attachmentsState);
  const { t } = useTranslation();

  const { user } = useAuth();
  const { sendMessage, replyMessage } = useChatInteract();
  const { askUser, chatSettingsInputs, disabled: _disabled } = useChatData();
  const { messages } = useChatMessages();
  const { threadQuota, userQuota, refreshUserQuota } = useQuotaSummary();
  const lastQuotaBlockedState = findLastQuotaBlockedState(messages);
  const currentQuotaBlockedReason = getQuotaBlockedReason(threadQuota, userQuota);
  const quotaBlockedReason =
    currentQuotaBlockedReason ||
    (!isBlockedStateResolved(lastQuotaBlockedState, threadQuota, userQuota)
      ? lastQuotaBlockedState?.reason
      : undefined);
  const hasPendingUploads = !!attachments.find((a) => !a.uploaded);
  const interactionDisabled = _disabled || hasPendingUploads;
  const disabled = interactionDisabled || !!quotaBlockedReason;

  const { config } = useConfig();
  const showSettingsInComposer =
    config?.ui?.chat_settings_location !== 'sidebar' &&
    chatSettingsInputs.length > 0;

  const isMobile = useIsMobile();

  // Get/set available modes from state - selections are tracked via the 'default' flag on options
  const [modes, setModes] = useRecoilState(modesState);

  const handleModeSelect = useCallback(
    (modeId: string, optionId: string) => {
      setModes((prevModes) =>
        prevModes.map((mode) => {
          if (mode.id !== modeId) return mode;
          return {
            ...mode,
            options: mode.options.map((opt: IModeOption) => ({
              ...opt,
              default: opt.id === optionId
            }))
          };
        })
      );
    },
    [setModes]
  );

  // Helper to get selected option for a mode (the one with default=true, or first option)
  const getSelectedOptionId = useCallback((mode: IMode): string | undefined => {
    const defaultOpt = mode.options.find((opt) => opt.default);
    return defaultOpt?.id || mode.options[0]?.id;
  }, []);

  let promptValue = '';
  try {
    const query = useQuery();
    promptValue = query.get('prompt') || '';
  } catch {
    console.warn('Could not parse query parameters');
  }

  const [promptUsed, setPromptUsed] = useState(false);

  const onFavoriteSelect = useCallback((content: string) => {
    setValue(content);
    if (inputRef.current) {
      inputRef.current.setValueExtern(content);
    }
  }, []);

  const onPaste = useCallback(
    (event: ClipboardEvent) => {
      if (event.clipboardData && event.clipboardData.items) {
        const items = Array.from(event.clipboardData.items);

        // If no text data, check for files (e.g., images)
        items.forEach((item) => {
          if (item.kind === 'file') {
            const file = item.getAsFile();
            if (file) {
              onFileUpload([file]);
            }
          }
        });
      }
    },
    [onFileUpload]
  );

  const onSubmit = useCallback(
    async (
      msg: string,
      attachments?: IAttachment[],
      selectedCommand?: string
    ) => {
      // Build modes dict: only include modes that have selections
      const modesDict: Record<string, string> = {};
      modes.forEach((mode) => {
        const selectedId = getSelectedOptionId(mode);
        if (selectedId) {
          modesDict[mode.id] = selectedId;
        }
      });

      const message: IStep = {
        threadId: '',
        command: selectedCommand,
        modes: Object.keys(modesDict).length > 0 ? modesDict : undefined,
        id: uuidv4(),
        name: user?.identifier || 'User',
        type: 'user_message',
        output: msg,
        createdAt: new Date().toISOString(),
        metadata: { location: window.location.href }
      };

      const fileReferences = attachments
        ?.filter((a) => !!a.serverId)
        .map((a) => ({ id: a.serverId! }));

      if (autoScrollRef) {
        autoScrollRef.current = true;
      }
      sendMessage(message, fileReferences);
    },
    [user, sendMessage, autoScrollRef, modes, getSelectedOptionId]
  );

  const onReply = useCallback(
    async (msg: string) => {
      const message: IStep = {
        threadId: '',
        id: uuidv4(),
        name: user?.identifier || 'User',
        type: 'user_message',
        output: msg,
        createdAt: new Date().toISOString(),
        metadata: { location: window.location.href }
      };

      replyMessage(message);
      if (autoScrollRef) {
        autoScrollRef.current = true;
      }
    },
    [user, replyMessage, autoScrollRef]
  );

  const submit = useCallback(async () => {
    if (
      (value.trim() === '' && attachments.length === 0 && !selectedCommand)
    ) {
      return;
    }

    const latestUserQuota = await refreshUserQuota();
    const blockedReasonAfterRefresh = getQuotaBlockedReason(
      threadQuota,
      latestUserQuota || userQuota
    );
    if (interactionDisabled || blockedReasonAfterRefresh) {
      return;
    }

    if (askUser) {
      onReply(value);
    } else {
      onSubmit(value, attachments, selectedCommand?.id);
    }

    setAttachments([]);
    setValue(''); // Clear the value state
    inputRef.current?.reset();
  }, [
    value,
    interactionDisabled,
    askUser,
    attachments,
    selectedCommand,
    setAttachments,
    onSubmit,
    onReply,
    refreshUserQuota,
    threadQuota,
    userQuota
  ]);

  useEffect(() => {
    if (inputRef.current && promptValue && !promptUsed) {
      const prompt = promptValue;
      if (prompt) {
        if (prompt.length > 1000) {
          inputRef.current?.setValueExtern(prompt.slice(0, 1000));
        } else {
          inputRef.current?.setValueExtern(prompt);
        }
        setPromptUsed(true);
      }
    }
  }, [promptValue, promptUsed]);

  return (
    <div
      id="message-composer"
      className="bg-accent dark:bg-card rounded-3xl p-3 px-4 w-full min-h-24 flex flex-col"
    >
      {attachments.length > 0 ? (
        <div className="mb-1">
          <Attachments />
        </div>
      ) : null}
      {threadQuota ? (
        <div className="mb-2 text-xs text-muted-foreground">
          当前对话 {formatTokenCount(threadQuota.used_tokens)} /{' '}
          {formatTokenCount(threadQuota.limit_tokens)} tok
        </div>
      ) : null}
      <Input
        ref={inputRef}
        id="chat-input"
        autoFocus={!isMobile}
        disabled={disabled}
        selectedCommand={selectedCommand}
        setSelectedCommand={setSelectedCommand}
        onChange={setValue}
        onPaste={onPaste}
        onEnter={submit}
        placeholder={t('chat.input.placeholder')}
      />
      <div className="flex items-center justify-between">
        <div className="flex items-center -ml-1.5">
          <VoiceButton disabled={disabled} />
          <UploadButton
            disabled={disabled}
            fileSpec={fileSpec}
            onFileUploadError={onFileUploadError}
            onFileUpload={onFileUpload}
          />
          {showSettingsInComposer && (
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    id="chat-settings-open-modal"
                    disabled={disabled}
                    onClick={() => setChatSettingsOpen(true)}
                    className="hover:bg-muted rounded-full"
                    variant="ghost"
                    size="icon"
                  >
                    <Settings className="!size-6" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>
                  <p>{t('navigation.user.menu.settings')}</p>
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
          )}
          <McpButton disabled={disabled} />
          {modes.map((mode) => (
            <ModePicker
              key={mode.id}
              mode={mode}
              disabled={disabled}
              selectedOptionId={getSelectedOptionId(mode)}
              onOptionSelect={handleModeSelect}
            />
          ))}
          <CommandButton
            disabled={disabled}
            selectedCommandId={selectedCommand?.id}
            onCommandSelect={setSelectedCommand}
          />
          <CommandButtons
            disabled={disabled}
            selectedCommandId={selectedCommand?.id}
            onCommandSelect={setSelectedCommand}
          />

          <FavoriteButton disabled={disabled} onSelect={onFavoriteSelect} />
        </div>
        <div className="flex items-center gap-1">
          <SubmitButton
            onSubmit={submit}
            disabled={
              disabled ||
              (!value.trim() && !selectedCommand && attachments.length === 0)
            }
          />
        </div>
      </div>
      {quotaBlockedReason ? (
        <div className="mt-2 text-xs text-destructive">{quotaBlockedReason}</div>
      ) : null}
    </div>
  );
}
