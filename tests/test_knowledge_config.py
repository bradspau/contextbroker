"""The knowledge settings: defaults, validation that names the variable and never the value."""
import ast
import sys
from pathlib import Path

import pytest

from okf_retrieval import config


def test_the_knowledge_root_defaults_to_the_repository_directory_and_follows_the_variable():
    assert config.knowledge_root({}).name == "knowledge"
    assert config.knowledge_root({}).parent == Path(config.__file__).resolve().parent.parent
    assert config.knowledge_root({"BROKER_KNOWLEDGE_PATH": "/x"}) == Path("/x")


def test_the_qdrant_url_defaults_and_accepts_an_http_or_https_url():
    assert config.qdrant_url({}) == "http://localhost:6333"
    assert config.qdrant_url({"BROKER_QDRANT_URL": "http://qdrant:6333"}) == "http://qdrant:6333"
    assert config.qdrant_url({"BROKER_QDRANT_URL": "https://q.example:443"}) == "https://q.example:443"


@pytest.mark.parametrize("value", [
    "ftp://h", "http://", "http://h/?q=1", "http://h#f", "http://h/?", "http://u:p@h", "http://h:99999",
    "http://h\n", "http://h with space", "localhost:6333", "http://h:abc"])
def test_a_bad_qdrant_url_is_refused_naming_the_variable_and_not_the_value(value):
    with pytest.raises(ValueError) as raised:
        config.qdrant_url({"BROKER_QDRANT_URL": value})
    assert "BROKER_QDRANT_URL" in str(raised.value)
    assert value.strip() == "" or value not in str(raised.value)
    assert raised.value.__cause__ is None


def test_the_api_key_is_absent_by_default_and_letters_and_digits_when_set():
    assert config.qdrant_api_key({}) is None
    assert config.qdrant_api_key({"QDRANT_API_KEY": "abc123XYZ"}) == "abc123XYZ"


@pytest.mark.parametrize("value", ["has space", "punct!", "new\nline", "a-b", "k" * 129])
def test_a_bad_api_key_is_refused_naming_the_variable_and_not_the_value(value):
    with pytest.raises(ValueError) as raised:
        config.qdrant_api_key({"QDRANT_API_KEY": value})
    assert "QDRANT_API_KEY" in str(raised.value)
    assert value not in str(raised.value)


def test_the_model_directory_defaults_and_follows_the_variable():
    assert config.model_dir({}) == Path("/opt/models")
    assert config.model_dir({"BROKER_EMBED_MODEL_DIR": "/m"}) == Path("/m")


def test_the_pinned_model_constants():
    assert config.DENSE_MODEL == "BAAI/bge-small-en-v1.5"
    assert config.DENSE_SOURCE == "Qdrant/bge-small-en-v1.5-onnx-Q"
    assert config.DENSE_REVISION == "aa8f8b060edb00e03bfdd08813a2949946c8ba55"
    assert config.DENSE_DIM == 384 and config.DENSE_SUBDIR == "bge"
    assert config.SPARSE_MODEL == "Qdrant/bm25"
    assert config.SPARSE_REVISION == "22b8d2af71a76161e18dd432d2cee0eefa66e412"
    assert config.SPARSE_SUBDIR == "bm25"
    assert config.QDRANT_TIMEOUT_S == 2
    assert config.DEFAULT_QDRANT_URL == "http://localhost:6333"
    assert config.DEFAULT_MODEL_DIR == "/opt/models"
    assert (config.ENV_KNOWLEDGE_PATH, config.ENV_QDRANT_URL, config.ENV_QDRANT_API_KEY,
            config.ENV_MODEL_DIR) == ("BROKER_KNOWLEDGE_PATH", "BROKER_QDRANT_URL", "QDRANT_API_KEY",
                                      "BROKER_EMBED_MODEL_DIR")


def test_the_readers_default_to_the_process_environment(monkeypatch):
    monkeypatch.setenv("BROKER_QDRANT_URL", "http://from-env:6333")
    monkeypatch.setenv("QDRANT_API_KEY", "envkey1")
    monkeypatch.setenv("BROKER_KNOWLEDGE_PATH", "/from-env")
    monkeypatch.setenv("BROKER_EMBED_MODEL_DIR", "/models-env")
    assert config.qdrant_url() == "http://from-env:6333"
    assert config.qdrant_api_key() == "envkey1"
    assert config.knowledge_root() == Path("/from-env")
    assert config.model_dir() == Path("/models-env")


def test_the_module_imports_only_the_standard_library():
    tree = ast.parse(Path(config.__file__).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
    assert roots <= set(sys.stdlib_module_names)
