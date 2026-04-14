import AppWrapper from 'AppWrapper';
import { apiClient } from 'api';
import React from 'react';
import ReactDOM from 'react-dom/client';
import { RecoilRoot } from 'recoil';

import { ChainlitContext } from '@chainlit/react-client';

import './index.css';

import ChatLifecycleHarness from './e2e/ChatLifecycleHarness';
import { i18nSetupLocalization } from './i18n';

i18nSetupLocalization();

const isE2EChatLifecycleRoute = window.location.pathname.includes(
  '/__e2e__/chat-lifecycle'
);

const app = (
  <ChainlitContext.Provider value={apiClient}>
    {isE2EChatLifecycleRoute ? (
      <ChatLifecycleHarness />
    ) : (
      <RecoilRoot>
        <AppWrapper />
      </RecoilRoot>
    )}
  </ChainlitContext.Provider>
);

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  isE2EChatLifecycleRoute ? app : <React.StrictMode>{app}</React.StrictMode>
);
