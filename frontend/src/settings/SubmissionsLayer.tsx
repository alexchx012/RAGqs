/*
 * 我的投稿层（settings-personal §8，仅 user/minister；知识库模块下钻子界面）。
 * - 六档筛选 chip（全部/待审核/已通过/已驳回/已撤回/已失效），切换即重新请求。
 * - 行 = 文件名 + 目标空间 + 投稿时间 + 状态 tag 五态着色；驳回原因填了才显示在 tag 下方；
 *   invalidated 显示固定机器原因提示，不展示原始机器码、不提供审核重试。
 * - 行操作按状态渲染：pending=查看内容+撤回；approved=查看内容；rejected/withdrawn/invalidated=查看内容+删除。
 * - 查看内容：触发原件下载 GET /submissions/{id}/content；404 submission_content_unavailable 行尾就地提示。
 * - 撤回：二次确认固定两点说明；200 后行原地保留转「已撤回」，状态 tag 一次性淡入。
 * - 删除：204 后行 opacity→0 收拢移除；无回收站与恢复入口。
 * - 失败处理：行尾危险红错误行 + 重试文字链；409 version_conflict 刷新后基于最新 version 重新确认；
 *   409 submission_state_conflict 刷新列表按最新状态呈现。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../api/errors';
import { copy } from '../copy';
import { ConfirmDialog } from '../ui/ConfirmDialog';
import { EmptyState, ErrorState, LoadingRows } from '../ui/states';
import { TextLink } from '../ui/TextLink';
import { useSettings } from './SettingsProvider';
import { downloadSubmissionContent } from './download-submission-content';
import { createIdempotencyScope, isBusinessResponse } from './idempotency';
import type { Submission, SubmissionStatus } from './types';

function formatDateTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString('zh-CN');
}

const FILTERS: readonly { readonly value: SubmissionStatus | 'all'; readonly label: string }[] = [
  { value: 'all', label: copy.settings.knowledge.submissions.filters.all },
  { value: 'pending', label: copy.settings.knowledge.submissions.filters.pending },
  { value: 'approved', label: copy.settings.knowledge.submissions.filters.approved },
  { value: 'rejected', label: copy.settings.knowledge.submissions.filters.rejected },
  { value: 'withdrawn', label: copy.settings.knowledge.submissions.filters.withdrawn },
  { value: 'invalidated', label: copy.settings.knowledge.submissions.filters.invalidated },
];

/** 状态 tag 一次性淡入标记时长（--duration-fast 150ms + 余量；模式同 DepartmentsLayer RENAME_FADE_MS）。 */
const STATUS_FADE_MS = 200;

/** 五态 tag 着色（pending ash-gray / approved 成功绿 / rejected 危险红 / withdrawn slate-gray / invalidated 警告琥珀）。 */
function statusClass(status: SubmissionStatus): string {
  switch (status) {
    case 'pending':
      return 'bg-ash-gray/20 text-ash-gray';
    case 'approved':
      return 'bg-success/15 text-success';
    case 'rejected':
      return 'bg-danger/15 text-danger';
    case 'withdrawn':
      return 'bg-slate-gray/15 text-slate-gray';
    case 'invalidated':
      return 'bg-warning/15 text-warning';
  }
}

export function SubmissionsLayer() {
  const { api } = useSettings();
  const [filter, setFilter] = useState<SubmissionStatus | 'all'>('all');
  const [submissions, setSubmissions] = useState<readonly Submission[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [pendingWithdraw, setPendingWithdraw] = useState<Submission | null>(null);
  const [pendingDelete, setPendingDelete] = useState<Submission | null>(null);
  const [confirmingWithdraw, setConfirmingWithdraw] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [rowErrors, setRowErrors] = useState<Map<string, { message: string; retry: () => void }>>(new Map());
  /** 就地状态迁移（撤回）后需一次性淡入状态 tag 的行 id（《用户端设计.md》§5.2 交叉淡变）。 */
  const [statusChangedIds, setStatusChangedIds] = useState<ReadonlySet<string>>(new Set());
  const withdrawIdem = useRef(createIdempotencyScope());
  const deleteIdem = useRef(createIdempotencyScope());
  // filter/view generation（review A4）：切换 filter 使旧 mutation closure 失效；
  // 迟到成功/冲突不得用旧 filter 覆盖新筛选视图。
  const mutationEpochRef = useRef(0);

  const submissionsSeqRef = useRef(0);
  const loadSubmissions = useCallback(
    async (nextFilter: SubmissionStatus | 'all') => {
      // filter 切换即重新请求：旧 filter 响应不得覆盖当前结果（review Major 2）
      const seq = ++submissionsSeqRef.current;
      setLoading(true);
      setLoadError(false);
      try {
        const response = await api.listSubmissions(nextFilter);
        if (seq !== submissionsSeqRef.current) {
          return;
        }
        setSubmissions(response.items);
      } catch {
        if (seq === submissionsSeqRef.current) {
          setLoadError(true);
        }
      } finally {
        if (seq === submissionsSeqRef.current) {
          setLoading(false);
        }
      }
    },
    [api],
  );

  useEffect(() => {
    void loadSubmissions(filter);
  }, [filter, loadSubmissions]);

  const openContent = async (submission: Submission) => {
    setRowErrors((errors) => {
      const next = new Map(errors);
      next.delete(submission.submission_id);
      return next;
    });
    try {
      const blob = await api.getSubmissionContent(submission.submission_id);
      downloadSubmissionContent(blob, submission.file_name);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404 && error.code === 'submission_content_unavailable') {
        setRowErrors((errors) => {
          const next = new Map(errors);
          next.set(submission.submission_id, {
            message: copy.settings.knowledge.submissions.contentUnavailable,
            retry: () => void openContent(submission),
          });
          return next;
        });
      } else {
        setRowErrors((errors) => {
          const next = new Map(errors);
          next.set(submission.submission_id, {
            message: copy.settings.knowledge.submissions.actionError,
            retry: () => void openContent(submission),
          });
          return next;
        });
      }
    }
  };

  /** 撤回等就地状态迁移后，新状态 tag 以 --duration-fast 淡入一次（模式同 DepartmentsLayer.markRenamed）。 */
  const markStatusChanged = (submissionId: string): void => {
    setStatusChangedIds((current) => new Set(current).add(submissionId));
    window.setTimeout(() => {
      setStatusChangedIds((current) => {
        const next = new Set(current);
        next.delete(submissionId);
        return next;
      });
    }, STATUS_FADE_MS);
  };

  /** 实际撤回（接收 submission 参数，避免 retry 读取尚未提交的 pendingWithdraw 而 no-op）。 */
  const runWithdraw = async (submission: Submission) => {
    const epoch = mutationEpochRef.current;
    const filterAtLaunch = filter;
    setConfirmingWithdraw(true);
    setActionError(null);
    setRowErrors((errors) => {
      const next = new Map(errors);
      next.delete(submission.submission_id);
      return next;
    });
    // key 绑定 target(submission)+payload(version)
    const idempotencyKey = withdrawIdem.current.keyFor(
      'withdraw-submission',
      submission.submission_id,
      `version:${submission.version}`,
    );
    try {
      const updated = await api.withdrawSubmission(submission.submission_id, submission.version, idempotencyKey);
      if (epoch !== mutationEpochRef.current) {
        return; // 已切换 filter：旧 mutation 失效，忽略本地更新（由新 filter 加载覆盖）
      }
      withdrawIdem.current.clear();
      // 契约 §6.10：撤回响应只含 { submission_id, version, status }；与原行合并。
      // 《用户端设计.md》§5.2：200 后该行原地保留并转「已撤回」（状态 tag 一次性淡入），
      // 即使当前筛选为「待审核」也不移除；下一次按筛选重新请求后按服务端结果呈现。
      markStatusChanged(updated.submission_id);
      setSubmissions((items) => {
        if (epoch !== mutationEpochRef.current) {
          return items; // 旧 mutation 已失效：不写入
        }
        if (!items.some((item) => item.submission_id === updated.submission_id)) {
          return items;
        }
        return items.map((item) =>
          item.submission_id === updated.submission_id
            ? { ...item, version: updated.version, status: updated.status }
            : item,
        );
      });
      setPendingWithdraw(null);
    } catch (error) {
      if (epoch !== mutationEpochRef.current) {
        // 旧 mutation 已失效（filter 已切换/确认框已关闭）：
        // 409 不得 loadSubmissions(oldFilter)、不得关闭新确认框、不得覆盖当前错误（review A2）
        return;
      }
      if (error instanceof ApiError && error.status === 409 && error.code === 'version_conflict') {
        // 刷新后基于最新 version 重新确认（保留待确认行）
        withdrawIdem.current.businessResponse();
        setPendingWithdraw(null);
        await loadSubmissions(filterAtLaunch);
        // 刷新挂起期间可能已切换 filter：await 后写 UI 前检查最新 epoch（review Medium 3）
        if (epoch !== mutationEpochRef.current) {
          return;
        }
        setActionError(copy.settings.knowledge.submissions.versionConflict);
      } else if (error instanceof ApiError && error.status === 409) {
        withdrawIdem.current.businessResponse();
        setPendingWithdraw(null);
        await loadSubmissions(filterAtLaunch);
        if (epoch !== mutationEpochRef.current) {
          return;
        }
        setActionError(copy.settings.knowledge.submissions.stateConflict);
      } else if (isBusinessResponse(error)) {
        // 明确业务响应：清键；行级错误 + 重试链（行不移除）
        withdrawIdem.current.businessResponse();
        setPendingWithdraw(null);
        setRowErrors((errors) => {
          const next = new Map(errors);
          next.set(submission.submission_id, {
            message: copy.settings.knowledge.submissions.actionError,
            retry: () => {
              void runWithdraw(submission);
            },
          });
          return next;
        });
      } else {
        // 网络未知/超时：复用同键同体重试；行级错误 + 重试链
        setPendingWithdraw(null);
        setRowErrors((errors) => {
          const next = new Map(errors);
          next.set(submission.submission_id, {
            message: copy.settings.knowledge.submissions.actionError,
            retry: () => {
              void runWithdraw(submission);
            },
          });
          return next;
        });
      }
    } finally {
      if (epoch === mutationEpochRef.current) {
        setConfirmingWithdraw(false);
      }
    }
  };

  const confirmWithdraw = async () => {
    const submission = pendingWithdraw;
    if (submission === null) {
      return;
    }
    await runWithdraw(submission);
  };

  /** 实际删除（接收 submission 参数，避免 retry 读取尚未提交的 pendingDelete 而 no-op）。 */
  const runDelete = async (submission: Submission) => {
    const epoch = mutationEpochRef.current;
    const filterAtLaunch = filter;
    setConfirmingDelete(true);
    setActionError(null);
    setRowErrors((errors) => {
      const next = new Map(errors);
      next.delete(submission.submission_id);
      return next;
    });
    const idempotencyKey = deleteIdem.current.keyFor(
      'delete-submission',
      submission.submission_id,
      `version:${submission.version}`,
    );
    try {
      await api.deleteSubmission(submission.submission_id, submission.version, idempotencyKey);
      if (epoch !== mutationEpochRef.current) {
        return; // 已切换 filter：旧 mutation 失效，不本地移除
      }
      deleteIdem.current.clear();
      setPendingDelete(null);
      // 行 opacity→0 收拢移除（--duration-base；无回收站与恢复入口）
      setSubmissions((items) => items.filter((item) => item.submission_id !== submission.submission_id));
    } catch (error) {
      if (epoch !== mutationEpochRef.current) {
        return; // 旧 mutation 已失效：不刷新旧 filter、不关闭新确认框、不覆盖当前错误
      }
      if (error instanceof ApiError && error.status === 409 && error.code === 'version_conflict') {
        deleteIdem.current.businessResponse();
        setPendingDelete(null);
        await loadSubmissions(filterAtLaunch);
        if (epoch !== mutationEpochRef.current) {
          return;
        }
        setActionError(copy.settings.knowledge.submissions.versionConflict);
      } else if (error instanceof ApiError && error.status === 409) {
        deleteIdem.current.businessResponse();
        setPendingDelete(null);
        await loadSubmissions(filterAtLaunch);
        if (epoch !== mutationEpochRef.current) {
          return;
        }
        setActionError(copy.settings.knowledge.submissions.stateConflict);
      } else if (isBusinessResponse(error)) {
        // 明确业务响应：清键；行级错误 + 重试链（行不移除）
        deleteIdem.current.businessResponse();
        setPendingDelete(null);
        setRowErrors((errors) => {
          const next = new Map(errors);
          next.set(submission.submission_id, {
            message: copy.settings.knowledge.submissions.actionError,
            retry: () => {
              void runDelete(submission);
            },
          });
          return next;
        });
      } else {
        // 网络未知/超时：复用同键同体重试；行级错误 + 重试链
        setPendingDelete(null);
        setRowErrors((errors) => {
          const next = new Map(errors);
          next.set(submission.submission_id, {
            message: copy.settings.knowledge.submissions.actionError,
            retry: () => {
              void runDelete(submission);
            },
          });
          return next;
        });
      }
    } finally {
      if (epoch === mutationEpochRef.current) {
        setConfirmingDelete(false);
      }
    }
  };

  const confirmDelete = async () => {
    const submission = pendingDelete;
    if (submission === null) {
      return;
    }
    await runDelete(submission);
  };

  return (
    <section aria-label={copy.settings.knowledge.submissions.title} className="pb-10">
      {/* 六档筛选 chip（超出分段控件合理宽度故用 chip；切换即重新请求） */}
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label={copy.settings.knowledge.submissions.title}>
        {FILTERS.map((entry) => (
          <button
            key={entry.value}
            type="button"
            aria-pressed={filter === entry.value}
            aria-label={copy.settings.knowledge.submissions.filterAria(entry.label)}
            onClick={() => {
              mutationEpochRef.current += 1;
              // 视图切换：释放旧 mutation 的 confirming（受控 Dialog 不会自动回调 onOpenChange）
              setConfirmingWithdraw(false);
              setConfirmingDelete(false);
              setFilter(entry.value);
            }}
            className={`inline-flex h-8 items-center rounded-[var(--radius-buttons)] border px-3 text-[14px] transition-colors duration-[var(--duration-fast)] ${
              filter === entry.value
                ? 'border-ink-black bg-ink-black text-paper-white'
                : 'border-[var(--color-hairline)] bg-transparent text-ink-black hover:bg-mist-gray'
            }`}
          >
            {entry.label}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="mt-4">
          <LoadingRows count={3} />
        </div>
      ) : loadError ? (
        <div className="mt-4">
          <ErrorState onRetry={() => void loadSubmissions(filter)} />
        </div>
      ) : submissions.length === 0 ? (
        <div className="mt-4">
          <EmptyState text={copy.settings.knowledge.submissions.empty} />
        </div>
      ) : (
        <ul className="mt-4 divide-y divide-[var(--color-hairline)]">
          {submissions.map((submission) => (
            <SubmissionRow
              key={submission.submission_id}
              submission={submission}
              error={rowErrors.get(submission.submission_id) ?? null}
              statusChanged={statusChangedIds.has(submission.submission_id)}
              onView={() => void openContent(submission)}
              onWithdraw={() => setPendingWithdraw(submission)}
              onDelete={() => setPendingDelete(submission)}
            />
          ))}
        </ul>
      )}
      {actionError !== null && (
        <p role="alert" className="mt-4 text-caption text-danger">
          {actionError}
        </p>
      )}

      <ConfirmDialog
        open={pendingWithdraw !== null}
        confirming={confirmingWithdraw}
        onOpenChange={(open) => {
          if (!open) {
            mutationEpochRef.current += 1;
            setConfirmingWithdraw(false);
            setPendingWithdraw(null);
          }
        }}
        title={copy.settings.knowledge.submissions.withdrawConfirmTitle}
        description={copy.settings.knowledge.submissions.withdrawConfirmDescription}
        confirmLabel={copy.settings.knowledge.submissions.withdraw}
        onConfirm={() => void confirmWithdraw()}
      />
      <ConfirmDialog
        open={pendingDelete !== null}
        confirming={confirmingDelete}
        onOpenChange={(open) => {
          if (!open) {
            mutationEpochRef.current += 1;
            setConfirmingDelete(false);
            setPendingDelete(null);
          }
        }}
        title={copy.settings.knowledge.submissions.deleteConfirmTitle}
        description={copy.settings.knowledge.submissions.deleteConfirmDescription(pendingDelete?.file_name ?? '')}
        confirmLabel={copy.settings.knowledge.submissions.delete}
        danger
        onConfirm={() => void confirmDelete()}
      />
    </section>
  );
}

interface SubmissionRowProps {
  readonly submission: Submission;
  readonly error: { message: string; retry: () => void } | null;
  /** 状态刚就地迁移（撤回）时为 true：状态 tag 以 --duration-fast 淡入一次。 */
  readonly statusChanged: boolean;
  readonly onView: () => void;
  readonly onWithdraw: () => void;
  readonly onDelete: () => void;
}

function SubmissionRow({ submission, error, statusChanged, onView, onWithdraw, onDelete }: SubmissionRowProps) {
  const { status } = submission;
  return (
    <li className="py-4" data-submission-id={submission.submission_id}>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="truncate text-body text-ink-black">{submission.file_name}</p>
          <p className="mt-1 text-caption text-smoke-gray">
            {submission.media_kind} · {copy.settings.knowledge.submissions.submittedAt(formatDateTime(submission.created_at))}
          </p>
          <span
            key={status}
            className={`mt-2 inline-block rounded-[var(--radius-buttons)] px-2 py-0.5 text-caption ${statusClass(status)}${statusChanged ? ' ui-fade-enter-fast' : ''}`}
          >
            {copy.settings.knowledge.submissions.statusTag[status]}
          </span>
          {error !== null && (
            <p className="mt-2 text-caption text-danger">
              {error.message}
              <TextLink className="ml-2" onClick={error.retry}>
                {copy.settings.knowledge.submissions.retry}
              </TextLink>
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <TextLink onClick={onView}>{copy.settings.knowledge.submissions.viewContent}</TextLink>
          {status === 'pending' && (
            <TextLink danger onClick={onWithdraw}>
              {copy.settings.knowledge.submissions.withdraw}
            </TextLink>
          )}
          {(status === 'rejected' || status === 'withdrawn' || status === 'invalidated') && (
            <TextLink danger onClick={onDelete}>
              {copy.settings.knowledge.submissions.delete}
            </TextLink>
          )}
        </div>
      </div>
    </li>
  );
}
