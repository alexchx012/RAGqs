/*
 * prefers-reduced-motion 探测（共用基座 §2.5 降级约定）。
 * dashboard 数值交叉淡变与 sparkline 形变在 reduce 时降级为直出；
 * 与 DrawerHost 内同名 hook 同语义（admin 域自持一份，不动壳层）。
 */

import { useEffect, useState } from 'react';

export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  );
  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const onChange = (event: MediaQueryListEvent) => setReduced(event.matches);
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, []);
  return reduced;
}
