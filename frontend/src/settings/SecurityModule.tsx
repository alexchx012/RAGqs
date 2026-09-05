import { useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react';
import { ApiError } from '../api/errors';
import { useAuthState, useAuthStore } from '../auth/AuthProvider';
import type { DeviceSession } from '../auth/types';
import { copy } from '../copy';
import { EyeIcon, EyeOffIcon } from '../pages/login/LoginPage';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import { Pill } from '../ui/Pill';
import { Switch } from '../ui/Switch';
import { TextLink } from '../ui/TextLink';
import { useSettings } from './SettingsProvider';
import { usePreferences } from './use-preferences';

type PasswordErrors = {
  readonly oldPassword: string | null;
  readonly newPassword: string | null;
  readonly confirmPassword: string | null;
};

const EMPTY_PASSWORD_ERRORS: PasswordErrors = { oldPassword: null, newPassword: null, confirmPassword: null };

function isValidPassword(value: string): boolean {
  return value.length >= 8 && /[A-Za-z]/.test(value) && /\d/.test(value);
}

function sessionTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString('zh-CN');
}

/** 密码框可见性切换（A37）：24px 视觉钮 + ui-touch-target 外扩至 44px 命中区（A49）。 */
function PasswordVisibilityToggle({
  visible,
  onToggle,
}: {
  readonly visible: boolean;
  readonly onToggle: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={visible ? copy.login.hidePassword : copy.login.showPassword}
      aria-pressed={visible}
      onClick={onToggle}
      className="ui-touch-target absolute top-1/2 right-2 flex h-6 w-6 -translate-y-1/2 items-center
        justify-center rounded-[var(--radius-images)] text-slate-gray transition-colors
        duration-[var(--duration-fast)] hover:text-ink-black [--touch-expand:-10px]"
    >
      <span key={visible ? 'hide' : 'show'} className="block h-4 w-4">
        {visible ? <EyeOffIcon /> : <EyeIcon />}
      </span>
    </button>
  );
}

export function SecurityModule() {
  const { api } = useSettings();
  const authStore = useAuthStore();
  const authState = useAuthState();
  // 隐私区卡（共用基座 §5.4）：ab_opt_out 开关读写偏好，与外观模块共用同一套偏好机制。
  const preferencesSync = usePreferences();
  const [sessions, setSessions] = useState<readonly DeviceSession[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [sessionsError, setSessionsError] = useState(false);
  const [sessionActionError, setSessionActionError] = useState<string | null>(null);
  const [sessionActionPending, setSessionActionPending] = useState<string | null>(null);
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [passwordErrors, setPasswordErrors] = useState<PasswordErrors>(EMPTY_PASSWORD_ERRORS);
  const [submittingPassword, setSubmittingPassword] = useState(false);
  // A37：新旧密码框可见性切换
  const [showOldPassword, setShowOldPassword] = useState(false);
  const [showNewPassword, setShowNewPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  // A38：「退出全部设备」二次确认（danger ConfirmDialog）
  const [pendingLogoutAll, setPendingLogoutAll] = useState(false);

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
  }, [sessionKey]);

  function onOldPasswordChange(event: ChangeEvent<HTMLInputElement>): void {
    setOldPassword(event.target.value);
    setPasswordErrors((errors) => ({ ...errors, oldPassword: null }));
  }

  function onNewPasswordChange(event: ChangeEvent<HTMLInputElement>): void {
    setNewPassword(event.target.value);
    setPasswordErrors((errors) => ({ ...errors, newPassword: null }));
  }

  function onConfirmPasswordChange(event: ChangeEvent<HTMLInputElement>): void {
    setConfirmPassword(event.target.value);
    setPasswordErrors((errors) => ({ ...errors, confirmPassword: null }));
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
        confirmPassword: null,
      });
      return;
    }
    if (newPassword !== confirmPassword) {
      setPasswordErrors({
        oldPassword: null,
        newPassword: null,
        confirmPassword: copy.settings.security.passwordMismatch,
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
        setPasswordErrors({ ...EMPTY_PASSWORD_ERRORS, newPassword: copy.settings.security.invalidPasswordRule });
      } else if (error instanceof ApiError && error.status === 403 && error.code === 'wrong_old_password') {
        setPasswordErrors({ ...EMPTY_PASSWORD_ERRORS, oldPassword: copy.settings.security.wrongOldPassword });
      } else {
        setPasswordErrors({ ...EMPTY_PASSWORD_ERRORS, newPassword: copy.settings.security.passwordChangeError });
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
      // A38：确认后无论成败都收起确认框（失败错误行在本层展示，不被确认框遮盖）
      setPendingLogoutAll(false);
    }
  }

  const authenticated = authState.status === 'authenticated';
  const sessionActionsDisabled = !authenticated || sessionActionPending !== null;

  const toggleAbOptOut = (checked: boolean) => {
    const { preferences, saving, save } = preferencesSync;
    if (preferences === null || saving || checked === preferences.ab_opt_out) {
      return;
    }
    save({ ...preferences, ab_opt_out: checked });
  };

  return (
    <section aria-label={copy.settings.security.sectionLabel} className="pb-10">
      <form onSubmit={(event) => void changePassword(event)} noValidate>
        <h2 className="text-subheading font-medium text-ink-black">{copy.settings.security.passwordTitle}</h2>
        <div className="mt-5">
          <label htmlFor="settings-old-password" className="mb-2 block text-caption text-slate-strong">
            {copy.settings.security.oldPasswordLabel}
          </label>
          <div className="relative">
            <input
              id="settings-old-password"
              type={showOldPassword ? 'text' : 'password'}
              autoComplete="current-password"
              value={oldPassword}
              onChange={onOldPasswordChange}
              aria-invalid={passwordErrors.oldPassword !== null}
              className="h-10 w-full rounded-[var(--radius-inputs)] border border-[var(--color-hairline)] bg-paper-white px-3 pr-10 text-body text-ink-black focus:border-ink-black"
            />
            <PasswordVisibilityToggle
              visible={showOldPassword}
              onToggle={() => setShowOldPassword((value) => !value)}
            />
          </div>
          {passwordErrors.oldPassword !== null && (
            <p role="alert" className="mt-2 text-caption text-danger">
              {passwordErrors.oldPassword}
            </p>
          )}
        </div>
        <div className="mt-5">
          <label htmlFor="settings-new-password" className="mb-2 block text-caption text-slate-strong">
            {copy.settings.security.newPasswordLabel}
          </label>
          <div className="relative">
            <input
              id="settings-new-password"
              type={showNewPassword ? 'text' : 'password'}
              autoComplete="new-password"
              value={newPassword}
              onChange={onNewPasswordChange}
              aria-invalid={passwordErrors.newPassword !== null}
              className="h-10 w-full rounded-[var(--radius-inputs)] border border-[var(--color-hairline)] bg-paper-white px-3 pr-10 text-body text-ink-black focus:border-ink-black"
            />
            <PasswordVisibilityToggle
              visible={showNewPassword}
              onToggle={() => setShowNewPassword((value) => !value)}
            />
          </div>
          <p className="mt-2 text-caption text-slate-strong">{copy.settings.security.passwordRule}</p>
          {passwordErrors.newPassword !== null && (
            <p role="alert" className="mt-2 text-caption text-danger">
              {passwordErrors.newPassword}
            </p>
          )}
        </div>
        <div className="mt-5">
          <label htmlFor="settings-confirm-password" className="mb-2 block text-caption text-slate-strong">
            {copy.settings.security.confirmPasswordLabel}
          </label>
          <div className="relative">
            <input
              id="settings-confirm-password"
              type={showConfirmPassword ? 'text' : 'password'}
              autoComplete="new-password"
              value={confirmPassword}
              onChange={onConfirmPasswordChange}
              aria-invalid={passwordErrors.confirmPassword !== null}
              className="h-10 w-full rounded-[var(--radius-inputs)] border border-[var(--color-hairline)] bg-paper-white px-3 pr-10 text-body text-ink-black focus:border-ink-black"
            />
            <PasswordVisibilityToggle
              visible={showConfirmPassword}
              onToggle={() => setShowConfirmPassword((value) => !value)}
            />
          </div>
          {passwordErrors.confirmPassword !== null && (
            <p role="alert" className="mt-2 text-caption text-danger">
              {passwordErrors.confirmPassword}
            </p>
          )}
        </div>
        <Pill type="submit" loading={submittingPassword} className="mt-5">
          {copy.settings.security.changePassword}
        </Pill>
        {/* A37：提交区固定注明全设备退出（与改密的服务端行为一致） */}
        <p className="mt-3 text-caption text-slate-strong">
          {copy.settings.security.passwordSessionNote}
        </p>
      </form>

      <div className="mt-12">
        <div className="flex items-center justify-between gap-4">
          <h2 className="text-subheading font-medium text-ink-black">{copy.settings.security.sessionsTitle}</h2>
          <TextLink
            disabled={sessionActionsDisabled}
            danger
            aria-busy={sessionActionPending === 'all' || undefined}
            onClick={() => setPendingLogoutAll(true)}
          >
            {copy.settings.security.logoutAll}
          </TextLink>
        </div>
        {/* A38：退出全部设备 = 撤销含当前设备在内的全部会话，danger 二次确认后再执行 */}
        <ConfirmDialog
          open={pendingLogoutAll}
          confirming={sessionActionPending === 'all'}
          onOpenChange={(open) => {
            if (!open) {
              setPendingLogoutAll(false);
            }
          }}
          title={copy.settings.security.logoutAllConfirmTitle}
          description={copy.settings.security.logoutAllConfirmDescription}
          confirmLabel={copy.settings.security.logoutAll}
          danger
          onConfirm={() => void logoutAllDevices()}
        />
        {sessionActionError !== null && (
          <p role="alert" className="mt-4 text-caption text-danger">
            {sessionActionError}
          </p>
        )}
        {sessionsLoading ? (
          <p className="mt-4 text-caption text-slate-strong">{copy.settings.security.sessionsLoading}</p>
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
                  <p className="mt-1 text-caption text-slate-strong">
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

      {/* 隐私区卡（共用基座 §5.4）：标题 + 说明 + 开关；读写 ab_opt_out 偏好字段，语义不变 */}
      <section className="mt-12" aria-labelledby="settings-security-privacy">
        <h2 id="settings-security-privacy" className="text-subheading font-medium text-ink-black">
          {copy.settings.security.privacyTitle}
        </h2>
        {preferencesSync.loading ? (
          <p role="status" className="mt-4 text-caption text-slate-strong">
            {copy.settings.security.preferencesLoading}
          </p>
        ) : preferencesSync.loadError ? (
          <div className="mt-4 flex items-center gap-3">
            <p role="alert" className="text-caption text-danger">
              {copy.settings.security.preferencesLoadError}
            </p>
            <TextLink onClick={preferencesSync.reload}>{copy.states.retry}</TextLink>
          </div>
        ) : (
          preferencesSync.preferences !== null && (
            <div className="mt-4 flex items-start justify-between gap-6">
              <div>
                <p className="text-body text-ink-black">{copy.settings.security.abOptOutLabel}</p>
                <p className="mt-2 max-w-[520px] text-caption text-slate-strong">
                  {copy.settings.security.abOptOutDescription}
                </p>
              </div>
              <Switch
                checked={preferencesSync.preferences.ab_opt_out}
                onCheckedChange={toggleAbOptOut}
                disabled={preferencesSync.saving}
                ariaLabel={copy.settings.security.abOptOutLabel}
              />
            </div>
          )
        )}
        {preferencesSync.saveError && (
          <p role="alert" className="mt-4 text-caption text-danger">
            {copy.settings.security.preferencesSaveError}
          </p>
        )}
      </section>
    </section>
  );
}
