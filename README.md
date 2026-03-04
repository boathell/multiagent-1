# multiagent-orchestrator

Plane-driven multi-agent orchestrator MVP.

## Current notification mode

- Feishu notification is currently disabled.
- Track progress in Plane only.
- Keep `POST /webhooks/plane` enabled for orchestration.
- Optional relay endpoint `POST /webhooks/plane/relay` can remain unconfigured.

## TDD issue template workflow

- Create issue in Plane and paste `docs/tdd/plane_issue_template.md` into description.
- Human can provide only business goal/scope; Design agent (Kimi/Claude role) auto-fills `Red / Green / Refactor`.
- Orchestrator reads issue description and injects TDD context into Design/Coding/Review prompts.
- When Design auto-fills missing sections, orchestrator syncs them back to Plane issue description.
- If Design output still misses key sections, pipeline continues and Plane receives `[TDD-提醒]`.
- See `docs/tdd/plane_issue_fill_guide.md` and `docs/tdd/examples/sample_issue_feature.md`.

## Review 回流仲裁与代码行数限制

- 当 Review 回流次数超过 `REVIEW_MAX_LOOPS`，编排器会触发一次 Design 仲裁（Kimi）。
- 仲裁首行仅允许 `CONTINUE_CODING` 或 `STOP_REVIEW`，并要求输出 `DIAGNOSIS/REASON/ACTIONS`。
- 若仲裁决定 `STOP_REVIEW`，issue 直接进入 `Blocked`；若继续编码，则最多再允许 `REVIEW_ARBITER_MAX_LOOPS` 次仲裁。
- Coding 成功后会执行全仓代码文件行数检查（`MAX_CODE_FILE_LINES`，默认 1000）。
- 任一代码文件超过上限会直接阻断并在 Plane 评论中标注超限文件与行数。

## 多 Agent 治理（M7-M12）

- 新增 `Issue Contract` 解析：目标/范围/DoD/风险/回滚，缺项会在 Plane 中文评论中给出 `[TDD-Contract-提醒]`（建议性，不阻断）。
- 新增三阶段协议校验（strict/advisory）：
  - Design: `TDD_RED/TDD_GREEN/TDD_REFACTOR/TDD_ACCEPTANCE`
  - Coding: `RED_RESULT/GREEN_RESULT/REFACTOR_NOTE/CHANGED_FILES`
  - Review: 首行必须 `APPROVED` 或 `NEEDS_CHANGES`
- 新增失败分类：`QUALITY_ISSUE / GEMINI_ISSUE / ENV_ISSUE / PROTOCOL_VIOLATION`。
- 新增人类接管（`[HUMAN-HANDOFF]`）触发：
  - 同阶段工具故障连续 2 次
  - 协议违规连续 2 次
  - Review 仲裁超限或仲裁判定停止审查
  - 命中高风险关键词（如数据迁移/删除/权限变更）
- Coding 成功后会把 TDD 摘要同步到 PR 评论；Review 失败会把修复清单同步到 Plane + PR（双向一致）。

## 新配置项

- `HUMAN_HANDOFF_ENABLED=true|false`：是否启用自动人工接管。
- `TDD_ENFORCEMENT_MODE=strict|advisory`：协议校验模式（默认 `strict`）。
- `REVIEW_MAX_LOOPS=1`、`REVIEW_ARBITER_MAX_LOOPS=1`、`MAX_CODE_FILE_LINES=1000`：治理阈值。
- `config/agents.yaml` 支持 review 配置：

  ```yaml
  review:
    max_loops: 1
    arbiter_max_loops: 1
  ```

- Review 回流阈值优先级：环境变量（`REVIEW_MAX_LOOPS` / `REVIEW_ARBITER_MAX_LOOPS`） > `agents.yaml` > 默认值（`1` / `1`）。
