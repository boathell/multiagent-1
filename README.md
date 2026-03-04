# multiagent-orchestrator

一个由 Plane 驱动的多 Agent 编排器（Design → Coding → Review），支持自动状态流转、质量门禁、失败分类、人类接管与 PR/评论同步。

## 1. 核心能力

- **工作流编排**：`Todo -> Design -> Coding -> Review -> Done/Blocked`
- **Webhook 驱动**：接收 Plane `work_item.created/updated` 事件后自动触发
- **治理策略**：
  - 协议校验（Design/Coding/Review 三阶段）
  - Review 证据完整性门禁
  - 失败分类（`QUALITY_ISSUE / GEMINI_ISSUE / ENV_ISSUE / PROTOCOL_VIOLATION`）
  - 人类接管（`[HUMAN-HANDOFF]`）
- **质量门禁**：支持命令检查 + 单文件代码行数上限（默认 `1000`）
- **协同同步**：
  - Coding 成功后同步 TDD 摘要到 PR
  - Review 失败后同步修复清单到 Plane + PR

## 2. 目录结构（关键）

```text
src/app/
  orchestrator.py              # 主流程（薄封装 + 委托）
  orch/
    events.py                  # event/issue 提取、description 归一化、入口判定
    review_context.py          # git diff 收集与分片
    governance.py              # 协议校验、证据门禁、失败分类、handoff 触发
    pr_sync.py                 # PR 摘要与修复清单同步
    comments.py                # stage/retry/blocked/review-fix 评论渲染
  adapters/
    plane_client.py            # Plane API（状态/评论/描述回写）
    github_client.py           # GitHub 分支/PR/评论
    agents/cli_adapter.py      # Kimi/Codex/Gemini CLI 适配
  api/
    webhooks.py                # /webhooks/plane
    internal.py                # /internal/issues/{id}/retry, /trace
```

## 3. 环境要求

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- 可用的 Agent CLI（按 `config/agents.yaml`）
- Plane / GitHub 凭据

## 4. 快速开始

### 4.1 安装依赖

```bash
uv sync
```

### 4.2 配置环境变量

复制并填写 `.env`：

```bash
cp .env.example .env
```

关键项：

- `PLANE_BASE_URL`
- `PLANE_WORKSPACE_SLUG`
- `PLANE_API_TOKEN`
- `PLANE_WEBHOOK_SECRET`
- `GITHUB_TOKEN`
- `AGENTS_USE_MOCK`
- `REVIEW_MAX_LOOPS`
- `REVIEW_ARBITER_MAX_LOOPS`
- `MAX_CODE_FILE_LINES`

> 提示：本项目对本地 Plane 常用 `http://localhost:8080`。若遇到 SSL/代理干扰，优先检查全局环境变量是否覆盖 `.env`。

### 4.3 启动服务

```bash
uv run uvicorn app.main:app --app-dir src --host 0.0.0.0 --port 8787
```

健康检查：

```bash
curl -sS http://127.0.0.1:8787/healthz
```

## 5. 配置说明

### 5.1 项目映射 `config/projects.yaml`

为每个 Plane 项目配置仓库与状态映射：

- `repo_url`
- `local_path`
- `base_branch`
- `state_map`
- `checks`（例如 `uv run pytest -q`）

### 5.2 Agent 配置 `config/agents.yaml`

定义 Design/Coding/Review 的命令、参数与超时。支持 `use_mock` 与 `tdd_enabled`。

## 6. API 入口

- `POST /webhooks/plane`
  - Plane webhook 主入口（含签名校验）
- `POST /internal/issues/{issue_id}/retry`
  - 对已存在 issue 执行重试
- `GET /internal/issues/{issue_id}/trace`
  - 查询执行轨迹

## 7. TDD 合同与提醒

推荐在 Plane issue 描述中使用模板：

- `docs/tdd/plane_issue_template.md`
- `docs/tdd/plane_issue_fill_guide.md`
- `docs/tdd/examples/sample_issue_feature.md`

行为规则：

- Design 可自动补全 `Red/Green/Refactor`
- 缺少关键段落会给出 `[TDD-提醒]`（降级提示）
- Issue 合同缺项会给出 `[TDD-Contract-提醒]`

## 8. 治理阈值与优先级

- `REVIEW_MAX_LOOPS`：Review 回流上限
- `REVIEW_ARBITER_MAX_LOOPS`：设计仲裁上限
- `MAX_CODE_FILE_LINES`：单文件代码行数上限
- `HUMAN_HANDOFF_ENABLED`：是否启用人类接管
- `TDD_ENFORCEMENT_MODE`：`strict | advisory`

阈值优先级：

1. 环境变量
2. `config/agents.yaml`
3. 代码默认值

## 9. 观测与排障

- 本地状态库：`.data/orchestrator.db`
- 服务日志：`.data/uvicorn.log`
- 常见检查：

```bash
# 最近 issue 状态
uv run python - <<'PY'
import sqlite3
con=sqlite3.connect('.data/orchestrator.db')
cur=con.cursor()
for r in cur.execute("select issue_id,state,updated_at from issue_runs order by updated_at desc limit 10"):
    print(r)
con.close()
PY

# 查看某 issue 轨迹
curl -sS "http://127.0.0.1:8787/internal/issues/<issue_id>/trace"
```

## 10. 测试

```bash
uv run pytest -q
```

## 11. 当前通知模式

- Feishu 默认关闭
- 以 Plane 评论为主观测渠道
- `/webhooks/plane/relay` 可按需启用
