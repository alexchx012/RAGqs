/*
 * Idempotency-Key 生成工具（契约 §1 / §3.7–§3.9）。
 * 调用方负责生成并保存：提问、重试、反馈、A/B 投票均须携带；网络结果未知时
 * 以原请求体复用同一键，不自动换键；同一键不得用于不同请求内容。
 */

/** 生成幂等键；优先 crypto.randomUUID，不可用时回退为随机十六进制（测试/非安全上下文）。 */
export function createIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}
