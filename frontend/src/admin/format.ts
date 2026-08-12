/** 管理面板共享展示格式化（与 settings 模块同口径：非法值原样回显）。 */

export function formatDateTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString('zh-CN');
}

export function formatDate(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleDateString('zh-CN');
}

/** 文件大小（投稿审核五列之一；与 ManageLayer 同口径）。 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const kb = bytes / 1024;
  if (kb < 1024) {
    return `${kb.toFixed(1)} KB`;
  }
  return `${(kb / 1024).toFixed(1)} MB`;
}

/** 0–1 小数 → 百分比字符串（§11 采样率展示；仅格式化展示，不改变原值）。 */
export function formatPercent(value: number): string {
  if (!Number.isFinite(value)) {
    return String(value);
  }
  return `${Number((value * 100).toFixed(2))}%`;
}

/** HH:mm 时间（校准窗口收口倒计时等；与 formatDateTime 同口径：非法值原样回显）。 */
export function formatTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? value
    : parsed.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}
