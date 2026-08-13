import { readdirSync, readFileSync, statSync } from 'node:fs';
import { extname, join, relative, sep } from 'node:path';
import { describe, expect, it } from 'vitest';
import { copy } from '../copy';

/*
 * 文案纪律（规格 §6）：全部「措辞后定」文案集中在单一文案常量文件 src/copy/，
 * 组件、样式、e2e 中不允许硬编码中文文案。
 */

// vitest 以前端工程根为 cwd 运行
const frontendRoot = process.cwd();

const SCANNED_EXTENSIONS = new Set(['.ts', '.tsx', '.css', '.html']);

// CJK 统一表意文字 + CJK 标点 + 全角字符
const CJK_PATTERN = /[\u2e80-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]/;

function walk(dir: string): string[] {
  const entries: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      if (name === 'node_modules' || name === 'copy') {
        continue;
      }
      entries.push(...walk(path));
    } else if (SCANNED_EXTENSIONS.has(extname(name))) {
      entries.push(path);
    }
  }
  return entries;
}

function stripComments(code: string): string {
  return code.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/\/\/[^\n]*/g, ' ');
}

function collectScannedFiles(): string[] {
  const files = [join(frontendRoot, 'index.html'), ...walk(join(frontendRoot, 'src'))];
  try {
    files.push(...walk(join(frontendRoot, 'e2e')));
  } catch {
    // e2e 目录不存在时跳过
  }
  return files.filter(
    (path) =>
      // src/copy/ 是文案唯一归宿，自身不受扫描
      !path.includes(`${sep}copy${sep}`) &&
      // src/mocks/ 是模拟服务端：种子数据里的中文是「后端下发」内容（如提醒 title），
      // 前端原样展示，不属于 UI 文案（契约 §5.1）
      !path.includes(`${sep}mocks${sep}`) &&
      // 单元测试不参与渲染，describe/it 描述面向开发者，可用中文
      !/\.test\.(ts|tsx)$/.test(path),
  );
}

describe('单一文案常量文件机制', () => {
  it('文案仅存在于 src/copy/（组件/样式/e2e/index.html 无硬编码中文文案）', () => {
    const offenders: string[] = [];
    for (const path of collectScannedFiles()) {
      const stripped = stripComments(readFileSync(path, 'utf8'));
      if (CJK_PATTERN.test(stripped)) {
        offenders.push(relative(frontendRoot, path));
      }
    }
    expect(offenders).toEqual([]);
  });

  it('单一文案常量文件可用且占位文案齐备', () => {
    expect(copy.appName.length).toBeGreaterThan(0);
    expect(copy.shell.drawer.topPlaceholderBody.length).toBeGreaterThan(0);
    expect(copy.shell.skipToContent.length).toBeGreaterThan(0);
  });
});
