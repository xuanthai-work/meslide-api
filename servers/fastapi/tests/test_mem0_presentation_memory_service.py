import asyncio
import uuid
from unittest.mock import patch

import services.mem0_oss_memory as mem0_oss
from services.mem0_presentation_memory_service import Mem0PresentationMemoryService


class FakeMemoryClient:
    instances: list["FakeMemoryClient"] = []

    def __init__(self, config=None):
        self.config = config
        self.add_calls = []
        self.search_calls = []
        self.next_search_response = {"results": []}
        FakeMemoryClient.instances.append(self)

    @classmethod
    def from_config(cls, config):
        return cls(config=config)

    def add(self, *args, **kwargs):
        messages = kwargs.get("messages") if "messages" in kwargs else None
        if messages is None and args:
            messages = args[0]

        self.add_calls.append(
            {
                "messages": messages,
                "user_id": kwargs.get("user_id"),
                "infer": kwargs.get("infer"),
            }
        )
        return {"ok": True}

    def search(self, query, *args, **kwargs):
        self.search_calls.append(
            {
                "query": query,
                "filters": kwargs.get("filters"),
                "user_id": kwargs.get("user_id"),
                "top_k": kwargs.get("top_k"),
            }
        )
        return self.next_search_response


def _mem0_oss_fresh() -> None:
    mem0_oss._shared_client = None  # type: ignore[attr-defined]
    mem0_oss._init_attempted = False  # type: ignore[attr-defined]


class TestMem0PresentationMemoryService:
    def setup_method(self):
        FakeMemoryClient.instances = []
        _mem0_oss_fresh()

    def test_shared_client_uses_openai_when_key_is_configured(self):
        captured = {}

        def _fake_memory_from_config(config, telemetry_base):
            captured["config"] = config
            captured["telemetry_base"] = telemetry_base
            return FakeMemoryClient.from_config(config)

        with patch.dict(
            "os.environ",
            {
                "MEM0_ENABLED": "true",
                "MEM0_REQUIRE_SPACY_MODEL": "false",
                "APP_DATA_DIRECTORY": "/tmp/mem0-test",
                "OPENAI_API_KEY": "test-openai-key",
                "OPENAI_MODEL": "gpt-4o-mini",
            },
            clear=False,
        ), patch(
            "services.mem0_oss_memory.memory_from_config",
            side_effect=_fake_memory_from_config,
        ):
            client = mem0_oss.get_shared_mem0_client()

        assert client is not None
        assert captured["telemetry_base"].endswith("/mem0/telemetry/oss")
        assert captured["config"]["llm"]["provider"] == "openai"
        assert captured["config"]["llm"]["config"]["model"] == "gpt-4o-mini"
        assert captured["config"]["llm"]["config"]["api_key"] == "test-openai-key"
        assert captured["config"]["vector_store"]["provider"] == "qdrant"
        assert captured["config"]["embedder"]["provider"] == "fastembed"

    def test_openai_embedder_uses_custom_base_url_and_api_key(self):
        captured = {}

        def _fake_memory_from_config(config, telemetry_base):
            captured["config"] = config
            return FakeMemoryClient.from_config(config)

        with patch.dict(
            "os.environ",
            {
                "MEM0_ENABLED": "true",
                "MEM0_REQUIRE_SPACY_MODEL": "false",
                "APP_DATA_DIRECTORY": "/tmp/mem0-test",
                "MEM0_LLM_BASE_URL": "http://host.docker.internal:5000/v1",
                "MEM0_LLM_API_KEY": "sk-custom-proxy",
                "MEM0_LLM_MODEL": "cb/hnw-llm",
                "MEM0_EMBEDDER_PROVIDER": "openai",
                "MEM0_EMBEDDER_MODEL": "text-embedding-3-small",
                "MEM0_EMBEDDING_DIMS": "1536",
            },
            clear=False,
        ), patch(
            "services.mem0_oss_memory.memory_from_config",
            side_effect=_fake_memory_from_config,
        ):
            client = mem0_oss.get_shared_mem0_client()

        assert client is not None
        embedder = captured["config"]["embedder"]
        assert embedder["provider"] == "openai"
        assert embedder["config"]["api_key"] == "sk-custom-proxy"
        assert embedder["config"]["openai_base_url"] == (
            "http://host.docker.internal:5000/v1"
        )
        assert embedder["config"]["model"] == "text-embedding-3-small"
        assert captured["config"]["llm"]["config"]["model"] == "cb/hnw-llm"

    def test_store_generation_context_uses_presentation_scope(self):
        with patch.dict(
            "os.environ",
            {
                "MEM0_ENABLED": "true",
                "APP_DATA_DIRECTORY": "/tmp/mem0-test",
            },
            clear=False,
        ), patch(
            "services.mem0_presentation_memory_service.get_shared_mem0_client",
            return_value=FakeMemoryClient.from_config(
                {
                    "vector_store": {
                        "provider": "qdrant",
                        "config": {
                            "on_disk": True,
                            "embedding_model_dims": 384,
                        },
                    },
                    "embedder": {
                        "provider": "fastembed",
                        "config": {
                            "model": "BAAI/bge-small-en-v1.5",
                            "embedding_dims": 384,
                        },
                    },
                }
            ),
        ):
            service = Mem0PresentationMemoryService()
            presentation_id = uuid.uuid4()
            asyncio.run(
                service.store_generation_context(
                    presentation_id=presentation_id,
                    system_prompt="system prompt",
                    user_prompt="user prompt",
                    extracted_document_text="doc text",
                    source_content="seed prompt",
                    instructions="be concise",
                )
            )

        assert len(FakeMemoryClient.instances) == 1
        client = FakeMemoryClient.instances[0]
        assert client.config is not None
        assert client.config["vector_store"]["provider"] == "qdrant"
        assert client.config["embedder"]["provider"] == "fastembed"
        assert (
            client.config["embedder"]["config"]["model"]
            == "BAAI/bge-small-en-v1.5"
        )
        assert client.config["embedder"]["config"]["embedding_dims"] == 384
        assert client.config["vector_store"]["config"]["on_disk"] is True
        assert client.config["vector_store"]["config"]["embedding_model_dims"] == 384
        assert len(client.add_calls) == 5

        scoped_user_id = f"presentation:{presentation_id}"
        for call in client.add_calls:
            assert call["user_id"] == scoped_user_id
            assert call["infer"] is False

        serialized_messages = "\n".join(
            str(call["messages"][0]["content"]) for call in client.add_calls
        )
        assert "[outline_system_prompt]" in serialized_messages
        assert "[outline_user_prompt]" in serialized_messages
        assert "[document_extracted_text]" in serialized_messages
        assert "[presentation_source_prompt]" in serialized_messages

    def test_retrieve_context_uses_same_scope_and_deduplicates(self):
        with patch.dict(
            "os.environ",
            {
                "MEM0_ENABLED": "true",
                "MEM0_TOP_K": "5",
                "APP_DATA_DIRECTORY": "/tmp/mem0-test",
            },
            clear=False,
        ), patch(
            "services.mem0_presentation_memory_service.get_shared_mem0_client",
            return_value=FakeMemoryClient.from_config(
                {
                    "vector_store": {"provider": "qdrant", "config": {}},
                    "embedder": {
                        "provider": "fastembed",
                        "config": {
                            "model": "BAAI/bge-small-en-v1.5",
                            "embedding_dims": 384,
                        },
                    },
                }
            ),
        ):
            service = Mem0PresentationMemoryService()
            presentation_id = uuid.uuid4()

            asyncio.run(
                service.store_generated_outlines(
                    presentation_id,
                    {"slides": [{"content": "One"}]},
                )
            )

            client = FakeMemoryClient.instances[0]
            client.next_search_response = {
                "results": [
                    {"memory": "Memory A"},
                    {"memory": "Memory A"},
                    {"memory": "Memory B"},
                ]
            }

            context = asyncio.run(
                service.retrieve_context(
                    presentation_id=presentation_id,
                    query="change the conclusion",
                )
            )

        assert "Memory A" in context
        assert "Memory B" in context
        assert context.count("Memory A") == 1

        assert len(client.search_calls) == 1
        assert client.search_calls[0]["query"] == "change the conclusion"
        assert client.search_calls[0]["filters"] == {
            "user_id": f"presentation:{presentation_id}"
        }
        assert client.search_calls[0]["top_k"] == 5


def test_oss_config_defaults_are_gslide(monkeypatch):
    monkeypatch.delenv("APP_DATA_DIRECTORY", raising=False)
    monkeypatch.delenv("MEM0_COLLECTION_NAME", raising=False)
    monkeypatch.delenv("MEM0_DIR", raising=False)

    _mem0_dir, _qdrant, _history, collection, _dims, config = mem0_oss._oss_config_from_env()

    assert collection == "gslide_memories"
    assert _mem0_dir.replace("\\", "/").startswith("/tmp/gslide")
    assert config["vector_store"]["config"]["collection_name"] == "gslide_memories"
