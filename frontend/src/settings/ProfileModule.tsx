/*
 * 账号设置（个人资料）：显示名走草稿-保存，姓名/部门/角色为只读文本行，更换头像为即时动作。
 *
 * 即时动作边界（本模块的核心）：更换头像会改变他人可见状态、且走独立上传路径，点击即执行——
 * 不进草稿、不使表单进入 dirty、不因「取消」而撤销；只有「只改本人偏好」的显示名走 useDraftForm。
 *
 * 保存进行中禁用显示名输入框（R11 契约）：真实保存响应每次都是新对象（api/client.ts 的
 * `response.json()`），useDraftForm 见 submitted 变化即重置草稿；窗口内若还能改，那笔在途编辑
 * 会在响应落地时被静默丢弃。禁用态需可见，故带 disabled:opacity-60 disabled:cursor-not-allowed
 * （disabled fieldset 的 opacity:0 默认值是另一回事，与 input 无关，故不写 enabled:opacity-100）。
 *
 * submitted 只随显示名取值换身份：会话用户对象在头像上传、其它展示字段提交后也会换身份
 * （createCurrentUserPresentationSync 以新对象落状态），若 submitted 跟着 user 对象重建，
 * useDraftForm 会把草稿重置回服务端快照，正在编辑的显示名被静默丢弃；传内联字面量则每次渲染
 * 都换身份，会直接进入渲染死循环（Task 8 实测：用例挂住、0 条执行）。
 *
 * 只读行用 FormRow 的 readOnlyValue 渲染纯文本（禁用输入框会传达「本可编辑但当前不可用」的
 * 错误语义），且不传 htmlFor（没有可关联的控件）。行分隔线由 FormRow 始终渲染，末行由父容器用
 * [&>*:last-child]:border-b-0 关闭——故五行包在一个容器里（FormFooter 必须是卡片的直接子节点
 * 且留在最后，负外边距依赖该结构）。
 *
 * 卡片只渲染一次具名区域：外层 section 不再挂 aria-label，避免与 SettingsCard 形成嵌套同名 landmark。
 * 卡片标题与灰色副标题由 SettingsCard 的 title/description 渲染（设计图原文，见 copy 的 cardTitle/cardDescription）。
 * A39 保留：显示名有未保存更改时，Esc / 页头关闭钮 / 刷新三个关闭入口先弹「放弃未保存的更改」确认。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent } from 'react';
import { useNavigate } from 'react-router';
import { useAuthState } from '../auth/AuthProvider';
import type { Role } from '../auth/types';
import { copy } from '../copy';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import { FormFooter } from '../ui/FormFooter';
import { FormRow } from '../ui/FormRow';
import { Pill } from '../ui/Pill';
import { SettingsCard } from '../ui/SettingsCard';
import { useSettings } from './SettingsProvider';
import { useDraftForm } from './use-draft-form';

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

/** 显示名草稿的形状：本模块只有这一个字段参与草稿-保存。 */
interface DisplayNameDraft {
  display_name: string;
}

export function ProfileModule() {
  const { api, beginCurrentUserPresentationSync } = useSettings();
  const { user } = useAuthState();
  const navigate = useNavigate();
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
  const avatarInputRef = useRef<HTMLInputElement | null>(null);

  const committedDisplayName = user?.display_name;
  // 见文件头：submitted 只在显示名取值变化时换身份（头像上传等换掉 user 身份时不得重置草稿）。
  const submitted = useMemo<DisplayNameDraft | null>(
    () => (committedDisplayName === undefined ? null : { display_name: committedDisplayName }),
    [committedDisplayName],
  );

  const submitDisplayName = useCallback(
    async (next: DisplayNameDraft): Promise<void> => {
      const sync = beginCurrentUserPresentationSync(['display_name']);
      setSavingProfile(true);
      setProfileError(null);
      setSavedFeedback('idle');
      try {
        const updated = await api.updateProfile({ display_name: next.display_name });
        sync.commit({ display_name: updated.display_name });
        setSavedFeedback('visible');
      } catch {
        setProfileError(copy.settings.profile.saveError);
      } finally {
        setSavingProfile(false);
      }
    },
    [api, beginCurrentUserPresentationSync],
  );

  const saveDisplayName = useCallback(
    (next: DisplayNameDraft) => {
      void submitDisplayName(next);
    },
    [submitDisplayName],
  );

  const { draft, dirty, set, reset, commit } = useDraftForm(submitted, saveDisplayName);

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

  // A39（beforeunload 语义）：有未保存更改时拦截刷新 / 关闭标签页，交给浏览器原生确认。
  useEffect(() => {
    if (!dirty) {
      return;
    }
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      // Chromium 需要 returnValue 才会展示原生「更改未保存」确认
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [dirty]);

  // A39（Esc 入口）：Esc 关抽屉是 URL 驱动的模块外行为，模块内以 window capture 监听
  // 先于 esc-stack（document 冒泡）拦截，弹「放弃未保存的更改」确认；确认框打开期间
  // 让路（Radix 自身处理 Esc = 取消，符合 ConfirmDialog 的 settings 契约）。
  useEffect(() => {
    if (!dirty) {
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
  }, [dirty]);

  // A39（页头关闭钮入口）：关闭钮渲染在壳层（DrawerHost），模块内以 document capture 监听
  // 拦截（React 17+ 事件委托在根容器，document capture 先于根容器触发），转为本模块的
  // 放弃确认；确认框打开期间让路（Radix 处理 Esc/遮罩 = 取消）。
  useEffect(() => {
    if (!dirty) {
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
  }, [dirty]);

  /** 「取消」：丢弃草稿，回到已提交快照。 */
  function cancelDraft(): void {
    reset();
    setProfileError(null);
  }

  /** 确认放弃：回退显示名并关闭抽屉（Esc 的原始意图）。 */
  function discardChangesAndClose(): void {
    reset();
    setProfileError(null);
    setDiscardConfirmOpen(false);
    navigate('/');
  }

  /** 即时动作：按钮把点击转给隐藏的 file input（按钮本身键盘可达，input 只是实现细节）。 */
  function openAvatarPicker(): void {
    avatarInputRef.current?.click();
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
    <section className="pb-10">
      {/* 加载态以 draft === null 判断（会话用户未就位时 useDraftForm 不产出草稿），卡片整体不渲染。 */}
      {draft !== null && (
        <SettingsCard
          ariaLabel={copy.settings.profile.sectionLabel}
          title={copy.settings.profile.cardTitle}
          description={copy.settings.profile.cardDescription}
        >
          {/* 五行的父容器负责关闭末行分隔线（FormRow 始终渲染 border-b）；
              FormFooter 必须仍是卡片的直接子节点且留在最后。 */}
          <div className="[&>*:last-child]:border-b-0">
            <FormRow label={copy.settings.profile.avatarLabel}>
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
                  {/* 即时动作：点击即上传，不进草稿、不参与保存态 */}
                  <Pill variant="ghost" onClick={openAvatarPicker} disabled={uploadingAvatar}>
                    {uploadingAvatar
                      ? copy.settings.profile.avatarUploading
                      : copy.settings.profile.avatarInputLabel}
                  </Pill>
                  <input
                    ref={avatarInputRef}
                    type="file"
                    accept="image/*"
                    aria-label={copy.settings.profile.avatarInputLabel}
                    onChange={(event) => void uploadAvatar(event)}
                    disabled={uploadingAvatar}
                    className="hidden"
                  />
                  {avatarError !== null && (
                    <p role="alert" className="mt-2 text-caption text-danger">
                      {avatarError}
                    </p>
                  )}
                </div>
              </div>
            </FormRow>

            <FormRow label={copy.settings.profile.displayNameLabel} htmlFor="settings-display-name">
              <input
                id="settings-display-name"
                value={draft.display_name}
                onChange={(event) => set({ display_name: event.target.value })}
                disabled={savingProfile}
                className="h-10 w-full rounded-[var(--radius-inputs)] border border-[var(--color-hairline)] bg-paper-white px-3 text-body text-ink-black focus:border-ink-black disabled:cursor-not-allowed disabled:opacity-60"
              />
            </FormRow>

            {/* 由管理员维护的三行：只读文本，不套输入控件、不参与保存 */}
            <FormRow
              label={copy.settings.profile.realNameLabel}
              description={copy.settings.profile.adminManaged}
              readOnlyValue={user?.real_name ?? copy.states.empty}
            />
            <FormRow
              label={copy.settings.profile.departmentLabel}
              description={copy.settings.profile.adminManaged}
              readOnlyValue={user?.department?.name ?? copy.states.empty}
            />
            <FormRow
              label={copy.settings.profile.roleLabel}
              description={copy.settings.profile.adminManaged}
              readOnlyValue={roleLabel(user?.role)}
            />
          </div>

          {profileError !== null && (
            <p role="alert" className="pt-4 text-caption text-danger">
              {profileError}
            </p>
          )}

          {/* FormFooter 是卡片的直接子节点且为最后一个子节点（负外边距抵消卡片 p-8）。
              「已保存」小字经 statusSlot 进入页脚那一行（按钮组左侧），不额外占一行、不改变页脚高度。 */}
          <FormFooter
            onCancel={cancelDraft}
            onSave={commit}
            saving={savingProfile}
            statusSlot={
              savedFeedback === 'idle' ? undefined : (
                <p
                  role="status"
                  className={`text-caption text-success ${
                    savedFeedback === 'fading'
                      ? 'opacity-0 transition-opacity duration-[var(--duration-fast)]'
                      : 'ui-fade-enter-fast'
                  }`}
                >
                  {copy.settings.profile.saved}
                </p>
              )
            }
          />
        </SettingsCard>
      )}

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
