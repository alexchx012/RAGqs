/*
 * FormRow 测试（drawer-visual-system）：两栏行——左列固定 260px（标签 + 灰色说明）、
 * 列间距 24px、右列弹性；行上下各 20px；行间 1px 分隔线；只读行不渲染输入控件。
 * 左列断点断言按空白切分精确比对：子串断言会被 md: 前缀满足。
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { FormRow } from './FormRow';

describe('FormRow', () => {
  it('两栏：左列 260px 固定、列间距 24px、行上下各 20px', () => {
    render(
      <FormRow label="主题" description="说明文字" htmlFor="theme">
        <input id="theme" />
      </FormRow>,
    );
    const row = screen.getByTestId('form-row');
    expect(row.className).toContain('py-5');
    expect(row.className).toContain('gap-6');
    const labelCol = screen.getByTestId('form-row-label');
    // 精确断言：260px 只在 md 断点生效。用 toContain('w-[260px]') 会被 md:w-[260px]
    // 的子串满足，回归成裸 w-[260px]（窄屏也固定 260px）时逃过断言。
    const labelClasses = labelCol.className.split(/\s+/);
    expect(labelClasses).toContain('md:w-[260px]');
    expect(labelClasses).not.toContain('w-[260px]');
    expect(labelCol.className).toContain('shrink-0');
  });

  it('标签与说明同列，标签经 htmlFor 关联控件', () => {
    render(
      <FormRow label="主题" description="说明文字" htmlFor="theme">
        <input id="theme" />
      </FormRow>,
    );
    expect(screen.getByLabelText('主题')).toBeInTheDocument();
    expect(screen.getByText('说明文字')).toBeInTheDocument();
  });

  it('只读行与可编辑行并存时，只有可编辑行渲染输入控件', () => {
    // 只读行单独渲染时「查不到输入框」恒成立（没有 children 本就不会有控件），
    // 必须与可编辑行并存才能证明只读分支真的不渲染 children。
    render(
      <>
        <FormRow label="显示名">
          <input aria-label="显示名输入" />
        </FormRow>
        <FormRow label="部门" readOnlyValue="Finance" />
      </>,
    );
    expect(screen.getByLabelText('显示名输入')).toBeInTheDocument();
    expect(screen.getByText('Finance')).toBeInTheDocument();
    expect(screen.queryByLabelText('部门输入')).toBeNull();
    expect(screen.getAllByRole('textbox')).toHaveLength(1);
  });

  it('组件始终渲染行间分隔线，末行由父容器关闭', () => {
    render(
      <FormRow label="主题">
        <input />
      </FormRow>,
    );
    const row = screen.getByTestId('form-row');
    expect(row.className).toContain('border-b');
    expect(row.className).toContain('border-hairline');
  });

  it('窄屏为单列、桌面为两栏', () => {
    render(
      <FormRow label="主题">
        <input />
      </FormRow>,
    );
    const row = screen.getByTestId('form-row');
    expect(row.className).toContain('flex-col');
    expect(row.className).toContain('md:flex-row');
  });
});
