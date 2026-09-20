/*
 * 表单草稿状态（drawer-visual-system）：控件只改本地草稿，「保存」提交、「取消」丢弃。
 *
 * 提交交回 use-preferences 的既有保存路径；本 hook 不复制会话 fence / 请求序号 fence /
 * 失败回滚——保存失败时底层会把已提交快照回滚，本 hook 因 submitted 变化自动重置草稿。
 *
 * 边界：会改变他人可见状态或不可撤销的动作（更换头像、退出设备/会话、退出登录、危险删除）
 * 即时执行，不进入本 hook；只改本人偏好的字段才走草稿。
 * 不做「离开时提示未保存」：需求未要求，且抽屉有四类关闭路径。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

export interface DraftForm<T> {
  /** 当前草稿；已提交快照加载完成前为 null。 */
  draft: T | null;
  /** 草稿与已提交快照是否存在差异。 */
  dirty: boolean;
  /** 只改本地草稿，不提交。 */
  set: (patch: Partial<T>) => void;
  /** 丢弃草稿，回到已提交快照。 */
  reset: () => void;
  /** 仅在 dirty 时提交完整草稿。 */
  commit: () => void;
}

function differs<T extends object>(a: T, b: T): boolean {
  return Object.keys(a).some((key) => a[key as keyof T] !== b[key as keyof T]);
}

export function useDraftForm<T extends object>(
  submitted: T | null,
  save: (next: T) => void,
): DraftForm<T> {
  const [draft, setDraft] = useState<T | null>(submitted);
  const draftRef = useRef<T | null>(submitted);
  const submittedRef = useRef<T | null>(submitted);

  useEffect(() => {
    submittedRef.current = submitted;
    draftRef.current = submitted;
    setDraft(submitted);
  }, [submitted]);

  useEffect(() => {
    draftRef.current = draft;
  }, [draft]);

  const dirty = useMemo(
    () => (draft === null || submitted === null ? false : differs(draft, submitted)),
    [draft, submitted],
  );

  const set = useCallback((patch: Partial<T>) => {
    setDraft((current) => (current === null ? current : { ...current, ...patch }));
  }, []);

  const reset = useCallback(() => {
    setDraft(submittedRef.current);
  }, []);

  const commit = useCallback(() => {
    const current = draftRef.current;
    const committed = submittedRef.current;
    if (current === null || committed === null) {
      return;
    }
    if (differs(current, committed)) {
      save(current);
    }
  }, [save]);

  return { draft, dirty, set, reset, commit };
}
