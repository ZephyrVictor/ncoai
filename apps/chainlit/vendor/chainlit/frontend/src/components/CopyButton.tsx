import { Check, Copy } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';

import { useTranslation } from '@/components/i18n/Translator';
import { Button } from '@/components/ui/button';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger
} from '@/components/ui/tooltip';

interface Props {
  content: unknown;
  className?: string;
  contentRef?: React.RefObject<HTMLDivElement>;
}

const fallbackCopyText = async (text: string) => {
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', 'true');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  textarea.style.pointerEvents = 'none';
  document.body.appendChild(textarea);
  textarea.select();
  textarea.setSelectionRange(0, text.length);
  const copied = document.execCommand('copy');
  document.body.removeChild(textarea);
  if (!copied) {
    throw new Error('clipboard unavailable');
  }
};

const CopyButton = ({ content, className, contentRef }: Props) => {
  const [copied, setCopied] = useState(false);
  const { t } = useTranslation();

  const copyToClipboard = async () => {
    try {
      const textToCopy =
        typeof content === 'object'
          ? JSON.stringify(content, null, 2)
          : String(content);
      const clipboardApi = navigator.clipboard;
      const ClipboardItemCtor =
        typeof window !== 'undefined'
          ? (
              window as Window &
                typeof globalThis & {
                  ClipboardItem?: typeof ClipboardItem;
                }
            ).ClipboardItem
          : undefined;

      let copiedSuccessfully = false;

      if (clipboardApi?.write && ClipboardItemCtor && contentRef?.current) {
        try {
          await clipboardApi.write([
            new ClipboardItemCtor({
              'text/plain': new Blob([textToCopy], { type: 'text/plain' }),
              'text/html': new Blob([contentRef.current.innerHTML], {
                type: 'text/html'
              })
            })
          ]);
          copiedSuccessfully = true;
        } catch {
          copiedSuccessfully = false;
        }
      }

      if (!copiedSuccessfully && clipboardApi?.writeText) {
        try {
          await clipboardApi.writeText(textToCopy);
          copiedSuccessfully = true;
        } catch {
          copiedSuccessfully = false;
        }
      }

      if (!copiedSuccessfully) {
        await fallbackCopyText(textToCopy);
      }

      setCopied(true);

      // Reset copied state after 2 seconds
      setTimeout(() => {
        setCopied(false);
      }, 2000);
    } catch (err) {
      toast.error('Failed to copy: ' + String(err));
    }
  };

  return (
    <TooltipProvider delayDuration={100}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            onClick={copyToClipboard}
            variant="ghost"
            size="icon"
            className={`text-muted-foreground ${className}`}
          >
            {copied ? (
              <Check className="h-4 w-4" />
            ) : (
              <Copy className="h-4 w-4" />
            )}
          </Button>
        </TooltipTrigger>
        <TooltipContent>
          <p>
            {copied
              ? t('chat.messages.actions.copy.success')
              : t('chat.messages.actions.copy.button')}
          </p>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
};

export default CopyButton;
