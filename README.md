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
  workspace_manager.py         # issue 级 git worktree 工作区管理
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
- `ISSUE_MAX_CONCURRENCY`
- `ISSUE_WORKTREE_ENABLED`
- `ISSUE_WORKTREE_ROOT`
- `ISSUE_WORKTREE_CLEANUP_ENABLED`
- `ISSUE_WORKTREE_RETENTION_HOURS`

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
- `ISSUE_MAX_CONCURRENCY`：全局 issue 并发上限（默认 `2`）
- `ISSUE_WORKTREE_ENABLED`：是否启用 issue 独立 worktree（默认 `true`）
- `ISSUE_WORKTREE_ROOT`：issue worktree 根目录（默认 `.data/worktrees`）
- `ISSUE_WORKTREE_CLEANUP_ENABLED`：是否在 issue 终态后自动清理陈旧 worktree（默认 `false`）
- `ISSUE_WORKTREE_RETENTION_HOURS`：陈旧 worktree 保留时长（小时，默认 `72`）

### 8.1 关键配置项说明（重点）

- `REVIEW_MAX_LOOPS`
  - **用途**：限制同一 issue 在 Review 阶段被打回（`NEEDS_CHANGES`）后，自动回到 Coding 的最大次数。
  - **触发点**：每次 Review 返回 `NEEDS_CHANGES`，计数 +1；超过阈值后不再直接回 Coding，而是进入“设计仲裁”决策。
  - **建议**：日常可用 `1~2`。值越大，自动迭代更久，但也可能增加无效循环时间。

- `REVIEW_ARBITER_MAX_LOOPS`
  - **用途**：限制“Review 超限后触发 Design 仲裁”的最大次数。
  - **触发点**：当 `REVIEW_MAX_LOOPS` 已超限时，才会消耗该计数。
  - **行为**：超过该值后，流程直接进入 `Blocked`，避免无限仲裁循环。
  - **建议**：通常与 `REVIEW_MAX_LOOPS` 保持一致或更小（例如都为 `1` 或 `2`）。

- `MAX_CODE_FILE_LINES`
  - **用途**：限制单个代码文件最大行数（质量门禁）。
  - **触发点**：Coding 成功后执行质量门禁时检查；任一文件超限即阻断并进入失败路径。
  - **建议**：默认 `1000`；如果团队偏向小文件治理，可下调到 `600~800`。

- `HUMAN_HANDOFF_ENABLED`
  - **用途**：是否启用自动人类接管评论（`[HUMAN-HANDOFF]`）与阻断策略。
  - **建议**：生产环境保持 `true`，便于异常场景快速止损。

- `TDD_ENFORCEMENT_MODE`
  - `strict`：协议缺项直接判失败并进入失败处理链路。
  - `advisory`：协议缺项仅告警，流程可继续（适合迁移期/试运行）。

### 8.2 两个 Review 阈值如何配合

- 常见配置：
  - `REVIEW_MAX_LOOPS=2`
  - `REVIEW_ARBITER_MAX_LOOPS=2`
- 语义：
  1. Review 最多允许 2 次自动回流到 Coding。
  2. 超过后最多允许 2 次“设计仲裁”做继续/停止决策。
  3. 仲裁次数也超限时，直接 `Blocked` 并提示人工介入。

> 简单理解：`REVIEW_MAX_LOOPS` 控制“回流次数”，`REVIEW_ARBITER_MAX_LOOPS` 控制“超限后还能仲裁几次”。

### 8.3 并发与工作区隔离（新增）

- 默认启用 issue 隔离工作区：每个 issue 使用独立 `git worktree` 执行 `Design/Coding/Review`。
- 路径规则：`<ISSUE_WORKTREE_ROOT>/<project_id>/<issue_id>/`。
- 全局并发受 `ISSUE_MAX_CONCURRENCY` 控制；超限后在内存中排队，不丢任务。
- 若 worktree 初始化失败，issue 直接进入 `Blocked`（`failure_class=ENV_ISSUE`），并给出人工处理建议。
- 若启用 `ISSUE_WORKTREE_CLEANUP_ENABLED`，在 issue 进入 `Done/Blocked` 后会机会式清理超过 `ISSUE_WORKTREE_RETENTION_HOURS` 且不活跃的 worktree。

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
