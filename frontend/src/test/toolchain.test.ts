import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

/*
 * 工具链纪律（规格 §1）：React + TypeScript + Vite；Tailwind CSS v4 + 自定义 CSS 变量语义层；
 * Radix UI 无头原语，不引入带默认样式的组件库；不引入全局状态库；
 * vitest + @playwright/test；npm；Node 22。
 */

interface PackageJson {
  engines?: Record<string, string>;
  dependencies?: Record<string, string>;
  devDependencies?: Record<string, string>;
}

const pkg = JSON.parse(
  readFileSync(join(process.cwd(), 'package.json'), 'utf8'),
) as PackageJson;

const allDeps = { ...pkg.dependencies, ...pkg.devDependencies };

const STYLED_COMPONENT_LIBS = [
  '@mui/material',
  'antd',
  '@chakra-ui/react',
  'styled-components',
  '@emotion/react',
  '@emotion/styled',
  'primereact',
  'semantic-ui-react',
];

const GLOBAL_STATE_LIBS = [
  'redux',
  '@reduxjs/toolkit',
  'zustand',
  'mobx',
  'jotai',
  'valtio',
  'recoil',
  'xstate',
];

describe('工具链与规格 §1 一致', () => {
  it('框架 React + 构建 Vite + TypeScript', () => {
    expect(pkg.dependencies?.react).toMatch(/^\^19\./);
    expect(pkg.dependencies?.['react-dom']).toMatch(/^\^19\./);
    expect(pkg.devDependencies?.vite).toBeDefined();
    expect(pkg.devDependencies?.typescript).toBeDefined();
    expect(pkg.devDependencies?.['@vitejs/plugin-react']).toBeDefined();
  });

  it('样式：Tailwind CSS v4（@theme 接入）+ vite 插件', () => {
    expect(pkg.devDependencies?.tailwindcss).toMatch(/^\^4\./);
    expect(pkg.devDependencies?.['@tailwindcss/vite']).toMatch(/^\^4\./);
  });

  it('组件：Radix UI 无头原语在场；不引入带默认样式的组件库', () => {
    expect(pkg.dependencies?.['@radix-ui/react-dialog']).toBeDefined();
    for (const lib of STYLED_COMPONENT_LIBS) {
      expect(allDeps[lib], `不应引入 ${lib}`).toBeUndefined();
    }
  });

  it('不引入全局状态库', () => {
    for (const lib of GLOBAL_STATE_LIBS) {
      expect(allDeps[lib], `不应引入 ${lib}`).toBeUndefined();
    }
  });

  it('测试：vitest + @playwright/test；包管理器 npm；运行环境 Node 22', () => {
    expect(pkg.devDependencies?.vitest).toBeDefined();
    expect(pkg.devDependencies?.['@playwright/test']).toBeDefined();
    expect(pkg.engines?.node).toBe('>=22.22'); // A49：Node 下限收紧至 22.22
    expect(process.version).toMatch(/^v22\./);
  });
});
