import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import { useNavigate } from 'react-router-dom';
import { apiJson, ApiError, registerUnauthorizedHandler } from '../../api/client';
import type { AuthMeData } from '../../api/types';

export type AuthStatus = 'loading' | 'authenticated' | 'unauthenticated' | 'error';

export interface AuthContextValue {
  status: AuthStatus;
  userId: string | null;
  roles: string[];
  spaces: string[];
  errorMessage: string | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function applyIdentity(
  data: AuthMeData | undefined,
  setUserId: (v: string | null) => void,
  setRoles: (v: string[]) => void,
  setSpaces: (v: string[]) => void,
) {
  setUserId(data?.user_id ?? null);
  setRoles(Array.isArray(data?.roles) ? data!.roles : []);
  setSpaces(Array.isArray(data?.spaces) ? data!.spaces : []);
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const navigate = useNavigate();
  const [status, setStatus] = useState<AuthStatus>('loading');
  const [userId, setUserId] = useState<string | null>(null);
  const [roles, setRoles] = useState<string[]>([]);
  const [spaces, setSpaces] = useState<string[]>([]);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setStatus('loading');
    setErrorMessage(null);
    try {
      const res = await apiJson<AuthMeData>('/auth/me', undefined, {
        skipUnauthorizedHandler: true,
      });
      applyIdentity(res.data, setUserId, setRoles, setSpaces);
      setStatus('authenticated');
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 401) {
        setUserId(null);
        setRoles([]);
        setSpaces([]);
        setStatus('unauthenticated');
        return;
      }
      setErrorMessage(err instanceof Error ? err.message : '登录态探测失败');
      setStatus('error');
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    registerUnauthorizedHandler(() => {
      setUserId(null);
      setRoles([]);
      setSpaces([]);
      setStatus('unauthenticated');
      navigate('/login', { replace: true });
    });
    return () => registerUnauthorizedHandler(null);
  }, [navigate]);

  const login = useCallback(async (username: string, password: string) => {
    // 故意不用 apiJson，避免 401 触发全局跳转死循环
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (res.status === 401) {
      throw new ApiError('用户名或密码错误', 401);
    }
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        detail = body.detail || body.message || detail;
      } catch {
        /* ignore */
      }
      throw new ApiError(detail, res.status);
    }
    const body = await res.json();
    const data = body?.data as AuthMeData | undefined;
    if (!data || typeof data.user_id !== 'string' || !data.user_id) {
      throw new ApiError('登录响应缺少用户信息', res.status);
    }
    applyIdentity(data, setUserId, setRoles, setSpaces);
    setErrorMessage(null);
    setStatus('authenticated');
  }, []);

  const logout = useCallback(async () => {
    try {
      await apiJson('/auth/logout', { method: 'POST' });
    } catch {
      /* 无论成败都清本地态 */
    } finally {
      setUserId(null);
      setRoles([]);
      setSpaces([]);
      setStatus('unauthenticated');
    }
  }, []);

  const value = useMemo(
    () => ({ status, userId, roles, spaces, errorMessage, login, logout, refresh }),
    [status, userId, roles, spaces, errorMessage, login, logout, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}
