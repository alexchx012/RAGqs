/*
 * 用户偏好读写（settings-personal）：外观模块（主题/对话字号）与安全模块隐私区
 * （ab_opt_out 开关）共用的加载/保存机制——外观与安全在抽屉中不同时挂载，
 * 各自挂载时经同一会话绑定的 appearance runtime 读写完整 preferences 快照。
 * - 会话 fence：capture 发起时的逻辑会话，响应落地时仍为当前会话才提交；
 * - 保存：乐观应用 + 失败回滚（runtime 副作用与本地快照同步回退）；
 * - 请求序号 fence：旧响应不得覆盖新结果；卸载后迟到响应不写 React state。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuthState, useAuthStore } from '../auth/AuthProvider';
import { useSettings, type AppearanceSyncHandle } from './SettingsProvider';
import type { UserPreferences } from './types';

export interface UsePreferencesResult {
  /** 已提交的偏好快照；加载完成前为 null。 */
  readonly preferences: UserPreferences | null;
  readonly loading: boolean;
  readonly loadError: boolean;
  /** 保存失败（已回滚到上次提交的快照）。 */
  readonly saveError: boolean;
  /** 任一保存进行中（消费方据此禁用相关控件）。 */
  readonly saving: boolean;
  readonly reload: () => void;
  /** 以完整快照保存（契约要求全量 PUT）；加载完成前调用为 no-op。 */
  readonly save: (next: UserPreferences) => void;
}

export function usePreferences(): UsePreferencesResult {
  const { beginAppearanceSync } = useSettings();
  const authState = useAuthState();
  const authStore = useAuthStore();
  const authSessionId = authStore.getAuthSessionId();
  const sessionKey =
    authState.status === 'authenticated' && authState.user !== null && authSessionId !== null
      ? `${authSessionId}:${authState.user.id}`
      : null;
  const [preferences, setPreferences] = useState<UserPreferences | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [saveError, setSaveError] = useState(false);
  const [saving, setSaving] = useState(false);
  const requestSequenceRef = useRef(0);
  const mountedRef = useRef(false);
  const committedPreferencesRef = useRef<UserPreferences | null>(null);

  const isCurrentOperation = useCallback(
    (sequence: number, sync: AppearanceSyncHandle): boolean =>
      mountedRef.current && requestSequenceRef.current === sequence && sync.isCurrent(),
    [],
  );

  const loadPreferences = useCallback(async () => {
    const sequence = ++requestSequenceRef.current;
    const sync = beginAppearanceSync('load');
    committedPreferencesRef.current = null;
    setLoading(true);
    setLoadError(false);
    setSaveError(false);
    setSaving(false);

    if (sessionKey === null || !sync.isCurrent()) {
      setLoading(false);
      return;
    }

    try {
      const loaded = await sync.load();
      if (!isCurrentOperation(sequence, sync)) {
        return;
      }
      committedPreferencesRef.current = loaded;
      setPreferences(loaded);
      sync.apply(loaded);
    } catch {
      if (!isCurrentOperation(sequence, sync)) {
        return;
      }
      setLoadError(true);
    } finally {
      if (isCurrentOperation(sequence, sync)) {
        setLoading(false);
      }
    }
  }, [beginAppearanceSync, isCurrentOperation, sessionKey]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestSequenceRef.current += 1;
    };
  }, []);

  useEffect(() => {
    if (sessionKey === null) {
      ++requestSequenceRef.current;
      committedPreferencesRef.current = null;
      setPreferences(null);
      setLoading(false);
      setLoadError(false);
      setSaveError(false);
      setSaving(false);
      return;
    }
    void loadPreferences();
  }, [loadPreferences, sessionKey]);

  const save = useCallback(
    async (next: UserPreferences) => {
      const previous = committedPreferencesRef.current;
      if (previous === null || sessionKey === null) {
        return;
      }
      const sequence = ++requestSequenceRef.current;
      const sync = beginAppearanceSync('save');
      if (!sync.isCurrent()) {
        return;
      }
      setSaving(true);
      setSaveError(false);
      setPreferences(next);
      sync.apply(next);

      try {
        const saved = await sync.save(next, previous);
        if (!sync.isCurrent()) {
          return;
        }
        // The component may have unmounted after the drawer closed. A current-session
        // terminal response still owns the shared runtime, but never updates React state.
        sync.apply(saved);
        if (!isCurrentOperation(sequence, sync)) {
          return;
        }
        committedPreferencesRef.current = saved;
        setPreferences(saved);
      } catch {
        if (!sync.isCurrent()) {
          return;
        }
        // A current-session failure must also rollback after unmount; only the mounted
        // component receives the visible error and local snapshot update.
        sync.apply(previous);
        if (!isCurrentOperation(sequence, sync)) {
          return;
        }
        committedPreferencesRef.current = previous;
        setPreferences(previous);
        setSaveError(true);
      } finally {
        if (isCurrentOperation(sequence, sync)) {
          setSaving(false);
        }
      }
    },
    [beginAppearanceSync, isCurrentOperation, sessionKey],
  );

  return {
    preferences,
    loading,
    loadError,
    saveError,
    saving,
    reload: () => void loadPreferences(),
    save: (next: UserPreferences) => void save(next),
  };
}
