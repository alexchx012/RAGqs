import React from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../features/auth/AuthContext';

export default function AppNav() {
  const { roles, logout } = useAuth();
  const navigate = useNavigate();
  const isAdmin = roles.includes('admin');

  const handleLogout = async () => {
    await logout();
    navigate('/login', { replace: true });
  };

  return (
    <nav className="app-nav" data-testid="app-nav">
      <Link to="/chat">聊天</Link>
      <Link to="/knowledge">知识库</Link>
      {isAdmin && <Link to="/admin/projects">项目管理</Link>}
      <button type="button" onClick={() => void handleLogout()} data-testid="logout-button">
        登出
      </button>
    </nav>
  );
}
