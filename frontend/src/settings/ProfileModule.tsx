import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react';
import { useNavigate } from 'react-router';
import { useAuthState } from '../auth/AuthProvider';
import type { Role } from '../auth/types';
import { copy } from '../copy';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import { Pill } from '../ui/Pill';
import { useSettings } from './SettingsProvider';

function roleLabel(role: Role | undefined): string {
  switch (role) {
    case 'minister':
      return copy.settings.profile.roleMinister;
    case 'ops':
      return copy.settings.profile.roleOps;
    case 'admin':
      return copy.settings.profile.roleAdmin;
    case 'user':
    default:
      return copy.settings.profile.roleUser;
  }
}

/** 「已保存」小字的淡出时长（与 --duration-fast 一致；jsdom 不触发 transitionend，用定时器卸载）。 */
const SAVED_FADE_OUT_MS = 150;
/** 保存成功反馈停留约 2s 后淡出（共用基座 §5.3）。 */
const SAVED_VISIBLE_MS = 2000;

type SavedFeedback = 'idle' | 'visible' | 'fading';

export function ProfileModule() {
  const { api, beginCurrentUserPresentationSync } = useSettings();
  const { user } = useAuthState();
  const navigate = useNavigate();
  const [displayName, setDisplayName] = useState(user?.display_name ?? '');
  const [savingProfile, setSavingProfile] = useState(false);
  const [uploadingAvatar, setUploadingAvatar] = useState(false);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [avatarError, setAvatarError] = useState<string | null>(null);
  const [avatarFailed, setAvatarFailed] = useState(false);
  const [savedFeedback, setSavedFeedback] = useState<SavedFeedback>('idle');
  // A39：显示名有未保存更改时，从模块内拦截抽屉关闭入口（Esc / 刷新）并确认放弃
  const [discardConfirmOpen, setDiscardConfirmOpen] = useState(false);
  const discardConfirmOpenRef = useRef(false);
  discardConfirmOpenRef.current = discardConfirmOpen;

  useEffect(() => {
    setDisplayName(user?.display_name ?? '');
  }, [user?.display_name]);

  // 「已保存」反馈：淡入后停留约 2s，再按 --duration-fast 淡出并卸载。
  useEffect(() => {
    if (savedFeedback === 'idle') {
      return;
    }
    const timer = window.setTimeout(
      () => setSavedFeedback(savedFeedback === 'visible' ? 'fading' : 'idle'),
      savedFeedback === 'visible' ? SAVED_VISIBLE_MS : SAVED_FADE_OUT_MS,
    );
    return () => window.clearTimeout(timer);
  }, [savedFeedback]);

  const committedDisplayName = user?.display_name ?? '';
  const hasUnsavedChanges = displayName !== committedDisplayName;

  // A39（beforeunload 语义）：有未保存更改时拦截刷新 / 关闭标签页，交给浏览器原生确认。
  useEffect(() => {
    if (!hasUnsavedChanges) {
      return;
    }
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      // Chromium 需要 returnValue 才会展示原生「更改未保存」确认
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [hasUnsavedChanges]);

  // A39（Esc 入口）：Esc 关抽屉是 URL 驱动的模块外行为，模块内以 window capture 监听
  // 先于 esc-stack（document 冒泡）拦截，弹「放弃未保存的更改」确认；确认框打开期间
  // 让路（Radix 自身处理 Esc = 取消，符合 ConfirmDialog 的 settings 契约）。
  useEffect(() => {
    if (!hasUnsavedChanges) {
      return;
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || discardConfirmOpenRef.current) {
        return;
      }
      event.stopPropagation();
      event.preventDefault();
      setDiscardConfirmOpen(true);
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [hasUnsavedChanges]);

  // A39（页头关闭钮入口）：关闭钮渲染在壳层（DrawerHost），模块内以 document capture 监听
  // 拦截（React 17+ 事件委托在根容器，document capture 先于根容器触发），转为本模块的
  // 放弃确认；确认框打开期间让路（Radix 处理 Esc/遮罩 = 取消）。
  useEffect(() => {
    if (!hasUnsavedChanges) {
      return;
    }
    const closeSelector = `button[aria-label="${copy.shell.drawer.closeAria}"]`;
    const onClickCapture = (event: MouseEvent) => {
      if (discardConfirmOpenRef.current) {
        return;
      }
      if (event.target instanceof Element && event.target.closest(closeSelector) !== null) {
        event.stopPropagation();
        event.preventDefault();
        setDiscardConfirmOpen(true);
      }
    };
    document.addEventListener('click', onClickCapture, true);
    return () => document.removeEventListener('click', onClickCapture, true);
  }, [hasUnsavedChanges]);

  /** 确认放弃：回退显示名并关闭抽屉（Esc 的原始意图）。 */
  function discardChangesAndClose(): void {
    setDisplayName(committedDisplayName);
    setProfileError(null);
    setDiscardConfirmOpen(false);
    navigate('/');
  }

  async function saveProfile(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (savingProfile || !hasUnsavedChanges) {
      return;
    }
    const sync = beginCurrentUserPresentationSync(['display_name']);
    setSavingProfile(true);
    setProfileError(null);
    setSavedFeedback('idle');
    try {
      const updated = await api.updateProfile({ display_name: displayName });
      sync.commit({ display_name: updated.display_name });
      setSavedFeedback('visible');
    } catch {
      setProfileError(copy.settings.profile.saveError);
    } finally {
      setSavingProfile(false);
    }
  }

  function revertProfile(): void {
    if (savingProfile) {
      return;
    }
    setDisplayName(committedDisplayName);
    setProfileError(null);
  }

  async function uploadAvatar(event: ChangeEvent<HTMLInputElement>): Promise<void> {
    const file = event.target.files?.item(0) ?? null;
    // A39：立即重置 input value，使重复选择同一文件也能再次触发 onChange
    event.target.value = '';
    if (file === null || uploadingAvatar) {
      return;
    }
    const sync = beginCurrentUserPresentationSync(['avatar_url']);
    setUploadingAvatar(true);
    setAvatarError(null);
    try {
      const updated = await api.uploadAvatar(file);
      setAvatarFailed(false);
      sync.commit({ avatar_url: updated.avatar_url });
    } catch {
      setAvatarError(copy.settings.profile.avatarError);
    } finally {
      setUploadingAvatar(false);
    }
  }

  return (
    <section aria-label={copy.settings.profile.sectionLabel} className="pb-10">
      <div className="flex items-center gap-4">
        {avatarFailed || (user?.avatar_url ?? '') === '' ? (
          <span
            aria-label={copy.settings.profile.avatarAlt}
            className="flex h-16 w-16 items-center justify-center rounded-full bg-mist-gray text-body-lg font-w500 text-ink-black"
          >
            {(user?.display_name ?? '').slice(0, 2)}
          </span>
        ) : (
          <img
            src={user?.avatar_url ?? ''}
            alt={copy.settings.profile.avatarAlt}
            onError={() => setAvatarFailed(true)}
            className="h-16 w-16 rounded-full bg-mist-gray object-cover"
          />
        )}
        <div>
          {/* A39：上传中 label 禁用样式 + 行内加载反馈（input 同步 disabled） */}
          <label
            htmlFor="settings-avatar"
            className={`inline-flex h-8 items-center rounded-[var(--radius-buttons)] border border-ink-black px-3 text-[14px] text-ink-black transition-colors duration-[var(--duration-fast)] ${
              uploadingAvatar ? 'cursor-not-allowed opacity-60' : 'cursor-pointer hover:bg-mist-gray'
            }`}
          >
            {uploadingAvatar
              ? copy.settings.profile.avatarUploading
              : copy.settings.profile.avatarInputLabel}
          </label>
          <input
            id="settings-avatar"
            type="file"
            accept="image/*"
            onChange={(event) => void uploadAvatar(event)}
            disabled={uploadingAvatar}
            className="mt-2 hidden"
          />
          {avatarError !== null && (
            <p role="alert" className="mt-2 text-caption text-danger">
              {avatarError}
            </p>
          )}
        </div>
      </div>

      <form className="mt-8" onSubmit={(event) => void saveProfile(event)} noValidate>
        <label htmlFor="settings-display-name" className="mb-2 block text-caption text-slate-strong">
          {copy.settings.profile.displayNameLabel}
        </label>
        <input
          id="settings-display-name"
          value={displayName}
          onChange={(event) => setDisplayName(event.target.value)}
          className="h-10 w-full rounded-[var(--radius-inputs)] border border-[var(--color-hairline)] bg-paper-white px-3 text-body text-ink-black focus:border-ink-black"
        />
        {profileError !== null && (
          <p role="alert" className="mt-2 text-caption text-danger">
            {profileError}
          </p>
        )}
        {/* 卡底部操作行（共用基座 §5.3）：仅有未保存变更（或保存反馈展示中）时出现 */}
        {(hasUnsavedChanges || savingProfile || savedFeedback !== 'idle') && (
          <div className="mt-4 flex items-center gap-3">
            <Pill type="submit" loading={savingProfile} disabled={!hasUnsavedChanges}>
              {copy.settings.profile.save}
            </Pill>
            <Pill variant="ghost" disabled={savingProfile} onClick={revertProfile}>
              {copy.controls.cancel}
            </Pill>
            {savedFeedback !== 'idle' && (
              <span
                role="status"
                className={`text-caption text-success ${
                  savedFeedback === 'fading'
                    ? 'opacity-0 transition-opacity duration-[var(--duration-fast)]'
                    : 'ui-fade-enter-fast'
                }`}
              >
                {copy.settings.profile.saved}
              </span>
            )}
          </div>
        )}
      </form>

      <dl className="mt-10 divide-y divide-[var(--color-hairline)]">
        <ReadOnlyProfileRow
          label={copy.settings.profile.realNameLabel}
          value={user?.real_name ?? copy.states.empty}
        />
        <ReadOnlyProfileRow
          label={copy.settings.profile.departmentLabel}
          value={user?.department?.name ?? copy.states.empty}
        />
        <ReadOnlyProfileRow
          label={copy.settings.profile.roleLabel}
          value={roleLabel(user?.role)}
        />
      </dl>

      {/* A39：关闭抽屉入口（Esc / 刷新）拦截确认；Esc=取消由 ConfirmDialog 承担 */}
      <ConfirmDialog
        open={discardConfirmOpen}
        onOpenChange={(open) => {
          if (!open) {
            setDiscardConfirmOpen(false);
          }
        }}
        title={copy.settings.profile.unsavedConfirmTitle}
        description={copy.settings.profile.unsavedConfirmDescription}
        confirmLabel={copy.settings.profile.unsavedConfirm}
        danger
        onConfirm={discardChangesAndClose}
      />
    </section>
  );
}

function ReadOnlyProfileRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="py-4">
      <dt className="text-caption text-slate-strong">{label}</dt>
      <dd className="mt-1 text-body text-ink-black">{value}</dd>
      <p className="mt-1 text-caption text-slate-strong">{copy.settings.profile.adminManaged}</p>
    </div>
  );
}
