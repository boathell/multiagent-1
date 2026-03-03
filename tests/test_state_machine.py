from app.models import PipelineState, StageStatus
from app.state_machine import StateMachineError, can_transition, next_state, require_transition


def test_valid_transitions():
    assert can_transition(PipelineState.TODO, PipelineState.DESIGN)
    assert can_transition(PipelineState.REVIEW, PipelineState.DONE)


def test_invalid_transition_raises():
    try:
        require_transition(PipelineState.TODO, PipelineState.DONE)
    except StateMachineError:
        assert True
    else:
        assert False


def test_next_state_mapping():
    assert next_state(PipelineState.DESIGN, StageStatus.SUCCESS) == PipelineState.CODING
    assert next_state(PipelineState.CODING, StageStatus.FAILED) == PipelineState.BLOCKED
    assert next_state(PipelineState.REVIEW, StageStatus.NEEDS_CHANGES) == PipelineState.CODING

