import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { RecoilRoot } from 'recoil';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useApi } from '../src/api/hooks/api';
import { useUserManagement } from '../src/api/hooks/auth/userManagement';
import { userState } from '../src/state';

vi.mock('../src/api/hooks/api', () => ({ useApi: vi.fn() }));

const user = { id: 'user-1', identifier: 'signed-in-user', metadata: {} };

function Wrapper({ children }: { children: React.ReactNode }) {
  return (
    <RecoilRoot initializeState={({ set }) => set(userState, user)}>
      {children}
    </RecoilRoot>
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Authentication revalidation', () => {
  it.each([
    new TypeError('Failed to fetch'),
    Object.assign(new Error('Service unavailable'), { status: 503 })
  ])('keeps the signed-in user after a transient failure: %s', async (error) => {
    vi.mocked(useApi).mockReturnValue({ error } as any);
    const { result, rerender } = renderHook(() => useUserManagement(), {
      wrapper: Wrapper
    });
    expect(result.current.user).toEqual(user);

    const refreshed = { ...user, metadata: { display_name: 'Refreshed' } };
    vi.mocked(useApi).mockReturnValue({ data: refreshed } as any);
    rerender();
    await waitFor(() => expect(result.current.user).toEqual(refreshed));
  });

  it('clears the user when authentication is rejected', async () => {
    const error = Object.assign(new Error('Unauthorized'), { status: 401 });
    vi.mocked(useApi).mockReturnValue({ error } as any);
    const { result } = renderHook(() => useUserManagement(), {
      wrapper: Wrapper
    });
    await waitFor(() => expect(result.current.user).toBeNull());
  });
});
