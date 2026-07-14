import React, { useState } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';
import { useAuth } from './AuthContext';
import { ApiError } from '../../api/client';

export default function LoginPage() {
  const { status, login } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (status === 'loading') {
    return (
      <div className="auth-loading" data-testid="auth-loading">
        加载中...
      </div>
    );
  }
  if (status === 'authenticated') {
    return <Navigate to="/chat" replace />;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(username, password);
      navigate('/chat', { replace: true });
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 401) {
        setError('用户名或密码错误');
      } else {
        setError('登录失败，请重试');
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-page" data-testid="login-page">
      <form className="login-form" onSubmit={handleSubmit}>
        <h1>登录</h1>
        <label>
          用户名
          <input
            name="username"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
          />
        </label>
        <label>
          密码
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        {error && (
          <div className="login-error" role="alert">
            {error}
          </div>
        )}
        <button type="submit" disabled={submitting || !username || !password}>
          {submitting ? '登录中...' : '登录'}
        </button>
      </form>
    </div>
  );
}
