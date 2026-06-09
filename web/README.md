# NovelForge Web

NovelForge 的前端：营销官网（Landing）+ 创作工作台（Studio）。

## 技术栈

- **Vite 5** — 开发服务器与构建工具
- **React 18** + **TypeScript** — 全部为函数组件（`.tsx`）
- **全局 CSS**（`src/styles/global.css`）— 营销组件直接复用设计稿中的 class，不使用 CSS-in-JS

设计真相源为 `design-reference.html`，组件 1:1 还原其 markup / class / SVG / 文案。

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

```bash
npm run build     # tsc -b 类型检查 + vite build，产物在 dist/
npm run preview   # 本地预览 dist/ 产物
```

## 环境变量

- **`VITE_API_BASE`** — API 基础地址。
  - 留空（默认）：使用相对路径，开发期由 Vite proxy 转发到后端；
    生产部署时若前端与后端同源，同样留空即可。
  - 跨域部署时设为后端地址，例如 `VITE_API_BASE=https://api.example.com`，
    所有 API 请求会拼到该前缀之上。
