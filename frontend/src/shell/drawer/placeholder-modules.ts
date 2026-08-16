/*
 * 抽屉内置模块注册（shared-shell 规格 §1、§3）。
 * 该文件注册抽屉内置模块；业务模块（个人设置、管理面板等）已由
 * fe-settings-personal 与 fe-admin-panels 提供真实渲染。
 * 模块名与顺序以《共用基座设计.md》§5、《运维端设计.md》§7.1、《超管端设计.md》§7.1 为准。
 */

import { createElement } from 'react';
import { ApprovalSubmissionsLayer, QuotaRequestsLayer } from '../../admin/ApprovalsModule';
import { DashboardModule } from '../../admin/DashboardModule';
import { EvaluationModule } from '../../admin/EvaluationModule';
import { OperationsMetricsLayer, OpsJobsLayer } from '../../admin/OperationsModule';
import { DepartmentLibsLayer, PersonalLibsLayer, PublicSpaceLayer } from '../../admin/SpacesModule';
import {
  ApprovalsSummaryBadge,
  EvaluationWindowDot,
  OperationsStaleBadge,
  QuotaRequestsSummaryBadge,
} from '../../admin/summaries';
import { DepartmentsLayer } from '../../admin/DepartmentsLayer';
import { UsersModule } from '../../admin/UsersModule';
import { copy } from '../../copy';
import { AppearanceModule } from '../../settings/AppearanceModule';
import { KnowledgeModule } from '../../settings/KnowledgeModule';
import { ApprovalsLayer, ManageLayer } from '../../settings/ManageLayer';
import { ProfileModule } from '../../settings/ProfileModule';
import { SecurityModule } from '../../settings/SecurityModule';
import { SubmissionsLayer } from '../../settings/SubmissionsLayer';
import { UploadsLayer } from '../../settings/UploadsLayer';
import { VersionsLayer } from '../../settings/VersionsLayer';
import type { DrawerModule } from './registry';

const modules = copy.shell.drawer.modules;
const MEMBER_ROLES = ['user', 'minister'] as const;
const MINISTER = ['minister'] as const;
const OPS = ['ops'] as const;
const ADMIN = ['admin'] as const;
const ADMIN_SEGMENT_ROLES = ['ops', 'admin'] as const;

export function createPlaceholderModules(): DrawerModule[] {
  return [
    // 个人段（全角色；顺序固定）。Profile/Security 在首次登记时合成真实 render，绝不二次 register 同 ID。
    {
      id: 'profile',
      title: modules.profile,
      segment: 'personal',
      order: 10,
      render: () => createElement(ProfileModule),
    },
    {
      id: 'security',
      title: modules.security,
      segment: 'personal',
      order: 20,
      render: () => createElement(SecurityModule),
    },
    {
      id: 'appearance',
      title: modules.appearance,
      segment: 'personal',
      order: 30,
      render: () => createElement(AppearanceModule),
    },
    {
      id: 'knowledge',
      title: modules.knowledge,
      segment: 'personal',
      order: 40,
      render: () => createElement(KnowledgeModule),
      children: [
        // 上传结果层（§5.7）全角色共用；我的投稿层仅普通用户与部长；
        // 版本记录（按文档参数化下钻）与部长部门库/投稿审核为知识库下钻子界面。
        { id: 'uploads', title: modules.uploads, render: () => createElement(UploadsLayer) },
        { id: 'submissions', title: modules.submissions, roles: MEMBER_ROLES, render: () => createElement(SubmissionsLayer) },
        {
          id: 'versions',
          title: modules.versions,
          render: (context) => createElement(VersionsLayer, { path: context.path }),
        },
        { id: 'manage', title: modules.manage, roles: MINISTER, render: (context) => createElement(ManageLayer, { path: context.path }), children: [
          { id: 'approvals', title: modules.knowledgeApprovals, roles: MINISTER, render: (context) => createElement(ApprovalsLayer, { path: context.path }) },
        ] },
      ],
    },
    // 管理段：运维六模块（《运维端设计.md》§7.1）；管理面板骨架在 fe-admin-panels 首次登记时
    // 直接合成真实 render（与个人段 Profile/Security 同一约定，绝不二次 register 同 ID）。
    {
      id: 'dashboard',
      title: modules.dashboard,
      segment: 'admin',
      order: 10,
      roles: ADMIN_SEGMENT_ROLES,
      render: () => createElement(DashboardModule),
    },
    {
      id: 'approvals',
      title: modules.approvals,
      segment: 'admin',
      order: 20,
      roles: OPS,
      // 项右侧摘要：可靠的配额待处理徽标（§8.1；为 0 不显示）
      renderSummary: () => createElement(ApprovalsSummaryBadge),
      children: [
        // 配额申请（§8.2）与投稿审核（§8.4）为审批中心下钻子界面
        { id: 'quota', title: modules.quotaRequests, roles: OPS, render: () => createElement(QuotaRequestsLayer), renderSummary: () => createElement(QuotaRequestsSummaryBadge) },
        { id: 'submissions', title: modules.knowledgeApprovals, roles: OPS, render: () => createElement(ApprovalSubmissionsLayer) },
      ],
    },
    {
      id: 'spaces',
      title: modules.spaces,
      segment: 'admin',
      order: 30,
      roles: ADMIN_SEGMENT_ROLES,
      children: [
        { id: 'public', title: modules.publicSpace, render: () => createElement(PublicSpaceLayer) },
        // 运维三下钻：公共库 / 用户个人库 / 部门库；超管增加「投稿审核」（spec §4）
        { id: 'personal-libs', title: modules.personalLibs, render: () => createElement(PersonalLibsLayer) },
        { id: 'department-libs', title: modules.departmentLibs, render: () => createElement(DepartmentLibsLayer) },
        { id: 'submissions', title: modules.knowledgeApprovals, roles: ADMIN, render: () => createElement(ApprovalSubmissionsLayer) },
      ],
    },
    {
      id: 'evaluation',
      title: modules.evaluation,
      segment: 'admin',
      order: 40,
      roles: ADMIN_SEGMENT_ROLES,
      // 项右侧摘要：校准窗口状态点（开窗中 = 成功绿）
      renderSummary: () => createElement(EvaluationWindowDot),
      render: () => createElement(EvaluationModule),
    },
    {
      id: 'operations',
      title: modules.operations,
      segment: 'admin',
      order: 50,
      roles: ADMIN_SEGMENT_ROLES,
      // 项右侧摘要：超时任务计数徽标（stale_count >0 警告琥珀）
      renderSummary: () => createElement(OperationsStaleBadge),
      children: [
        // 任务队列（§10）与指标看板（§9.2）为系统运维下钻子界面；任务队列项带超时计数徽标（>0 警告琥珀）
        { id: 'jobs', title: modules.opsJobs, render: () => createElement(OpsJobsLayer), renderSummary: () => createElement(OperationsStaleBadge) },
        { id: 'metrics', title: modules.opsMetrics, render: () => createElement(OperationsMetricsLayer) },
      ],
    },
    // 同一模块 id 按角色给不同名：运维「用户管理」、超管「人员与权限」
    {
      id: 'users',
      title: modules.usersOps,
      segment: 'admin',
      order: 60,
      roles: OPS,
      render: () => createElement(UsersModule),
    },
    {
      id: 'users',
      title: modules.usersAdmin,
      segment: 'admin',
      order: 60,
      roles: ADMIN,
      render: () => createElement(UsersModule),
      children: [
        { id: 'departments', title: modules.departments, roles: ADMIN, render: () => createElement(DepartmentsLayer) },
      ],
    },
  ];
}
