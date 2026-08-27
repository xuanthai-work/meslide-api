from templates.v2.content import (
    content_name_candidates,
    hydrate_repeated_top_level_groups,
    lookup_template_content,
    read_template_text,
    repeated_content_keys_for_name,
    template_asset_prompt,
)


def _repeated_groups():
    return [
        {
            "type": "group",
            "name": f"timeline_{index}",
            "position": {"x": index * 100, "y": index * 20},
            "children": [
                {
                    "type": "group",
                    "name": "timeline_items",
                    "children": [
                        {
                            "type": "image",
                            "decorative": True,
                            "name": "connector_branch_path",
                            "data": f"connector-{index}.svg",
                            "is_icon": False,
                        }
                    ],
                },
                {
                    "type": "group",
                    "name": "timeline_milestone",
                    "children": [
                        {
                            "type": "text",
                            "decorative": False,
                            "name": "milestone_title",
                            "min_length": 4,
                            "max_length": 20,
                        }
                    ],
                },
            ],
        }
        for index in (4, 5, 3, 1, 2)
    ]


def test_hydrate_repeated_top_level_groups_maps_items_to_whole_groups():
    elements = _repeated_groups()
    content = {
        "timeline": [
            {"timeline_milestone": {"milestone_title": title}}
            for title in ("First", "Second", "Third", "Fourth", "Fifth")
        ]
    }

    def apply_item(element, item):
        element["applied_title"] = item["timeline_milestone"]["milestone_title"]
        return element

    hydrated = hydrate_repeated_top_level_groups(
        elements,
        content,
        apply_item=apply_item,
    )

    assert hydrated is not None
    assert [element["name"] for element in hydrated] == [
        "timeline_4",
        "timeline_5",
        "timeline_3",
        "timeline_1",
        "timeline_2",
    ]
    assert [element["applied_title"] for element in hydrated] == [
        "First",
        "Second",
        "Third",
        "Fourth",
        "Fifth",
    ]
    assert [len(element["children"]) for element in hydrated] == [2] * 5
    assert hydrated[0]["children"][0]["children"][0]["data"] == "connector-4.svg"
    assert "applied_title" not in elements[0]


def test_hydrate_repeated_top_level_groups_uses_center_out_prefix_for_minimum():
    elements = _repeated_groups()
    content = {
        "timeline": [
            {"timeline_milestone": {"milestone_title": "Center lower"}},
            {"timeline_milestone": {"milestone_title": "Center upper"}},
        ]
    }

    hydrated = hydrate_repeated_top_level_groups(
        elements,
        content,
        apply_item=lambda element, _item: element,
    )

    assert hydrated is not None
    assert [element["name"] for element in hydrated] == [
        "timeline_4",
        "timeline_5",
    ]


def test_content_name_candidates_strips_numeric_tokens_and_prefix():
    assert content_name_candidates("block_heading_text") == [
        "block_heading_text",
        "heading_text",
    ]
    assert content_name_candidates("gallery_photo_2") == [
        "gallery_photo_2",
        "gallery_photo",
        "photo",
    ]


def test_lookup_template_content_prefers_explicit_keys_then_candidates():
    content = {
        "heading_text": "from suffix",
        "block_heading_text": "from full name",
        "block_heading_text_2": "from preferred",
    }

    found, value = lookup_template_content(
        content,
        "block_heading_text",
        preferred_keys=["block_heading_text_2"],
    )
    assert found is True
    assert value == "from preferred"

    found, value = lookup_template_content(content, "block_heading_text")
    assert found is True
    assert value == "from full name"


def test_lookup_template_content_treats_empty_string_as_present():
    found, value = lookup_template_content({"title": ""}, "title")
    assert found is True
    assert value == ""


def test_repeated_content_keys_for_name_suffixes_later_occurrences():
    content = {"callout": "A", "callout_2": "B"}
    occurrences: dict[str, int] = {}

    assert repeated_content_keys_for_name("callout", content, occurrences) is None
    assert repeated_content_keys_for_name("callout", content, occurrences) == [
        "callout_2"
    ]
    assert occurrences == {"callout": 2}


def test_template_asset_prompt_reads_image_then_icon_keys():
    assert (
        template_asset_prompt(
            {"image_prompt": "sky", "prompt": "ignored"},
            is_icon=False,
        )
        == "sky"
    )
    assert (
        template_asset_prompt(
            {"icon_query": "arrow", "query": "ignored"},
            is_icon=True,
        )
        == "arrow"
    )
    assert template_asset_prompt("not-a-dict", is_icon=False) is None


def test_read_template_text_accepts_str_number_and_dict_text():
    assert read_template_text("hello") == "hello"
    assert read_template_text(12) == "12"
    assert read_template_text({"text": 3.5}) == "3.5"
    assert read_template_text({"text": True}) is None
    assert read_template_text(True) is None

