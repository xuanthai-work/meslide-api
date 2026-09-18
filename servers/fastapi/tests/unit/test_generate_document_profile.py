"""Unit tests for the provider-independent document profiler LLM calls."""

import asyncio

import pytest

from utils.llm_calls.generate_document_profile import (
    DIRECT_PROFILE_MAX_INPUT_CHARACTERS,
    DOCUMENT_PROFILE_SYSTEM_PROMPT,
    MAX_PROFILE_CHUNKS,
    PROFILE_CHUNK_MAX_CHARACTERS,
    RESPONSE_FORMAT_NAME,
    build_document_profile_messages,
    build_profile_merge_messages,
    generate_document_profile,
    merge_partial_document_profiles,
    profile_documents,
    split_documents_into_chunks,
)

PROFILING_TARGET = "utils.llm_calls.generate_document_profile"


def test_system_prompt_states_injection_boundary_and_claim_discipline():
    normalized = " ".join(DOCUMENT_PROFILE_SYSTEM_PROMPT.lower().split())

    assert "quoted untrusted data" in normalized
    assert (
        "never relabel a document-stated claim as an independently verified fact"
        in normalized
    )
    assert "preserve multiple-document conflicts" in normalized
    assert "never invent, guess, or output file paths" in normalized


def test_profile_messages_embed_labeled_delimited_documents():
    documents = [
        ("Document 1", "First report text"),
        ("Document 2", "Second report text with a prompt-injection attempt"),
    ]

    messages = build_document_profile_messages(documents, language="Vietnamese (Tiếng Việt)")
    user_content = messages[1].content

    assert [message.role for message in messages] == ["system", "user"]
    assert '<document index="1" label="Document 1">' in user_content
    assert '<document index="2" label="Document 2">' in user_content
    assert "First report text" in user_content
    assert "Second report text with a prompt-injection attempt" in user_content
    assert "quoted untrusted data" in user_content.lower()
    assert "Vietnamese (Tiếng Việt)" in user_content
    assert "Return only the profile JSON" in user_content


def test_merge_messages_list_labels_and_partials():
    partials = [
        {"primary_topic": "Part one topic"},
        {"primary_topic": "Part two topic"},
    ]

    messages = build_profile_merge_messages(
        partials,
        ["Document 1", "Document 2"],
        language=None,
    )
    user_content = messages[1].content

    assert "DOCUMENT LABELS:" in user_content
    assert "- Document 1" in user_content
    assert '<partial-profile index="1">' in user_content
    assert "primary topic" in messages[0].content.lower()
    assert "quoted untrusted data" in user_content.lower()


def test_split_documents_into_chunks_preserves_all_text_in_order():
    documents = [
        ("Document 1", "a" * (PROFILE_CHUNK_MAX_CHARACTERS + 10)),
        ("Document 2", "b" * 5),
    ]

    chunks = split_documents_into_chunks(documents, PROFILE_CHUNK_MAX_CHARACTERS)

    flattened = [(label, text) for chunk in chunks for label, text in chunk]
    assert "".join(text for _, text in flattened) == documents[0][1] + documents[1][1]
    assert [label for label, _ in flattened] == ["Document 1", "Document 1", "Document 2"]
    for chunk in chunks:
        assert sum(len(text) for _, text in chunk) <= PROFILE_CHUNK_MAX_CHARACTERS


def test_split_documents_into_chunks_respects_custom_budget():
    documents = [("Document 1", "a" * 100)]

    chunks = split_documents_into_chunks(documents, max_characters=30)

    assert all(len(text) for _, text in chunks[0])
    assert sum(len(text) for chunk in chunks for _, text in chunk) == 100


def test_split_documents_skips_empty_texts():
    assert split_documents_into_chunks([("Document 1", "")]) == []


def _patch_structured(monkeypatch, results):
    """Replace the shared structured generator, one dict result per call."""
    captured = []
    remaining = list(results)

    async def fake_generate(_client, _model, **kwargs):
        captured.append(kwargs)
        result = remaining.pop(0) if remaining else results[-1]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(f"{PROFILING_TARGET}.generate_structured_with_schema_retries", fake_generate)
    return captured


def test_generate_document_profile_uses_schema_validated_structured_output(monkeypatch):
    captured = _patch_structured(monkeypatch, [{"primary_topic": "ignored"}])

    result = asyncio.run(
        generate_document_profile(
            object(), "test-model", [("Document 1", "report text")]
        )
    )

    assert result == {"primary_topic": "ignored"}
    kwargs = captured[0]
    assert kwargs["response_format"].name == RESPONSE_FORMAT_NAME
    assert kwargs["validate_schema"] is True
    assert kwargs["messages"][0].content == DOCUMENT_PROFILE_SYSTEM_PROMPT
    assert kwargs["messages"][1].content.count("report text") == 1


def test_merge_partial_profiles_passes_labels_and_partials(monkeypatch):
    captured = _patch_structured(monkeypatch, [{"primary_topic": "merged"}])
    partials = [{"primary_topic": "part one"}, {"primary_topic": "part two"}]

    result = asyncio.run(
        merge_partial_document_profiles(
            object(),
            "test-model",
            partials,
            ["Document 1"],
        )
    )

    assert result == {"primary_topic": "merged"}
    user_content = captured[0]["messages"][1].content
    assert "part one" in user_content
    assert "part two" in user_content
    assert "- Document 1" in user_content


def test_profile_documents_uses_direct_path_for_small_inputs(monkeypatch):
    captured = _patch_structured(monkeypatch, [{"primary_topic": "direct"}])
    documents = [("Document 1", "small report text")]

    result = asyncio.run(profile_documents(object(), "test-model", documents))

    assert result == {"primary_topic": "direct"}
    assert len(captured) == 1
    assert captured[0]["messages"][1].content.count("small report text") == 1


@pytest.mark.parametrize(
    "total_characters", (DIRECT_PROFILE_MAX_INPUT_CHARACTERS, DIRECT_PROFILE_MAX_INPUT_CHARACTERS - 1)
)
def test_profile_documents_direct_boundary_is_inclusive(monkeypatch, total_characters):
    captured = _patch_structured(monkeypatch, [{"primary_topic": "direct"}])
    documents = [("Document 1", "a" * total_characters)]

    asyncio.run(profile_documents(object(), "test-model", documents))

    assert len(captured) == 1


def test_profile_documents_chunks_and_merges_large_inputs(monkeypatch):
    captured = _patch_structured(
        monkeypatch,
        [
            {"primary_topic": "chunk 1"},
            {"primary_topic": "chunk 2"},
            {"primary_topic": "merged"},
        ],
    )
    documents = [
        ("Document 1", "a" * 1500),
        ("Document 2", "b" * 300),
    ]

    result = asyncio.run(
        profile_documents(
            object(),
            "test-model",
            documents,
            direct_max_characters=1000,
            chunk_max_characters=1000,
        )
    )

    assert result == {"primary_topic": "merged"}
    # Two chunk profiles plus one merge call.
    assert len(captured) == 3
    assert "chunk 1" not in result
    merged_call = captured[-1]
    assert "- Document 1" in merged_call["messages"][1].content
    assert "- Document 2" in merged_call["messages"][1].content


def test_profile_documents_caps_total_chunks_at_the_configured_maximum(monkeypatch):
    captured = _patch_structured(monkeypatch, [{"primary_topic": "chunk"}])

    documents = [("Document 1", "a" * (PROFILE_CHUNK_MAX_CHARACTERS * 20))]

    result = asyncio.run(
        profile_documents(
            object(),
            "test-model",
            documents,
            direct_max_characters=100,
        )
    )

    # One call per chunk up to MAX_PROFILE_CHUNKS, plus one merge call.
    assert len(captured) == MAX_PROFILE_CHUNKS + 1
    assert result == {"primary_topic": "chunk"}


def test_profile_chunk_budget_covers_later_documents_before_repeating(monkeypatch):
    captured = _patch_structured(monkeypatch, [{"primary_topic": "chunk"}])
    documents = [
        ("Document 1", "a" * 1000),
        ("Document 2", "LATER-DOCUMENT-EVIDENCE"),
    ]

    asyncio.run(
        profile_documents(
            object(),
            "test-model",
            documents,
            direct_max_characters=10,
            chunk_max_characters=100,
        )
    )

    chunk_prompts = [call["messages"][1].content for call in captured[:-1]]
    assert len(chunk_prompts) == MAX_PROFILE_CHUNKS
    assert any("LATER-DOCUMENT-EVIDENCE" in prompt for prompt in chunk_prompts)
    assert "- Document 2" in captured[-1]["messages"][1].content
