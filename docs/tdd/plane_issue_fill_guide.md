# TDD Issue 填写指南（Plane）

## 使用方式
1. 在 Plane 新建 issue。
2. 将 `docs/tdd/plane_issue_template.md` 全量复制到 issue 描述。
3. 你可以只填写“背景与目标 / 范围”，Red/Green/Refactor 可留空。
4. 提交后由编排器读取 issue 描述；Design 阶段会优先补全 Red/Green/Refactor，并回填到 Plane issue 描述，再传给 Coding/Review。

## 最小必填项（建议）
- 背景与目标：必须写清“要解决什么问题”。
- 范围 / 非目标：至少给出 1-2 条边界，防止设计偏航。
- Red/Green/Refactor：可不手填，由 Design 自动补全；若你已明确，也可提前填写。

## 缺项时系统行为
- 不会阻断流程。
- 仅当 Design 输出后仍缺 Red/Green/Refactor，Plane 才会自动评论：
  - `[TDD-提醒] 设计阶段未补全 Red/Green/Refactor，已按降级模式执行。`

## 编排阶段消费规则
- Design（Kimi）：负责补全 TDD 设计（Red/Green/Refactor + 验收标准），并评估覆盖与风险。
- Coding（Codex）：按 Red -> Green -> Refactor 顺序实施并回报。
- Review（Gemini）：核对顺序与证据，输出 `APPROVED` 或 `NEEDS_CHANGES`。
