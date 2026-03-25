import { useContext, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { LoginForm } from '@/components/LoginForm';
import { Logo } from '@/components/Logo';

import { useQuery } from 'hooks/query';

import { ChainlitContext, useAuth } from 'client-types/*';

export const LoginError = new Error(
  'Error logging in. Please try again later.'
);

export default function Login() {
  const query = useQuery();
  const { data: config, user, setUserFromAPI } = useAuth();
  const [error, setError] = useState('');
  const apiClient = useContext(ChainlitContext);
  const navigate = useNavigate();

  const handleCookieAuth = (json: any): void => {
    if (json?.success != true) throw LoginError;

    // Validate login cookie and get user data.
    setUserFromAPI();
  };

  const handleAuth = async (
    jsonPromise: Promise<any>,
    redirectURL?: string
  ) => {
    try {
      const json = await jsonPromise;

      handleCookieAuth(json);

      if (redirectURL) {
        navigate(redirectURL);
      }
    } catch (error: any) {
      setError(error.message);
    }
  };

  const handleHeaderAuth = async () => {
    const jsonPromise = apiClient.headerAuth();

    // Why does apiClient redirect to '/' but handlePasswordLogin to callbackUrl?
    await handleAuth(jsonPromise, '/');
  };

  const handlePasswordLogin = async (email: string, password: string) => {
    const formData = new FormData();
    formData.append('username', email);
    formData.append('password', password);

    const jsonPromise = apiClient.passwordAuth(formData);
    await handleAuth(jsonPromise);
  };

  useEffect(() => {
    setError(query.get('error') || '');
  }, [query]);

  useEffect(() => {
    if (!config) {
      return;
    }
    if (!config.requireLogin) {
      navigate('/');
    }
    if (config.headerAuth && !user) {
      handleHeaderAuth();
    }
    if (user) {
      navigate('/');
    }
  }, [config, user]);

  return (
    <div className="grid min-h-svh lg:grid-cols-2">
      <div className="flex flex-col gap-4 p-6 md:p-10">
        <div className="flex justify-center gap-2 md:justify-start">
          <Logo className="w-[150px]" />
        </div>
        <div className="flex flex-1 items-center justify-center">
          <div className="w-full max-w-xs">
            <LoginForm
              error={error}
              callbackUrl="/"
              providers={config?.oauthProviders || []}
              onPasswordSignIn={
                config?.passwordAuth ? handlePasswordLogin : undefined
              }
              onOAuthSignIn={async (provider: string) => {
                window.location.href = apiClient.getOAuthEndpoint(provider);
              }}
            />
          </div>
        </div>
      </div>
      {!config?.headerAuth ? (
        <div className="relative hidden overflow-hidden bg-[radial-gradient(circle_at_top,_hsl(var(--accent))_0%,_transparent_32%),linear-gradient(180deg,hsl(var(--background))_0%,hsl(var(--muted))_100%)] lg:block">
          <div className="absolute inset-0 opacity-60">
            <div className="absolute left-[14%] top-[16%] h-40 w-40 rounded-full bg-primary/10 blur-3xl" />
            <div className="absolute right-[16%] top-[22%] h-56 w-56 rounded-full bg-primary/8 blur-3xl" />
            <div className="absolute bottom-[14%] left-[22%] h-48 w-48 rounded-full bg-foreground/5 blur-3xl" />
          </div>
          <div className="absolute inset-0 bg-[linear-gradient(135deg,transparent_0%,transparent_52%,hsl(var(--border))_52%,transparent_53%,transparent_100%)] opacity-20" />
          <div className="absolute inset-0">
            <div className="absolute left-[18%] top-[18%] h-24 w-24 rounded-3xl border border-border/60 bg-background/20 backdrop-blur-sm" />
            <div className="absolute right-[20%] top-[42%] h-16 w-16 rounded-full border border-border/50 bg-background/10" />
            <div className="absolute bottom-[20%] left-[34%] h-20 w-20 rotate-12 rounded-2xl border border-border/50 bg-background/10" />
          </div>
        </div>
      ) : null}
    </div>
  );
}
