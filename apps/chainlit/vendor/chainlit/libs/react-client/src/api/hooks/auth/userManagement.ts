import { useEffect } from 'react';
import { IUser } from 'src/types';

import { useApi } from '../api';
import { useAuthState } from './state';

export const useUserManagement = () => {
  const { user, setUser } = useAuthState();

  const {
    data: userData,
    error,
    mutate: setUserFromAPI
  } = useApi<IUser>('/user');

  useEffect(() => {
    if (userData) {
      setUser(userData);
    }
  }, [userData, setUser]);

  useEffect(() => {
    // A failed revalidation can be a temporary network or server error.
    // Only an explicit authentication rejection invalidates the user.
    if (error && 'status' in error && error.status === 401) {
      setUser(null);
    }
  }, [error, setUser]);

  return { user, setUserFromAPI };
};
