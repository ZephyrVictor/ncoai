import capitalize from 'lodash/capitalize';
import { LogOut } from 'lucide-react';

import { useAuth } from '@chainlit/react-client';

import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import { Button } from '@/components/ui/button';
import getRouterBasename from '@/lib/router';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu';
import { Translator } from 'components/i18n';
import { useQuotaSummary } from '@/hooks/useQuotaSummary';
import { formatTokenCount } from '@/lib/quota';

export default function UserNav() {
  const { user, logout } = useAuth();
  const { userQuota } = useQuotaSummary();

  if (!user) return null;
  const displayName = user?.display_name || user?.identifier;

  const handleLogout = async () => {
    await logout(false);
    window.location.href = getRouterBasename() + '/login';
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          id="user-nav-button"
          variant="ghost"
          className="relative h-8 rounded-full px-2"
        >
          <div className="flex items-center gap-2">
            <Avatar className="h-8 w-8">
              <AvatarImage src={user?.metadata.image} alt="user image" />
              <AvatarFallback className="bg-primary text-primary-foreground font-semibold">
                {capitalize(displayName[0])}
              </AvatarFallback>
            </Avatar>
            {userQuota ? (
              <span className="text-[11px] font-medium text-muted-foreground whitespace-nowrap">
                {formatTokenCount(userQuota.used_tokens)} /{' '}
                {formatTokenCount(userQuota.limit_tokens)}
              </span>
            ) : null}
          </div>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent className="w-26" align="end" forceMount>
        <DropdownMenuLabel className="font-normal">
          <div className="flex flex-col space-y-1">
            <p className="text-sm font-medium leading-none">{displayName}</p>
          </div>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={handleLogout}>
          <Translator path="navigation.user.menu.logout" />
          <LogOut className="ml-auto" />
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
