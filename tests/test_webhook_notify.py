from app.api.webhooks import _extract_notify_message


def test_extract_start_message():
    payload = {
        "event": "work_item.created",
        "data": {"work_item": {"id": "11", "name": "Task A"}},
    }
    msg = _extract_notify_message(payload)
    assert msg is not None
    assert msg[0] == "[Plane] 任务开始"


def test_extract_pr_message_from_orch_comment():
    payload = {
        "event": "comment.created",
        "data": {
            "comment": {
                "comment_html": "[ORCH] | stage=coding | status=success | summary=ok | pr_url=https://x/p/1"
            }
        },
    }
    msg = _extract_notify_message(payload)
    assert msg is not None
    assert msg[0] == "[Plane] PR 已创建"


def test_extract_pr_message_from_cn_orch_comment():
    payload = {
        "event": "comment.created",
        "data": {
            "comment": {
                "comment_html": "[编排器] | 阶段：编码 | 状态：成功 | 摘要：已创建 PR | PR：https://x/p/2"
            }
        },
    }
    msg = _extract_notify_message(payload)
    assert msg is not None
    assert msg[0] == "[Plane] PR 已创建"


def test_extract_done_state_message():
    payload = {
        "event": "work_item.updated",
        "data": {
            "work_item": {
                "id": "11",
                "state": {"name": "Done"},
            }
        },
    }
    msg = _extract_notify_message(payload)
    assert msg is not None
    assert msg[0] == "[Plane] 任务完成"
