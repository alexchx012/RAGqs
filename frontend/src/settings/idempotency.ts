/*
 * 写操作 Idempotency-Key 作用域管理（review A3）。
 * - key 绑定 operation + target + payload 指纹：同一 op/target/payload 才复用同 key；
 *   用户改动目标/内容（payload 指纹变化）自动换新键，杜绝跨文档/投稿/部门/任务复用。
 * - 未知网络/超时（无业务响应）：保留同 key，重试同请求体（幂等语义）。
 * - 明确业务响应（ApiError.status !== null）：调用方调 businessResponse() 清键，
 *   下次用户显式操作拿新键；idempotency_key_conflict 同样只清键、不自动换键重发。
 * - clear() 用于对话框关闭 / 目标切换时清理，避免 A 文件残留键用于 B 文档。
 */

export interface IdempotencyScope {
  /** 取当前操作应使用的 key：op/target/payload 指纹任一变化即生成新键。 */
  keyFor(op: string, target: string, payloadFingerprint: string): string;
  /** 已收明确业务响应（status !== null）：清键（含 idempotency_key_conflict）。 */
  businessResponse(): void;
  /** 对话框关闭 / 目标切换 / 成功完成：清键。 */
  clear(): void;
}

export function newIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `idem_${crypto.randomUUID()}`;
  }
  return `idem_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
}

/** 区分「业务响应」与「网络未知/超时」：status === null 视为网络未知，可同键重试。 */
export function isBusinessResponse(error: unknown): boolean {
  return (
    error !== null &&
    typeof error === 'object' &&
    'status' in error &&
    (error as { status: unknown }).status !== null
  );
}

export function createIdempotencyScope(): IdempotencyScope {
  let op = '';
  let target = '';
  let payloadFingerprint = '';
  let key: string | null = null;

  return {
    keyFor(nextOp: string, nextTarget: string, nextPayloadFingerprint: string): string {
      if (
        key === null ||
        op !== nextOp ||
        target !== nextTarget ||
        payloadFingerprint !== nextPayloadFingerprint
      ) {
        op = nextOp;
        target = nextTarget;
        payloadFingerprint = nextPayloadFingerprint;
        key = newIdempotencyKey();
      }
      return key;
    },
    businessResponse(): void {
      key = null;
    },
    clear(): void {
      key = null;
    },
  };
}
