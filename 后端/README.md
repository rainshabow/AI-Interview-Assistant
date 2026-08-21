# 本地后端 (FastAPI) — 快速上手

说明：此后端实现了与前端 `main.html` 兼容的基本 REST API，包括认证、岗位管理、面试会话、消息与分析报告。大模型集成为可选：将 `OPENAI_API_KEY` 写入 `.env` 即可启用真实模型调用；否则将使用本地模拟回复。

步骤：

1. 创建并激活 Python 虚拟环境（推荐）

```bash
python -m venv .venv
.
```

2. 安装依赖

```bash
pip install -r backend/requirements.txt
```

3. 复制 `.env.example` 为 `.env` 并填写 `OPENAI_API_KEY`（如有）

4. 启动服务（开发模式）

```bash
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

5. 前端中将 `API_CONFIG.baseURL` 指向本机：`http://localhost:8000`，或修改 `.env` 中 `PORT`。

提示：数据保存在 `backend/data.json` 中，便于本地调试。生产环境请改用数据库与更健壮的鉴权方案。
