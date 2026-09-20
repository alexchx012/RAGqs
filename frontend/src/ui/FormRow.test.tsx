/*
 * FormRow 测试（drawer-visual-system）：两栏行——左列固定 260px（标签 + 灰色说明）、
 * 列间距 24px、右列弹性；行上下各 20px；行间 1px 分隔线；只读行不渲染输入控件。
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
    expect(labelCol.className).toContain('w-[260px]');
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

  it('只读行渲染纯文本值，不渲染任何输入控件', () => {
    render(<FormRow label="部门" readOnlyValue="Finance" />);
    expect(screen.getByText('Finance')).toBeInTheDocument();
    expect(screen.queryByRole('textbox')).toBeNull();
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
