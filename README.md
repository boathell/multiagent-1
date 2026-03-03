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
