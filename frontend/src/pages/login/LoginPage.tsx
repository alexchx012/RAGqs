/*
 * 登录页（规格 §5；UI 唯一来源《前端/登录页设计.md》）。
 * - 纯登录：无自助注册、无忘记密码、无 SSO、无企业/租户选择器，登录请求不携带租户标识。
 * - 错误全部就地一行文字（密码框下方 8px），不出现 toast / 系统提示条；
 *   界面行为由 error.code 驱动：invalid_credentials → 双框红边 + 错误行（再输入即时清除），
 *   too_many_attempts → 按 error.details.retry_after_seconds 倒计时禁用登录键，其余（5xx/超时/网络）→ 服务不可用行。
 * - 成功：整页 opacity 1→0 150ms 后按角色落地；暗色跟随系统（theme.ts 默认 'system'）。
 */

import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react';
import { useNavigate } from 'react-router';
import { ApiError } from '../../api/errors';
import { useAuthStore } from '../../auth/AuthProvider';
import { copy } from '../../copy';
import { landingTargetFor } from '../../router/landing';
import './login.css';

type ErrorKind = 'invalid' | 'throttled' | 'unavailable' | null;

/** 整页淡出时长（--duration-fast）。 */
const LEAVE_FADE_MS = 150;

export function LoginPage() {
  const store = useAuthStore();
  const navigate = useNavigate();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [errorKind, setErrorKind] = useState<ErrorKind>(null);
  const [retryAfter, setRetryAfter] = useState(0);
  const [leaving, setLeaving] = useState(false);
  const leaveTimerRef = useRef<number | null>(null);
  const mountedRef = useRef(true);

  // 卸载：置 mounted=false 并取消待发的延迟落地跳转（见 onSubmit）
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (leaveTimerRef.current !== null) {
        window.clearTimeout(leaveTimerRef.current);
        leaveTimerRef.current = null;
      }
    };
  }, []);

  // 429 限流倒计时：期满恢复登录键
  useEffect(() => {
    if (retryAfter <= 0) {
      return undefined;
    }
    const timer = setInterval(() => {
      setRetryAfter((value) => (value > 1 ? value - 1 : 0));
    }, 1000);
    return () => clearInterval(timer);
  }, [retryAfter > 0]);

  const disabled = username === '' || password === '' || submitting || retryAfter > 0 || leaving;

  function clearInvalid(): void {
    // 任一框再次输入时红色态即时清除
    if (errorKind === 'invalid') {
      setErrorKind(null);
    }
  }

  function onUsernameChange(event: ChangeEvent<HTMLInputElement>): void {
    setUsername(event.target.value);
    clearInvalid();
  }

  function onPasswordChange(event: ChangeEvent<HTMLInputElement>): void {
    setPassword(event.target.value);
    clearInvalid();
  }

  async function onSubmit(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (disabled) {
      return;
    }
    setSubmitting(true);
    setErrorKind(null);
    try {
      const user = await store.login(username, password);
      // 组件已被 RedirectIfAuthenticated 带离卸载时，落地跳转由守卫的 <Navigate>
      // （携同一 state）完成；丢弃这里的延迟跳转。否则 await 续体会在卸载后照样设下
      // 幽灵定时器，150ms 后把用户随后的手动导航（如立刻点开抽屉）顶回落地页
      // （e2e 弹回 / 的根因）。
      if (!mountedRef.current) {
        return;
      }
      const target = landingTargetFor(user.role);
      setLeaving(true);
      leaveTimerRef.current = window.setTimeout(() => {
        leaveTimerRef.current = null;
        navigate(target.path, { replace: true, state: target.state });
      }, LEAVE_FADE_MS);
    } catch (error) {
      if (error instanceof ApiError && error.code === 'invalid_credentials') {
        setErrorKind('invalid');
      } else if (error instanceof ApiError && error.code === 'too_many_attempts') {
        setErrorKind('throttled');
        const seconds = Number(error.details['retry_after_seconds']);
        setRetryAfter(Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : 0);
      } else {
        // 5xx / 超时 / 网络异常：错误行 + 登录键恢复可点
        setErrorKind('unavailable');
      }
      setSubmitting(false);
    }
  }

  const errorText =
    errorKind === 'invalid'
      ? copy.login.errorInvalidCredentials
      : errorKind === 'throttled'
        ? copy.login.errorTooManyAttempts
        : errorKind === 'unavailable'
          ? copy.login.errorServiceUnavailable
          : null;

  const fieldBorder = (invalid: boolean): string =>
    invalid ? 'border-danger' : 'border-[var(--color-hairline)] focus-within:border-ink-black';

  return (
    <div
      className={`login-page-fade flex min-h-screen bg-paper-white ${leaving ? 'opacity-0' : 'opacity-100'}`}
    >
      {/* 品牌区：≥768px 左栏 44%，内容块中心位于视口高 42% 处 */}
      <aside className="relative hidden bg-fog-white md:block md:w-[44%]">
        <div className="login-brand-block">
          <div className="login-enter">
            <div className="flex h-12 w-12 items-center justify-center rounded-[var(--radius-images)] bg-blush-peach font-signifier text-[24px] text-sienna-brown">
              {copy.appName.charAt(0)}
            </div>
            <p className="mt-6 font-signifier text-heading leading-heading tracking-heading">
              {copy.appName}
            </p>
            <p className="mt-2 text-body text-slate-gray">{copy.login.tagline}</p>
          </div>
        </div>
        {copy.login.brandFooter !== '' && (
          <p className="absolute inset-x-0 bottom-6 text-center text-[14px] text-ash-gray">
            {copy.login.brandFooter}
          </p>
        )}
      </aside>

      {/* 表单区：≥768px 右栏；窄屏单栏，品牌区收起为顶部品牌行 */}
      <main className="flex flex-1 flex-col md:border-l md:border-[var(--color-hairline)]">
        <div className="mt-10 flex items-center justify-center gap-3 md:hidden">
          <div className="flex h-8 w-8 items-center justify-center rounded-[var(--radius-images)] bg-blush-peach font-signifier text-[16px] text-sienna-brown">
            {copy.appName.charAt(0)}
          </div>
          <p className="font-sohne text-heading-sm leading-heading-sm tracking-heading-sm font-medium">
            {copy.appName}
          </p>
        </div>
        <div className="flex flex-1">
          <div className="login-enter login-enter-late mx-auto w-full max-w-[400px] self-center px-6 max-md:mt-[10vh] max-md:self-start">
            <h1 className="font-signifier text-heading leading-heading tracking-heading font-normal max-md:font-sohne max-md:text-heading-sm max-md:leading-heading-sm max-md:tracking-heading-sm max-md:font-medium">
              {copy.login.title}
            </h1>
            <form className="mt-10" onSubmit={(event) => void onSubmit(event)} noValidate>
              <div>
                <label htmlFor="login-username" className="mb-2 block text-caption text-slate-gray">
                  {copy.login.usernameLabel}
                </label>
                <div
                  className={`flex h-10 items-center rounded-[var(--radius-inputs)] border bg-paper-white px-3 transition-colors duration-150 ${fieldBorder(errorKind === 'invalid')}`}
                >
                  <input
                    id="login-username"
                    type="text"
                    autoComplete="username"
                    spellCheck={false}
                    aria-invalid={errorKind === 'invalid'}
                    value={username}
                    onChange={onUsernameChange}
                    className="w-full bg-transparent text-body text-ink-black"
                  />
                </div>
              </div>
              <div className="mt-5">
                <label htmlFor="login-password" className="mb-2 block text-caption text-slate-gray">
                  {copy.login.passwordLabel}
                </label>
                <div
                  className={`relative flex h-10 items-center rounded-[var(--radius-inputs)] border bg-paper-white px-3 transition-colors duration-150 ${fieldBorder(errorKind === 'invalid')}`}
                >
                  <input
                    id="login-password"
                    type={showPassword ? 'text' : 'password'}
                    autoComplete="current-password"
                    aria-invalid={errorKind === 'invalid'}
                    value={password}
                    onChange={onPasswordChange}
                    className="w-full bg-transparent pr-6 text-body text-ink-black"
                  />
                  <button
                    type="button"
                    aria-label={showPassword ? copy.login.hidePassword : copy.login.showPassword}
                    aria-pressed={showPassword}
                    onClick={() => setShowPassword((value) => !value)}
                    className="absolute top-1/2 right-3 -translate-y-1/2 text-slate-gray transition-colors duration-150 hover:text-ink-black"
                  >
                    <span key={showPassword ? 'hide' : 'show'} className="login-eye-icon block h-4 w-4">
                      {showPassword ? <EyeOffIcon /> : <EyeIcon />}
                    </span>
                  </button>
                </div>
                {errorText !== null && (
                  <p role="alert" className="login-error-enter mt-2 text-caption text-danger">
                    {errorText}
                  </p>
                )}
              </div>
              <button
                type="submit"
                disabled={disabled}
                className="mt-8 flex h-10 w-full items-center justify-center rounded-full bg-ink-black text-[16px] text-paper-white transition-opacity duration-150 enabled:hover:opacity-[0.88] disabled:bg-mist-gray disabled:text-smoke-gray"
              >
                {submitting || leaving ? (
                  <span className="loading-dots" role="status" aria-label={copy.login.submitting}>
                    <span />
                    <span />
                    <span />
                  </span>
                ) : retryAfter > 0 ? (
                  copy.login.retryCountdown(retryAfter)
                ) : (
                  copy.login.submit
                )}
              </button>
              <p className="mt-4 text-center text-caption text-smoke-gray">{copy.login.guide}</p>
            </form>
          </div>
        </div>
      </main>
    </div>
  );
}

/** 16px 眼睛图标（密码明文/掩码切换；该控件规格供后续新增用户对话框复用）。 */
function EyeIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className="h-4 w-4"
    >
      <path d="M1.5 8s2.2-3.6 6.5-3.6S14.5 8 14.5 8s-2.2 3.6-6.5 3.6S1.5 8 1.5 8Z" />
      <circle cx="8" cy="8" r="1.8" />
    </svg>
  );
}

function EyeOffIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className="h-4 w-4"
    >
      <path d="M1.5 8s2.2-3.6 6.5-3.6S14.5 8 14.5 8s-2.2 3.6-6.5 3.6S1.5 8 1.5 8Z" />
      <circle cx="8" cy="8" r="1.8" />
      <path d="M2.5 2.5l11 11" />
    </svg>
  );
}
