/*
 * 安全设置：三张设置卡片（修改密码 / 活跃会话 / 隐私），共用 SettingsCard + FormRow + FormFooter 基座。
 *
 * 三条语义边界（本模块的核心）：
 * - 密码字段 = 显式提交：输入只改本地 state，点「修改密码」才发请求，校验失败就地提示。三个字段不进
 *   useDraftForm——页脚的「保存」只属于隐私偏好草稿，改密是事务性提交（成功后全部会话失效），两者不同。
 * - 退出类动作（退出全部设备 / 退出此设备 / 退出登录）= 即时动作：点击即执行（退出全部设备沿用 A38 的
 *   danger 二次确认），不进草稿、不使表单 dirty、不因「取消」而撤销。文字色走 text-danger——抽屉作用域
 *   把它解析为 #D64545（全局仍是旧值），故不写死 hex。
 * - 隐私开关 = 草稿-保存：接 useDraftForm，「保存」才经 usePreferences 写偏好，「取消」丢弃草稿。
 *
 * 组装约束：每组 FormRow 由一层容器承载 [&>*:last-child]:border-b-0 关闭末行分隔线（FormRow 始终渲染
 * border-b，自己不做「是否最后一行」判断）；该规则不得加在卡片上——隐私卡里 FormFooter 是最后一个子节点，
 * 加在卡片上命中的会是页脚。FormFooter 必须是 SettingsCard 的直接子节点且留在最后（负外边距抵消卡片
 * p-8）。条件渲染的元素（保存失败提示、加载/错误行、动作错误行）都放在行容器之外，避免成为容器的
 * :last-child。
 *
 * submitted 取 usePreferences 的快照 state（稳定引用）：传真值字面量会让每次渲染都换身份，useDraftForm
 * 见 submitted 变化即重置草稿 → 改草稿 → 重渲染 → 再重置，足以进入渲染死循环（Task 8 实测：用例挂住、
 * 0 条执行）。真实保存响应每次都是新对象（api/client.ts 的 `response.json()`），保存成功后 submitted
 * 换身份是预期行为，草稿据此重置为服务端快照。
 *
 * 眼睛图标沿用归档 change `fix-settings-scroll-and-password-eye` 的实现与可访问名约定（24px 视觉钮 +
 * ui-touch-target 外扩至 44px 命中区；aria-label 在「显示密码 / 隐藏密码」之间切换），不另起第二套。
 */
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
  type ReactNode,
} from 'react';
import { ApiError } from '../api/errors';
import { useAuthState, useAuthStore } from '../auth/AuthProvider';
import type { DeviceSession } from '../auth/types';
import { copy } from '../copy';
import { EyeIcon, EyeOffIcon } from '../pages/login/LoginPage';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import { FormFooter } from '../ui/FormFooter';
import { FormRow } from '../ui/FormRow';
import { Pill } from '../ui/Pill';
import { SettingsCard } from '../ui/SettingsCard';
import { Switch } from '../ui/Switch';
import { TextLink } from '../ui/TextLink';
import { useSettings } from './SettingsProvider';
import { useDraftForm } from './use-draft-form';
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
      aria-label={visible ? copy.settings.security.hidePassword : copy.settings.security.showPassword}
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

/*
 * 保存期禁用外壳（R11 契约）：纳入草稿的控件在保存进行中必须禁用，且禁用态可见。
 * fieldset 原生 disabled 让后代控件不可交互；disabled:opacity-60 + disabled:cursor-not-allowed 让禁用态
 * 可见（cursor 可继承，开关本身也拿到 not-allowed）。不写 enabled:opacity-100：浏览器与 Tailwind
 * preflight 都不给 disabled fieldset 加 opacity:0，该值本就是初始值 1，加上去只是重复声明（Task 8 据此
 * 写错了因果，已被审查证伪）。与常规设置同形但留在本模块内：抽成共享原语要改另一个模块，超出本任务范围。
 */
function DraftFieldSet({ saving, children }: { saving: boolean; children: ReactNode }) {
  return (
    <fieldset
      disabled={saving}
      className="m-0 min-w-0 border-0 p-0 disabled:cursor-not-allowed disabled:opacity-60"
    >
      {children}
    </fieldset>
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

  // 隐私草稿：唯一纳入草稿-保存的字段。submitted 见文件头——内联字面量会进入渲染死循环。
  const { draft, set, reset, commit } = useDraftForm(preferencesSync.preferences, preferencesSync.save);

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

  return (
    <section className="flex flex-col gap-12 pb-10">
      {/* 修改密码卡：三个密码字段是显式提交，不纳入草稿；页脚「保存」只属于隐私草稿 */}
      <SettingsCard ariaLabel={copy.settings.security.passwordTitle}>
        <h2 className="text-subheading font-medium text-ink-black">
          {copy.settings.security.passwordTitle}
        </h2>
        <form onSubmit={(event) => void changePassword(event)} noValidate>
          <div className="[&>*:last-child]:border-b-0">
            <FormRow label={copy.settings.security.oldPasswordLabel} htmlFor="settings-old-password">
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
            </FormRow>

            <FormRow label={copy.settings.security.newPasswordLabel} htmlFor="settings-new-password">
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
              {/* 规则说明跟在框下方（不走 FormRow 的 description：那是左列灰色说明位） */}
              <p className="mt-2 text-caption text-slate-strong">{copy.settings.security.passwordRule}</p>
              {passwordErrors.newPassword !== null && (
                <p role="alert" className="mt-2 text-caption text-danger">
                  {passwordErrors.newPassword}
                </p>
              )}
            </FormRow>

            <FormRow
              label={copy.settings.security.confirmPasswordLabel}
              htmlFor="settings-confirm-password"
            >
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
            </FormRow>
          </div>

          <div className="flex flex-col gap-3 pt-5 md:flex-row md:items-center md:justify-between">
            {/* A37：提交区固定注明全设备退出（与改密的服务端行为一致） */}
            <p className="text-caption text-slate-strong">
              {copy.settings.security.passwordSessionNote}
            </p>
            <Pill type="submit" loading={submittingPassword}>
              {copy.settings.security.changePassword}
            </Pill>
          </div>
        </form>
      </SettingsCard>

      {/* 活跃会话卡：每会话一行（FormRow），行尾退出动作是即时动作 */}
      <SettingsCard ariaLabel={copy.settings.security.sessionsTitle}>
        <h2 className="text-subheading font-medium text-ink-black">
          {copy.settings.security.sessionsTitle}
        </h2>
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
          <div className="[&>*:last-child]:border-b-0">
            {sessions.map((session) => (
              <FormRow
                key={session.id}
                label={session.device}
                description={copy.settings.security.lastActiveAt(sessionTime(session.last_active_at))}
              >
                <div className="flex items-center justify-end gap-3">
                  {session.current && (
                    <span className="text-caption text-success">
                      {copy.settings.security.currentDevice}
                    </span>
                  )}
                  {session.current ? (
                    <TextLink
                      danger
                      disabled={sessionActionsDisabled}
                      aria-busy={sessionActionPending === 'current' || undefined}
                      onClick={() => void logoutCurrentDevice()}
                    >
                      {copy.settings.security.logoutCurrent}
                    </TextLink>
                  ) : (
                    <TextLink
                      danger
                      disabled={sessionActionsDisabled}
                      aria-busy={sessionActionPending === session.id || undefined}
                      onClick={() => void revokeOtherDevice(session)}
                    >
                      {copy.settings.security.logoutOther}
                    </TextLink>
                  )}
                </div>
              </FormRow>
            ))}
          </div>
        )}
        {sessionActionError !== null && (
          <p role="alert" className="pt-4 text-caption text-danger">
            {sessionActionError}
          </p>
        )}
        {/* 卡底部「退出全部设备」：即时动作，A38 危险确认后再执行 */}
        <div className="flex justify-end pt-4">
          <TextLink
            danger
            disabled={sessionActionsDisabled}
            aria-busy={sessionActionPending === 'all' || undefined}
            onClick={() => setPendingLogoutAll(true)}
          >
            {copy.settings.security.logoutAll}
          </TextLink>
        </div>
      </SettingsCard>

      {/* 隐私卡：唯一纳入草稿的字段；页脚「取消 / 保存」只作用于它 */}
      <SettingsCard ariaLabel={copy.settings.security.privacyTitle}>
        <h2 className="text-subheading font-medium text-ink-black">
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
          draft !== null && (
            <>
              <div className="[&>*:last-child]:border-b-0">
                <FormRow
                  label={copy.settings.security.abOptOutLabel}
                  description={copy.settings.security.abOptOutDescription}
                >
                  <div className="flex justify-end">
                    <DraftFieldSet saving={preferencesSync.saving}>
                      <Switch
                        checked={draft.ab_opt_out}
                        onCheckedChange={(checked) => set({ ab_opt_out: checked })}
                        disabled={preferencesSync.saving}
                        ariaLabel={copy.settings.security.abOptOutLabel}
                      />
                    </DraftFieldSet>
                  </div>
                </FormRow>
              </div>

              {preferencesSync.saveError && (
                <p role="alert" className="pt-4 text-caption text-danger">
                  {copy.settings.security.preferencesSaveError}
                </p>
              )}

              {/* FormFooter 是卡片的直接子节点且为最后一个子节点（负外边距抵消卡片 p-8） */}
              <FormFooter
                onCancel={reset}
                onSave={commit}
                saving={preferencesSync.saving}
              />
            </>
          )
        )}
      </SettingsCard>

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
    </section>
  );
}
