/*
 * 会话与问答契约 mock 行为验证（fe-chat-home 规格 §8；契约《前端接口需求.md》§3、§6.1）。
 * 直接驱动 MockChatController 全局单例（用例间经 vitest-setup 自动 reset）；
 * 鉴权头由 mockAuth.login 对种子账号签发（zhangsan→u_user、ops-wang→u_ops、admin→u_admin）。
 * 覆盖：SSE 事件序列完整性（event 名、event_seq 递增、start=1、终态互斥）、Last-Event-ID 重放、
 * 幂等键三类 409、A/B 读模型两态、反馈 409、§6.1 三 usage 集合。
 */

import { describe, expect, it } from 'vitest';
import { resolveUrl } from '../api/client';
import { MockHttpError } from './chat-contract';
import { mockAuth, mockChat } from './testing';

function bearerOf(username: string): string {
  const { accessToken } = mockAuth.login(username, 'password123', 'vitest');
  return `Bearer ${accessToken}`;
}

function expectHttpError(fn: () => unknown, status: number, code: string): void {
  try {
    fn();
  } catch (error) {
    expect(error).toBeInstanceOf(MockHttpError);
    const httpError = error as MockHttpError;
    expect(httpError.status).toBe(status);
    expect(httpError.code).toBe(code);
    return;
  }
  throw new Error(`expected MockHttpError ${status} ${code}`);
}

const ASK_BODY = { content: '测试问题', effort_level: 'quick', overrides: null } as const;

/** 为指定用户新建一个 A/B 采样会话（open 双候选），避免跨用户共享种子 pair。 */
function createAbPair(auth: string): { conversationId: string; messageId: string; pairId: string } {
  const created = mockChat.createConversation(auth);
  mockChat.abEnabled = true;
  const ask = mockChat.ask(
    auth,
    created.id,
    { content: 'A/B 新对比对', effort_level: 'think', overrides: null },
    `ab-key-${created.id}`,
  );
  void ask;
  const detail = mockChat.getConversation(auth, created.id);
  const message = detail.messages.find((item) => item.role === 'assistant');
  return {
    conversationId: created.id,
    messageId: message?.id ?? '',
    pairId: message?.role === 'assistant' ? (message.ab?.pair_id ?? '') : '',
  };
}

describe('会话与问答契约 mock', () => {
  describe('§3.7 提问 SSE 事件序列', () => {
    it('quick：start→answer→done，event_seq 从 1 单调递增', () => {
      const auth = bearerOf('zhangsan');
      const result = mockChat.ask(auth, 'c_1', { ...ASK_BODY }, 'k-quick');
      expect(result.events[0]?.seq).toBe(1);
      expect(result.events[0]?.event).toBe('start');
      const names = result.events.map((event) => event.event);
      expect(names).toEqual(['start', 'answer', 'done']);
      for (let index = 0; index < result.events.length; index += 1) {
        expect(result.events[index]?.seq).toBe(index + 1);
      }
      // start 载荷：四字段齐备
      expect(result.events[0]?.data).toMatchObject({
        generation_id: result.generationId,
        message_id: result.messageId,
        user_message_id: result.userMessageId,
        attempt_number: 1,
      });
    });

    it('think：start→stage×2→notice(effort_upgraded)→answer→done', () => {
      const auth = bearerOf('zhangsan');
      const result = mockChat.ask(auth, 'c_1', { ...ASK_BODY, effort_level: 'think' }, 'k-think');
      const names = result.events.map((event) => event.event);
      expect(names).toEqual(['start', 'stage', 'stage', 'notice', 'answer', 'done']);
      expect(result.events.find((event) => event.event === 'notice')?.data).toMatchObject({
        kind: 'effort_upgraded',
        detail: { from: 'quick', to: 'think' },
      });
    });

    it('deep：start→stage→多条 step→notice(rerank_degraded)→answer→done，answer 携带两引用', () => {
      const auth = bearerOf('zhangsan');
      const result = mockChat.ask(auth, 'c_1', { ...ASK_BODY, effort_level: 'deep' }, 'k-deep');
      const names = result.events.map((event) => event.event);
      expect(names).toEqual([
        'start',
        'stage',
        'step',
        'step',
        'step',
        'stage',
        'step',
        'notice',
        'answer',
        'done',
      ]);
      expect(result.events.filter((event) => event.event === 'step').length).toBe(4);
      const answer = result.events.find((event) => event.event === 'answer');
      expect((answer?.data as { citations: unknown[] }).citations.length).toBe(2);
      const notice = result.events.find((event) => event.event === 'notice');
      expect((notice?.data as { kind: string }).kind).toBe('rerank_degraded');
    });

    it('error 终态：start→stage→error，code 来自夹具', () => {
      const auth = bearerOf('zhangsan');
      mockChat.setNextError('source_scope_changed');
      const result = mockChat.ask(auth, 'c_1', { ...ASK_BODY }, 'k-err');
      const names = result.events.map((event) => event.event);
      expect(names).toEqual(['start', 'stage', 'error']);
      expect(result.events.at(-1)?.data).toMatchObject({ code: 'source_scope_changed' });
    });

    it('stopped 终态：start→stage→answer→stopped，stop_reason 来自夹具；其他事件无 stopped', () => {
      const auth = bearerOf('zhangsan');
      mockChat.setNextStopped('manual_request');
      const result = mockChat.ask(auth, 'c_1', { ...ASK_BODY }, 'k-stop');
      const names = result.events.map((event) => event.event);
      expect(names).toEqual(['start', 'stage', 'answer', 'stopped']);
      expect(result.events.at(-1)?.data).toMatchObject({ status: 'stopped', stop_reason: 'manual_request' });
    });

    it('A/B：start→stage→ab_start→stage→answer(0)→answer(1)→done，ab_start 先于双 answer', () => {
      const auth = bearerOf('zhangsan');
      mockChat.abEnabled = true;
      const result = mockChat.ask(auth, 'c_1', { ...ASK_BODY, effort_level: 'think' }, 'k-ab');
      const names = result.events.map((event) => event.event);
      expect(names).toEqual(['start', 'stage', 'ab_start', 'stage', 'answer', 'answer', 'done']);
      const abStartIndex = names.indexOf('ab_start');
      const firstAnswer = names.indexOf('answer');
      expect(abStartIndex).toBeGreaterThan(0);
      expect(firstAnswer).toBeGreaterThan(abStartIndex);
      const answers = result.events
        .filter((event) => event.event === 'answer')
        .map((event) => (event.data as { candidate: number }).candidate);
      expect(answers).toEqual([0, 1]);
      expect((result.events.find((event) => event.event === 'ab_start')?.data as { candidates: number[] }).candidates).toEqual([0, 1]);
    });

    it('终态互斥：一次 generation 只含 done / error / stopped 之一', () => {
      const auth = bearerOf('zhangsan');
      const done = mockChat.ask(auth, 'c_1', { ...ASK_BODY }, 'k-mx-1');
      const terminalKinds = done.events.map((event) => event.event).filter((name) => ['done', 'error', 'stopped'].includes(name));
      expect(terminalKinds).toEqual(['done']);

      mockChat.setNextError('provider_error');
      const errored = mockChat.ask(auth, 'c_1', { ...ASK_BODY }, 'k-mx-2');
      expect(errored.events.map((event) => event.event).filter((name) => ['done', 'error', 'stopped'].includes(name))).toEqual(['error']);

      mockChat.setNextStopped('client_disconnected');
      const stopped = mockChat.ask(auth, 'c_1', { ...ASK_BODY }, 'k-mx-3');
      expect(stopped.events.map((event) => event.event).filter((name) => ['done', 'error', 'stopped'].includes(name))).toEqual(['stopped']);
    });

    it('m15：A/B 采样 + error/stopped 终态 → 读模型 ab=null、按唯一稳定候选作为普通回答（§3.3）', () => {
      const auth = bearerOf('zhangsan');
      // 校准采样 + error 终态：error 分支不发 answer（§3.7 停止/失败发生在成功提交前则无正文）
      mockChat.abEnabled = true;
      mockChat.setNextError('provider_error');
      const failed = mockChat.ask(auth, 'c_1', { ...ASK_BODY, effort_level: 'think' }, 'k-ab-fail');
      expect(failed.events.at(-1)?.event).toBe('error');
      const failedDetail = mockChat.getConversation(auth, 'c_1');
      const failedMsg = failedDetail.messages.find((m) => m.role === 'assistant' && m.id === failed.messageId);
      expect(failedMsg?.role).toBe('assistant');
      if (failedMsg?.role === 'assistant') {
        // 终态 A/B pair 不再进入对比视图（ab=null），按普通回答渲染
        expect(failedMsg.ab).toBeNull();
        expect(failedMsg.status).toBe('failed');
      }

      // 校准采样 + stopped 终态：stopped 分支先发 candidate 0 answer 再终态
      mockChat.abEnabled = true;
      mockChat.setNextStopped('manual_request');
      const stopped = mockChat.ask(auth, 'c_1', { ...ASK_BODY, effort_level: 'think' }, 'k-ab-stop');
      expect(stopped.events.at(-1)?.event).toBe('stopped');
      const stoppedDetail = mockChat.getConversation(auth, 'c_1');
      const stoppedMsg = stoppedDetail.messages.find((m) => m.role === 'assistant' && m.id === stopped.messageId);
      expect(stoppedMsg?.role).toBe('assistant');
      if (stoppedMsg?.role === 'assistant') {
        expect(stoppedMsg.ab).toBeNull();
        expect(stoppedMsg.status).toBe('stopped');
        expect(stoppedMsg.stop_reason).toBe('manual_request');
        expect(stoppedMsg.content.length).toBeGreaterThan(0); // 已收稳定候选正文保留
      }
    });
  });

  describe('Last-Event-ID 重放', () => {
    it('不带 Last-Event-ID：从 start 完整重放；带 =1：跳过 start（seq=1）重放其余', () => {
      const auth = bearerOf('zhangsan');
      const created = mockChat.ask(auth, 'c_1', { ...ASK_BODY }, 'k-replay');
      const generationId = created.generationId;

      const full = mockChat.listEvents(auth, generationId, null);
      expect(full.map((event) => event.seq)).toEqual(created.events.map((event) => event.seq));
      expect(full[0]?.event).toBe('start');

      const partial = mockChat.listEvents(auth, generationId, 1);
      expect(partial.every((event) => event.seq > 1)).toBe(true);
      expect(partial.some((event) => event.event === 'start')).toBe(false);
      expect(partial.some((event) => event.event === 'answer')).toBe(true);
    });
  });

  describe('幂等键语义（§3.7–§3.9）', () => {
    it('提问：同键同内容重放原 generation（不新增 user 消息）；同键不同内容 → 409 idempotency_key_conflict', () => {
      const auth = bearerOf('zhangsan');
      const first = mockChat.ask(auth, 'c_1', { ...ASK_BODY }, 'key-ask');
      // 重放不新增用户消息：读模型 user 消息数与提问前一致（种子 c_1 已含一条）
      const before = mockChat.getConversation(auth, 'c_1').messages.filter((message) => message.role === 'user').length;
      const replay = mockChat.ask(auth, 'c_1', { ...ASK_BODY }, 'key-ask');
      expect(replay.generationId).toBe(first.generationId);
      expect(replay.messageId).toBe(first.messageId);
      const after = mockChat.getConversation(auth, 'c_1').messages.filter((message) => message.role === 'user').length;
      expect(after).toBe(before);

      expectHttpError(
        () => mockChat.ask(auth, 'c_1', { ...ASK_BODY, content: '不同内容' }, 'key-ask'),
        409,
        'idempotency_key_conflict',
      );
    });

    it('重试：同键同目标重放；新键重复操作关联既有重试结果（不创建分叉）；同键不同目标 → 409', () => {
      const auth = bearerOf('zhangsan');
      mockChat.setNextError('provider_error');
      const failed = mockChat.ask(auth, 'c_1', { ...ASK_BODY }, 'retry-parent');

      const first = mockChat.retry(auth, failed.generationId, 'retry-key');
      expect(first.attemptNumber).toBe(2);
      expect(first.events[0]?.data).toMatchObject({ attempt_number: 2, user_message_id: failed.userMessageId });
      const replay = mockChat.retry(auth, failed.generationId, 'retry-key');
      expect(replay.generationId).toBe(first.generationId);

      // 新键对同一 failed generation 的重复操作：关联既有重试结果，不创建分叉（§3.7）
      const repeat = mockChat.retry(auth, failed.generationId, 'retry-key-other');
      expect(repeat.generationId).toBe(first.generationId);

      // 同键不同目标（另一 failed generation）：409 idempotency_key_conflict
      mockChat.setNextError('provider_error');
      const otherFailed = mockChat.ask(auth, 'c_1', { ...ASK_BODY, content: '另一个失败' }, 'retry-parent-2');
      expectHttpError(() => mockChat.retry(auth, otherFailed.generationId, 'retry-key'), 409, 'idempotency_key_conflict');

      // 重试链：读模型三条 assistant 消息（种子 + 原失败 + 后继），后继同 root_generation_id
      const detail = mockChat.getConversation(auth, 'c_1');
      const assistants = detail.messages.filter((message) => message.role === 'assistant');
      const original = assistants.find((message) => message.role === 'assistant' && message.generation_id === failed.generationId);
      const successor = assistants.find((message) => message.role === 'assistant' && message.generation_id === first.generationId);
      expect(original?.role).toBe('assistant');
      expect(successor?.role).toBe('assistant');
      if (original?.role === 'assistant' && successor?.role === 'assistant') {
        expect(original.status).toBe('failed');
        expect(successor.status).toBe('completed');
        expect(original.root_generation_id).toBe(successor.root_generation_id);
        expect(successor.retry_of_generation_id).toBe(original.generation_id);
        expect(successor.attempt_number).toBe(2);
      }
    });

    it('停止：running 首次 202 stop_requested；随后自动补发 stopped 并推 live stream；重复 stop 200', async () => {
      const auth = bearerOf('zhangsan');
      // 种子 c_1 的 assistant 已 completed：对其 stop → 409 generation_already_terminal
      const detail = mockChat.getConversation(auth, 'c_1');
      const seeded = detail.messages.find((message) => message.role === 'assistant');
      expectHttpError(() => mockChat.stopGeneration(auth, seeded?.role === 'assistant' ? seeded.generation_id : 'g_missing'), 409, 'generation_already_terminal');

      // running generation：stop → 立即 202 stop_requested，微任务后补发 stopped(manual_request)
      const pending = mockChat.startPendingGeneration(auth, 'c_1', { content: '运行中', effort_level: 'quick' });
      const pushed: string[] = [];
      let closed = false;
      const handle = {
        push(frame: string) {
          pushed.push(frame);
        },
        close() {
          closed = true;
        },
      };
      mockChat.registerLiveStream(auth, pending.generationId, handle);
      expect(mockChat.liveStreamCount(pending.generationId)).toBe(1);

      const first = mockChat.stopGeneration(auth, pending.generationId);
      expect(first.status).toBe('stop_requested');
      // 微任务调度补发：等一个 tick
      await Promise.resolve();
      await Promise.resolve();
      expect(pushed.some((frame) => frame.includes('event: stopped'))).toBe(true);
      expect(pushed.some((frame) => frame.includes('manual_request'))).toBe(true);
      expect(closed).toBe(true);
      expect(mockChat.liveStreamCount(pending.generationId)).toBe(0);

      // 已 stopped 后重复 stop → 200 终态形状
      const again = mockChat.stopGeneration(auth, pending.generationId);
      expect(again.status).toBe('stopped');
      expect(again.stop_reason).toBe('manual_request');
    });

    it('live stream unregister 必须用同一 handle 引用（cancel 清理有效）', () => {
      const auth = bearerOf('zhangsan');
      const pending = mockChat.startPendingGeneration(auth, 'c_1', { content: '流清理', effort_level: 'quick' });
      const handle = { push() {}, close() {} };
      mockChat.registerLiveStream(auth, pending.generationId, handle);
      expect(mockChat.liveStreamCount(pending.generationId)).toBe(1);
      // 错误：新对象 delete 不命中
      mockChat.unregisterLiveStream(pending.generationId, { push() {}, close() {} });
      expect(mockChat.liveStreamCount(pending.generationId)).toBe(1);
      // 正确：同引用
      mockChat.unregisterLiveStream(pending.generationId, handle);
      expect(mockChat.liveStreamCount(pending.generationId)).toBe(0);
    });
  });

  describe('反馈与 A/B 投票（§3.8 / §3.9）', () => {
    it('反馈：首次 204 幂等记录；同键不同请求 409；新键重复 409 feedback_already_submitted；👎 校验 reason', () => {
      const auth = bearerOf('zhangsan');
      // 种子 c_ab 的 assistant 未投反馈
      const detail = mockChat.getConversation(auth, 'c_ab');
      const abMessage = detail.messages.find((message) => message.role === 'assistant');
      expect(abMessage?.role).toBe('assistant');
      const messageId = abMessage?.role === 'assistant' ? abMessage.id : '';
      expect(messageId).toBeTruthy();

      expect(() => mockChat.submitFeedback(auth, messageId, { vote: 'down', reason: 'no_grounding' }, 'fb-key')).not.toThrow();
      expect(() => mockChat.submitFeedback(auth, messageId, { vote: 'down', reason: 'no_grounding' }, 'fb-key')).not.toThrow();
      expectHttpError(
        () => mockChat.submitFeedback(auth, messageId, { vote: 'down', reason: 'no_grounding' }, 'fb-key-other'),
        409,
        'feedback_already_submitted',
      );
      expectHttpError(
        () => mockChat.submitFeedback(auth, messageId, { vote: 'up' }, 'fb-key-yet'),
        409,
        'feedback_already_submitted',
      );
      expectHttpError(
        () => mockChat.submitFeedback(auth, messageId, { vote: 'down', reason: 'bad_reason' as never }, 'fb-key-bad'),
        422,
        'validation_error',
      );
      // 读模型已投态
      const after = mockChat.getConversation(auth, 'c_ab');
      const voted = after.messages.find((message) => message.id === messageId);
      expect(voted?.role).toBe('assistant');
      if (voted?.role === 'assistant') {
        expect(voted.feedback).toEqual({ vote: 'down', down_reason: 'no_grounding' });
      }
    });

    it('A/B 投票：0/1 后读模型 voted:true 单候选且恢复常设反馈；neither 后无正文无反馈', () => {
      const auth = bearerOf('zhangsan');
      const auth2 = bearerOf('minister-li');
      const ab1 = createAbPair(auth);
      const ab2 = createAbPair(auth2);

      // 0/1：投票后读模型 voted:true 单候选
      const result = mockChat.submitAbVote(auth, ab1.messageId, { pair_id: ab1.pairId, choice: '0' }, 'ab-key');
      expect(result).toEqual({ pair_id: ab1.pairId, voted: true, choice: '0' });

      const voted = mockChat.getConversation(auth, ab1.conversationId);
      const votedMessage = voted.messages.find((item) => item.id === ab1.messageId);
      expect(votedMessage?.role).toBe('assistant');
      if (votedMessage?.role !== 'assistant') return;
      expect(votedMessage.ab).toMatchObject({ status: 'voted', voted: true, choice: '0', candidates: null });
      expect(votedMessage.content).toBeTruthy(); // 所选候选正文保留
      expect(votedMessage.content).not.toContain('[candidate 1]');
      // 0/1 投票后恢复常设 👍👎（feedback 照常参与）
      expect(() => mockChat.submitFeedback(auth, ab1.messageId, { vote: 'up' }, 'ab-fb-key')).not.toThrow();

      // neither：无候选正文、feedback=null、不渲染常设反馈
      mockChat.submitAbVote(auth2, ab2.messageId, { pair_id: ab2.pairId, choice: 'neither' }, 'ab-key-n');
      const afterNeither = mockChat.getConversation(auth2, ab2.conversationId);
      const neitherMessage = afterNeither.messages.find((item) => item.id === ab2.messageId);
      expect(neitherMessage?.role).toBe('assistant');
      if (neitherMessage?.role !== 'assistant') return;
      expect(neitherMessage.content).toBe('');
      expect(neitherMessage.feedback).toBeNull();
      expect(neitherMessage.ab).toMatchObject({ status: 'voted', voted: true, choice: 'neither' });
      // neither 不渲染常设反馈：再投反馈 → 409
      expectHttpError(() => mockChat.submitFeedback(auth2, ab2.messageId, { vote: 'up' }, 'n-fb-key'), 409, 'feedback_already_submitted');
    });

    it('A/B 投票 409：换键重复 → ab_vote_already_submitted；pair 过期 → ab_pair_expired；同键不同请求 → idempotency_key_conflict', () => {
      const auth = bearerOf('zhangsan');
      const authO = bearerOf('minister-li');
      const ab1 = createAbPair(auth);
      const ab2 = createAbPair(authO);

      mockChat.submitAbVote(auth, ab1.messageId, { pair_id: ab1.pairId, choice: '0' }, 'ab-k1');
      expectHttpError(() => mockChat.submitAbVote(auth, ab1.messageId, { pair_id: ab1.pairId, choice: '1' }, 'ab-k2'), 409, 'ab_vote_already_submitted');
      // 同键不同请求体（choice 不同）：409 idempotency_key_conflict
      expectHttpError(() => mockChat.submitAbVote(auth, ab1.messageId, { pair_id: ab1.pairId, choice: '1' }, 'ab-k1'), 409, 'idempotency_key_conflict');
      // 同键同请求体：重放原 200
      const replay = mockChat.submitAbVote(auth, ab1.messageId, { pair_id: ab1.pairId, choice: '0' }, 'ab-k1');
      expect(replay).toEqual({ pair_id: ab1.pairId, voted: true, choice: '0' });

      // 过期 pair：expirePair 后再投 → ab_pair_expired
      mockChat.expirePair(authO, ab2.pairId);
      expectHttpError(() => mockChat.submitAbVote(authO, ab2.messageId, { pair_id: ab2.pairId, choice: '0' }, 'ab-kO'), 409, 'ab_pair_expired');
    });
  });

  describe('会话与分组 CRUD（§3.1–§3.6）', () => {
    it('分组 CRUD：创建 / 重命名 / 删除（删组后组内会话归未分组）', () => {
      const auth = bearerOf('zhangsan');
      const group = mockChat.createGroup(auth, '新分组');
      expect(group.name).toBe('新分组');
      const renamed = mockChat.patchGroup(auth, group.id, '改名');
      expect(renamed.name).toBe('改名');
      // 移入分组
      mockChat.patchConversation(auth, 'c_1', { group_id: group.id });
      const inGroup = mockChat.listConversations(auth).items.find((item) => item.id === 'c_1');
      expect(inGroup?.group_id).toBe(group.id);
      // 删除分组：归未分组
      mockChat.deleteGroup(auth, group.id);
      const after = mockChat.listConversations(auth).items.find((item) => item.id === 'c_1');
      expect(after?.group_id).toBeNull();
      // 已删分组再删 → 404
      expectHttpError(() => mockChat.deleteGroup(auth, group.id), 404, 'not_found');
    });

    it('PATCH 三合一：title / pinned / group_id 分别或组合生效；q 按标题过滤', () => {
      const auth = bearerOf('zhangsan');
      mockChat.patchConversation(auth, 'c_1', { title: '置顶标题', pinned: true });
      const all = mockChat.listConversations(auth);
      const c1 = all.items.find((item) => item.id === 'c_1');
      expect(c1).toMatchObject({ title: '置顶标题', pinned: true });
      expect(all.items[0]?.id).toBe('c_1'); // 置顶优先

      const filtered = mockChat.listConversations(auth, '置顶');
      expect(filtered.items.every((item) => item.title.includes('置顶'))).toBe(true);
      const unmatched = mockChat.listConversations(auth, '不存在关键词');
      expect(unmatched.items).toHaveLength(0);
    });

    it('删除会话：204 语义（controller 无返回）；再读 → 404', () => {
      const auth = bearerOf('zhangsan');
      mockChat.deleteConversation(auth, 'c_1');
      expectHttpError(() => mockChat.getConversation(auth, 'c_1'), 404, 'conversation_not_found');
    });

    it('无有效 Bearer → 401', () => {
      expectHttpError(() => mockChat.listConversations(null), 401, 'invalid_token');
      expectHttpError(() => mockChat.ask(null, 'c_1', { ...ASK_BODY }, 'k'), 401, 'invalid_token');
      expectHttpError(() => mockChat.listSpaces(null, 'retrieval'), 401, 'invalid_token');
    });
  });

  describe('§3.3 读模型（刷新恢复）', () => {
    it('generating 状态消息：running generation 的 assistant 为 generating（供刷新恢复订阅）', () => {
      const auth = bearerOf('zhangsan');
      const pending = mockChat.startPendingGeneration(auth, 'c_1', { content: '在跑问题', effort_level: 'think' });
      const detail = mockChat.getConversation(auth, 'c_1');
      const message = detail.messages.find((item) => item.id === pending.messageId);
      expect(message?.role).toBe('assistant');
      if (message?.role === 'assistant') {
        expect(message.status).toBe('generating');
        expect(message.generation_id).toBe(pending.generationId);
      }
    });

    it('重试链读模型：同 root_generation_id 多条 assistant，原失败保留、后继为 completed', () => {
      const auth = bearerOf('zhangsan');
      mockChat.setNextError('provider_error');
      const failed = mockChat.ask(auth, 'c_1', { ...ASK_BODY }, 'chain-ask');
      const retried = mockChat.retry(auth, failed.generationId, 'chain-retry');
      const detail = mockChat.getConversation(auth, 'c_1');
      const assistants = detail.messages.filter((message) => message.role === 'assistant');
      const original = assistants.find(
        (message) => message.role === 'assistant' && message.generation_id === failed.generationId,
      );
      const successor = assistants.find(
        (message) => message.role === 'assistant' && message.generation_id === retried.generationId,
      );
      expect(original?.role).toBe('assistant');
      expect(successor?.role).toBe('assistant');
      if (original?.role === 'assistant' && successor?.role === 'assistant') {
        expect(original.root_generation_id).toBe(successor.root_generation_id);
        expect(original.retry_of_generation_id).toBeNull();
        expect(original.status).toBe('failed');
        expect(successor.status).toBe('completed');
        expect(successor.retry_of_generation_id).toBe(original.generation_id);
        expect(successor.attempt_number).toBe(2);
      }
    });
  });

  describe('§6.1 GET /spaces 三 usage 返回集', () => {
    it('retrieval（普通用户）：本人个人库 + active 部门库 + 公共库，不含他人个人库与 inactive 部门库', () => {
      const auth = bearerOf('zhangsan');
      const items = mockChat.listSpaces(auth, 'retrieval');
      const ids = items.map((item) => item.id);
      expect(ids).toContain('personal:u_user');
      expect(ids).toContain('department:d_finance');
      expect(ids).toContain('public');
      expect(ids).not.toContain('personal:u_minister');
      expect(ids).not.toContain('department:d_archived');
      expect(ids).not.toContain('department:d_hr'); // 普通用户非本部门
      // 权限值：本人个人库 manage、公共库 contribute、本部门库 read
      const personal = items.find((item) => item.id === 'personal:u_user');
      const publicSpace = items.find((item) => item.id === 'public');
      const finance = items.find((item) => item.id === 'department:d_finance');
      expect(personal?.permission).toBe('manage');
      expect(publicSpace?.permission).toBe('contribute');
      expect(finance?.permission).toBe('read');
    });

    it('retrieval（ops）：本人个人库 + 全部 active 部门库 + 公共库', () => {
      const auth = bearerOf('ops-wang');
      const ids = mockChat.listSpaces(auth, 'retrieval').map((item) => item.id);
      expect(ids).toContain('personal:u_ops');
      expect(ids).toContain('department:d_finance');
      expect(ids).toContain('department:d_hr');
      expect(ids).toContain('public');
      expect(ids).not.toContain('department:d_archived'); // inactive 不进 retrieval
      expect(ids).not.toContain('personal:u_user'); // 他人个人库
    });

    it('upload：retrieval 集合中 permission 为 manage/contribute；read 空间不是上传目标', () => {
      const auth = bearerOf('zhangsan');
      const ids = mockChat.listSpaces(auth, 'upload').map((item) => item.id);
      expect(ids).toContain('personal:u_user');
      expect(ids).toContain('public');
      expect(ids).not.toContain('department:d_finance'); // 普通用户本部门为 read，非上传目标
      // 部长：本部门 manage → 进入 upload
      const ministerIds = mockChat.listSpaces(bearerOf('minister-li'), 'upload').map((item) => item.id);
      expect(ministerIds).toContain('department:d_finance');
    });

    it('manage：全部可读空间，含他人个人库（read）与 inactive 部门库（read）', () => {
      const auth = bearerOf('ops-wang');
      const items = mockChat.listSpaces(auth, 'manage');
      const ids = items.map((item) => item.id);
      expect(ids).toContain('personal:u_user');
      expect(ids).toContain('personal:u_ops');
      expect(ids).toContain('department:d_archived');
      expect(ids).toContain('department:d_finance');
      const archived = items.find((item) => item.id === 'department:d_archived');
      expect(archived?.permission).toBe('read');
      expect(archived?.department_status).toBe('inactive');
      const others = items.find((item) => item.id === 'personal:u_user');
      expect(others?.permission).toBe('read');
      expect(items.find((item) => item.id === 'personal:u_ops')?.permission).toBe('manage');
    });

    it('非法 usage → 422 validation_error', () => {
      const auth = bearerOf('zhangsan');
      expectHttpError(() => mockChat.listSpaces(auth, 'bogus' as never), 422, 'validation_error');
    });
  });

  describe('SSE 心跳 comment 不计入事件序号（传输层）', () => {
    it('mock 序列不含 comment 事件且 seq 连续从 1 起', () => {
      // comment 由 handler 在流首注入（不进入 controller 事件数组）；此处验证事件数组
      // seq 严格连续且无 0 / 无跳号——心跳不占 event_seq 的传输层语义由
      // chat-handlers 的 sseFrame 组装保证（comment 帧不带 id，不计入序号）。
      const auth = bearerOf('zhangsan');
      const result = mockChat.ask(auth, 'c_1', { ...ASK_BODY }, 'k-heartbeat');
      const seqs = result.events.map((event) => event.seq);
      expect(seqs[0]).toBe(1);
      for (let index = 1; index < seqs.length; index += 1) {
        expect(seqs[index]).toBe(seqs[index - 1] + 1);
      }
      // 事件数组不含 comment（comment 由 handler 在流首注入，不进 controller 序列）
      expect(result.events.every((event) => (event.event as string) !== 'comment')).toBe(true);
    });

    it('经 MSW 真实流：流首心跳 comment 帧无 id，start 帧 id=1（心跳不占 event_seq）', async () => {
      const { accessToken } = mockAuth.login('zhangsan', 'password123', 'vitest');
      const response = await fetch(resolveUrl('/v1/conversations/c_1/messages'), {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${accessToken}`,
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
          'Idempotency-Key': 'k-heartbeat-wire',
        },
        body: JSON.stringify({ content: '心跳验证', effort_level: 'quick', overrides: null }),
      });
      expect(response.status).toBe(200);
      expect(response.headers.get('Content-Type')).toBe('text/event-stream');
      const text = await response.text();
      // 流首为心跳 comment：无 id 行，不计入 event_seq
      expect(text.startsWith(': heartbeat\n\n')).toBe(true);
      // start 帧 id=1
      expect(text).toContain('id: 1\nevent: start\n');
      // 事件 id 连续且从 1 起，无 id: 0 / 跳号
      const ids = [...text.matchAll(/^id: (\d+)$/gm)].map((match) => Number(match[1]));
      expect(ids[0]).toBe(1);
      for (let index = 1; index < ids.length; index += 1) {
        expect(ids[index]).toBe(ids[index - 1] + 1);
      }
      // comment 帧不产生 id 行（若 comment 占了序号，ids 会含 0 或跳号）
      expect(ids.some((id) => id === 0)).toBe(false);
    });
  });
});
