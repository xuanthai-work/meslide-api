from models.generate_presentation_request import GeneratePresentationRequest


def test_generated_presentations_include_required_structure_by_default():
    request = GeneratePresentationRequest(content="Create a product update")

    assert request.include_title_slide is True
    assert request.include_table_of_contents is True
