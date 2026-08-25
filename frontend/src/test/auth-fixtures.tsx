/*
 * 测试辅助：构造认证层测试装配（内存总线 + 可覆盖的 fake AuthApi），
 * 以及经 AuthProvider + MemoryRouter 渲染的便捷入口。
 * renderWithShell 额外包共享壳层 provider（Esc 栈 / 抽屉注册表 / 通知轮询层），
 * 供 AppShell、DrawerHost、铃铛等壳层组件测试使用。
 */

import { render, type RenderResult } from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter, type InitialEntry } from 'react-router';
import { vi } from 'vitest';
import type { AdminApi } from '../admin/api';
import { AdminProvider } from '../admin/AdminProvider';
import type { AuthApi } from '../auth/api';
import { AuthProvider } from '../auth/AuthProvider';
import { createMemoryAuthHub } from '../auth/channel';
import { AuthSessionStore } from '../auth/session';
import type { User } from '../auth/types';
import { EscStackProvider } from '../lib/esc-stack-provider';
import type { NotificationsApi } from '../notifications/api';
import { NotificationsProvider } from '../notifications/NotificationsProvider';
import { NotificationsStore } from '../notifications/store';
import { createDrawerRegistry, DrawerRegistryProvider } from '../shell/drawer/DrawerRegistryProvider';
import type { SettingsApi } from '../settings/api';
import { SettingsProvider } from '../settings/SettingsProvider';
import { ThemeController } from '../theme/theme';

export function testUser(overrides: Partial<User> = {}): User {
  return {
    id: 'u_1',
    username: 'zhangsan',
    display_name: 'zhangsan',
    real_name: 'zhangsan',
    department: { id: 'd_finance', name: 'Finance' },
    role: 'user',
    avatar_url: null,
    ...overrides,
  };
}

export function fakeAuthApi(overrides: Partial<AuthApi> = {}): AuthApi {
  return {
    login: vi.fn(async () => ({ token: 'tok_login', user: testUser() })),
    logout: vi.fn(async () => {}),
    refresh: vi.fn(async () => ({ token: 'tok_refresh' })),
    me: vi.fn(async () => testUser()),
    listSessions: vi.fn(async () => []),
    revokeSession: vi.fn(async () => {}),
    revokeAllSessions: vi.fn(async () => {}),
    ...overrides,
  };
}

export function createTestStore(api: AuthApi = fakeAuthApi()): AuthSessionStore {
  return new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
}

/** 已认证 store：bootstrap 完成（静默 refresh + 拉取档案）。 */
export async function createAuthedStore(user: User = testUser()): Promise<AuthSessionStore> {
  const store = createTestStore(
    fakeAuthApi({ login: vi.fn(async () => ({ token: 'tok_login', user })), me: vi.fn(async () => user) }),
  );
  await store.bootstrap();
  return store;
}

export function renderWithAuth(
  ui: ReactElement,
  store: AuthSessionStore,
  initialEntries: string[] = ['/'],
): RenderResult {
  return render(
    <AuthProvider store={store}>
      <MemoryRouter initialEntries={initialEntries}>{ui}</MemoryRouter>
    </AuthProvider>,
  );
}

export interface SettingsRenderDependencies {
  readonly api: SettingsApi;
  readonly theme: ThemeController;
  readonly notifications?: NotificationsStore;
}

/** 设置模块装配：AuthProvider + MemoryRouter + SettingsProvider。 */
export function renderWithSettings(
  ui: ReactElement,
  store: AuthSessionStore,
  dependencies: SettingsRenderDependencies,
  initialEntries: InitialEntry[] = ['/'],
): RenderResult {
  const notifications = dependencies.notifications ?? new NotificationsStore(fakeNotificationsApi());
  return render(
    <AuthProvider store={store}>
      <MemoryRouter initialEntries={initialEntries}>
        <SettingsProvider
          api={dependencies.api}
          authStore={store}
          theme={dependencies.theme}
          notifications={notifications}
        >
          {ui}
        </SettingsProvider>
      </MemoryRouter>
    </AuthProvider>,
  );
}

/** fake 通知 API：默认空列表、未读 0；可按用例覆盖。 */
export function fakeNotificationsApi(overrides: Partial<NotificationsApi> = {}): NotificationsApi {
  return {
    list: vi.fn(async () => ({ items: [] })),
    unreadCount: vi.fn(async () => ({ count: 0 })),
    markRead: vi.fn(async () => {}),
    markAllRead: vi.fn(async () => {}),
    ack: vi.fn(async () => {}),
    ...overrides,
  };
}

/** fake 管理面板 API：读方法默认空读模型，写方法抛出（壳层测试不触及）；可按用例覆盖。 */
export function fakeAdminApi(overrides: Partial<AdminApi> = {}): AdminApi {
  const unexpectedWrite = async (): Promise<never> => {
    throw new Error('fakeAdminApi: unexpected write call');
  };
  return {
    getDashboard: vi.fn(async () => ({ window: '7d' as const, packs: [] })),
    getOperationsMetrics: vi.fn(async () => ({ window: '7d' as const, cards: [] })),
    listUserDocuments: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    listDepartmentDocuments: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    getApprovalSummary: vi.fn(async () => ({ quota_pending: 0, submission_pending: 0 })),
    listQuotaRequests: vi.fn(async () => ({ items: [] })),
    approveQuotaRequest: vi.fn(unexpectedWrite),
    rejectQuotaRequest: vi.fn(unexpectedWrite),
    listApprovalSubmissions: vi.fn(async () => ({ items: [] })),
    approveSubmission: vi.fn(unexpectedWrite),
    rejectSubmission: vi.fn(unexpectedWrite),
    getCurrentGraphBuild: vi.fn(async () => ({
      space_id: 'public' as const,
      source_revision: 12,
      graph_availability: 'stale' as const,
      active_generation: null,
      latest_run: null,
    })),
    createGraphBuild: vi.fn(unexpectedWrite),
    cancelGraphBuild: vi.fn(unexpectedWrite),
    listOpsJobs: vi.fn(async () => ({ items: [], stale_count: 0 })),
    listOpsBackups: vi.fn(async () => ({ items: [], page: 1, page_size: 10, total: 0 })),
    createOpsBackup: vi.fn(unexpectedWrite),
    getOpsBackup: vi.fn(async () => ({
      backup_id: 'bk_1',
      status: 'complete' as const,
      created_at: '2026-08-17T18:30:00Z',
      completed_at: '2026-08-17T18:32:00Z',
      restorable: true,
      components: [],
    })),
    listOpsRestores: vi.fn(async () => ({ items: [], page: 1, page_size: 10, total: 0 })),
    createOpsRestore: vi.fn(unexpectedWrite),
    getOpsRestore: vi.fn(async () => ({
      restore_id: 'rs_1',
      backup_id: 'bk_1',
      status: 'succeeded' as const,
      created_at: '2026-08-18T03:00:00Z',
      completed_at: '2026-08-18T03:05:00Z',
      failure_reason: null,
      stages: [],
      repair_targets: [],
    })),
    retryOpsRepairTarget: vi.fn(unexpectedWrite),
    getOpsBackupPolicy: vi.fn(async () => ({
      enabled: false,
      frequency: 'daily' as const,
      local_time: '02:30',
      weekdays: [1, 2, 3, 4, 5],
      timezone: 'Asia/Shanghai',
      keep_last: 7,
      retention_days: 30,
      version: 1,
      next_run_at: null,
      last_scheduled_for: null,
      last_outcome: null,
    })),
    patchOpsBackupPolicy: vi.fn(unexpectedWrite),
    getLeaderboard: vi.fn(async () => ({
      entries: [],
      shadow_entries: [],
      policy: {
        policy_version: 'eval_2026_v1',
        min_real_queries: 50,
        shadow_max_examples: 200,
        shadow_max_candidate_configs: 3,
        calibration_open_score_gap: 0.03,
        cold_start_sample_rate: 0.4,
        sentinel_sample_rate: 0.03,
      },
    })),
    getCalibrationWindow: vi.fn(async () => ({
      window_id: null,
      status: 'closed' as const,
      opened_at: null,
      closed_at: null,
      pairs_collected: 0,
      close_deadline_at: null,
      window_kind: null,
      policy_version: null,
      sample_rate: 0,
      opened_by: null,
      closed_by: null,
    })),
    postCalibrationWindow: vi.fn(unexpectedWrite),
    listUsers: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 20 })),
    createUser: vi.fn(unexpectedWrite),
    patchUser: vi.fn(unexpectedWrite),
    deleteUser: vi.fn(unexpectedWrite),
    listDepartments: vi.fn(async () => ({ items: [] })),
    createDepartment: vi.fn(unexpectedWrite),
    renameDepartment: vi.fn(unexpectedWrite),
    deactivateDepartment: vi.fn(unexpectedWrite),
    getPermissionMatrix: vi.fn(async () => ({ capabilities: [] })),
    ...overrides,
  };
}

/** 共享壳层装配渲染：与 App.tsx 同序——AuthProvider → SettingsProvider → AdminProvider → Esc 栈 → 抽屉注册表 → 通知轮询层。 */
export function renderWithShell(
  ui: ReactElement,
  store: AuthSessionStore,
  initialEntries: InitialEntry[] = ['/'],
  options: { notifications?: NotificationsStore; settingsApi?: SettingsApi; adminApi?: AdminApi } = {},
): RenderResult {
  const notifications = options.notifications ?? new NotificationsStore(fakeNotificationsApi());
  const settingsApi: SettingsApi = options.settingsApi ?? ({
    getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
    updatePreferences: vi.fn(async (next: { theme: string; chat_font_size: string; ab_opt_out: boolean }) => next),
    getQuota: vi.fn(async () => ({
      used: 120,
      base_limit: 500,
      extra_granted: 0,
      effective_limit: 500,
      unlimited: false,
      reset_at: '2026-09-01T00:00:00+08:00',
      business_timezone: 'Asia/Shanghai',
      quota_period: '2026-08',
      business_calendar_version_id: 'calendar_1',
      pending_request: null,
    })),
    listDocuments: vi.fn(async () => ({ items: [], total: 0, page: 1, page_size: 10 })),
    listJobs: vi.fn(async () => ({ items: [], limit: 50, max_limit: 200, has_more: false })),
    listUploadSpaces: vi.fn(async () => ({ items: [] })),
    listManageSpaces: vi.fn(async () => ({ items: [] })),
    getApprovalSummary: vi.fn(async () => ({ quota_pending: 0, submission_pending: 0 })),
    listSubmissions: vi.fn(async () => ({ items: [] })),
    listVersions: vi.fn(async () => ({ document_id: 'doc_1', version: 1, active_version_id: null, items: [] })),
    listApprovals: vi.fn(async () => ({ items: [] })),
  } as unknown as SettingsApi);
  return render(
    <AuthProvider store={store}>
      <MemoryRouter initialEntries={initialEntries}>
        <SettingsProvider
          api={settingsApi}
          authStore={store}
          theme={new ThemeController(
            { dataset: {}, classList: { add: () => {}, remove: () => {} }, style: { colorScheme: '' } },
            { matches: false, addEventListener: () => {}, removeEventListener: () => {} },
          )}
          notifications={notifications}
        >
          <AdminProvider api={options.adminApi ?? fakeAdminApi()}>
            <EscStackProvider>
              <DrawerRegistryProvider registry={createDrawerRegistry()}>
                <NotificationsProvider store={notifications}>{ui}</NotificationsProvider>
              </DrawerRegistryProvider>
            </EscStackProvider>
          </AdminProvider>
        </SettingsProvider>
      </MemoryRouter>
    </AuthProvider>,
  );
}
