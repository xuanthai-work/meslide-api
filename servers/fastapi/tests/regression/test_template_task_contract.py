from api.v1.ppt.endpoints.template import _template_task_progress_data


def test_template_progress_keeps_indexed_statuses_and_nullable_metadata():
    data = _template_task_progress_data(
        created_layouts=1,
        remaining_layouts=2,
        name="Deck",
        thumbnail=None,
        completed_layout_indices={2},
    )
    assert data == {
        "created_layouts": 1,
        "remaining_layouts": 2,
        "slide_layout_statuses": [
            {"index": 0, "status": "pending"},
            {"index": 1, "status": "pending"},
            {"index": 2, "status": "completed"},
        ],
        "name": "Deck",
        "thumbnail": None,
    }
