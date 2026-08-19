import { useEffect, useState, type ChangeEvent, type FormEvent } from 'react';
import { useAuthState } from '../auth/AuthProvider';
import type { Role } from '../auth/types';
import { copy } from '../copy';
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
  const [displayName, setDisplayName] = useState(user?.display_name ?? '');
  const [savingProfile, setSavingProfile] = useState(false);
  const [uploadingAvatar, setUploadingAvatar] = useState(false);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [avatarError, setAvatarError] = useState<string | null>(null);
  const [avatarFailed, setAvatarFailed] = useState(false);
  const [savedFeedback, setSavedFeedback] = useState<SavedFeedback>('idle');

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
          <label
            htmlFor="settings-avatar"
            className="inline-flex h-8 cursor-pointer items-center rounded-[var(--radius-buttons)] border border-ink-black px-3 text-[14px] text-ink-black transition-colors duration-[var(--duration-fast)] hover:bg-mist-gray"
          >
            {copy.settings.profile.avatarInputLabel}
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
        <label htmlFor="settings-display-name" className="mb-2 block text-caption text-slate-gray">
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
    </section>
  );
}

function ReadOnlyProfileRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="py-4">
      <dt className="text-caption text-slate-gray">{label}</dt>
      <dd className="mt-1 text-body text-ink-black">{value}</dd>
      <p className="mt-1 text-caption text-smoke-gray">{copy.settings.profile.adminManaged}</p>
    </div>
  );
}
