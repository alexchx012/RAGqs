import type { User } from '../auth/types';
import type { QuotaRequestResult, QuotaSnapshot, UserPreferences } from '../settings/types';
import { MockAuthController, MockHttpError } from './auth-contract';
import { MockQuotaStore } from './quota-contract';

export interface ProfileUpdateInput {
  readonly display_name: string;
}

export interface PasswordChangeInput {
  readonly old_password: string;
  readonly new_password: string;
}

export const DEFAULT_USER_PREFERENCES: UserPreferences = Object.freeze({
  theme: 'system',
  chat_font_size: 'standard',
  ab_opt_out: false,
});

export function isValidPassword(value: string): boolean {
  return value.length >= 8 && /[A-Za-z]/.test(value) && /\d/.test(value);
}

/**
 * Settings endpoints compose with MockAuthController instead of maintaining a second user/session store.
 * This preserves the real server semantic that a successful password update invalidates every device session.
 * 配额读数与申请统一委托给共享 MockQuotaStore（知识库上传的 quota_exceeded 判定共用同一实例）。
 */
export class MockSettingsController {
  private readonly preferences = new Map<string, UserPreferences>();

  constructor(
    private readonly auth: MockAuthController,
    private quota: MockQuotaStore | null = null,
  ) {}

  reset(): void {
    this.preferences.clear();
  }

  updateProfile(authorization: string | null, input: ProfileUpdateInput): User {
    return this.auth.updateCurrentUserDisplayName(authorization, input.display_name);
  }

  uploadAvatar(authorization: string | null, fileName: string): { avatar_url: string } {
    const avatarUrl = `/mock/avatars/${encodeURIComponent(fileName)}`;
    this.auth.updateCurrentUserAvatar(authorization, avatarUrl);
    return { avatar_url: avatarUrl };
  }

  changePassword(authorization: string | null, input: PasswordChangeInput): void {
    if (!isValidPassword(input.new_password)) {
      throw new MockHttpError(400, 'invalid_password_rule');
    }
    this.auth.changeCurrentUserPasswordAndRevokeAll(
      authorization,
      input.old_password,
      input.new_password,
    );
  }

  getPreferences(authorization: string | null): UserPreferences {
    const userId = this.auth.me(authorization).id;
    return { ...(this.preferences.get(userId) ?? DEFAULT_USER_PREFERENCES) };
  }

  updatePreferences(authorization: string | null, input: UserPreferences): UserPreferences {
    const userId = this.auth.me(authorization).id;
    const next = { ...input };
    this.preferences.set(userId, next);
    return { ...next };
  }

  getQuota(authorization: string | null): QuotaSnapshot {
    const user = this.auth.me(authorization);
    return this.quotaStore().snapshot(user.id);
  }

  requestQuota(
    authorization: string | null,
    requestedPages: number,
    idempotencyKey: string,
  ): QuotaRequestResult {
    const user = this.auth.me(authorization);
    return this.quotaStore().request(user.id, requestedPages, idempotencyKey);
  }

  private quotaStore(): MockQuotaStore {
    if (this.quota !== null) {
      return this.quota;
    }
    // 独立装配兜底：与 testing.ts 共享装配不同，这里按角色推导 unlimited。
    const fallback = new MockQuotaStore([
      { userId: 'u_user', unlimited: false, used: 120, baseLimit: 500 },
      { userId: 'u_minister', unlimited: false, used: 120, baseLimit: 500 },
      { userId: 'u_ops', unlimited: true, used: 0, baseLimit: 500 },
      { userId: 'u_admin', unlimited: true, used: 0, baseLimit: 500 },
    ]);
    this.quota = fallback;
    return fallback;
  }
}
