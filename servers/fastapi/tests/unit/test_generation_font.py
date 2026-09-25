from utils.generation_font import generation_font_assets, with_generation_font
from templates.v2.models.elements import Chart


def test_generation_font_replaces_nested_text_styles_without_changing_template():
    template_layout = {
        "layouts": [
            {
                "id": "custom-slide",
                "components": [
                    {
                        "elements": [
                            {
                                "type": "text",
                                "font": {"family": "Poppins", "size": 32, "bold": True},
                                "runs": [
                                    {"text": "Title", "font": {"family": "Inter", "size": 32}}
                                ],
                            },
                            {
                                "type": "table",
                                "rows": [
                                    [
                                        {
                                            "font": {"family": "Sniglet", "size": 12},
                                            "runs": [
                                                {"text": "Cell", "font": {"family": "Arial"}}
                                            ],
                                        }
                                    ]
                                ],
                            },
                            {"type": "text", "runs": [{"text": "No font supplied"}]},
                            {"type": "chart", "title": "Revenue"},
                        ]
                    }
                ],
            }
        ]
    }

    generated = with_generation_font(template_layout)
    elements = generated["layouts"][0]["components"][0]["elements"]

    assert elements[0]["font"] == {
        "family": "Be Vietnam Pro",
        "size": 32,
        "bold": True,
    }
    assert elements[0]["runs"][0]["font"]["family"] == "Be Vietnam Pro"
    assert elements[1]["rows"][0][0]["font"] == {
        "family": "Be Vietnam Pro",
        "size": 12,
    }
    assert elements[1]["rows"][0][0]["runs"][0]["font"]["family"] == "Be Vietnam Pro"
    assert elements[2]["font"] == {"family": "Be Vietnam Pro"}
    assert elements[3]["font"] == {"family": "Be Vietnam Pro"}
    assert template_layout["layouts"][0]["components"][0]["elements"][0]["font"]["family"] == "Poppins"


def test_generation_font_assets_load_be_vietnam_pro():
    assert generation_font_assets() == {
        "Be Vietnam Pro": "https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:ital,wght@0,100;0,200;0,300;0,400;0,500;0,600;0,700;0,800;0,900;1,100;1,200;1,300;1,400;1,500;1,600;1,700;1,800;1,900&display=swap"
    }


def test_generation_font_assets_keeps_template_font_files_for_other_assets():
    template_fonts = {"Brand Icons": "/app_data/fonts/brand-icons.ttf"}

    assets = generation_font_assets(template_fonts)

    assert assets["Brand Icons"] == "/app_data/fonts/brand-icons.ttf"
    assert assets["Be Vietnam Pro"].startswith(
        "https://fonts.googleapis.com/css2?family=Be+Vietnam+Pro:"
    )
    assert template_fonts == {"Brand Icons": "/app_data/fonts/brand-icons.ttf"}


def test_generated_chart_font_survives_template_validation():
    generated = with_generation_font(
        {
            "type": "chart",
            "chart_type": "bar",
            "decorative": False,
            "name": "revenue",
        }
    )

    assert Chart.model_validate(generated).model_dump()["font"]["family"] == "Be Vietnam Pro"
