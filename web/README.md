# NovelForge Web

NovelForge 的前端：营销官网（Landing）+ 创作工作台（Studio）。

## 技术栈

- **Vite 5** — 开发服务器与构建工具
- **React 18** + **TypeScript** — 全部为函数组件（`.tsx`）
- **React Router** — URL 是当前项目、章节和工作区视图的唯一定位来源
- **全局 CSS**（`src/styles/global.css`）— 营销组件直接复用设计稿中的 class，不使用 CSS-in-JS

营销页的设计真相源为 `design-reference.html`，组件复用其 markup、class、SVG 与文案。
创作工作台在原有奶油色、墨黑、荧光绿、粉色设计令牌之上采用
**Editorial Neo-Brutalist Writing Desk**：硬边框、实体阴影、编辑部式排版和高密度证据面板，
核心写作页由 Story Spine、正文稿纸和章节契约/偏航检查三栏组成。

## URL 驱动的创作工作台

`/studio` 是项目书架与项目选择入口。进入项目后，项目 ID 和当前章节都保存在 URL 中，
因此刷新、复制深链或同时打开多个项目标签页不会串书，也不依赖 localStorage 恢复业务位置。

| 路径 | 用途 |
| --- | --- |
| `/studio/:projectId/write/:chapter` | 章节写作、候选稿、契约与偏航证据 |
| `/studio/:projectId/plan` | 全书/卷规划与未来章节窗口 |
| `/studio/:projectId/world` | 世界设定、Canon 与章节时点状态 |
| `/studio/:projectId/review` | 待审事实与人工晋升队列 |
| `/studio/:projectId/settings` | 项目记忆和运行设置 |

访问 `/studio/:projectId` 会重定向到该项目的 `plan` 页面；切换项目则返回 `/studio` 书架。

## 本地开发

前端通过 Vite proxy 把 `/v1` 与 `/health` 转发到后端，因此需要先把后端跑起来。

1. **启动后端**（FastAPI，监听 `127.0.0.1:8787`）：

   ```bash
   uvicorn novelforge.app.main:app --host 127.0.0.1 --port 8787 --reload
   ```

2. **安装依赖并启动前端**：

   ```bash
   cd web
   npm install
   npm run dev
   ```

   打开 http://localhost:5173 ——
   开发模式下 `/v1/*` 与 `/health` 请求会被 Vite proxy 转发到 `http://127.0.0.1:8787`，
   因此前端无需关心跨域，直接用相对路径调用 API。

## 构建与预览

在 `web/` 目录执行：

```bash
npm run build     # tsc -b 类型检查 + vite build，产物在 dist/
npm run preview   # 本地预览 dist/ 产物（默认端口 4173）
```

质量检查与测试命令：

```bash
npm run typecheck # TypeScript：应用、配置和 E2E 测试
npm run lint      # ESLint flat config，包含 TypeScript 与 Hooks 规则
npm test          # Vitest 单元测试（单次运行）
npm run test:e2e  # Playwright Chromium 路由测试；自动启动 Vite
npm run check     # typecheck + lint + test + build
```

首次运行 E2E 前安装 Chromium：

```bash
npx playwright install chromium
```

## 环境变量

- **`VITE_API_BASE`** — API 基础地址。
  - 留空（默认）：使用相对路径，开发期由 Vite proxy 转发到后端；
    生产部署时若前端与后端同源，同样留空即可。
  - 跨域部署时设为后端地址，例如 `VITE_API_BASE=https://api.example.com`，
    所有 API 请求会拼到该前缀之上。
