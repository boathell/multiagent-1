from __future__ import annotations

from app.models import PipelineState, Stage, StageStatus


class StateMachineError(ValueError):
    pass


TRANSITIONS: dict[PipelineState, set[PipelineState]] = {
    PipelineState.TODO: {PipelineState.DESIGN},
    PipelineState.DESIGN: {PipelineState.CODING, PipelineState.BLOCKED},
    PipelineState.CODING: {PipelineState.REVIEW, PipelineState.BLOCKED},
    PipelineState.REVIEW: {PipelineState.CODING, PipelineState.DONE, PipelineState.BLOCKED},
    PipelineState.DONE: set(),
    PipelineState.BLOCKED: {PipelineState.DESIGN, PipelineState.CODING, PipelineState.REVIEW},
}


def to_state(value: str | PipelineState) -> PipelineState:
    if isinstance(value, PipelineState):
        return value
    for s in PipelineState:
        if s.value.lower() == str(value).lower():
            return s
    raise StateMachineError(f"Unknown state: {value}")


def can_transition(current: str | PipelineState, nxt: str | PipelineState) -> bool:
    current_state = to_state(current)
    next_state = to_state(nxt)
    return next_state in TRANSITIONS[current_state]


def require_transition(current: str | PipelineState, nxt: str | PipelineState) -> PipelineState:
    if not can_transition(current, nxt):
        raise StateMachineError(f"Illegal transition: {current} -> {nxt}")
    return to_state(nxt)


def stage_for_state(state: str | PipelineState) -> Stage | None:
    s = to_state(state)
    if s == PipelineState.DESIGN:
        return Stage.DESIGN
    if s == PipelineState.CODING:
        return Stage.CODING
    if s == PipelineState.REVIEW:
        return Stage.REVIEW
    return None


def next_state(current: str | PipelineState, result: StageStatus) -> PipelineState:
    state = to_state(current)
    if state == PipelineState.DESIGN:
        candidate = PipelineState.CODING if result == StageStatus.SUCCESS else PipelineState.BLOCKED
    elif state == PipelineState.CODING:
        candidate = PipelineState.REVIEW if result == StageStatus.SUCCESS else PipelineState.BLOCKED
    elif state == PipelineState.REVIEW:
        if result == StageStatus.SUCCESS:
            candidate = PipelineState.DONE
        elif result == StageStatus.NEEDS_CHANGES:
            candidate = PipelineState.CODING
        else:
            candidate = PipelineState.BLOCKED
    else:
        raise StateMachineError(f"State {state.value} has no stage execution")

    return require_transition(state, candidate)

