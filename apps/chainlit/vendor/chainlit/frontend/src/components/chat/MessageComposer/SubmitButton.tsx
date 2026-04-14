import {
  useChatData,
  useChatInteract
} from '@chainlit/react-client';

import { Send } from '@/components/icons/Send';
import { Stop } from '@/components/icons/Stop';
import { Button } from '@/components/ui/button';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger
} from '@/components/ui/tooltip';
import { Translator } from 'components/i18n';

interface SubmitButtonProps {
  disabled?: boolean;
  onSubmit: () => void;
}

export default function SubmitButton({
  disabled,
  onSubmit
}: SubmitButtonProps) {
  const { generation } = useChatData();
  const { stopTask } = useChatInteract();
  const isGenerating = generation.phase !== 'idle';
  const isStopping = generation.phase === 'stopping';

  return (
    <TooltipProvider>
      {isGenerating ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              id="stop-button"
              onClick={stopTask}
              disabled={isStopping}
              size="icon"
              className="rounded-full h-8 w-8"
            >
              <Stop className="!size-6" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            <p>
              <Translator path="chat.input.actions.stop" />
            </p>
          </TooltipContent>
        </Tooltip>
      ) : (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              id="chat-submit"
              disabled={disabled}
              onClick={onSubmit}
              size="icon"
              className="rounded-full h-8 w-8"
            >
              <Send className="!size-6" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            <p>
              <Translator path="chat.input.actions.send" />
            </p>
          </TooltipContent>
        </Tooltip>
      )}
    </TooltipProvider>
  );
}
