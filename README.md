# multiagent-orchestrator

Plane-driven multi-agent orchestrator MVP.

## Current notification mode

- Feishu notification is currently disabled.
- Track progress in Plane only.
- Keep `POST /webhooks/plane` enabled for orchestration.
- Optional relay endpoint `POST /webhooks/plane/relay` can remain unconfigured.

## TDD issue template workflow

- Create issue in Plane and paste `docs/tdd/plane_issue_template.md` into description.
- Fill at least `Red / Green / Refactor` sections.
- Orchestrator reads issue description and injects TDD context into Design/Coding/Review prompts.
- Missing sections do not block pipeline, but Plane will receive `[TDD-提醒]` comment.
- See `docs/tdd/plane_issue_fill_guide.md` and `docs/tdd/examples/sample_issue_feature.md`.
