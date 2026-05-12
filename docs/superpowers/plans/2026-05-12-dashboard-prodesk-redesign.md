# Shark 2.0 Dashboard Pro Desk 重构 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `web/` 仪表盘对齐已批准的 spec（Pro Desk 气质、单页滚动、表格式 KPI、2:1 角色/风控、舒朗数据表、窄屏风控在上、KPI 窄屏横滚）。

**Architecture:** 以 `index.css` 的语义化 token（`--table-row-pad-y`、`--kpi-table-*`）驱动密度；`Dashboard` 从卡片网格改为单行 KPI 表；`App.tsx` 用网格类 + `order` 实现宽屏 2:1 / 窄屏先风控；星场与卡片 hover 弱化；用 Vitest + Testing Library 锁住 KPI 表结构与关键可访问性。

**Tech stack:** React 18、Vite 5、TypeScript、Tailwind（`index.css` 内 `@tailwind`）、Zustand（不变）。

**Spec 依据:** `docs/superpowers/specs/2026-05-12-dashboard-prodesk-redesign-design.md`

**窄屏 KPI 策略（已锁死）:** `max-width: 900px` 时 KPI 外包一层 `overflow-x: auto`，表 `min-width` 保证列不全挤扁，用户横滑读数（不实现列折叠，避免状态机）。

---

## 文件结构（将创建 / 修改）

| 文件 | 职责 |
|------|------|
| `web/package.json` | 增加 `vitest`、`@testing-library/react`、`@testing-library/jest-dom`、`jsdom`；增加 `test` 脚本。 |
| `web/vite.config.ts` | `test` 配置：`environment: 'jsdom'`、`setupFiles`。 |
| `web/src/test/setup.ts` | 引入 `@testing-library/jest-dom/vitest`。 |
| `web/src/components/Dashboard.test.tsx` | KPI 渲染为 `table`、关键列表头存在。 |
| `web/src/components/Dashboard.tsx` | 表格式 KPI + 单元格闪烁动画（延续 `useFlashRef` 思路）。 |
| `web/src/index.css` | Pro Desk token；`.kpi-table-desk`；`.layout-room-risk*`；弱化 `.card:hover`、`body::before` 透明度；`.data-table` 舒朗行高。 |
| `web/src/App.tsx` | 星场粒子减量/降对比；主区 `id` 锚点；顶栏精简；`layout-room-risk` 包装；可选「跳转」文字链。 |
| `web/src/components/PositionsTable.tsx` | Pro Desk 空状态（减少装饰 emoji 或改为纯文字）；依赖全局 `.data-table` 舒朗样式。 |
| `web/src/components/TradeHistory.tsx` | 同上（空状态）；分页条样式随 token。 |

---

### Task 1: Vitest + Testing Library 脚手架

**Files:**
- Modify: `web/package.json`
- Modify: `web/vite.config.ts`
- Create: `web/src/test/setup.ts`

- [ ] **Step 1: 安装依赖**

在工作区根目录执行（或 `cd web` 后执行）：

```bash
cd web && npm install -D vitest@1.6.0 @vitejs/plugin-react@4.2.1 @testing-library/react@14.2.1 @testing-library/jest-dom@6.4.2 jsdom@24.0.0
```

说明：`@vitejs/plugin-react` 若已满足范围可略去；执行后 `npm ls vitest` 应显示 `1.6.0`。

- [ ] **Step 2: 在 `web/package.json` 的 `scripts` 中增加**

```json
"test": "vitest run",
"test:watch": "vitest"
```

- [ ] **Step 3: 修改 `web/vite.config.ts`** — 在文件顶部 `import` 保持不动，将 `export default defineConfig({` 改为传入含 `test` 的配置：

```ts
export default defineConfig({
  plugins: [react(), webVideoDirPlugin()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: true,
  },
  build: {
```

（其余 `build` / `server` 块保持不变。）

- [ ] **Step 4: 创建 `web/src/test/setup.ts`**

```ts
import '@testing-library/jest-dom/vitest'
```

- [ ] **Step 5: 运行 Vitest（应通过 0 个测试或占位测试报 0）**

```bash
cd web && npm run test
```

**Expected:** 进程退出码 0；若无测试文件则显示 collected 0 tests 仍为成功（取决于 vitest 版本；若有报错则修正 config）。

- [ ] **Step 6: Commit**

```bash
git add web/package.json web/package-lock.json web/vite.config.ts web/src/test/setup.ts
git commit -m "chore(web): add vitest and testing-library for dashboard tests"
```

---

### Task 2: KPI 表 — 失败测试

**Files:**
- Create: `web/src/components/Dashboard.test.tsx`

- [ ] **Step 1: 写入测试（针对「未来将实现的」DOM 契约）**

```tsx
import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import Dashboard from './Dashboard'

const baseProps = {
  equity: 10000,
  balance: 9000,
  freeCash: 8000,
  realizedPnl: 12.34,
  winRate: 0.52,
  positions: 2,
  equityChange: 100,
  safetyBlocked: false,
  totalFees: 1.2,
  marginLocked: 50,
}

describe('Dashboard Pro Desk KPI table', () => {
  it('renders a single KPI table with header and value row', () => {
    render(<Dashboard {...baseProps} />)
    const table = screen.getByRole('table', { name: /kpi overview/i })
    expect(table).toBeInTheDocument()
    const rowHead = within(table).getAllByRole('row')[0]
    expect(within(rowHead).getByText('总权益')).toBeInTheDocument()
    expect(within(rowHead).getByText(/可用/i)).toBeInTheDocument()
    const dataRows = within(table).getAllByRole('row')
    expect(dataRows.length).toBeGreaterThanOrEqual(2)
  })
})
```

- [ ] **Step 2: 运行测试并确认失败**

```bash
cd web && npm run test -- --run src/components/Dashboard.test.tsx
```

**Expected:** FAIL — `Unable to find role="table"` 或 `name` 不匹配（因为 `Dashboard` 仍为卡片网格）。

- [ ] **Step 3: Commit 失败测试**

```bash
git add web/src/components/Dashboard.test.tsx
git commit -m "test(web): expect Dashboard KPI table layout"
```

---

### Task 3: `Dashboard.tsx` 改为表格式 KPI

**Files:**
- Modify: `web/src/components/Dashboard.tsx`
- Modify: `web/src/index.css`（本任务至少加入 `.kpi-table-desk` 最小样式；Task 4 可再收敛 token）

- [ ] **Step 1: 用表结构重写 `Dashboard`**

要求：
- 外层 wrapper：`<div className="kpi-strip-scroll">` 内包 `<table aria-label="KPI overview" className="kpi-table-desk">`。
- 第一行 `<thead><tr>`：表头为中文短标签，与 spec 一致（总权益、余额、锁定保证金、可用、已实现盈亏、胜率、持仓数、风控、累计手续费）；「总权益」下可加小号 secondary 行显示当日变动，**实现方式二选一**：`(a)` `thead` 里用两行（第一行 label、第二行 sub 仅第一列有 equityChange）；或 `(b)` 将 equity 子文案放在 `tbody` 第一格内两行。择更简单且对齐好的方案。
- `tbody` 一行数值格：数字格式化逻辑与当前 `KpiCard` 相同（`toFixed`、正负号、`safetyBlocked` 显示 熔断/正常）。
- 保留涨跌闪烁：对每个**数值单元格**使用 `useFlashRef(parseFloat(…))` 或等价解析；`风控` 与 `胜率` 等非纯美元数字用稳定排序规则解析（与现实现一致即可）。

**参考骨架（需补全解析与 sub 文案）：**

```tsx
// src/components/Dashboard.tsx — 结构示意
export default function Dashboard(props: Props) {
  // ... format helpers, useFlashRef unchanged ...
  return (
    <div className="kpi-strip-scroll">
      <table className="kpi-table-desk" aria-label="KPI overview">
        <thead>
          <tr>
            <th scope="col">总权益</th>
            {/* ... 其余 th ... */}
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>{/* equity + change */}</td>
            {/* ... */}
          </tr>
        </tbody>
      </table>
    </div>
  )
}
```

- [ ] **Step 2: 在 `index.css` 增加最小样式**

```css
.kpi-strip-scroll {
  width: 100%;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}

.kpi-table-desk {
  width: 100%;
  min-width: 720px;
  border-collapse: collapse;
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}

.kpi-table-desk thead th {
  text-align: right;
  padding: 6px 8px 4px;
  color: var(--text-secondary);
  font-weight: 600;
  text-transform: none;
  border-bottom: 1px solid var(--border-subtle);
}

.kpi-table-desk thead th:first-child {
  text-align: left;
}

.kpi-table-desk tbody td {
  text-align: right;
  padding: 8px 8px 10px;
  border-bottom: 1px solid var(--border-subtle);
  font-variant-numeric: tabular-nums;
}

.kpi-table-desk tbody td:first-child {
  text-align: left;
}

@media (max-width: 900px) {
  .kpi-table-desk {
    min-width: 640px;
  }
}
```

- [ ] **Step 3: 运行测试**

```bash
cd web && npm run test -- --run src/components/Dashboard.test.tsx
```

**Expected:** PASS

- [ ] **Step 4: 类型检查与构建**

```bash
cd web && npm run build
```

**Expected:** `tsc && vite build` 无报错。

- [ ] **Step 5: Commit**

```bash
git add web/src/components/Dashboard.tsx web/src/index.css
git commit -m "feat(web): Dashboard KPI as Pro Desk table row"
```

---

### Task 4: Pro Desk 全局 token + 弱化背景 / 卡片光晕

**Files:**
- Modify: `web/src/index.css`

- [ ] **Step 1: 在 `:root` 增加 token**

```css
  --table-row-pad-y: 12px;
  --table-cell-pad-x: 14px;
  --table-font-size: 13px;
  --kpi-strip-bg: rgba(15, 22, 40, 0.45);
```

- [ ] **Step 2: 弱化 `body::before` 与 `.card:hover`**

将 `body::before` 的 `opacity` 从 `0.55` 降至约 `0.28`（或等价降低背景图与叠层强度）。  
将 `.card:hover` 的 `box-shadow: var(--glow-cyan)` 改为更弱，例如：

```css
.card:hover {
  border-color: var(--border-accent);
  box-shadow: 0 0 0 1px rgba(0, 240, 255, 0.12);
}
```

- [ ] **Step 3: `.data-table` 舒朗行**（覆盖现有紧凑值）

```css
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--table-font-size);
}

.data-table thead th {
  padding: var(--table-row-pad-y) var(--table-cell-pad-x);
  /* 保留现有颜色/边框则合并，不重复冲突规则 */
}

.data-table tbody td {
  padding: var(--table-row-pad-y) var(--table-cell-pad-x);
}
```

（把旧 `font-size: 12px` 与 `padding` 合并进上述块，避免重复选择器。）

- [ ] **Step 4: `.kpi-strip-scroll` 可选底** — 与 Pro 平面一致：

```css
.kpi-strip-scroll {
  background: var(--kpi-strip-bg);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius);
  padding: 4px 6px;
}
```

- [ ] **Step 5: 验证**

```bash
cd web && npm run build
```

- [ ] **Step 6: Commit**

```bash
git add web/src/index.css
git commit -m "style(web): Pro Desk tokens, relax tables, soften chrome"
```

---

### Task 5: `App.tsx` — 2:1 栅格、窄屏风控在上、锚点、星场降载

**Files:**
- Modify: `web/src/App.tsx`
- Modify: `web/src/index.css`

- [ ] **Step 1: 在 `index.css` 增加布局类**

```css
.layout-room-risk {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 10px;
  align-items: stretch;
}

.layout-room-risk__room,
.layout-room-risk__risk {
  min-width: 0;
}

@media (max-width: 900px) {
  .layout-room-risk {
    grid-template-columns: 1fr;
  }
  .layout-room-risk__risk {
    order: -1;
  }
  .layout-room-risk__room {
    order: 1;
  }
}
```

- [ ] **Step 2: 调整主内容 `App.tsx`**
  - 在最外 `main` 内容区给块加 `id`：`id="section-kpi"`（包 `Dashboard`）、`id="section-room"`（包房间+风控整块）、`id="section-positions"`、`id="section-history"`。
  - 将原 `gridTemplateColumns: '2fr 1fr'` 的内联 grid 换为：

```tsx
<div className="layout-room-risk" id="section-room">
  <div className="layout-room-risk__room card">...</div>
  <div className="layout-room-risk__risk card">...</div>
</div>
```

  - 顶栏右侧增加极简文字跳转（可选，若嫌挤可只做 `id` 供浏览器书签/扩展使用）：

```tsx
<nav aria-label="Section skip" style={{ fontSize: 10, display: 'flex', gap: 10 }}>
  <a href="#section-kpi">KPI</a>
  <a href="#section-room">舱室</a>
  <a href="#section-positions">持仓</a>
  <a href="#section-history">历史</a>
</nav>
```

  样式用 `color: var(--text-muted)`，`:hover` 提亮即可。

- [ ] **Step 3: 星场 `useEffect` 内** — 将星星数量从 `120` 改为 `48`，`globalAlpha` 上限略降（例如 `Math.max(0.06, s.o)`），`fillStyle` 里降低饱和或透明度（具体数以肉眼不明显抢夺表格为准）。

- [ ] **Step 4: 顶栏品牌** — 保留文字 Shark 2.0；emoji 仅保留一处或改为纯文字（按 spec「少用 emoji」）。

- [ ] **Step 5: 构建**

```bash
cd web && npm run build
```

- [ ] **Step 6: Commit**

```bash
git add web/src/App.tsx web/src/index.css
git commit -m "feat(web): Pro Desk layout grid, anchors, lighter starfield"
```

---

### Task 6: 空状态与进化按钮 — Pro Desk 低噪

**Files:**
- Modify: `web/src/components/PositionsTable.tsx`
- Modify: `web/src/components/TradeHistory.tsx`
- Modify: `web/src/App.tsx`（进化按钮文案）

- [ ] **Step 1: `PositionsTable` / `TradeHistory` 空状态** — 去掉 `empty-icon` 中大 emoji 或整格 icon，改为纯文字 + `className="empty-state empty-state--pro"`，在 CSS 中 `.empty-state--pro { font-size: 13px; color: var(--text-secondary); }`。

- [ ] **Step 2: 进化待批按钮** — 将 `🧬` 改为前缀文字「进化」+ 数量，或仅 `自进化 · N`，避免彩虹 emoji 堆叠。

- [ ] **Step 3: 构建 + 抽样跑测试**

```bash
cd web && npm run test -- --run
cd web && npm run build
```

- [ ] **Step 4: Commit**

```bash
git add web/src/components/PositionsTable.tsx web/src/components/TradeHistory.tsx web/src/App.tsx web/src/index.css
git commit -m "style(web): calmer empty states and evo control labels"
```

---

## Plan 自检

**1. Spec 对应**

| Spec 要求 | 对应 Task |
|-----------|-----------|
| Pro Desk 气质 / 弱化背景与 hover | Task 4, Task 5 星场 |
| 单页滚动 + 锚点 | Task 5 |
| 表格式 KPI + 窄屏横滚 | Task 2–3、Task 4 `kpi-strip-scroll` |
| 2:1 角色/风控 + 窄屏风控在上 | Task 5 `layout-room-risk` |
| 表格舒朗 | Task 4 `.data-table` |
| 验收：无 Tab/侧栏 | 全程未引入 |
| 台词库 | 排除（另 spec） |

**2. 占位符扫描** — 无 TBD；窄屏 KPI 已锁横滚。

**3. 类型/命名** — `aria-label="KPI overview"` 与测试 `/kpi overview/i` 一致。

---

**Plan 已保存至:** `docs/superpowers/plans/2026-05-12-dashboard-prodesk-redesign.md`

**执行方式任选：**

1. **Subagent-Driven（推荐）** — 每 Task 派独立子代理，Task 间复审，迭代快。需使用 **superpowers:subagent-driven-development**。
2. **Inline Execution** — 本会话内按 Task 执行，批量检查点。需使用 **superpowers:executing-plans**。

你想用 **1** 还是 **2**？
