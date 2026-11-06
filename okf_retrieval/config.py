"""The knowledge settings, read from the environment, and the pinned embedding-model constants.

Standard library only. Each reader takes a mapping (default: the process environment) so a test passes
its own. An error names the variable and never the value, so a bad secret is not echoed.
"""
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

ENV_KNOWLEDGE_PATH = "BROKER_KNOWLEDGE_PATH"
ENV_QDRANT_URL = "BROKER_QDRANT_URL"
ENV_QDRANT_API_KEY = "QDRANT_API_KEY"
ENV_MODEL_DIR = "BROKER_EMBED_MODEL_DIR"

DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_MODEL_DIR = "/opt/models"
QDRANT_TIMEOUT_S = 2

# The two embedding models, pinned to the Hugging Face commit the image build downloads and the embedder
# loads, so one source names both. The dense model is fastembed's quantised copy of DENSE_MODEL.
DENSE_MODEL = "BAAI/bge-small-en-v1.5"
DENSE_SOURCE = "Qdrant/bge-small-en-v1.5-onnx-Q"
DENSE_REVISION = "aa8f8b060edb00e03bfdd08813a2949946c8ba55"
DENSE_DIM = 384
DENSE_SUBDIR = "bge"
SPARSE_MODEL = "Qdrant/bm25"
SPARSE_REVISION = "22b8d2af71a76161e18dd432d2cee0eefa66e412"
SPARSE_SUBDIR = "bm25"

_PRINTABLE = re.compile(r"[!-~]+")
_KEY = re.compile(r"[A-Za-z0-9]{1,128}")
_URL_SCHEMES = ("http", "https")


def _path(environ, name: str, default: str) -> Path:
    raw = environ.get(name) or default
    if "\x00" in raw:
        raise ValueError(f"{name} must be a directory path")
    return Path(raw)


def knowledge_root(environ=os.environ) -> Path:
    """The knowledge directory: BROKER_KNOWLEDGE_PATH, else the `knowledge` directory beside the package."""
    return _path(environ, ENV_KNOWLEDGE_PATH, str(Path(__file__).resolve().parent.parent / "knowledge"))


def qdrant_url(environ=os.environ) -> str:
    """The Qdrant URL: an http or https URL with a host and no credentials, query or fragment."""
    raw = environ.get(ENV_QDRANT_URL) or DEFAULT_QDRANT_URL
    message = f"{ENV_QDRANT_URL} must be an http or https URL with a host and no query or fragment"
    if not _PRINTABLE.fullmatch(raw):
        raise ValueError(message)
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        raise ValueError(message) from None
    if (parts.scheme not in _URL_SCHEMES or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment
            or "?" in raw or "#" in raw):
        raise ValueError(message)
    return raw


def qdrant_api_key(environ=os.environ) -> str | None:
    """The Qdrant key, or None when unset; letters and digits only."""
    raw = environ.get(ENV_QDRANT_API_KEY)
    if not raw:
        return None
    if not _KEY.fullmatch(raw):
        raise ValueError(f"{ENV_QDRANT_API_KEY} must be letters and digits only")
    return raw


def model_dir(environ=os.environ) -> Path:
    """The directory holding the two baked-in models (`bge` and `bm25` beneath it)."""
    return _path(environ, ENV_MODEL_DIR, DEFAULT_MODEL_DIR)
