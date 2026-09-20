/*
 * 安全设置：整页**一张** SettingsCard（R13/R14 裁决，按新设计图像素实测），共用 SettingsCard + FormRow +
 * FormFooter 基座。设计图是**连续的两栏表单行序列、没有小节标题**，故卡内自上而下就是：
 * 当前密码 / 新密码 / 再次输入新密码 → 活跃会话（右列是「退出全部设备」）→ 各设备会话行 → 隐私（右列是开关）。
 *
 * 三条语义边界（本模块的核心）：
 * - 密码字段 = 显式提交的命令：输入只改本地 state，页脚「保存」才发请求（校验失败就地提示）。
 *   密码不是偏好（提交后会撤销其它会话），故不进 useDraftForm。
 * - 退出类动作（退出全部设备 / 退出此设备 / 退出登录）= 即时动作：点击即执行（退出全部设备沿用 A38 的
 *   danger 二次确认），不进草稿、不使表单 dirty、不因「取消」而撤销。文字色走 text-danger——抽屉作用域
 *   把它解析为 #D64545（全局仍是旧值），故不写死 hex。
 * - 隐私开关 = 草稿-保存：接 useDraftForm，页脚「保存」才经 usePreferences 写偏好，「取消」丢弃草稿。
 *
 * 页脚提交语义（R13）：一个页脚提交本页可提交的全部内容——密码字段非空则走改密，隐私草稿有改动则写偏好
 * （useDraftForm.commit 自带 dirty 门槛），两者都没有则 no-op；「取消」清空三个密码字段（含就地错误行）
 * 并丢弃隐私草稿。正在提交（改密或偏好任一在途）时两个键都禁用。A37 的「改密后全设备退出」固定说明也
 * 落在页脚（提交区）内：设计图的行序列里没有它这一行。
 *
 * 组装约束：全部行由**同一层**容器承载 [&>*:last-child]:border-b-0，关闭的正是卡片最后一行（隐私行）的
 * 分隔线；该规则不得加在卡片上——卡片的直接末子节点是 FormFooter，加在卡片上命中的会是页脚。FormFooter
 * 必须是卡片的直接子节点且留在最后（负外边距抵消卡片 p-8），因此它不能放进 <form>：卡内只给密码三行套
 * 一个 <form>（Enter 隐式提交的载体），form 是行容器的第一个子节点，三行各自的 border-b 保留。
 * 条件渲染的元素一律不当行容器的直接子节点：会话区的加载/错误/动作错误渲染在「活跃会话」行的右列内，
 * 隐私区的加载/加载失败/保存失败渲染在行容器之外、页脚之上——否则它们会抢走末行的 :last-child。
 *
 * submitted 取 usePreferences 的快照 state（稳定引用）：传真值字面量会让每次渲染都换身份，useDraftForm
 * 见 submitted 变化即重置草稿 → 改草稿 → 重渲染 → 再重置，足以进入渲染死循环（Task 8 实测：用例挂住、
 * 0 条执行）。真实保存响应每次都是新对象（api/client.ts 的 `response.json()`），保存成功后 submitted
 * 换身份是预期行为，草稿据此重置为服务端快照。
 *
 * 眼睛图标沿用归档 change `fix-settings-scroll-and-password-eye` 的实现（24px 视觉钮 + ui-touch-target
 * 外扩至 44px 命中区、aria-pressed 反映是否明文）；可访问名按 R13 改为字段限定（显示/隐藏当前密码、
 * 新密码、确认新密码）——同页三个同名按钮读屏无法区分。
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
  showLabel,
  hideLabel,
}: {
  readonly visible: boolean;
  readonly onToggle: () => void;
  /** 按字段限定的可访问名（R13）：同页三个眼睛按钮必须各自唯一。 */
  readonly showLabel: string;
  readonly hideLabel: string;
}) {
  return (
    <button
      type="button"
      aria-label={visible ? hideLabel : showLabel}
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
  // 隐私区（共用基座 §5.4）：ab_opt_out 开关读写偏好，与外观模块共用同一套偏好机制。
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
  // A37：三个密码框可见性切换
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

  function clearPasswordErrors(): void {
    setPasswordErrors(EMPTY_PASSWORD_ERRORS);
  }

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

  /** 改密的显式提交：本地校验 → 请求 → 成功即清理全部服务端会话（既有语义原样保留）。 */
  async function submitPasswordChange(): Promise<void> {
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
    clearPasswordErrors();
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
  const pageSaving = submittingPassword || preferencesSync.saving;

  /**
   * 页脚「保存」：提交本页可提交的内容。密码字段非空即走改密（校验失败就地报错、不发请求），
   * 隐私草稿有改动即写偏好（commit 自带 dirty 门槛）；两者都没有则 no-op。
   */
  function savePage(): void {
    if (pageSaving) {
      return;
    }
    if (oldPassword !== '' || newPassword !== '' || confirmPassword !== '') {
      void submitPasswordChange();
    }
    commit();
  }

  /** 页脚「取消」：清空三个密码字段（含就地错误行）并丢弃隐私草稿。 */
  function cancelPage(): void {
    setOldPassword('');
    setNewPassword('');
    setConfirmPassword('');
    clearPasswordErrors();
    reset();
  }

  return (
    <section className="pb-10">
      <SettingsCard ariaLabel={copy.settings.security.sectionLabel}>
        {/* 全部行由这一层容器承载：末行分隔线（隐私行的 border-b）由它的 :last-child 规则关闭。
            条件渲染的元素都不当这个容器的直接子节点，见文件头。 */}
        <div className="[&>*:last-child]:border-b-0">
          {/* 密码三行套 <form>（Enter 隐式提交的载体）：form 是行容器的第一个子节点，
              三行各自的 border-b 保留，末行规则只作用于容器的最后一个子节点。 */}
          <form
            onSubmit={(event: FormEvent<HTMLFormElement>) => {
              event.preventDefault();
              void submitPasswordChange();
            }}
            noValidate
          >
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
                  showLabel={copy.settings.security.showOldPassword}
                  hideLabel={copy.settings.security.hideOldPassword}
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
                  showLabel={copy.settings.security.showNewPassword}
                  hideLabel={copy.settings.security.hideNewPassword}
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
                  showLabel={copy.settings.security.showConfirmPassword}
                  hideLabel={copy.settings.security.hideConfirmPassword}
                />
              </div>
              {passwordErrors.confirmPassword !== null && (
                <p role="alert" className="mt-2 text-caption text-danger">
                  {passwordErrors.confirmPassword}
                </p>
              )}
            </FormRow>
            {/* 可见提交键在卡片页脚；这里保留隐藏的默认提交键，使密码框内按 Enter 仍走隐式提交 */}
            <button type="submit" className="hidden" />
          </form>

          {/* 活跃会话行：右列是「退出全部设备」即时动作；会话区的三条状态行渲染在本行右列内，
              避免成为行容器的直接子节点（那会抢走末行的分隔线关闭规则）。 */}
          <FormRow label={copy.settings.security.sessionsTitle}>
            <div className="flex flex-col items-end gap-2">
              <TextLink
                danger
                disabled={sessionActionsDisabled}
                aria-busy={sessionActionPending === 'all' || undefined}
                onClick={() => setPendingLogoutAll(true)}
              >
                {copy.settings.security.logoutAll}
              </TextLink>
              {sessionsLoading && (
                <p className="text-caption text-slate-strong">{copy.settings.security.sessionsLoading}</p>
              )}
              {!sessionsLoading && sessionsError && (
                <div className="flex items-center gap-3">
                  <p role="alert" className="text-caption text-danger">
                    {copy.settings.security.sessionsError}
                  </p>
                  <TextLink onClick={() => void loadSessions()}>{copy.states.retry}</TextLink>
                </div>
              )}
              {sessionActionError !== null && (
                <p role="alert" className="text-caption text-danger">
                  {sessionActionError}
                </p>
              )}
            </div>
          </FormRow>

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

          {/* 隐私行：草稿就绪后才渲染（也是本卡的末行，末行分隔线由外层容器关闭） */}
          {draft !== null && (
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
          )}
        </div>

        {/* 行容器之外：隐私区的加载/加载失败/保存失败提示（不得成为行容器的子节点） */}
        {preferencesSync.loading && (
          <p role="status" className="pt-4 text-caption text-slate-strong">
            {copy.settings.security.preferencesLoading}
          </p>
        )}
        {!preferencesSync.loading && preferencesSync.loadError && (
          <div className="pt-4 flex items-center gap-3">
            <p role="alert" className="text-caption text-danger">
              {copy.settings.security.preferencesLoadError}
            </p>
            <TextLink onClick={preferencesSync.reload}>{copy.states.retry}</TextLink>
          </div>
        )}
        {preferencesSync.saveError && (
          <p role="alert" className="pt-4 text-caption text-danger">
            {copy.settings.security.preferencesSaveError}
          </p>
        )}

        {/* FormFooter 是卡片的直接子节点且为最后一个子节点（负外边距抵消卡片 p-8）。
            A37 的固定说明经 statusSlot 落在提交区那一行的左侧，不占行序列里的额外一行。 */}
        <FormFooter
          onCancel={cancelPage}
          onSave={savePage}
          saving={pageSaving}
          statusSlot={
            <p className="text-caption text-slate-strong">{copy.settings.security.passwordSessionNote}</p>
          }
        />
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
