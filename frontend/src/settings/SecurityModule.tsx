import { useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react';
import { ApiError } from '../api/errors';
import { useAuthState, useAuthStore } from '../auth/AuthProvider';
import type { DeviceSession } from '../auth/types';
import { copy } from '../copy';
import { Pill } from '../ui/Pill';
import { TextLink } from '../ui/TextLink';
import { useSettings } from './SettingsProvider';

type PasswordErrors = {
  readonly oldPassword: string | null;
  readonly newPassword: string | null;
};

const EMPTY_PASSWORD_ERRORS: PasswordErrors = { oldPassword: null, newPassword: null };

function isValidPassword(value: string): boolean {
  return value.length >= 8 && /[A-Za-z]/.test(value) && /\d/.test(value);
}

function sessionTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString('zh-CN');
}

export function SecurityModule() {
  const { api } = useSettings();
  const authStore = useAuthStore();
  const authState = useAuthState();
  const [sessions, setSessions] = useState<readonly DeviceSession[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [sessionsError, setSessionsError] = useState(false);
  const [sessionActionError, setSessionActionError] = useState<string | null>(null);
  const [sessionActionPending, setSessionActionPending] = useState<string | null>(null);
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [passwordErrors, setPasswordErrors] = useState<PasswordErrors>(EMPTY_PASSWORD_ERRORS);
  const [submittingPassword, setSubmittingPassword] = useState(false);

  // 会话 fence：capture 发起时的逻辑会话，响应落地时仍为当前会话才提交（A 的列表不在 B 显示）。
  const sessionsSeqRef = useRef(0);
  const activeSessionKeyRef = useRef<string | null>(null);
  const authSessionId = authStore.getAuthSessionId();
  const sessionKey =
    authState.status === 'authenticated' && authState.user !== null && authSessionId !== null
      ? `${authSessionId}:${authState.user.id}`
      : null;

  const loadSessions = useCallback(async () => {
    const seq = ++sessionsSeqRef.current;
    activeSessionKeyRef.current = sessionKey;
    setSessionsLoading(true);
    setSessionsError(false);
    try {
      const next = await authStore.listSessions();
      if (seq === sessionsSeqRef.current && activeSessionKeyRef.current === sessionKey) {
        setSessions(next);
      }
    } catch {
      if (seq === sessionsSeqRef.current) {
        setSessionsError(true);
      }
    } finally {
      if (seq === sessionsSeqRef.current) {
        setSessionsLoading(false);
      }
    }
  }, [authStore, sessionKey]);

  // 会话切换：立即清空账号相关 state（配合 DrawerHost 重挂载；此处兜底）。
  useEffect(() => {
    sessionsSeqRef.current += 1;
    activeSessionKeyRef.current = sessionKey;
    setSessions([]);
    setSessionsError(false);
    setSessionActionError(null);
    setSessionActionPending(null);
    if (sessionKey !== null) {
      void loadSessions();
    } else {
      setSessionsLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionKey]);

  function onOldPasswordChange(event: ChangeEvent<HTMLInputElement>): void {
    setOldPassword(event.target.value);
    setPasswordErrors((errors) => ({ ...errors, oldPassword: null }));
  }

  function onNewPasswordChange(event: ChangeEvent<HTMLInputElement>): void {
    setNewPassword(event.target.value);
    setPasswordErrors((errors) => ({ ...errors, newPassword: null }));
  }

  async function changePassword(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (submittingPassword) {
      return;
    }
    if (!isValidPassword(newPassword)) {
      setPasswordErrors({
        oldPassword: null,
        newPassword: copy.settings.security.invalidPasswordRule,
      });
      return;
    }

    setSubmittingPassword(true);
    setPasswordErrors(EMPTY_PASSWORD_ERRORS);
    // 捕获发起改密时的逻辑会话 identity：若响应延迟期间用户已 logout/login，不得清理新会话。
    const initiatedAuthSessionId = authStore.getAuthSessionId();
    try {
      await api.changePassword({ old_password: oldPassword, new_password: newPassword });
      // PUT /users/me/password already invalidated every server session; do not call DELETE /auth/sessions.
      authStore.handleServerAllSessionsRevoked(initiatedAuthSessionId);
    } catch (error) {
      if (error instanceof ApiError && error.status === 400 && error.code === 'invalid_password_rule') {
        setPasswordErrors({ oldPassword: null, newPassword: copy.settings.security.invalidPasswordRule });
      } else if (error instanceof ApiError && error.status === 403 && error.code === 'wrong_old_password') {
        setPasswordErrors({ oldPassword: copy.settings.security.wrongOldPassword, newPassword: null });
      } else {
        setPasswordErrors({ oldPassword: null, newPassword: copy.settings.security.passwordChangeError });
      }
    } finally {
      setSubmittingPassword(false);
    }
  }

  async function logoutCurrentDevice(): Promise<void> {
    if (sessionActionPending !== null) {
      return;
    }
    setSessionActionPending('current');
    setSessionActionError(null);
    try {
      await authStore.logout();
    } catch {
      setSessionActionError(copy.settings.security.sessionActionError);
    } finally {
      setSessionActionPending(null);
    }
  }

  async function revokeOtherDevice(session: DeviceSession): Promise<void> {
    if (sessionActionPending !== null) {
      return;
    }
    setSessionActionPending(session.id);
    setSessionActionError(null);
    try {
      await authStore.revokeSession(session.id, { current: false });
      setSessions((items) => items.filter((item) => item.id !== session.id));
    } catch {
      setSessionActionError(copy.settings.security.sessionActionError);
    } finally {
      setSessionActionPending(null);
    }
  }

  async function logoutAllDevices(): Promise<void> {
    if (sessionActionPending !== null) {
      return;
    }
    setSessionActionPending('all');
    setSessionActionError(null);
    try {
      await authStore.revokeAllSessions();
    } catch {
      setSessionActionError(copy.settings.security.sessionActionError);
    } finally {
      setSessionActionPending(null);
    }
  }

  const authenticated = authState.status === 'authenticated';
  const sessionActionsDisabled = !authenticated || sessionActionPending !== null;

  return (
    <section aria-label={copy.settings.security.sectionLabel} className="pb-10">
      <form onSubmit={(event) => void changePassword(event)} noValidate>
        <h2 className="text-subheading font-medium text-ink-black">{copy.settings.security.passwordTitle}</h2>
        <div className="mt-5">
          <label htmlFor="settings-old-password" className="mb-2 block text-caption text-slate-gray">
            {copy.settings.security.oldPasswordLabel}
          </label>
          <input
            id="settings-old-password"
            type="password"
            autoComplete="current-password"
            value={oldPassword}
            onChange={onOldPasswordChange}
            aria-invalid={passwordErrors.oldPassword !== null}
            className="h-10 w-full rounded-[var(--radius-inputs)] border border-[var(--color-hairline)] bg-paper-white px-3 text-body text-ink-black focus:border-ink-black"
          />
          {passwordErrors.oldPassword !== null && (
            <p role="alert" className="mt-2 text-caption text-danger">
              {passwordErrors.oldPassword}
            </p>
          )}
        </div>
        <div className="mt-5">
          <label htmlFor="settings-new-password" className="mb-2 block text-caption text-slate-gray">
            {copy.settings.security.newPasswordLabel}
          </label>
          <input
            id="settings-new-password"
            type="password"
            autoComplete="new-password"
            value={newPassword}
            onChange={onNewPasswordChange}
            aria-invalid={passwordErrors.newPassword !== null}
            className="h-10 w-full rounded-[var(--radius-inputs)] border border-[var(--color-hairline)] bg-paper-white px-3 text-body text-ink-black focus:border-ink-black"
          />
          <p className="mt-2 text-caption text-smoke-gray">{copy.settings.security.passwordRule}</p>
          {passwordErrors.newPassword !== null && (
            <p role="alert" className="mt-2 text-caption text-danger">
              {passwordErrors.newPassword}
            </p>
          )}
        </div>
        <Pill type="submit" loading={submittingPassword} className="mt-5">
          {copy.settings.security.changePassword}
        </Pill>
      </form>

      <div className="mt-12">
        <div className="flex items-center justify-between gap-4">
          <h2 className="text-subheading font-medium text-ink-black">{copy.settings.security.sessionsTitle}</h2>
          <TextLink
            disabled={sessionActionsDisabled}
            danger
            aria-busy={sessionActionPending === 'all' || undefined}
            onClick={() => void logoutAllDevices()}
          >
            {copy.settings.security.logoutAll}
          </TextLink>
        </div>
        {sessionActionError !== null && (
          <p role="alert" className="mt-4 text-caption text-danger">
            {sessionActionError}
          </p>
        )}
        {sessionsLoading ? (
          <p className="mt-4 text-caption text-smoke-gray">{copy.settings.security.sessionsLoading}</p>
        ) : sessionsError ? (
          <div className="mt-4">
            <p role="alert" className="text-caption text-danger">
              {copy.settings.security.sessionsError}
            </p>
            <TextLink className="mt-2" onClick={() => void loadSessions()}>
              {copy.states.retry}
            </TextLink>
          </div>
        ) : (
          <ul className="mt-4 divide-y divide-[var(--color-hairline)]">
            {sessions.map((session) => (
              <li key={session.id} className="flex items-center justify-between gap-4 py-4">
                <div>
                  <p className="text-body text-ink-black">{session.device}</p>
                  <p className="mt-1 text-caption text-slate-gray">
                    {copy.settings.security.lastActiveAt(sessionTime(session.last_active_at))}
                  </p>
                  {session.current && (
                    <span className="mt-2 inline-block text-caption text-success">
                      {copy.settings.security.currentDevice}
                    </span>
                  )}
                </div>
                {session.current ? (
                  <TextLink
                    disabled={sessionActionsDisabled}
                    danger
                    aria-busy={sessionActionPending === 'current' || undefined}
                    onClick={() => void logoutCurrentDevice()}
                  >
                    {copy.settings.security.logoutCurrent}
                  </TextLink>
                ) : (
                  <TextLink
                    disabled={sessionActionsDisabled}
                    danger
                    aria-busy={sessionActionPending === session.id || undefined}
                    onClick={() => void revokeOtherDevice(session)}
                  >
                    {copy.settings.security.logoutOther}
                  </TextLink>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
