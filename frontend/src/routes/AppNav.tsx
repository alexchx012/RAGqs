import React from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
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
      <div className="app-nav-brand" aria-hidden="true">
        <span className="app-nav-mark" />
        <span className="app-nav-title">RAGqs</span>
      </div>
      <div className="app-nav-tabs" role="list">
        <NavLink to="/chat" role="listitem">
          聊天
        </NavLink>
        <NavLink to="/knowledge" role="listitem">
          知识库
        </NavLink>
        {isAdmin && (
          <NavLink to="/admin/projects" role="listitem">
            项目管理
          </NavLink>
        )}
      </div>
      <div className="app-nav-actions">
        <button
          type="button"
          className="app-nav-logout"
          onClick={() => void handleLogout()}
          data-testid="logout-button"
        >
          登出
        </button>
      </div>
    </nav>
  );
}
