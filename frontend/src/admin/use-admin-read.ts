/*
 * 管理面板读序列 hook：generation fence + 三态（loading / error / data）。
 * 与 settings 各模块手写 fence 同语义：依赖变化 / 卸载时旧响应一律作废（fail-closed），
 * 不得把过期快照写入界面；reload 供错误态重试与写操作后刷新复用。
 */

import { useCallback, useEffect, useRef, useState } from 'react';

export interface AdminReadResult<T> {
  readonly data: T | null;
  readonly loading: boolean;
  readonly error: boolean;
  readonly reload: () => void;
}

export function useAdminRead<T>(
  fetcher: () => Promise<T>,
  deps: readonly unknown[],
): AdminReadResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  // 读序列代际：每次发起推进一代，只有当前代响应允许落地。
  const generationRef = useRef(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const reload = useCallback(() => {
    const generation = ++generationRef.current;
    setLoading(true);
    setError(false);
    void fetcherRef.current().then(
      (result) => {
        if (generationRef.current === generation) {
          setData(result);
          setLoading(false);
        }
      },
      () => {
        if (generationRef.current === generation) {
          setError(true);
          setLoading(false);
        }
      },
    );
  }, []);

  useEffect(() => {
    reload();
    return () => {
      // 依赖变化 / 卸载：作废旧响应（新一代由下一次 reload 推进）。
      generationRef.current += 1;
    };
  }, [reload, ...deps]);

  return { data, loading, error, reload };
}
