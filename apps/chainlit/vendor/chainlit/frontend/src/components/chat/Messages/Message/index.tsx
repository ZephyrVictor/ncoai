import { cn } from '@/lib/utils';
import { MessageContext } from 'contexts/MessageContext';
import { memo, useContext, useMemo, useRef } from 'react';

import {
  type IAction,
  type IMessageElement,
  type IStep
} from '@chainlit/react-client';

import { Loader } from '@/components/Loader';
import { useTranslation } from 'components/i18n/Translator';
import { useLayoutMaxWidth } from 'hooks/useLayoutMaxWidth';
import { formatTokenCount, hasTokenUsageMetadata } from '@/lib/quota';

import { Messages } from '..';
import { AskActionButtons } from './AskActionButtons';
import { AskFileButton } from './AskFileButton';
import { MessageAvatar } from './Avatar';
import { MessageButtons } from './Buttons';
import { MessageContent } from './Content';
import Step from './Step';
import UserMessage from './UserMessage';

interface Props {
  message: IStep;
  elements: IMessageElement[];
  actions: IAction[];
  indent: number;
  isRunning?: boolean;
  isScorable?: boolean;
  scorableRun?: IStep;
}

const EMPTY_ELEMENTS: IMessageElement[] = [];
type GenerationStatus =
  | 'pending'
  | 'streaming'
  | 'stopping'
  | 'stopped'
  | undefined;

const Message = memo(
  ({
    message,
    elements,
    actions,
    isRunning,
    indent,
    isScorable,
    scorableRun
  }: Props) => {
    const { allowHtml, cot, latex, renderUserMarkdown, onError } =
      useContext(MessageContext);
    const { t } = useTranslation();
    const layoutMaxWidth = useLayoutMaxWidth();
    const contentRef = useRef<HTMLDivElement>(null);
    const isUserMessage = message.type === 'user_message';
    const isStep = !message.type.includes('message');
    // Only keep tool calls if Chain of Thought is tool_call
    const toolCallSkip =
      isStep && cot === 'tool_call' && message.type !== 'tool';

    const hiddenSkip = isStep && cot === 'hidden';

    const skip = toolCallSkip || hiddenSkip;
    const showInputSection = Boolean(message.input && message.showInput);
    const shouldRenderOutput = !showInputSection || Boolean(message.output);
    const tokenUsage = hasTokenUsageMetadata(message.metadata?.token_usage)
      ? message.metadata?.token_usage
      : undefined;
    const generationStatus = message.metadata
      ?.generation_status as GenerationStatus;
    const isLocalResyncing = Boolean(
      message.metadata?.local_generation_resyncing
    );
    const shouldShowAssistantStatus =
      !isStep &&
      (isLocalResyncing ||
        (!!generationStatus &&
          (generationStatus !== 'pending' || !message.output)));

    const userMessageContent = useMemo(
      () => (
        <MessageContent
          elements={EMPTY_ELEMENTS}
          message={message}
          allowHtml={allowHtml}
          latex={latex}
          renderMarkdown={renderUserMarkdown}
        />
      ),
      [message, allowHtml, latex]
    );

    const assistantStatus = useMemo(() => {
      switch (generationStatus) {
        case 'streaming':
          if (isLocalResyncing) {
            return {
              label: '正在生成中，正在与服务器重新同步',
              loading: true
            };
          }
          return undefined;
        case 'pending':
          if (isLocalResyncing) {
            return {
              label: '正在生成中，正在与服务器重新同步',
              loading: true
            };
          }
          return {
            label: t('chat.messages.status.pendingResponse'),
            loading: true
          };
        case 'stopping':
          return {
            label: t('chat.messages.status.stoppingResponse'),
            loading: false
          };
        case 'stopped':
          return {
            label: t('chat.messages.status.stoppedResponse'),
            loading: false
          };
        default:
          if (isLocalResyncing) {
            return {
              label: '正在生成中，正在与服务器重新同步',
              loading: true
            };
          }
          return undefined;
      }
    }, [generationStatus, isLocalResyncing, t]);

    if (skip) {
      if (!message.steps) {
        return null;
      }
      return (
        <Messages
          messages={message.steps}
          elements={elements}
          actions={actions}
          indent={indent}
          isRunning={isRunning}
          scorableRun={scorableRun}
        />
      );
    }

    return (
      <>
        <div data-step-type={message.type} className="step py-2">
          <div
            className="flex flex-col"
            style={{
              maxWidth: indent ? '100%' : layoutMaxWidth
            }}
          >
            <div
              className={cn('flex flex-grow pb-2')}
              id={`step-${message.id}`}
            >
              {/* User message is displayed differently */}
              {isUserMessage ? (
                <div className="flex flex-col flex-grow max-w-full">
                  <UserMessage message={message} elements={elements}>
                    {userMessageContent}
                  </UserMessage>
                </div>
              ) : (
                <div className="ai-message flex gap-4 w-full">
                  {!isStep || !indent ? (
                    <MessageAvatar
                      author={message.metadata?.avatarName || message.name}
                      isError={message.isError}
                    />
                  ) : null}
                  {/* Display the step and its children */}
                  {isStep ? (
                    <Step step={message} isRunning={isRunning}>
                      {showInputSection ? (
                        <MessageContent
                          elements={elements}
                          message={message}
                          allowHtml={allowHtml}
                          latex={latex}
                          renderMarkdown={true}
                          sections={['input']}
                        />
                      ) : null}
                      {message.steps ? (
                        <Messages
                          messages={message.steps.filter(
                            (s) => !s.type.includes('message')
                          )}
                          elements={elements}
                          actions={actions}
                          indent={indent + 1}
                          isRunning={isRunning}
                        />
                      ) : null}
                      {shouldRenderOutput ? (
                        <MessageContent
                          ref={contentRef}
                          elements={elements}
                          message={message}
                          allowHtml={allowHtml}
                          latex={latex}
                          renderMarkdown={true}
                          sections={showInputSection ? ['output'] : undefined}
                        />
                      ) : null}
                      <MessageButtons
                        message={message}
                        actions={actions}
                        contentRef={contentRef}
                      />
                    </Step>
                  ) : (
                    // Display an assistant message
                    <div className="flex flex-col items-start min-w-[150px] flex-grow gap-2">
                      <MessageContent
                        ref={contentRef}
                        elements={elements}
                        message={message}
                        allowHtml={allowHtml}
                        latex={latex}
                        renderMarkdown={true}
                      />
                      {shouldShowAssistantStatus && assistantStatus ? (
                        <div className="flex items-center gap-2 text-sm text-muted-foreground">
                          {assistantStatus.loading ? <Loader /> : null}
                          <span>{assistantStatus.label}</span>
                        </div>
                      ) : null}
                      {tokenUsage ? (
                        <div className="text-xs text-muted-foreground">
                          本次 {formatTokenCount(tokenUsage.request_tokens)} tok ·
                          会话 {formatTokenCount(tokenUsage.session_used_tokens)} /{' '}
                          {formatTokenCount(tokenUsage.session_limit_tokens)}
                        </div>
                      ) : null}

                      <AskFileButton messageId={message.id} onError={onError} />
                      <AskActionButtons
                        actions={actions}
                        messageId={message.id}
                      />

                      <MessageButtons
                        message={message}
                        actions={actions}
                        run={
                          scorableRun && isScorable ? scorableRun : undefined
                        }
                        contentRef={contentRef}
                      />
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
        {/* Make sure the child assistant messages of a step are displayed at the root level. */}
        {message.steps && isStep ? (
          <Messages
            messages={message.steps.filter((s) => s.type.includes('message'))}
            elements={elements}
            actions={actions}
            indent={0}
            isRunning={isRunning}
            scorableRun={scorableRun}
          />
        ) : null}
        {/* Display the child steps if the message is not a step (usually a user message). */}
        {message.steps && !isStep ? (
          <Messages
            messages={message.steps}
            elements={elements}
            actions={actions}
            indent={indent}
            isRunning={isRunning}
          />
        ) : null}
      </>
    );
  }
);

export { Message };
