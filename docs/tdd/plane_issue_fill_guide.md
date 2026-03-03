# TDD Issue 填写指南（Plane）

## 使用方式
1. 在 Plane 新建 issue。
2. 将 `docs/tdd/plane_issue_template.md` 全量复制到 issue 描述。
3. 至少填写 Red/Green/Refactor 三个区块。
4. 提交后由编排器自动读取并传给 Design/Coding/Review 三阶段 Agent。

## 最小必填项（建议）
- Red 阶段：至少 1 条测试用例和执行命令。
- Green 阶段：至少 1 条最小实现改动。
- Refactor 阶段：至少 1 条重构目标和回归命令。

## 缺项时系统行为
- 不会阻断流程。
- Plane 会自动评论：
  - `[TDD-提醒] 缺少 Red/Green/Refactor 段落，已按降级模式执行`

## 编排阶段消费规则
- Design（Kimi）：评估 Red 用例覆盖与风险。
- Coding（Codex）：按 Red -> Green -> Refactor 顺序实施并回报。
- Review（Gemini）：核对顺序与证据，输出 `APPROVED` 或 `NEEDS_CHANGES`。

