# config.py
from __future__ import annotations
from dataclasses import dataclass, field
import logging
import os
import sys
from typing import Optional
from dotenv import load_dotenv, find_dotenv

#load_dotenv(dotenv_path=".env")  # load .env if existsxc
load_dotenv(find_dotenv())

# set offline mode for Hugging Face Hub and Transformers to avoid unexpected downloads in restricted environments
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

@dataclass
class ChunkingSettings:
    target_tokens: int = int(os.getenv("TARGET_TOKENS", "480"))
    window_chars: int = int(os.getenv("WINDOW_CHARS", "15000"))
    window_overlap: int = int(os.getenv("WINDOW_OVERLAP", "0"))
    inter_chunk_overlap: int = int(os.getenv("INTER_CHUNK_OVERLAP", "220"))

    infer_table_capabilities: bool = _env_bool("INFER_TABLE_CAPABILITIES", True)
    summarize_tables: bool = _env_bool("SUMMARIZE_TABLES", False)
    capabilities_model: Optional[str] = os.getenv("CAPABILITIES_MODEL", "gemma-4-26B-A4B-it")


@dataclass
class ModelSettings:
    # segmentation / structure extraction
    seg_model: str = os.getenv("SEG_MODEL", "gemma-4-26B-A4B-it")
    litellm_base_url: str = os.getenv("LITELLM_BASE_URL", "http://localhost:4000")
    # deprecated alias: legacy code reads ollama_base_url; routes to the same
    # OpenAI-compatible LiteLLM endpoint
    ollama_base_url: str = os.getenv("LITELLM_BASE_URL", "http://localhost:4000")
    embed_model: str = os.getenv("EMBED_MODEL", "embeddinggemma-300m")
    # EmbeddingGemma task prompts（模型卡要求；若 serving 端已自動加前綴，設成空字串 "" 停用）
    embed_query_prefix: str = os.getenv("EMBED_QUERY_PREFIX", "task: search result | query: ")
    embed_doc_prefix: str = os.getenv("EMBED_DOC_PREFIX", "title: none | text: ")
    chat_model: str = os.getenv("CHAT_MODEL", "gemma-4-26B-A4B-it")

    # vision
    vision_device: str = os.getenv("VISION_DEVICE", "cuda")
    clip_model: str = os.getenv("CLIP_MODEL", "openai/clip-vit-large-patch14")
    blip_model: str = os.getenv("BLIP_MODEL", "Salesforce/blip-image-captioning-base")

    # timeout
    embed_timeout: int = int(os.getenv("EMBED_TIMEOUT", "120"))

    # reranker
    # rerank 服務已於 2026-07 自外部 LiteLLM 移除；留空時 rerank 呼叫失敗會 fallback 原始排序
    rerank_model: str = os.getenv("RERANK_MODEL", "")
    
    # hf offline setting
    #hf_offline: bool = os.getenv("HF_HUB_OFFLINE", "1")
    #hf_offline: bool = _env_bool("HF_HUB_OFFLINE", True)
    #os.environ["HF_HUB_OFFLINE"] = "1"
    #transformer_offline: bool = os.getenv("TRANSFORMERS_OFFLINE", "1")


@dataclass
class DBSettings:
    #pg_dsn: str = os.getenv("PG_DSN", "dbname=block_FIRDI user=ai-x password=86891972 host=localhost port=5435")
    pg_dsn: str = os.getenv("PG_DSN", "dbname=acl_FIRDI user=ai-x password=86891972 host=localhost port=5435")
    tenant_id: str = os.getenv("DOCBLOCK_TENANT_ID", os.getenv("TENANT_ID", "firdi"))


@dataclass
class OutlineSettings:
    outline_url: str = os.getenv("OUTLINE_URL", "")
    api_token: str = os.getenv("OUTLINE_API_TOKEN", "")


@dataclass
class ToolSettings:
    # HTTP client timeout while waiting for the marker/pdf-to-md LiteLLM route
    marker_timeout: int = int(os.getenv("MARKER_TIMEOUT", "1800"))

    # LiteLLM proxy — used by ingest-worker to reach the marker/pdf-to-md route (external)
    litellm_proxy_url: str = os.getenv("LITELLM_PROXY_URL", "http://localhost:4000")
    litellm_api_key: str = os.getenv("LITELLM_API_KEY", "sk-litellm-internal")


## log setting
#LOG_LEVELS = ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]
#
#def parse_level(level_str: str, default="INFO"):
#    level_str = (level_str or default).upper()
#    return getattr(logging, level_str, logging.INFO)

@dataclass
class LogSettings:
#    global_level: int = parse_level(os.getenv("GLOBAL_LOG_LEVEL", "INFO"))
#    log_file: str = os.getenv("LOG_FILE", "app.log")
#    enable_console: bool = _env_bool("LOG_CONSOLE", True)
    logs_dir: str = os.getenv("LOG_DIR", "logs")  # base dir for all logs
    pipeline_error_log: str = os.getenv("PIPELINE_ERROR_LOG", "pipeline_error.log")
    marker_log: str = os.getenv("MARKER_LOG", "marker.log")
    build_blocks_log: str = os.getenv("BUILD_BLOCKS_LOG", "build_blocks.log")
    ingest_log: str = os.getenv("INGEST_LOG", "ingest.log")
    ingest_sum_log: str = os.getenv("INGEST_SUM_LOG", "ingest_sum.log")
    search_log: str = os.getenv("SEARCH_LOG", "search.log")
    

# main app settings
@dataclass
class AppSettings:
    pipeline_version: str = os.getenv("PIPELINE_VERSION", "v0.1")
    schema_version: str = os.getenv("SCHEMA_VERSION", "1.0")

    skip_embed_errors: bool = _env_bool("SKIP_EMBED_ERRORS", True)
    #skip_embed_errors: bool = os.getenv("SKIP_EMBED_ERRORS", "1").strip().lower() in ("1", "true", "yes", "y", "on")

    models: ModelSettings = field(default_factory=ModelSettings)
    chunking: ChunkingSettings = field(default_factory=ChunkingSettings)
    db: DBSettings = field(default_factory=DBSettings)
    tools: ToolSettings = field(default_factory=ToolSettings)
    logs: LogSettings = field(default_factory=LogSettings)
    outline: OutlineSettings = field(default_factory=OutlineSettings)

settings = AppSettings()

#print(f"Loaded settings: {settings}")
#print(f"logs_dir: {settings.logs.logs_dir}, marker_log: {settings.logs.#marker_log}, build_blocks_log: {settings.logs.build_blocks_log}")