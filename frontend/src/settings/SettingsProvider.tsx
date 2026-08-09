/*
 * 设置域依赖注入边界（无 UI）。
 * 显式依赖 { api, authStore, theme, notifications } 由 App 装配层注入；
 * 通过既有 placeholder composition 向抽屉提供 Profile/Security；
 * Context 仅暴露受控 presentation-sync 与会话绑定的外观 runtime，不暴露 AuthSessionStore 原始写能力。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useSyncExternalStore,
  type ReactNode,
} from 'react';
import type {
  AuthSessionStore,
  AuthState,
  CurrentUserPresentationField,
  CurrentUserPresentationPatch,
} from '../auth/session';
import type { NotificationsStore } from '../notifications/store';
import type { ThemeController } from '../theme/theme';
import type { SettingsApi } from './api';
import type { UserPreferences } from './types';

interface SettingsProviderProps {
  readonly api: SettingsApi;
  readonly authStore: AuthSessionStore;
  readonly theme: ThemeController;
  readonly notifications: NotificationsStore;
}

/** 未来 ProfileModule 在发起保存时取得，成功后仅可提交允许的展示字段。 */
export interface CurrentUserPresentationSyncHandle {
  readonly commit: (patch: CurrentUserPresentationPatch) => void;
}

export type AppearanceSyncKind = 'load' | 'save';

/**
 * 外观 runtime 句柄只绑定当前认证会话与一个局部 revision：
 * - load 只复用当前会话/当前 revision 的 in-flight Promise，不缓存已完成快照；
 * - save 会推进 revision，使此前的 GET 结果不能覆盖乐观应用；
 * - apply 只负责 ThemeController 与聊天字号这两个 runtime 副作用。
 */
export interface AppearanceSyncHandle {
  readonly load: () => Promise<UserPreferences>;
  readonly save: (preferences: UserPreferences, previous: UserPreferences) => Promise<UserPreferences>;
  readonly isCurrent: () => boolean;
  readonly apply: (preferences: UserPreferences) => boolean;
}

export interface SettingsContextValue {
  readonly api: SettingsApi;
  readonly theme: ThemeController;
  readonly notifications: NotificationsStore;
  /**
   * 保存请求启动时声明会影响的展示字段。每个字段独立按最新启动顺序接收提交。
   * 模块自行等待 API 响应后调用 handle.commit()，不会获得 AuthSessionStore 或整个 User 写入能力。
   */
  readonly beginCurrentUserPresentationSync: (
    affectedFields: readonly CurrentUserPresentationField[],
  ) => CurrentUserPresentationSyncHandle;
  /**
   * 创建会话绑定的主题/聊天字号 runtime 句柄。该句柄不持有完整 preferences snapshot，
   * 只提供瞬时 GET 去重、会话/revision fence 与两个即时视觉副作用。
   */
  readonly beginAppearanceSync: (kind?: AppearanceSyncKind) => AppearanceSyncHandle;
}

interface AppearanceLoadFlight {
  readonly api: SettingsApi;
  readonly sessionKey: string;
  readonly revision: number;
  readonly promise: Promise<UserPreferences>;
}

interface AppearanceSaveFlight {
  readonly api: SettingsApi;
  readonly sessionKey: string;
  readonly revision: number;
  readonly previous: UserPreferences;
  readonly promise: Promise<UserPreferences>;
}

const SettingsContext = createContext<SettingsContextValue | null>(null);

function appearanceSessionKey(state: AuthState, authSessionId: string | null): string | null {
  if (state.status !== 'authenticated' || state.user === null || authSessionId === null) {
    return null;
  }
  return `${authSessionId}:${state.user.id}`;
}

function applyAppearanceRuntime(theme: ThemeController, preferences: UserPreferences): void {
  theme.setPreference(preferences.theme);
  if (typeof document !== 'undefined') {
    document.documentElement.dataset.chatFontSize = preferences.chat_font_size;
  }
}

function resetAppearanceRuntime(theme: ThemeController): void {
  theme.setPreference('system');
  if (typeof document !== 'undefined') {
    delete document.documentElement.dataset.chatFontSize;
  }
}

export function SettingsProvider({
  api,
  authStore,
  theme,
  notifications,
  children,
}: SettingsProviderProps & { children: ReactNode }) {
  const nextPresentationSequence = useRef(0);
  const latestPresentationSequence = useRef<Partial<Record<CurrentUserPresentationField, number>>>({});
  const appearanceRevision = useRef(0);
  const previousAppearanceSessionKey = useRef<string | null>(null);
  const inFlightAppearanceLoad = useRef<AppearanceLoadFlight | null>(null);
  const inFlightAppearanceSave = useRef<AppearanceSaveFlight | null>(null);

  const subscribeAuth = useCallback((listener: () => void) => authStore.subscribe(listener), [authStore]);
  const getAuthSnapshot = useCallback(() => authStore.getState(), [authStore]);
  const authState = useSyncExternalStore(subscribeAuth, getAuthSnapshot);
  const authSessionId = authStore.getAuthSessionId();
  const currentAppearanceSessionKey = appearanceSessionKey(authState, authSessionId);

  // 在提交前同步推进 revision，确保 child passive effect 与 provider hydration effect 使用同一版本。
  if (previousAppearanceSessionKey.current !== currentAppearanceSessionKey) {
    previousAppearanceSessionKey.current = currentAppearanceSessionKey;
    appearanceRevision.current += 1;
  }

  const beginCurrentUserPresentationSync = useCallback(
    (affectedFields: readonly CurrentUserPresentationField[]): CurrentUserPresentationSyncHandle => {
      const sequence = ++nextPresentationSequence.current;
      const fields = [...new Set(affectedFields)];
      for (const field of fields) {
        latestPresentationSequence.current[field] = sequence;
      }
      const sync = authStore.createCurrentUserPresentationSync();
      return {
        commit(patch) {
          const acceptedPatch: CurrentUserPresentationPatch = {
            ...(fields.includes('display_name') &&
            latestPresentationSequence.current.display_name === sequence &&
            patch.display_name !== undefined
              ? { display_name: patch.display_name }
              : {}),
            ...(fields.includes('avatar_url') &&
            latestPresentationSequence.current.avatar_url === sequence &&
            patch.avatar_url !== undefined
              ? { avatar_url: patch.avatar_url }
              : {}),
          };
          sync(acceptedPatch);
        },
      };
    },
    [authStore],
  );

  const beginAppearanceSync = useCallback(
    (kind: AppearanceSyncKind = 'load'): AppearanceSyncHandle => {
      const capturedSessionKey = appearanceSessionKey(authStore.getState(), authStore.getAuthSessionId());
      const capturedRevision = kind === 'save' ? ++appearanceRevision.current : appearanceRevision.current;
      const isCurrent = () =>
        capturedSessionKey !== null &&
        capturedSessionKey === appearanceSessionKey(authStore.getState(), authStore.getAuthSessionId()) &&
        capturedRevision === appearanceRevision.current;

      const load = (): Promise<UserPreferences> => {
        if (!isCurrent()) {
          return Promise.reject(new Error('stale appearance preference request'));
        }
        if (capturedSessionKey === null) {
          return Promise.reject(new Error('appearance preferences require an authenticated session'));
        }
        const pendingSave = inFlightAppearanceSave.current;
        if (
          pendingSave !== null &&
          pendingSave.api === api &&
          pendingSave.sessionKey === capturedSessionKey &&
          pendingSave.revision === capturedRevision
        ) {
          // A reopened module must converge on the unresolved complete snapshot rather
          // than GET-ing an older server state while the prior PUT is still pending.
          return pendingSave.promise.catch(() => pendingSave.previous);
        }
        const existing = inFlightAppearanceLoad.current;
        if (
          existing !== null &&
          existing.api === api &&
          existing.sessionKey === capturedSessionKey &&
          existing.revision === capturedRevision
        ) {
          return existing.promise;
        }
        const promise = Promise.resolve().then(() => api.getPreferences());
        const flight: AppearanceLoadFlight = {
          api,
          sessionKey: capturedSessionKey,
          revision: capturedRevision,
          promise,
        };
        inFlightAppearanceLoad.current = flight;
        void promise.then(
          () => {
            if (inFlightAppearanceLoad.current === flight) {
              inFlightAppearanceLoad.current = null;
            }
          },
          () => {
            if (inFlightAppearanceLoad.current === flight) {
              inFlightAppearanceLoad.current = null;
            }
          },
        );
        return promise;
      };

      const save = (preferences: UserPreferences, previous: UserPreferences): Promise<UserPreferences> => {
        if (!isCurrent()) {
          return Promise.reject(new Error('stale appearance preference request'));
        }
        if (capturedSessionKey === null) {
          return Promise.reject(new Error('appearance preferences require an authenticated session'));
        }
        const existing = inFlightAppearanceSave.current;
        if (
          existing !== null &&
          existing.api === api &&
          existing.sessionKey === capturedSessionKey &&
          existing.revision === capturedRevision
        ) {
          return existing.promise;
        }
        const promise = Promise.resolve().then(() => api.updatePreferences(preferences));
        const flight: AppearanceSaveFlight = {
          api,
          sessionKey: capturedSessionKey,
          revision: capturedRevision,
          previous,
          promise,
        };
        inFlightAppearanceSave.current = flight;
        void promise.then(
          () => {
            if (inFlightAppearanceSave.current === flight) {
              inFlightAppearanceSave.current = null;
            }
          },
          () => {
            if (inFlightAppearanceSave.current === flight) {
              inFlightAppearanceSave.current = null;
            }
          },
        );
        return promise;
      };

      return {
        load,
        save,
        isCurrent,
        apply(preferences) {
          if (!isCurrent()) {
            return false;
          }
          applyAppearanceRuntime(theme, preferences);
          return true;
        },
      };
    },
    [api, authStore, theme],
  );

  // Hydrate theme/font immediately after login or refresh bootstrap; no completed preferences snapshot is retained here.
  useEffect(() => {
    const sync = beginAppearanceSync('load');
    resetAppearanceRuntime(theme);
    if (currentAppearanceSessionKey === null) {
      return;
    }
    void sync.load().then(
      (preferences) => {
        sync.apply(preferences);
      },
      () => {
        // AppearanceModule presents the load error when the drawer is opened; runtime stays system/standard.
      },
    );
  }, [currentAppearanceSessionKey, beginAppearanceSync, theme]);

  const value = useMemo<SettingsContextValue>(
    () => ({
      api,
      theme,
      notifications,
      beginCurrentUserPresentationSync,
      beginAppearanceSync,
    }),
    [api, theme, notifications, beginCurrentUserPresentationSync, beginAppearanceSync],
  );
  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}

export function useSettings(): SettingsContextValue {
  const value = useContext(SettingsContext);
  if (value === null) {
    throw new Error('useSettings must be used within SettingsProvider');
  }
  return value;
}
