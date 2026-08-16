import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import { AuthProvider } from '../auth/AuthProvider';
import { createMemoryAuthHub } from '../auth/channel';
import { AuthSessionStore } from '../auth/session';
import type { User } from '../auth/types';
import { copy } from '../copy';
import { EscStackProvider } from '../lib/esc-stack-provider';
import { ApiError } from '../api/errors';
import { mockAuth, mockKnowledge, mockNotifications } from '../mocks/testing';
import { NotificationsStore } from '../notifications/store';
import type { ThemeController } from '../theme/theme';
import type { SettingsApi } from './api';
import { SettingsProvider } from './SettingsProvider';
import { UploadsLayer } from './UploadsLayer';
import { clearUploadHistory, recordUploadHistory } from './upload-history';

function testUser(): User {
  return {
    id: 'u_user',
    username: 'zhangsan',
    display_name: 'zhangsan',
    real_name: 'zhangsan',
    department: { id: 'd_finance', name: '财务部' },
    role: 'user',
    avatar_url: null,
  };
}

async function createAuthedStore(): Promise<AuthSessionStore> {
  const user = testUser();
  const api = {
    login: vi.fn(async () => ({ token: 'tok_login', user })),
    logout: vi.fn(async () => {}),
    refresh: vi.fn(async () => ({ token: 'tok_refresh' })),
    me: vi.fn(async () => user),
    listSessions: vi.fn(async () => []),
    revokeSession: vi.fn(async () => {}),
    revokeAllSessions: vi.fn(async () => {}),
  };
  const store = new AuthSessionStore({ api, bus: createMemoryAuthHub().createBus() });
  await store.login('zhangsan', 'password123');
  return store;
}

/** 契约 mock 的 SettingsApi：listJobs / getUploadBatch / ack 代理到 mock 控制器。 */
function createContractApi(token: string): SettingsApi {
  return {
    getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
    listJobs: vi.fn(async (input?: { limit?: number }) =>
      mockKnowledge.listJobs(token, input?.limit),
    ),
    listUploadSpaces: vi.fn(async () => mockKnowledge.listSpacesForUpload(token)),
    getUploadBatch: vi.fn(async (batchId) => mockKnowledge.getUploadBatch(token, batchId)),
    ackNotification: vi.fn(async (eventId) => mockNotifications.ack(token, eventId)),
    cancelJob: vi.fn(async (jobId) => mockKnowledge.cancelJob(token, jobId)),
    replayJob: vi.fn(async (jobId) => mockKnowledge.replayJob(token, jobId)),
  } as unknown as SettingsApi;
}

async function renderLayer(api: SettingsApi, notifications: NotificationsStore) {
  const store = await createAuthedStore();
  let result!: ReturnType<typeof render>;
  await act(async () => {
    result = render(
      <AuthProvider store={store}>
        <MemoryRouter initialEntries={['/settings/knowledge/uploads']}>
          <EscStackProvider>
            <SettingsProvider
              api={api}
              authStore={store}
              theme={{ setPreference: vi.fn() } as unknown as ThemeController}
              notifications={notifications}
            >
              <UploadsLayer />
            </SettingsProvider>
          </EscStackProvider>
        </MemoryRouter>
      </AuthProvider>,
    );
    await Promise.resolve();
  });
  return result;
}

afterEach(() => {
  mockKnowledge.reset();
  mockAuth.reset();
  clearUploadHistory(null);
});

describe('UploadsLayer 上传结果层（Major7 ack 时序 + Major3 历史）', () => {
  it('succeeded 任务用量渲染后才 ack；未渲染（pending）不 ack', async () => {
    const { accessToken } = mockAuth.login('zhangsan', 'password123', 'uploader');
    const token = `Bearer ${accessToken}`;
    // 契约 mock：先上传一个 manage 文档，推进 succeeded（登记 ackable 事件）
    const upload = mockKnowledge.uploadDocuments(token, 'personal:u_user', [
      { name: 'ack文档.pdf', size: 4, type: 'application/pdf' },
    ], 'idem-ack-1');
    const jobId = upload.items[0] && 'job_id' in upload.items[0] ? (upload.items[0].job_id ?? '') : '';
    expect(jobId).toBeTruthy();

    const api = createContractApi(token);
    const notifications = new NotificationsStore({
      list: vi.fn(async () => ({ items: [] })),
      unreadCount: vi.fn(async () => ({ count: 3 })),
      markRead: vi.fn(async () => {}),
      markAllRead: vi.fn(async () => {}),
      ack: vi.fn(async () => {}),
    });

    // 先渲染层（任务为 pending → 不应 ack）；再推进 succeeded → 渲染用量 → ack
    await renderLayer(api, notifications);
    // 初始 pending：不 ack（状态标签 + stage 行都可能出现「排队中」）
    await waitFor(() =>
      expect(screen.getAllByText(copy.settings.knowledge.uploads.stateLabel('pending')).length).toBeGreaterThan(0),
    );
    expect(api.ackNotification).not.toHaveBeenCalled();

    // 推进 succeeded：poll 轮询（2s 间隔）拾取 → 用量渲染 → 渲染 effect 登记 → ack
    mockKnowledge.advanceJob(token, jobId, 'succeeded');
    await waitFor(() => expect(screen.getByText(/已入库/)).toBeInTheDocument(), { timeout: 5000 });
    await waitFor(() => expect(api.ackNotification).toHaveBeenCalled(), { timeout: 5000 });
  });

  it('上传历史：最近一次上传结果在层内稳定可见', async () => {
    const { accessToken } = mockAuth.login('zhangsan', 'password123', 'viewer');
    const token = `Bearer ${accessToken}`;
    const api = createContractApi(token);
    const notifications = new NotificationsStore({
      list: vi.fn(async () => ({ items: [] })),
      unreadCount: vi.fn(async () => ({ count: 0 })),
      markRead: vi.fn(async () => {}),
      markAllRead: vi.fn(async () => {}),
      ack: vi.fn(async () => {}),
    });
    // 直接写入历史（模拟 UploadDialog 成功后 recordUploadHistory；sessionKey 与层内计算一致）
    recordUploadHistory({
      response: {
        upload_batch_id: 'ub_1',
        items: [
          {
            filename: '历史文档.pdf',
            document_id: 'doc_1',
            document_version_id: 'ver_1',
            job_id: 'job_1',
            publication_id: 'pub_1',
            deduplicated: false,
            status: 'pending',
          },
          {
            filename: '已存在文档.pdf',
            document_id: 'doc_2',
            document_version_id: 'ver_2',
            job_id: null,
            publication_id: null,
            deduplicated: true,
            status: 'deduplicated',
          },
        ],
      },
      target: { id: 'personal:u_user', kind: 'personal', name: '个人库', permission: 'manage', document_count: 3 },
      at: new Date().toISOString(),
    }, 'tok_login:u_user');

    await renderLayer(api, notifications);

    expect(await screen.findByText(copy.settings.knowledge.uploads.historyTitle)).toBeInTheDocument();
    expect(screen.getByText(/历史文档\.pdf/)).toBeInTheDocument();
    expect(screen.getByText(/已存在文档\.pdf/)).toBeInTheDocument();
  });
});

describe('UploadsLayer ack 终态（review：404/409 终止重试）', () => {
  it('ack 404 终态：只调用一次，不再逐轮重试', async () => {
    const { accessToken } = mockAuth.login('zhangsan', 'password123', 'ack404');
    const token = `Bearer ${accessToken}`;
    // succeeded 任务带 ackable 事件
    const upload = mockKnowledge.uploadDocuments(token, 'personal:u_user', [
      { name: 'ack404文档.pdf', size: 4, type: 'application/pdf' },
    ], 'idem-ack404');
    const jobId = upload.items[0] && 'job_id' in upload.items[0] ? (upload.items[0].job_id ?? '') : '';
    mockKnowledge.advanceJob(token, jobId, 'succeeded');

    const ackNotification = vi.fn(async () => {
      // 服务端拒绝（事件不存在）：终态 404
      throw new ApiError({ status: 404, code: 'not_found', message: '', details: {}, requestId: null });
    });
    const api = {
      getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
      listJobs: vi.fn(async (input?: { limit?: number }) => mockKnowledge.listJobs(token, input?.limit)),
      listUploadSpaces: vi.fn(async () => mockKnowledge.listSpacesForUpload(token)),
      getUploadBatch: vi.fn(async (batchId) => mockKnowledge.getUploadBatch(token, batchId)),
      ackNotification,
      cancelJob: vi.fn(async () => {}),
      replayJob: vi.fn(async () => ({ job_id: '', state: '', replay_generation: 0 })),
    } as unknown as SettingsApi;
    const notifications = new NotificationsStore({
      list: vi.fn(async () => ({ items: [] })),
      unreadCount: vi.fn(async () => ({ count: 0 })),
      markRead: vi.fn(async () => {}),
      markAllRead: vi.fn(async () => {}),
      ack: vi.fn(async () => {}),
    });

    await renderLayer(api, notifications);
    // 渲染用量
    await waitFor(() => expect(screen.getByText(/已入库/)).toBeInTheDocument(), { timeout: 5000 });
    // 第一次 ack 404
    await waitFor(() => expect(ackNotification).toHaveBeenCalled(), { timeout: 5000 });
    const firstCalls = ackNotification.mock.calls.length;

    // 等待一个以上轮询周期：终态失败不再重试
    await new Promise((resolve) => setTimeout(resolve, 4500));
    expect(ackNotification.mock.calls.length).toBe(firstCalls);
  });
});

describe('UploadsLayer cancel operation token（review A3）', () => {
  it('取消 A 飞行中关闭确认再打开 B：A completion 不关 B 的确认框/不写错状态', async () => {
    const { accessToken } = mockAuth.login('zhangsan', 'password123', 'cancel-token');
    const token = `Bearer ${accessToken}`;
    // 两个任务：A（挂起取消）与 B
    const uploadA = mockKnowledge.uploadDocuments(token, 'personal:u_user', [
      { name: 'cancelA.pdf', size: 4, type: 'application/pdf', contentHash: 'hash-cancel-a' },
    ], 'idem-cancel-a');
    const jobA = uploadA.items[0] && 'job_id' in uploadA.items[0] ? (uploadA.items[0].job_id ?? '') : '';
    const uploadB = mockKnowledge.uploadDocuments(token, 'personal:u_user', [
      { name: 'cancelB.pdf', size: 4, type: 'application/pdf', contentHash: 'hash-cancel-b' },
    ], 'idem-cancel-b');
    const jobB = uploadB.items[0] && 'job_id' in uploadB.items[0] ? (uploadB.items[0].job_id ?? '') : '';

    let resolveCancelA!: () => void;
    const cancelJob = vi.fn((jobId: string) => new Promise<void>((resolve) => {
      resolveCancelA = () => {
        mockKnowledge.cancelJob(token, jobId, 'idem-cancel-run');
        resolve();
      };
    }));
    const api = {
      getPreferences: vi.fn(async () => ({ theme: 'system', chat_font_size: 'standard', ab_opt_out: false })),
      listJobs: vi.fn(async (input?: { limit?: number }) => mockKnowledge.listJobs(token, input?.limit)),
      listUploadSpaces: vi.fn(async () => mockKnowledge.listSpacesForUpload(token)),
      getUploadBatch: vi.fn(async (batchId) => mockKnowledge.getUploadBatch(token, batchId)),
      ackNotification: vi.fn(async () => {}),
      cancelJob,
      replayJob: vi.fn(async () => ({ job_id: '', state: '', replay_generation: 0 })),
    } as unknown as SettingsApi;
    const notifications = new NotificationsStore({
      list: vi.fn(async () => ({ items: [] })),
      unreadCount: vi.fn(async () => ({ count: 0 })),
      markRead: vi.fn(async () => {}),
      markAllRead: vi.fn(async () => {}),
      ack: vi.fn(async () => {}),
    });
    const user = userEvent.setup();
    await renderLayer(api, notifications);

    // 对 A 发起取消（挂起）
    await user.click((await screen.findAllByRole('button', { name: copy.settings.knowledge.uploads.cancel }))[0]);
    await user.click(screen.getByRole('button', { name: copy.settings.knowledge.uploads.cancel }));
    await waitFor(() => expect(cancelJob).toHaveBeenCalledTimes(1));

    // Esc 关闭确认（token 失效），再打开 B 的取消确认
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await user.click((await screen.findAllByRole('button', { name: copy.settings.knowledge.uploads.cancel }))[1]);
    const dialogB = await screen.findByRole('dialog', { name: copy.settings.knowledge.uploads.cancelConfirmTitle });
    const confirmB = within(dialogB).getByRole('button', { name: copy.settings.knowledge.uploads.cancel }) as HTMLButtonElement;

    // A 迟到成功：不关 B 的确认框
    await act(async () => {
      resolveCancelA();
      await Promise.resolve();
    });
    expect(screen.getByRole('dialog', { name: copy.settings.knowledge.uploads.cancelConfirmTitle })).toBeInTheDocument();
    expect(confirmB.disabled).toBe(false);
    void jobA;
    void jobB;
  });
});
