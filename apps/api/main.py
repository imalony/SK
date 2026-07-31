import asyncio
import base64
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import edge_tts
import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT_DIR = Path(__file__).resolve().parents[2]


def load_local_environment() -> None:
    env_path = ROOT_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if name:
            os.environ.setdefault(name, value)


load_local_environment()


DATA_DIR = ROOT_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
MEDIA_DIR = DATA_DIR / "media"
WORK_DIR = DATA_DIR / "work"
AD_MEDIA_DIR = MEDIA_DIR / "ad"
AD_WORK_DIR = WORK_DIR / "ad"
AD_PROMPT_DIR = Path(__file__).resolve().parent / "prompts" / "ad"
DATABASE_PATH = DATA_DIR / "sk2.db"
PROVIDERS_PATH = Path(
    os.getenv("SK2_PROVIDERS_PATH", str(ROOT_DIR / "providers.json"))
)

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)
AD_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
AD_WORK_DIR.mkdir(parents=True, exist_ok=True)
WORKFLOW_DIR = ROOT_DIR / "workflows"

OLLAMA_URL = os.getenv("SK2_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("SK2_OLLAMA_MODEL", "qwen3:4b-instruct")

job_lock = asyncio.Lock()
model_lock = asyncio.Lock()
stop_lock = asyncio.Lock()
event_clients: set[WebSocket] = set()
generation_tasks: dict[str, asyncio.Task[None]] = {}
edit_parser_tasks: set[asyncio.Task[Any]] = set()
ad_project_tasks: dict[str, asyncio.Task[None]] = {}
model_activity = "idle"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VideoProvider:
    id: str
    label: str
    kind: Literal[
        "comfyui",
        "http-api",
        "agnes-video",
        "wanx-video",
    ]
    model: str
    enabled: bool
    capabilities: frozenset[str]
    settings: dict[str, Any]


def load_providers() -> tuple[str, dict[str, VideoProvider]]:
    try:
        payload = json.loads(PROVIDERS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError(f"Provider configuration was not found: {PROVIDERS_PATH}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Provider configuration is invalid JSON: {error}") from error

    providers: dict[str, VideoProvider] = {}
    for item in payload.get("providers", []):
        provider = VideoProvider(
            id=str(item["id"]),
            label=str(item["label"]),
            kind=item["kind"],
            model=str(item["model"]),
            enabled=bool(item.get("enabled", False)),
            capabilities=frozenset(str(value) for value in item.get("capabilities", [])),
            settings=dict(item.get("settings", {})),
        )
        if provider.id in providers:
            raise RuntimeError(f"Duplicate provider id: {provider.id}")
        providers[provider.id] = provider

    default_provider_id = str(payload.get("default_provider", ""))
    if default_provider_id not in providers:
        raise RuntimeError("default_provider must reference a configured provider")
    return default_provider_id, providers


DEFAULT_PROVIDER_ID, PROVIDERS = load_providers()


def get_provider(provider_id: str, capability: str | None = None) -> VideoProvider:
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        raise HTTPException(status_code=422, detail=f"Unknown provider: {provider_id}")
    if not provider.enabled:
        raise HTTPException(status_code=422, detail=f"Provider is disabled: {provider_id}")
    if capability and capability not in provider.capabilities:
        raise HTTPException(
            status_code=422,
            detail=f"Provider {provider_id} does not support {capability}",
        )
    return provider


def provider_supports_continuation(provider: VideoProvider) -> bool:
    return (
        "video_edit" in provider.capabilities
        or "video_continue" in provider.capabilities
    )


def provider_resolution(provider: VideoProvider, requested: str | None) -> str | None:
    options = [str(value) for value in provider.settings.get("resolution_options", [])]
    value = (requested or str(provider.settings.get("default_resolution", ""))).strip()
    if options and value not in options:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported resolution for {provider.label}: {value or 'not selected'}",
        )
    return value or None


def provider_for_generation(generation: dict[str, Any]) -> VideoProvider:
    provider_id = generation["config"].get("provider_id", DEFAULT_PROVIDER_ID)
    if generation["mode"] == "continue":
        provider = get_provider(provider_id)
        if not provider_supports_continuation(provider):
            raise HTTPException(
                status_code=422,
                detail=f"Provider {provider_id} does not support video continuation",
            )
        return provider
    capability = {
        "text": "text_to_video",
        "image": "image_to_video",
        "edit": "video_edit",
    }[generation["mode"]]
    return get_provider(provider_id, capability)


def local_setting(provider: VideoProvider, key: str) -> str:
    if provider.kind != "comfyui":
        raise RuntimeError(f"Provider {provider.id} is not a ComfyUI provider")
    value = provider.settings.get(key)
    if not value:
        raise RuntimeError(f"Provider {provider.id} is missing setting: {key}")
    return str(value)


def provider_api_key(provider: VideoProvider) -> str:
    environment_name = str(provider.settings.get("api_key_env", "")).strip()
    if not environment_name:
        raise RuntimeError(f"Provider {provider.id} is missing setting: api_key_env")
    api_key = os.getenv(environment_name, "").strip()
    if not api_key:
        raise RuntimeError(
            f"Provider {provider.id} requires environment variable {environment_name}"
        )
    return api_key


def requires_payment_confirmation(provider: VideoProvider) -> bool:
    return bool(provider.settings.get("requires_payment_confirmation", False))


def ensure_payment_confirmation(
    provider: VideoProvider, payment_confirmed: bool
) -> None:
    if requires_payment_confirmation(provider) and not payment_confirmed:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{provider.label} is a paid cloud video provider. "
                "Confirm the paid generation again before submitting the task."
            ),
        )


def public_provider(provider: VideoProvider) -> dict[str, Any]:
    return {
        "id": provider.id,
        "label": provider.label,
        "kind": provider.kind,
        "model": provider.model,
        "enabled": provider.enabled,
        "capabilities": sorted(provider.capabilities),
        "requires_payment_confirmation": bool(
            provider.settings.get("requires_payment_confirmation", False)
        ),
        "resolution_options": [
            str(value)
            for value in provider.settings.get("resolution_options", [])
        ],
        "default_resolution": str(provider.settings.get("default_resolution", "")),
    }


class CreateGenerationRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=4000)
    negative_prompt: str = "blurry, distorted, flickering, text, watermark"
    reference_asset_id: str | None = None
    provider_id: str = DEFAULT_PROVIDER_ID
    width: int = Field(default=512, ge=128, le=1024, multiple_of=16)
    height: int = Field(default=288, ge=128, le=1024, multiple_of=16)
    length: int = Field(default=49, ge=5, le=81)
    fps: int = Field(default=8, ge=4, le=24)
    seed: int | None = Field(default=None, ge=0)
    resolution: str | None = Field(default=None, max_length=20)
    payment_confirmed: bool = False


class EditGenerationRequest(BaseModel):
    instruction: str = Field(min_length=3, max_length=4000)
    negative_prompt: str = "blurry, distorted, flickering, text, watermark"
    provider_id: str | None = None
    seed: int | None = Field(default=None, ge=0)
    payment_confirmed: bool = False


class ContinueGenerationRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=4000)
    negative_prompt: str = "blurry, distorted, flickering, text, watermark"
    provider_id: str | None = None
    length: int = Field(default=49, ge=5, le=81)
    fps: int = Field(default=8, ge=4, le=24)
    tail_frames: int = Field(default=8, ge=2, le=24)
    seed: int | None = Field(default=None, ge=0)
    payment_confirmed: bool = False


class CreateAdProjectRequest(BaseModel):
    brief: str = Field(min_length=3, max_length=2000)
    target_duration_seconds: int = Field(default=15, ge=5, le=120)
    voice_enabled: bool = True
    subtitle_enabled: bool = True
    bgm_enabled: bool = True
    tts_provider: str = "edge-tts"
    voice_id: str = "zh-CN-XiaoxiaoNeural"
    video_provider_id: str = DEFAULT_PROVIDER_ID
    video_resolution: str | None = Field(default=None, max_length=20)


class AdPlanFeedbackRequest(BaseModel):
    feedback: str = Field(min_length=2, max_length=2000)


class AdPlanFromSegmentRequest(BaseModel):
    from_segment: int = Field(ge=2, le=24)
    feedback: str = Field(min_length=2, max_length=2000)


class AdPlanApprovalRequest(BaseModel):
    version: int = Field(ge=1)
    payment_confirmed: bool = False


class AdGenerationRequest(BaseModel):
    payment_confirmed: bool = False


class AdPlanPromptUpdateRequest(BaseModel):
    version: int = Field(ge=1)
    prompts: list[str] = Field(min_length=1, max_length=24)


class AdSegmentPromptRewriteRequest(BaseModel):
    version: int = Field(ge=1)
    segment_index: int = Field(ge=0, le=23)
    current_prompt: str = Field(min_length=5, max_length=3000)
    instruction: str = Field(default="", max_length=1000)


class AdPlanPromptRewriteRequest(BaseModel):
    version: int = Field(ge=1)
    instruction: str = Field(default="", max_length=1000)


class AdVoicePreviewRequest(BaseModel):
    voice_id: str = Field(min_length=3, max_length=128)


class AdFinalEditRequest(BaseModel):
    voiceover_script: str | None = Field(default=None, max_length=2000)
    post_caption: str | None = Field(default=None, max_length=1000)
    hashtags: list[str] | None = Field(default=None, max_length=10)
    voice_enabled: bool | None = None
    subtitle_enabled: bool | None = None
    bgm_enabled: bool | None = None
    bgm_id: str | None = Field(default=None, max_length=200)
    voice_id: str | None = Field(default=None, max_length=128)


class AdFinalCopyRequest(BaseModel):
    instruction: str = Field(default="", max_length=1000)


class UpdateAdModelSettingsRequest(BaseModel):
    video_provider_id: str = DEFAULT_PROVIDER_ID
    llm_base_url: str = Field(
        default="https://fjbigmodel.fjdac.cn/v1", min_length=8, max_length=500
    )
    llm_model: str = Field(default="gpt-5.5", min_length=1, max_length=200)
    llm_api_key: str | None = Field(default=None, max_length=1000)


def now() -> int:
    return int(time.time())


def database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    config_json = result.pop("config_json", None)
    if config_json:
        result["config"] = json.loads(config_json)
    edit_spec_json = result.pop("edit_spec_json", None)
    if edit_spec_json:
        result["edit_spec"] = json.loads(edit_spec_json)
    if result.get("output_path"):
        result["output_url"] = f"/media/{result['output_path']}"
    return result


def get_app_setting(name: str, default: str = "") -> str:
    with database() as connection:
        row = connection.execute(
            "SELECT value FROM app_settings WHERE name = ?", (name,)
        ).fetchone()
    return str(row["value"]) if row else default


def set_app_setting(name: str, value: str) -> None:
    with database() as connection:
        connection.execute(
            """
            INSERT INTO app_settings (name, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (name, value, now()),
        )


def ad_llm_settings() -> dict[str, str]:
    return {
        "base_url": get_app_setting(
            "ad_llm_base_url",
            os.getenv("SK2_AD_LLM_BASE_URL", "https://fjbigmodel.fjdac.cn/v1"),
        ).rstrip("/"),
        "model": get_app_setting(
            "ad_llm_model", os.getenv("SK2_AD_LLM_MODEL", "gpt-5.5")
        ),
        "api_key": get_app_setting("ad_llm_api_key", "")
        or os.getenv("OPENAI_API_KEY2", "").strip(),
        "video_provider_id": get_app_setting(
            "ad_default_video_provider_id", DEFAULT_PROVIDER_ID
        ),
    }


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    AD_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    AD_WORK_DIR.mkdir(parents=True, exist_ok=True)
    for provider in PROVIDERS.values():
        if provider.kind == "comfyui" and provider.enabled:
            Path(local_setting(provider, "input_dir")).mkdir(parents=True, exist_ok=True)

    with database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS assets (
              id TEXT PRIMARY KEY,
              filename TEXT NOT NULL,
              stored_path TEXT NOT NULL,
              mime_type TEXT NOT NULL,
              created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_settings (
              name TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS generations (
              id TEXT PRIMARY KEY,
              parent_generation_id TEXT,
              mode TEXT NOT NULL,
              prompt TEXT NOT NULL,
              negative_prompt TEXT NOT NULL,
              reference_asset_id TEXT,
              edit_spec_json TEXT,
              config_json TEXT NOT NULL,
              status TEXT NOT NULL,
              progress REAL NOT NULL DEFAULT 0,
              error_message TEXT,
              output_path TEXT,
              comfy_prompt_id TEXT,
              created_at INTEGER NOT NULL,
              started_at INTEGER,
              completed_at INTEGER,
              FOREIGN KEY(parent_generation_id) REFERENCES generations(id),
              FOREIGN KEY(reference_asset_id) REFERENCES assets(id)
            );

            CREATE TABLE IF NOT EXISTS ad_projects (
              id TEXT PRIMARY KEY,
              brief TEXT NOT NULL,
              target_duration_seconds INTEGER NOT NULL,
              voice_enabled INTEGER NOT NULL,
              subtitle_enabled INTEGER NOT NULL,
              bgm_enabled INTEGER NOT NULL,
              bgm_id TEXT NOT NULL DEFAULT 'default/ambient',
              video_provider_id TEXT NOT NULL DEFAULT 'local-wan-vace',
              video_resolution TEXT,
              reference_video_path TEXT,
              reference_analysis_json TEXT,
              tts_provider TEXT NOT NULL,
              voice_id TEXT NOT NULL,
              status TEXT NOT NULL,
              approved_plan_version INTEGER,
              plan_approved_at INTEGER,
              master_output_path TEXT,
              final_output_path TEXT,
              error_message TEXT,
              created_at INTEGER NOT NULL,
              completed_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS ad_assets (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              asset_id TEXT NOT NULL,
              filename TEXT NOT NULL,
              stored_path TEXT NOT NULL,
              sort_order INTEGER NOT NULL,
              created_at INTEGER NOT NULL,
              FOREIGN KEY(project_id) REFERENCES ad_projects(id),
              FOREIGN KEY(asset_id) REFERENCES assets(id)
            );

            CREATE TABLE IF NOT EXISTS ad_plans (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              version INTEGER NOT NULL,
              plan_json TEXT NOT NULL,
              voiceover_script TEXT NOT NULL,
              post_caption TEXT NOT NULL,
              hashtags_json TEXT NOT NULL,
              prompt_bundle_version TEXT NOT NULL,
              parent_plan_id TEXT,
              replan_from_sequence INTEGER,
              revision_note TEXT,
              created_at INTEGER NOT NULL,
              approved_at INTEGER,
              UNIQUE(project_id, version),
              FOREIGN KEY(project_id) REFERENCES ad_projects(id)
            );

            CREATE TABLE IF NOT EXISTS ad_segments (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              plan_id TEXT NOT NULL,
              sequence_number INTEGER NOT NULL,
              asset_id TEXT,
              target_duration_seconds REAL NOT NULL,
              parent_segment_id TEXT,
              generation_id TEXT,
              prompt TEXT NOT NULL,
              review_json TEXT,
              retry_count INTEGER NOT NULL DEFAULT 0,
              output_path TEXT,
              status TEXT NOT NULL,
              FOREIGN KEY(project_id) REFERENCES ad_projects(id),
              FOREIGN KEY(plan_id) REFERENCES ad_plans(id)
            );

            CREATE TABLE IF NOT EXISTS ad_runs (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              stage TEXT NOT NULL,
              status TEXT NOT NULL,
              progress REAL NOT NULL DEFAULT 0,
              details_json TEXT,
              error_message TEXT,
              started_at INTEGER NOT NULL,
              completed_at INTEGER,
              FOREIGN KEY(project_id) REFERENCES ad_projects(id)
            );

            CREATE TABLE IF NOT EXISTS ad_recovery_attempts (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              plan_id TEXT NOT NULL,
              resume_from_sequence INTEGER NOT NULL,
              status TEXT NOT NULL,
              error_message TEXT,
              created_at INTEGER NOT NULL,
              completed_at INTEGER,
              FOREIGN KEY(project_id) REFERENCES ad_projects(id),
              FOREIGN KEY(plan_id) REFERENCES ad_plans(id)
            );

            CREATE TABLE IF NOT EXISTS ad_final_versions (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              version INTEGER NOT NULL,
              output_path TEXT NOT NULL,
              voiceover_script TEXT NOT NULL,
              post_caption TEXT NOT NULL,
              hashtags_json TEXT NOT NULL,
              voice_enabled INTEGER NOT NULL,
              subtitle_enabled INTEGER NOT NULL,
              bgm_enabled INTEGER NOT NULL,
              bgm_id TEXT NOT NULL,
              voice_id TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              UNIQUE(project_id, version),
              FOREIGN KEY(project_id) REFERENCES ad_projects(id)
            );
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(ad_projects)").fetchall()
        }
        if "bgm_id" not in columns:
            connection.execute(
                "ALTER TABLE ad_projects ADD COLUMN bgm_id TEXT NOT NULL DEFAULT 'default/ambient'"
            )
        if "reference_video_path" not in columns:
            connection.execute(
                "ALTER TABLE ad_projects ADD COLUMN reference_video_path TEXT"
            )
        if "reference_analysis_json" not in columns:
            connection.execute(
                "ALTER TABLE ad_projects ADD COLUMN reference_analysis_json TEXT"
            )
        if "video_provider_id" not in columns:
            connection.execute(
                f"ALTER TABLE ad_projects ADD COLUMN video_provider_id TEXT NOT NULL DEFAULT '{DEFAULT_PROVIDER_ID}'"
            )
        if "video_resolution" not in columns:
            connection.execute("ALTER TABLE ad_projects ADD COLUMN video_resolution TEXT")
        if "master_output_path" not in columns:
            connection.execute(
                "ALTER TABLE ad_projects ADD COLUMN master_output_path TEXT"
            )
        ad_plan_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(ad_plans)").fetchall()
        }
        if "parent_plan_id" not in ad_plan_columns:
            connection.execute("ALTER TABLE ad_plans ADD COLUMN parent_plan_id TEXT")
        if "replan_from_sequence" not in ad_plan_columns:
            connection.execute(
                "ALTER TABLE ad_plans ADD COLUMN replan_from_sequence INTEGER"
            )
        if "revision_note" not in ad_plan_columns:
            connection.execute("ALTER TABLE ad_plans ADD COLUMN revision_note TEXT")
        ad_segment_columns = {
            row["name"]: row
            for row in connection.execute("PRAGMA table_info(ad_segments)").fetchall()
        }
        asset_id_column = ad_segment_columns.get("asset_id")
        if asset_id_column and asset_id_column["notnull"]:
            connection.executescript(
                """
                CREATE TABLE ad_segments_migrated (
                  id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL,
                  plan_id TEXT NOT NULL,
                  sequence_number INTEGER NOT NULL,
                  asset_id TEXT,
                  target_duration_seconds REAL NOT NULL,
                  parent_segment_id TEXT,
                  generation_id TEXT,
                  prompt TEXT NOT NULL,
                  review_json TEXT,
                  retry_count INTEGER NOT NULL DEFAULT 0,
                  output_path TEXT,
                  status TEXT NOT NULL,
                  FOREIGN KEY(project_id) REFERENCES ad_projects(id),
                  FOREIGN KEY(plan_id) REFERENCES ad_plans(id)
                );
                INSERT INTO ad_segments_migrated (
                  id, project_id, plan_id, sequence_number, asset_id,
                  target_duration_seconds, parent_segment_id, generation_id, prompt,
                  review_json, retry_count, output_path, status
                )
                SELECT
                  id, project_id, plan_id, sequence_number, asset_id,
                  target_duration_seconds, parent_segment_id, generation_id, prompt,
                  review_json, retry_count, output_path, status
                FROM ad_segments;
                DROP TABLE ad_segments;
                ALTER TABLE ad_segments_migrated RENAME TO ad_segments;
                """
            )


async def broadcast(event: dict[str, Any]) -> None:
    stale_clients: list[WebSocket] = []
    for client in event_clients:
        try:
            await client.send_json(event)
        except Exception:
            stale_clients.append(client)
    for client in stale_clients:
        event_clients.discard(client)


def get_generation(generation_id: str) -> dict[str, Any]:
    with database() as connection:
        generation = row_to_dict(
            connection.execute(
                "SELECT * FROM generations WHERE id = ?", (generation_id,)
            ).fetchone()
        )
    if generation is None:
        raise HTTPException(status_code=404, detail="Generation not found")
    return generation


AD_VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓", "label": "女声 / 亲和自然"},
    {"id": "zh-CN-XiaoyiNeural", "name": "晓伊", "label": "女声 / 活力促销"},
    {"id": "zh-CN-YunxiNeural", "name": "云希", "label": "男声 / 年轻清晰"},
    {"id": "zh-CN-YunjianNeural", "name": "云健", "label": "男声 / 稳重专业"},
]


def load_ad_prompt(name: str) -> str:
    path = AD_PROMPT_DIR / name
    if not path.is_file():
        raise RuntimeError(f"Advertising prompt resource is missing: {path.name}")
    return path.read_text(encoding="utf-8")


def parse_json_response(value: str) -> dict[str, Any]:
    text = value.strip()
    if "```" in text:
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        if match:
            text = match.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Model response is not a JSON object")
    return parsed


def response_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    parts: list[str] = []
    for output in payload.get("output", []):
        for content in output.get("content", []) if isinstance(output, dict) else []:
            if isinstance(content, dict):
                value = content.get("text") or content.get("output_text")
                if isinstance(value, str):
                    parts.append(value)
    if parts:
        return "\n".join(parts)
    raise ValueError("Responses API returned no text output")


async def request_ad_llm(
    *,
    system_prompt: str,
    user_prompt: str,
    image_paths: list[Path] | None = None,
) -> dict[str, Any]:
    settings = ad_llm_settings()
    api_key = settings["api_key"]
    if not api_key:
        raise RuntimeError(
            "Advertising planning requires an API key. Configure it in Model Settings "
            "or set OPENAI_API_KEY2."
        )
    base_url = settings["base_url"]
    model = settings["model"]
    content: list[dict[str, Any]] = [{"type": "input_text", "text": user_prompt}]
    for path in (image_paths or [])[:12]:
        if path.is_file():
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            suffix = path.suffix.lower().lstrip(".") or "jpeg"
            mime = "jpeg" if suffix == "jpg" else suffix
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:image/{mime};base64,{encoded}",
                }
            )
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "temperature": 0.3,
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{base_url}/responses",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
        return parse_json_response(response_text(response.json()))
    except (httpx.HTTPError, ValueError, KeyError) as error:
        raise RuntimeError(f"Advertising language model is unavailable: {error}") from error


def ad_segment_count(duration_seconds: int) -> int:
    """Keep local video jobs in short, reliable clips."""
    return max(1, min(24, (duration_seconds + 4) // 5))


def ad_voiceover_duration_guidance(duration_seconds: int) -> dict[str, Any]:
    # Chinese commercial voiceover needs deliberate pauses for visual beats.
    target = max(12, round(duration_seconds * 3.4))
    return {
        "duration_seconds": duration_seconds,
        "target_chinese_characters": target,
        "recommended_chinese_character_range": [
            max(8, round(duration_seconds * 2.8)),
            max(12, round(duration_seconds * 3.8)),
        ],
        "pacing": (
            "Use a natural Chinese advertising cadence with pauses. Count spoken "
            "Chinese characters only; punctuation is not part of the budget."
        ),
    }


def ad_asset_image_paths(
    project: dict[str, Any], asset_indexes: list[int] | None = None
) -> list[Path]:
    assets = project.get("assets", [])
    indexes = asset_indexes if asset_indexes is not None else list(range(len(assets)))
    paths: list[Path] = []
    for index in indexes:
        if index < 0 or index >= len(assets):
            continue
        path = Path(assets[index]["stored_path"])
        if path.is_file() and path not in paths:
            paths.append(path)
    return paths


async def ad_reference_video_frame_paths(project: dict[str, Any]) -> list[Path]:
    relative_path = project.get("reference_video_path")
    if not relative_path:
        return []
    source = MEDIA_DIR / relative_path
    if not source.is_file():
        return []
    frame_dir = AD_WORK_DIR / project["id"] / "reference-analysis"
    frames = sorted(frame_dir.glob("reference-*.jpg"))
    if frames:
        return frames[:4]
    try:
        return await asyncio.to_thread(extract_reference_video_frames, source, frame_dir)
    except RuntimeError as error:
        logging.warning("Could not load reference video frames for ad planning: %s", error)
        return []


def split_ad_duration(duration_seconds: int, segment_count: int) -> list[int]:
    base, remainder = divmod(duration_seconds, segment_count)
    return [base + (1 if index < remainder else 0) for index in range(segment_count)]


def rebalance_ad_durations(durations: list[int], target_duration: int) -> list[int]:
    """Preserve the planner's pacing while making the total duration exact."""
    if not durations:
        return []
    source_total = sum(durations)
    if source_total <= 0:
        return split_ad_duration(target_duration, len(durations))

    balanced = [
        max(2, min(15, int(round(value * target_duration / source_total))))
        for value in durations
    ]
    difference = target_duration - sum(balanced)
    while difference:
        if difference > 0:
            candidates = [index for index, value in enumerate(balanced) if value < 15]
            if not candidates:
                break
            index = max(candidates, key=lambda item: (durations[item], -item))
            balanced[index] += 1
            difference -= 1
        else:
            candidates = [index for index, value in enumerate(balanced) if value > 2]
            if not candidates:
                break
            index = max(candidates, key=lambda item: (balanced[item], durations[item], -item))
            balanced[index] -= 1
            difference += 1
    return balanced


def fallback_ad_plan(project: dict[str, Any], asset_count: int) -> dict[str, Any]:
    duration = int(project["target_duration_seconds"])
    segment_count = ad_segment_count(duration)
    pacing_template = [
        3 if index == 0 else 6 if index < segment_count - 1 else 5
        for index in range(segment_count)
    ]
    segment_durations = rebalance_ad_durations(pacing_template, duration)
    segments = []
    for index in range(segment_count):
        segments.append(
            {
                "asset_index": index % asset_count if asset_count else -1,
                "duration_seconds": segment_durations[index],
                "purpose": "展示核心卖点" if index else "建立产品印象",
                "motion": "gentle product push-in with clean commercial lighting",
                "prompt": (
                    "竖屏商品广告视频，严格保留参考图中的商品外观与包装细节，镜头缓慢推进，高级自然光，干净背景，动作真实稳定，无文字，无水印。"
                    if asset_count
                    else "竖屏商品广告视频，高级商品主视觉镜头，镜头缓慢电影感运动，干净棚拍光线，真实稳定，无文字，无水印。"
                ),
            }
        )
    return {
        "title": "商品短视频广告",
        "strategy": "以素材中的商品细节建立第一印象，再依次展示卖点和行动引导。",
        "voiceover_script": "好看更好用，细节看得见。现在就来了解这款产品。",
        "post_caption": "把日常的好选择，分享给更多人。",
        "hashtags": ["#好物推荐", "#抖音广告", "#品质生活"],
        "segments": segments,
        "warning": "云端文案模型暂不可用，已使用基础方案。确认前可继续修改。",
    }


def normalize_ad_plan(plan: dict[str, Any], project: dict[str, Any], asset_count: int) -> dict[str, Any]:
    target = int(project["target_duration_seconds"])
    raw_segments = plan.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        return fallback_ad_plan(project, asset_count)
    normalized: list[dict[str, Any]] = []
    desired_segment_count = ad_segment_count(target)
    for index, item in enumerate(raw_segments[:desired_segment_count]):
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "asset_index": (
                    max(0, min(asset_count - 1, int(item.get("asset_index", index % asset_count))))
                    if asset_count
                    else -1
                ),
                "duration_seconds": max(2, min(15, int(round(float(item.get("duration_seconds", 4)))))),
                "purpose": str(item.get("purpose", "展示产品卖点"))[:300],
                "motion": str(item.get("motion", "gentle camera movement"))[:500],
                "prompt": str(item.get("prompt", ""))[:3000],
            }
        )
    if not normalized:
        return fallback_ad_plan(project, asset_count)

    # A local generation segment should remain short. If the planner returns too
    # few shots for the requested duration, use the deterministic fallback rather
    # than silently creating an impractically long final shot.
    minimum_segment_count = max(1, (target + 7) // 8)
    if len(normalized) < minimum_segment_count:
        return fallback_ad_plan(project, asset_count)

    planned_durations = [item["duration_seconds"] for item in normalized]
    for item, seconds in zip(normalized, rebalance_ad_durations(planned_durations, target)):
        item["duration_seconds"] = seconds
    plan["segments"] = normalized
    plan["title"] = str(plan.get("title", "商品短视频广告"))[:120]
    plan["strategy"] = str(plan.get("strategy", ""))[:2000]
    plan["voiceover_script"] = str(plan.get("voiceover_script", ""))[:2000]
    plan["post_caption"] = str(plan.get("post_caption", ""))[:1000]
    plan["hashtags"] = [str(value)[:80] for value in plan.get("hashtags", []) if str(value).strip()][:10]
    return plan


def ad_project_detail(project_id: str) -> dict[str, Any]:
    with database() as connection:
        project = connection.execute(
            "SELECT * FROM ad_projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise HTTPException(status_code=404, detail="Advertising project not found")
        assets = connection.execute(
            "SELECT * FROM ad_assets WHERE project_id = ? ORDER BY sort_order", (project_id,)
        ).fetchall()
        plans = connection.execute(
            "SELECT * FROM ad_plans WHERE project_id = ? ORDER BY version DESC", (project_id,)
        ).fetchall()
        segments = connection.execute(
            "SELECT * FROM ad_segments WHERE project_id = ? ORDER BY sequence_number", (project_id,)
        ).fetchall()
        runs = connection.execute(
            "SELECT * FROM ad_runs WHERE project_id = ? ORDER BY started_at DESC", (project_id,)
        ).fetchall()
        recovery_attempts = connection.execute(
            "SELECT * FROM ad_recovery_attempts WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
        final_versions = connection.execute(
            "SELECT * FROM ad_final_versions WHERE project_id = ? ORDER BY version DESC",
            (project_id,),
        ).fetchall()
    result = dict(project)
    result["voice_enabled"] = bool(result["voice_enabled"])
    result["subtitle_enabled"] = bool(result["subtitle_enabled"])
    result["bgm_enabled"] = bool(result["bgm_enabled"])
    if result.get("reference_video_path"):
        result["reference_video_url"] = f"/media/{result['reference_video_path']}"
    if result.get("reference_analysis_json"):
        result["reference_analysis"] = json.loads(result["reference_analysis_json"])
    if result.get("final_output_path"):
        result["output_url"] = f"/media/{result['final_output_path']}"
    if result.get("master_output_path"):
        result["master_output_url"] = f"/media/{result['master_output_path']}"
    result["assets"] = []
    for row in assets:
        asset = dict(row)
        stored_path = Path(asset["stored_path"])
        try:
            asset["url"] = f"/media/{stored_path.relative_to(MEDIA_DIR).as_posix()}"
        except ValueError:
            asset["url"] = None
        result["assets"].append(asset)
    result["plans"] = [
        {
            **dict(row),
            "plan": json.loads(row["plan_json"]),
            "hashtags": json.loads(row["hashtags_json"]),
        }
        for row in plans
    ]
    result["segments"] = [
        {**dict(row), "review": json.loads(row["review_json"]) if row["review_json"] else None}
        for row in segments
    ]
    generation_ids = [
        segment["generation_id"]
        for segment in result["segments"]
        if segment.get("generation_id")
    ]
    if generation_ids:
        placeholders = ",".join("?" for _ in generation_ids)
        with database() as connection:
            generation_rows = connection.execute(
                f"SELECT * FROM generations WHERE id IN ({placeholders})", generation_ids
            ).fetchall()
        generation_by_id = {
            row["id"]: row_to_dict(row)
            for row in generation_rows
        }
        for segment in result["segments"]:
            generation_id = segment.get("generation_id")
            if generation_id:
                segment["generation"] = generation_by_id.get(generation_id)
    result["runs"] = [
        {**dict(row), "details": json.loads(row["details_json"]) if row["details_json"] else None}
        for row in runs
    ]
    result["recovery_attempts"] = [dict(row) for row in recovery_attempts]
    result["final_versions"] = [
        {
            **dict(row),
            "output_url": f"/media/{row['output_path']}",
            "hashtags": json.loads(row["hashtags_json"]),
            "voice_enabled": bool(row["voice_enabled"]),
            "subtitle_enabled": bool(row["subtitle_enabled"]),
            "bgm_enabled": bool(row["bgm_enabled"]),
        }
        for row in final_versions
    ]
    return result


async def broadcast_ad_project(project_id: str) -> None:
    await broadcast({"type": "ad-project.updated", "project": ad_project_detail(project_id)})


async def set_ad_project_state(
    project_id: str, status: str, *, error_message: str | None = None, final_output_path: str | None = None
) -> None:
    assignments = ["status = ?"]
    values: list[Any] = [status]
    if error_message is not None:
        assignments.append("error_message = ?")
        values.append(error_message)
    if final_output_path is not None:
        assignments.append("final_output_path = ?")
        values.append(final_output_path)
    if status in {"completed", "failed", "cancelled"}:
        assignments.append("completed_at = ?")
        values.append(now())
    values.append(project_id)
    with database() as connection:
        connection.execute(f"UPDATE ad_projects SET {', '.join(assignments)} WHERE id = ?", values)
    await broadcast_ad_project(project_id)


async def add_ad_run(project_id: str, stage: str, progress: float, details: dict[str, Any] | None = None) -> str:
    run_id = uuid.uuid4().hex
    with database() as connection:
        connection.execute(
            "INSERT INTO ad_runs (id, project_id, stage, status, progress, details_json, started_at) VALUES (?, ?, ?, 'running', ?, ?, ?)",
            (run_id, project_id, stage, progress, json.dumps(details or {}, ensure_ascii=False), now()),
        )
    await broadcast_ad_project(project_id)
    return run_id


async def complete_ad_run(run_id: str, *, error: str | None = None) -> None:
    with database() as connection:
        connection.execute(
            "UPDATE ad_runs SET status = ?, error_message = ?, completed_at = ? WHERE id = ?",
            ("failed" if error else "succeeded", error, now(), run_id),
        )


async def set_generation_state(
    generation_id: str,
    status: str,
    *,
    progress: float | None = None,
    error_message: str | None = None,
    output_path: str | None = None,
    comfy_prompt_id: str | None = None,
) -> None:
    values: list[Any] = [status]
    assignments = ["status = ?"]
    if progress is not None:
        assignments.append("progress = ?")
        values.append(progress)
    if error_message is not None:
        assignments.append("error_message = ?")
        values.append(error_message)
    if output_path is not None:
        assignments.append("output_path = ?")
        values.append(output_path)
    if comfy_prompt_id is not None:
        assignments.append("comfy_prompt_id = ?")
        values.append(comfy_prompt_id)
    if status == "running":
        assignments.append("started_at = COALESCE(started_at, ?)")
        values.append(now())
    if status in {"succeeded", "failed"}:
        assignments.append("completed_at = ?")
        values.append(now())
    values.append(generation_id)

    with database() as connection:
        connection.execute(
            f"UPDATE generations SET {', '.join(assignments)} WHERE id = ?", values
        )
    await broadcast({"type": "generation.updated", "generation": get_generation(generation_id)})


def copy_to_comfy_input(
    provider: VideoProvider, source: Path, prefix: str
) -> str:
    destination_name = f"{prefix}-{uuid.uuid4().hex}{source.suffix.lower()}"
    destination = Path(local_setting(provider, "input_dir")) / destination_name
    shutil.copy2(source, destination)
    return destination_name


def read_workflow(name: str) -> dict[str, Any]:
    return json.loads((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def replace_placeholder(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: replace_placeholder(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_placeholder(item, replacements) for item in value]
    if isinstance(value, str):
        return replacements.get(value, value)
    return value


def configure_workflow(
    provider: VideoProvider,
    generation: dict[str, Any],
    source_input_name: str | None = None,
) -> dict[str, Any]:
    config = generation["config"]
    mode = generation["mode"]
    workflow_names = provider.settings["workflows"]
    if mode == "text":
        workflow = read_workflow(workflow_names["text"])
        replacements = {
            "__POSITIVE_PROMPT__": generation["prompt"],
            "__NEGATIVE_PROMPT__": generation["negative_prompt"],
        }
    elif mode == "image":
        workflow = read_workflow(workflow_names["image"])
        replacements = {
            "__POSITIVE_PROMPT__": generation["prompt"],
            "__NEGATIVE_PROMPT__": generation["negative_prompt"],
            "__REFERENCE_IMAGE__": source_input_name or "",
        }
    elif mode == "continue" and provider.settings.get("continuation_mode") == "last_frame":
        workflow = read_workflow(workflow_names["image"])
        replacements = {
            "__POSITIVE_PROMPT__": generation["prompt"],
            "__NEGATIVE_PROMPT__": generation["negative_prompt"],
            "__REFERENCE_IMAGE__": source_input_name or "",
        }
    elif mode in {"edit", "continue"}:
        workflow = read_workflow(workflow_names["edit"])
        replacements = {
            "__EDIT_PROMPT__": generation["prompt"],
            "__NEGATIVE_PROMPT__": generation["negative_prompt"],
            "__SOURCE_VIDEO__": source_input_name or "",
        }

    workflow = replace_placeholder(workflow, replacements)
    for node in workflow.values():
        if node["class_type"] == "WanVaceToVideo":
            node["inputs"]["width"] = config["width"]
            node["inputs"]["height"] = config["height"]
            node["inputs"]["length"] = config["length"]
        elif node["class_type"] in {"EmptyLTXVLatentVideo", "LTXVImgToVideo"}:
            node["inputs"]["width"] = config["width"]
            node["inputs"]["height"] = config["height"]
            node["inputs"]["length"] = max(9, ((config["length"] - 1 + 7) // 8) * 8 + 1)
        elif node["class_type"] == "LTXVConditioning":
            node["inputs"]["frame_rate"] = config["fps"]
        elif node["class_type"] == "KSampler":
            node["inputs"]["seed"] = config["seed"]
        elif node["class_type"] == "CreateVideo":
            node["inputs"]["fps"] = config["fps"]
        elif node["class_type"] == "SaveVideo":
            node["inputs"]["filename_prefix"] = f"video/{generation['id']}"
    return workflow


def ffmpeg_executable() -> str:
    configured_path = os.getenv("SK2_FFMPEG_EXECUTABLE")
    candidates = [
        configured_path,
        shutil.which("ffmpeg"),
        shutil.which("ffmpeg.exe"),
    ]
    package_root = (
        Path(os.getenv("LOCALAPPDATA", ""))
        / "Microsoft"
        / "WinGet"
        / "Packages"
    )
    if package_root.exists():
        candidates.extend(
            str(path)
            for path in package_root.glob(
                "Gyan.FFmpeg.Shared_*/ffmpeg-*/bin/ffmpeg.exe"
            )
        )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError(
        "FFmpeg was not found. Set SK2_FFMPEG_EXECUTABLE or install FFmpeg."
    )


def extract_video_tail(
    source: Path,
    target: Path,
    *,
    tail_frames: int,
    width: int,
    height: int,
) -> None:
    if not source.is_file():
        raise RuntimeError(f"Source video output was not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    video_filter = (
        f"reverse,select=lt(n\\,{tail_frames}),reverse,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        "setpts=N/FRAME_RATE/TB"
    )
    result = subprocess.run(
        [
            ffmpeg_executable(),
            "-y",
            "-i",
            str(source),
            "-vf",
            video_filter,
            "-an",
            "-frames:v",
            str(tail_frames),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError(
            f"FFmpeg could not extract the final frames: {result.stderr[-500:]}"
        )


def join_continuation_video(
    parent: Path,
    continuation: Path,
    target: Path,
    *,
    overlap_seconds: float,
    width: int,
    height: int,
    fps: int,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    video_filter = (
        f"[1:v]trim=start={overlap_seconds:.6f},fps={fps},"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        "setpts=PTS-STARTPTS[next];"
        f"[0:v]fps={fps},"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        "setpts=PTS-STARTPTS[base];"
        "[base][next]concat=n=2:v=1:a=0[out]"
    )
    result = subprocess.run(
        [
            ffmpeg_executable(),
            "-y",
            "-i",
            str(parent),
            "-i",
            str(continuation),
            "-filter_complex",
            video_filter,
            "-map",
            "[out]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError(
            f"FFmpeg could not join the continuation video: {result.stderr[-500:]}"
        )


async def release_comfy_models(
    client: httpx.AsyncClient, provider: VideoProvider
) -> None:
    if provider.kind != "comfyui":
        return
    try:
        await client.post(
            f"{local_setting(provider, 'base_url')}/free",
            json={"unload_models": True, "free_memory": True},
            timeout=20,
        )
    except httpx.HTTPError:
        pass


async def interrupt_comfy_provider(provider: VideoProvider) -> None:
    if provider.kind != "comfyui":
        return
    base_url = local_setting(provider, "base_url")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            await client.post(f"{base_url}/interrupt", json={})
            await client.post(f"{base_url}/queue", json={"clear": True})
            await release_comfy_models(client, provider)
    except httpx.HTTPError:
        pass


def agnes_frame_count(length: int) -> int:
    # Agnes Video V2 uses a frame count from the 8n + 1 sequence.
    return max(9, ((length - 1 + 7) // 8) * 8 + 1)


def agnes_task_id(payload: dict[str, Any]) -> str:
    for key in ("video_id", "id", "task_id"):
        value = payload.get(key)
        if value:
            return str(value)
    data = payload.get("data")
    if isinstance(data, dict):
        return agnes_task_id(data)
    raise RuntimeError("Agnes did not return a video task id")


def agnes_result(payload: dict[str, Any]) -> tuple[bool, str | None, str | None]:
    data = payload.get("data")
    result = data if isinstance(data, dict) else payload
    metadata = result.get("metadata")
    output_url = result.get("url")
    if not output_url and isinstance(metadata, dict):
        output_url = metadata.get("url")
    if output_url:
        return True, str(output_url), None
    status = str(result.get("status", "")).lower()
    if status in {"failed", "error", "cancelled", "canceled"}:
        error = result.get("error") or result.get("message") or status
        return False, None, str(error)
    return False, None, None


def generation_error_message(error: Exception) -> str:
    if isinstance(error, httpx.ConnectError):
        return (
            "Unable to connect to the cloud video provider. "
            "The TLS connection was reset; check your network, proxy, VPN, or firewall."
        )
    if isinstance(error, httpx.TimeoutException):
        return "The cloud video provider request timed out. Please try again."
    if isinstance(error, httpx.HTTPStatusError):
        return f"Cloud video provider returned HTTP {error.response.status_code}."
    return str(error) or f"{type(error).__name__}: no additional detail"


async def agnes_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    for attempt in range(3):
        try:
            return await client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.TimeoutException) as error:
            if attempt == 2:
                raise error
            await asyncio.sleep(attempt + 1)
    raise RuntimeError("Agnes request retry loop ended unexpectedly")


async def run_agnes_generation(
    provider: VideoProvider, generation: dict[str, Any]
) -> None:
    if generation["mode"] != "text":
        raise RuntimeError(
            f"Provider {provider.id} only supports text-to-video in this application"
        )

    base_url = str(provider.settings.get("base_url", "")).rstrip("/")
    result_url = str(provider.settings.get("result_url", "")).strip()
    if not base_url or not result_url:
        raise RuntimeError(f"Provider {provider.id} is missing API endpoints")

    config = generation["config"]
    request_payload = {
        "model": provider.model,
        "prompt": generation["prompt"],
        "width": config["width"],
        "height": config["height"],
        "num_frames": agnes_frame_count(config["length"]),
        "frame_rate": config["fps"],
        "seed": config["seed"],
    }
    headers = {"Authorization": f"Bearer {provider_api_key(provider)}"}
    poll_interval = max(1, int(provider.settings.get("poll_interval_seconds", 5)))
    poll_timeout = max(60, int(provider.settings.get("poll_timeout_seconds", 1200)))

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        response = await agnes_request(
            client,
            "POST",
            f"{base_url}/videos",
            json=request_payload,
            headers=headers,
        )
        response.raise_for_status()
        video_id = agnes_task_id(response.json())
        await set_generation_state(
            generation["id"],
            "running",
            progress=0.15,
            comfy_prompt_id=video_id,
        )

        deadline = time.monotonic() + poll_timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            response = await agnes_request(
                client,
                "GET",
                result_url,
                params={"video_id": video_id},
                headers=headers,
            )
            response.raise_for_status()
            complete, output_url, error_message = agnes_result(response.json())
            if error_message:
                raise RuntimeError(f"Agnes generation failed: {error_message}")
            if not complete:
                await set_generation_state(generation["id"], "running", progress=0.5)
                continue

            target_relative = Path(generation["id"]) / f"{generation['id']}.mp4"
            target = MEDIA_DIR / target_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            video_response = await agnes_request(client, "GET", output_url)
            video_response.raise_for_status()
            target.write_bytes(video_response.content)
            await set_generation_state(
                generation["id"],
                "succeeded",
                progress=1.0,
                output_path=target_relative.as_posix(),
            )
            return

    raise RuntimeError(f"Agnes generation timed out after {poll_timeout} seconds")


def wanx_task_id(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if isinstance(output, dict) and output.get("task_id"):
        return str(output["task_id"])
    for key in ("task_id", "id"):
        if payload.get(key):
            return str(payload[key])
    raise RuntimeError("Alibaba Wanxiang did not return a task id")


def wanx_task_result(payload: dict[str, Any]) -> tuple[bool, str | None, str | None]:
    output = payload.get("output")
    result = output if isinstance(output, dict) else payload
    status = str(result.get("task_status") or result.get("status") or "").upper()
    video_url = result.get("video_url") or result.get("url")
    if status in {"SUCCEEDED", "SUCCESS"}:
        if not video_url:
            raise RuntimeError("Alibaba Wanxiang completed without a video URL")
        return True, str(video_url), None
    if status in {"FAILED", "CANCELED", "CANCELLED", "UNKNOWN"}:
        code = result.get("code") or payload.get("code")
        message = result.get("message") or payload.get("message") or status
        return False, None, f"{code}: {message}" if code else str(message)
    return False, None, None


def wanx_image_data_url(path: Path) -> str:
    if not path.is_file():
        raise RuntimeError(f"Reference image was not found: {path}")
    mime_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(path.suffix.lower())
    if not mime_type:
        raise RuntimeError("Alibaba Wanxiang requires a PNG, JPG, JPEG, or WebP reference image")
    content = path.read_bytes()
    if len(content) > 20 * 1024 * 1024:
        raise RuntimeError("Alibaba Wanxiang reference images must be 20 MB or smaller")
    return f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"


def wanx_size(config: dict[str, Any]) -> str:
    configured_resolution = str(config.get("resolution") or "").strip()
    if configured_resolution:
        return configured_resolution
    width = int(config.get("width", 1280))
    height = int(config.get("height", 720))
    return "720P" if max(width, height) >= 720 else "480P"


def wanx_duration_seconds(config: dict[str, Any]) -> int:
    requested_seconds = int(round(
        int(config.get("length", 49)) / max(1, int(config.get("fps", 8)))
    ))
    return requested_seconds


def wanx_ratio(config: dict[str, Any]) -> str:
    width = int(config.get("width", 1280))
    height = int(config.get("height", 720))
    if height >= width * 1.4:
        return "9:16"
    if width >= height * 1.4:
        return "16:9"
    return "1:1"


def wanx_duration_for_provider(provider: VideoProvider, config: dict[str, Any]) -> int:
    minimum = int(provider.settings.get("duration_min_seconds", 2))
    maximum = int(provider.settings.get("duration_max_seconds", 15))
    return max(minimum, min(maximum, wanx_duration_seconds(config)))


def wanx_model(provider: VideoProvider, mode: str) -> str:
    setting = "image_model" if mode in {"image", "continue"} else "text_model"
    return str(provider.settings.get(setting) or provider.model)


def extract_video_last_frame(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        raise RuntimeError(f"Source video for continuation was not found: {source}")
    if target.exists():
        target.unlink()

    duration = probe_video_duration(source)
    timestamps = [max(0.0, duration - 0.05), 0.0]
    errors: list[str] = []
    for timestamp in timestamps:
        if target.exists():
            target.unlink()
        command = [ffmpeg_executable(), "-y", "-i", str(source)]
        if timestamp > 0:
            command.extend(["-ss", f"{timestamp:.3f}"])
        command.extend(
            [
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-q:v",
                "2",
                "-update",
                "1",
                str(target),
            ]
        )
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and target.is_file() and target.stat().st_size > 0:
            return
        errors.append(result.stderr[-300:])

    raise RuntimeError(
        "Could not extract a usable continuation frame for Alibaba Wanxiang: "
        + " | ".join(error.strip() for error in errors if error.strip())
    )


async def run_wanx_generation(
    provider: VideoProvider, generation: dict[str, Any]
) -> None:
    if generation["mode"] == "edit":
        raise RuntimeError(
            f"Provider {provider.id} does not support instruction-based video editing"
        )

    mode = generation["mode"]
    config = generation["config"]
    base_url = str(provider.settings.get("base_url", "")).rstrip("/")
    service_path = str(
        provider.settings.get(
            "service_path", "/api/v1/services/aigc/video-generation/video-synthesis"
        )
    )
    task_path = str(provider.settings.get("task_path", "/api/v1/tasks/{task_id}"))
    if not base_url:
        raise RuntimeError(f"Provider {provider.id} is missing setting: base_url")

    reference_path: Path | None = None
    if mode == "image":
        with database() as connection:
            asset = connection.execute(
                "SELECT stored_path FROM assets WHERE id = ?",
                (generation["reference_asset_id"],),
            ).fetchone()
        if asset is None:
            raise RuntimeError("Reference image was not found")
        reference_path = Path(asset["stored_path"])
    elif mode == "continue":
        parent = get_generation(generation["parent_generation_id"])
        if not parent.get("output_path"):
            raise RuntimeError("Source video output was not found")
        reference_path = WORK_DIR / generation["id"] / "continuation-reference.jpg"
        await asyncio.to_thread(
            extract_video_last_frame,
            MEDIA_DIR / parent["output_path"],
            reference_path,
        )

    prompt = generation["prompt"]
    input_payload: dict[str, Any] = {"prompt": prompt}
    if provider.settings.get("supports_negative_prompt", False):
        input_payload["negative_prompt"] = generation["negative_prompt"]
    if reference_path is not None:
        media_type = (
            str(provider.settings.get("continuation_media_type", "first_frame"))
            if mode == "continue"
            else str(provider.settings.get("media_type", "first_frame"))
        )
        reference_url = wanx_image_data_url(reference_path)
        media = [
            {
                "type": media_type,
                "url": reference_url,
            }
        ]
        if mode == "continue" and provider.settings.get(
            "continuation_add_reference_image", False
        ):
            media.append({"type": "reference_image", "url": reference_url})
        input_payload["media"] = media
        prefix = str(provider.settings.get("reference_prompt_prefix", "")).strip()
        if prefix:
            input_payload["prompt"] = f"{prefix}{prompt}"
    parameters: dict[str, Any] = {
        "duration": wanx_duration_for_provider(provider, config),
        "prompt_extend": bool(provider.settings.get("prompt_extend", True)),
        "watermark": False,
    }
    parameters[str(provider.settings.get("resolution_parameter", "size"))] = wanx_size(
        config
    )
    if provider.settings.get("supports_ratio", True):
        parameters["ratio"] = wanx_ratio(config)
    request_payload = {
        "model": wanx_model(provider, mode),
        "input": input_payload,
        "parameters": parameters,
    }
    headers = {
        "Authorization": f"Bearer {provider_api_key(provider)}",
        "X-DashScope-Async": "enable",
    }
    poll_interval = max(2, int(provider.settings.get("poll_interval_seconds", 5)))
    poll_timeout = max(60, int(provider.settings.get("poll_timeout_seconds", 1800)))

    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
        response = await client.post(
            f"{base_url}{service_path}", json=request_payload, headers=headers
        )
        response.raise_for_status()
        task_id = wanx_task_id(response.json())
        await set_generation_state(
            generation["id"], "running", progress=0.15, comfy_prompt_id=task_id
        )

        deadline = time.monotonic() + poll_timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            response = await client.get(
                f"{base_url}{task_path.format(task_id=task_id)}", headers=headers
            )
            response.raise_for_status()
            complete, output_url, error_message = wanx_task_result(response.json())
            if error_message:
                raise RuntimeError(f"Alibaba Wanxiang generation failed: {error_message}")
            if not complete:
                await set_generation_state(generation["id"], "running", progress=0.5)
                continue

            generated_path = WORK_DIR / generation["id"] / "wanx-generated.mp4"
            generated_path.parent.mkdir(parents=True, exist_ok=True)
            video_response = await client.get(output_url)
            video_response.raise_for_status()
            generated_path.write_bytes(video_response.content)

            target_relative = Path(generation["id"]) / f"{generation['id']}.mp4"
            target = MEDIA_DIR / target_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if mode == "continue" and config.get("join_parent", True):
                parent = get_generation(generation["parent_generation_id"])
                await asyncio.to_thread(
                    join_continuation_video,
                    MEDIA_DIR / parent["output_path"],
                    generated_path,
                    target,
                    overlap_seconds=0,
                    width=int(config["width"]),
                    height=int(config["height"]),
                    fps=int(config["fps"]),
                )
            else:
                shutil.copy2(generated_path, target)
            await set_generation_state(
                generation["id"],
                "succeeded",
                progress=1.0,
                output_path=target_relative.as_posix(),
            )
            return

    raise RuntimeError(
        f"Alibaba Wanxiang generation timed out after {poll_timeout} seconds"
    )


async def unload_ollama_model() -> None:
    configured_path = os.getenv("SK2_OLLAMA_EXECUTABLE")
    default_path = Path(
        os.getenv("LOCALAPPDATA", "")
    ) / "Programs" / "Ollama" / "ollama.exe"
    candidates = [configured_path, str(default_path), "ollama"]

    for candidate in candidates:
        if not candidate:
            continue
        if candidate != "ollama" and not Path(candidate).exists():
            continue
        try:
            process = await asyncio.create_subprocess_exec(
                candidate,
                "stop",
                OLLAMA_MODEL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), timeout=15)
            return
        except (FileNotFoundError, asyncio.TimeoutError):
            continue


def start_generation_task(generation_id: str) -> None:
    task = asyncio.create_task(run_generation(generation_id))
    generation_tasks[generation_id] = task
    task.add_done_callback(lambda _: generation_tasks.pop(generation_id, None))


async def request_edit_spec(instruction: str) -> dict[str, list[str]]:
    global model_activity
    task = asyncio.current_task()
    if task is not None:
        edit_parser_tasks.add(task)
    schema = {
        "type": "object",
        "properties": {
            "requested_changes": {"type": "array", "items": {"type": "string"}},
            "preserve_constraints": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["requested_changes", "preserve_constraints"],
        "additionalProperties": False,
    }
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "keep_alive": 0,
        "format": schema,
        "options": {"temperature": 0},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Extract video edit instructions. Return JSON only. "
                    "requested_changes contains requested visual changes. "
                    "preserve_constraints contains elements that must remain unchanged."
                ),
            },
            {"role": "user", "content": instruction},
        ],
    }
    try:
        async with model_lock:
            model_activity = "edit_parser"
            try:
                for provider in PROVIDERS.values():
                    if provider.enabled and provider.kind == "comfyui":
                        async with httpx.AsyncClient(timeout=30) as client:
                            await release_comfy_models(client, provider)
                async with httpx.AsyncClient(timeout=180) as client:
                    response = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
                    response.raise_for_status()
                parsed = json.loads(response.json()["message"]["content"])
                return {
                    "requested_changes": [str(item) for item in parsed["requested_changes"]],
                    "preserve_constraints": [str(item) for item in parsed["preserve_constraints"]],
                }
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as error:
                return {
                    "requested_changes": [instruction],
                    "preserve_constraints": [],
                    "parser_warning": f"Local parser unavailable: {error}",
                }
            finally:
                model_activity = "idle"
    finally:
        if task is not None:
            edit_parser_tasks.discard(task)


async def run_generation(generation_id: str) -> None:
    global model_activity
    async with job_lock:
        async with model_lock:
            model_activity = "video_provider"
            generation = get_generation(generation_id)
            await set_generation_state(generation_id, "preparing", progress=0.05)
            source_input_name: str | None = None
            provider: VideoProvider | None = None

            try:
                provider = provider_for_generation(generation)
                if provider.kind == "agnes-video":
                    model_activity = "cloud_video_provider"
                    await run_agnes_generation(provider, generation)
                    return
                if provider.kind == "wanx-video":
                    model_activity = "cloud_video_provider"
                    await run_wanx_generation(provider, generation)
                    return
                if provider.kind != "comfyui":
                    raise RuntimeError(
                        f"Provider {provider.id} has no execution adapter configured"
                    )
                async with httpx.AsyncClient(timeout=30) as client:
                    await release_comfy_models(client, provider)
                if generation["mode"] == "image":
                    with database() as connection:
                        asset = connection.execute(
                            "SELECT * FROM assets WHERE id = ?",
                            (generation["reference_asset_id"],),
                        ).fetchone()
                    if asset is None:
                        raise RuntimeError("Reference image was not found")
                    source_input_name = copy_to_comfy_input(
                        provider, Path(asset["stored_path"]), f"reference-{generation_id}"
                    )
                elif generation["mode"] == "edit":
                    parent = get_generation(generation["parent_generation_id"])
                    if not parent.get("output_path"):
                        raise RuntimeError("Source video output was not found")
                    source_input_name = copy_to_comfy_input(
                        provider,
                        MEDIA_DIR / parent["output_path"],
                        f"source-{generation_id}",
                    )
                elif generation["mode"] == "continue":
                    parent = get_generation(generation["parent_generation_id"])
                    if not parent.get("output_path"):
                        raise RuntimeError("Source video output was not found")
                    if provider.settings.get("continuation_mode") == "last_frame":
                        continuation_path = (
                            WORK_DIR / generation_id / "continuation-reference.jpg"
                        )
                        await asyncio.to_thread(
                            extract_video_last_frame,
                            MEDIA_DIR / parent["output_path"],
                            continuation_path,
                        )
                        source_input_name = copy_to_comfy_input(
                            provider,
                            continuation_path,
                            f"continuation-{generation_id}",
                        )
                    else:
                        tail_path = WORK_DIR / generation_id / "tail.mp4"
                        await asyncio.to_thread(
                            extract_video_tail,
                            MEDIA_DIR / parent["output_path"],
                            tail_path,
                            tail_frames=generation["config"]["tail_frames"],
                            width=generation["config"]["width"],
                            height=generation["config"]["height"],
                        )
                        source_input_name = copy_to_comfy_input(
                            provider,
                            tail_path,
                            f"continuation-{generation_id}",
                        )
                    await set_generation_state(
                        generation_id, "preparing", progress=0.1
                    )

                workflow = configure_workflow(provider, generation, source_input_name)
                base_url = local_setting(provider, "base_url")
                output_dir = Path(local_setting(provider, "output_dir"))
                async with httpx.AsyncClient(timeout=60) as client:
                    response = await client.post(
                        f"{base_url}/prompt",
                        json={"prompt": workflow, "client_id": "sk2-api"},
                    )
                    response.raise_for_status()
                    comfy_prompt_id = response.json()["prompt_id"]
                    await set_generation_state(
                        generation_id,
                        "running",
                        progress=0.15,
                        comfy_prompt_id=comfy_prompt_id,
                    )

                    while True:
                        await asyncio.sleep(2)
                        history_response = await client.get(
                            f"{base_url}/history/{comfy_prompt_id}"
                        )
                        history_response.raise_for_status()
                        history = history_response.json().get(comfy_prompt_id)
                        if not history:
                            continue
                        status = history.get("status", {})
                        status_str = str(status.get("status_str") or "").lower()
                        if status_str in {"error", "failed", "cancelled", "canceled"}:
                            messages = status.get("messages")
                            if isinstance(messages, list):
                                details = "\n".join(
                                    " ".join(str(part) for part in message)
                                    if isinstance(message, list)
                                    else str(message)
                                    for message in messages
                                )
                            else:
                                details = str(messages or "ComfyUI failed")
                            raise RuntimeError(f"ComfyUI {status_str}: {details}")
                        if not status.get("completed"):
                            continue
                        if status_str != "success":
                            raise RuntimeError(str(status.get("messages", "ComfyUI failed")))

                        output = history.get("outputs", {}).get("11") or history.get(
                            "outputs", {}
                        ).get("12") or history.get("outputs", {}).get("13")
                        if not output or not output.get("images"):
                            raise RuntimeError("ComfyUI did not return a video output")
                        video = output["images"][0]
                        source = output_dir / video.get("subfolder", "") / video["filename"]
                        if not source.exists():
                            raise RuntimeError(
                                f"ComfyUI output file does not exist: {source}"
                            )
                        relative_output = Path(generation_id) / video["filename"]
                        target = MEDIA_DIR / relative_output
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if generation["mode"] == "continue":
                            if generation["config"].get("join_parent", True):
                                parent = get_generation(generation["parent_generation_id"])
                                generated_segment = (
                                    WORK_DIR / generation_id / "generated-segment.mp4"
                                )
                                generated_segment.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(source, generated_segment)
                                await asyncio.to_thread(
                                    join_continuation_video,
                                    MEDIA_DIR / parent["output_path"],
                                    generated_segment,
                                    target,
                                    overlap_seconds=(
                                        generation["config"]["tail_frames"]
                                        / generation["config"]["fps"]
                                    ),
                                    width=generation["config"]["width"],
                                    height=generation["config"]["height"],
                                    fps=generation["config"]["fps"],
                                )
                            else:
                                shutil.copy2(source, target)
                        else:
                            shutil.copy2(source, target)
                        await set_generation_state(
                            generation_id,
                            "succeeded",
                            progress=1.0,
                            output_path=relative_output.as_posix(),
                        )
                        break
            except asyncio.CancelledError:
                await set_generation_state(
                    generation_id,
                    "failed",
                    progress=1.0,
                    error_message="Stopped by user",
                )
            except Exception as error:
                await set_generation_state(
                    generation_id,
                    "failed",
                    progress=1.0,
                    error_message=generation_error_message(error),
                )
            finally:
                async with httpx.AsyncClient(timeout=30) as client:
                    if provider is not None:
                        await release_comfy_models(client, provider)
                model_activity = "idle"


def make_generation(
    *,
    mode: Literal["text", "image", "edit", "continue"],
    prompt: str,
    negative_prompt: str,
    config: dict[str, Any],
    reference_asset_id: str | None = None,
    parent_generation_id: str | None = None,
    edit_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    generation_id = uuid.uuid4().hex
    with database() as connection:
        connection.execute(
            """
            INSERT INTO generations (
              id, parent_generation_id, mode, prompt, negative_prompt,
              reference_asset_id, edit_spec_json, config_json, status, progress,
              created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?)
            """,
            (
                generation_id,
                parent_generation_id,
                mode,
                prompt,
                negative_prompt,
                reference_asset_id,
                json.dumps(edit_spec) if edit_spec else None,
                json.dumps(config),
                now(),
            ),
        )
    return get_generation(generation_id)


def ffprobe_executable() -> str:
    ffmpeg = Path(ffmpeg_executable())
    candidate = ffmpeg.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if found:
        return found
    raise RuntimeError("FFprobe was not found alongside FFmpeg")


def probe_video_duration(path: Path) -> float:
    result = subprocess.run(
        [
            ffprobe_executable(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"FFprobe failed: {result.stderr[-300:]}")
    return max(0.0, float(result.stdout.strip()))


def extract_ad_keyframes(source: Path, target_dir: Path) -> list[Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    for existing_frame in target_dir.glob("frame-*.jpg"):
        existing_frame.unlink()
    duration = max(probe_video_duration(source), 0.1)
    timestamps = [0.0, duration * 0.5, duration * 0.8]
    frames: list[Path] = []
    for index, timestamp in enumerate(timestamps, start=1):
        frame_path = target_dir / f"frame-{index:02d}.jpg"
        result = subprocess.run(
            [
                ffmpeg_executable(),
                "-y",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale=512:-2",
                str(frame_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and frame_path.is_file():
            frames.append(frame_path)
    if not frames:
        raise RuntimeError("Could not extract any key frames from the generated video")
    return frames


def extract_reference_video_frames(source: Path, target_dir: Path) -> list[Path]:
    if not source.is_file():
        raise RuntimeError("Reference video was not found")
    target_dir.mkdir(parents=True, exist_ok=True)
    duration = max(probe_video_duration(source), 1.0)
    interval = max(duration / 4, 0.5)
    pattern = target_dir / "reference-%02d.jpg"
    result = subprocess.run(
        [
            ffmpeg_executable(),
            "-y",
            "-i",
            str(source),
            "-vf",
            f"fps=1/{interval:.4f},scale=512:-2",
            "-frames:v",
            "4",
            "-q:v",
            "3",
            str(pattern),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    frames = sorted(target_dir.glob("reference-*.jpg"))
    if result.returncode != 0 or not frames:
        raise RuntimeError(f"Could not extract reference frames: {result.stderr[-400:]}")
    return frames


async def analyze_reference_video(source: Path, frame_dir: Path) -> dict[str, Any]:
    frames = await asyncio.to_thread(extract_reference_video_frames, source, frame_dir)
    try:
        analysis = await request_ad_llm(
            system_prompt=load_ad_prompt("reference_video_analysis_v1.md"),
            user_prompt=(
                "请分析所提供的参考视频关键帧，仅返回 JSON。所有字段及提示词都使用中文。"
                "结果只能指导全新原创广告，不得复刻原视频的画面、品牌标识、人物、文字或具体场景。"
            ),
            image_paths=frames,
        )
        analysis["frame_count"] = len(frames)
        return analysis
    except RuntimeError as error:
        return {
            "visual_style": "参考视频分析暂不可用。",
            "shot_structure": [],
            "camera_language": "",
            "editing_rhythm": "",
            "color_lighting": "",
            "sound_mood": "",
            "generation_prompt": "",
            "negative_prompt": "品牌标识，复制的文字，水印，复刻原场景",
            "adaptation_notes": f"降级分析：{error}",
            "frame_count": len(frames),
        }


async def write_ad_final_copy(
    project: dict[str, Any], plan: dict[str, Any], instruction: str
) -> dict[str, Any]:
    output_relative = project.get("master_output_path") or project.get("final_output_path")
    if not output_relative:
        raise RuntimeError("Completed video output was not found")
    output_path = MEDIA_DIR / output_relative
    if not output_path.is_file():
        raise RuntimeError("Completed video file was not found")

    frames = await asyncio.to_thread(
        extract_reference_video_frames,
        output_path,
        AD_WORK_DIR / project["id"] / "final-copy-analysis",
    )
    result = await request_ad_llm(
        system_prompt=load_ad_prompt("final_copy_writer_v1.md"),
        user_prompt=json.dumps(
            {
                "brief": project["brief"],
                "target_duration_seconds": project["target_duration_seconds"],
                "voiceover_duration_guidance": ad_voiceover_duration_guidance(
                    int(project["target_duration_seconds"])
                ),
                "existing_voiceover_script": plan.get("voiceover_script", ""),
                "existing_post_caption": plan.get("post_caption", ""),
                "existing_hashtags": plan.get("hashtags", []),
                "user_instruction": instruction.strip(),
                "instruction": (
                    "Analyze the supplied frames from the actual completed video. "
                    "Create replacement copy that matches its visible scenes, pacing, "
                    "and duration. Do not invent features that are not supported by "
                    "the original brief or visible video."
                ),
            },
            ensure_ascii=False,
        ),
        image_paths=frames,
    )
    voiceover_script = str(result.get("voiceover_script", "")).strip()[:2000]
    post_caption = str(result.get("post_caption", "")).strip()[:1000]
    hashtags = [
        str(item).strip()[:80]
        for item in result.get("hashtags", [])
        if str(item).strip()
    ][:10]
    if not voiceover_script:
        raise RuntimeError("Advertising language model returned no voiceover text")
    return {
        "voiceover_script": voiceover_script,
        "post_caption": post_caption,
        "hashtags": hashtags,
        "frame_count": len(frames),
    }


def normalize_ad_clip(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ffmpeg_executable(),
            "-y",
            "-i",
            str(source),
            "-vf",
            "fps=24,scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not normalize ad video: {result.stderr[-500:]}")


def concat_ad_clips(clips: list[Path], target: Path, work_dir: Path) -> None:
    if not clips:
        raise RuntimeError("No video clips are available for composition")
    manifest = work_dir / "clips.txt"
    lines = []
    for path in clips:
        escaped_path = path.as_posix().replace("'", r"'\''")
        lines.append(f"file '{escaped_path}'")
    manifest.write_text("\n".join(lines), encoding="utf-8")
    result = subprocess.run(
        [
            ffmpeg_executable(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not join ad clips: {result.stderr[-500:]}")


def ass_timestamp(seconds: float) -> str:
    return f"{int(seconds // 3600)}:{int(seconds % 3600 // 60):02d}:{seconds % 60:05.2f}"


def write_ad_subtitles(script: str, duration: float, target: Path) -> None:
    sentences = [item.strip() for item in re.split(r"(?<=[。！？!?；;])", script) if item.strip()]
    total = sum(len(item) for item in sentences) or 1
    cursor = 0.0
    events: list[str] = []
    for item in sentences:
        end = min(duration, cursor + duration * len(item) / total)
        escaped = item.replace("{", "(").replace("}", ")").replace("\n", r"\N")
        events.append(f"Dialogue: 0,{ass_timestamp(cursor)},{ass_timestamp(end)},Default,,0,0,0,,{escaped}")
        cursor = end
    target.write_text(
        """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,Microsoft YaHei,54,&H00FFFFFF,&H000000FF,&H00101010,&H80000000,1,0,0,0,100,100,0,0,1,3,1,2,56,56,150,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
        + "\n".join(events),
        encoding="utf-8",
    )


def ad_bgm_directory() -> Path:
    configured = os.getenv("SK2_AD_BGM_DIR")
    return Path(configured) if configured else ROOT_DIR.parent / "SK" / "bgm"


def list_ad_bgm() -> list[dict[str, str]]:
    root = ad_bgm_directory()
    if not root.is_dir():
        return []
    tracks: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}:
            continue
        relative = path.relative_to(root)
        if len(relative.parts) < 2:
            continue
        tracks.append(
            {
                "id": f"{relative.parent.as_posix()}/{path.stem}",
                "name": path.stem,
                "scene": relative.parent.as_posix(),
                "path": str(path),
            }
        )
    return tracks


def resolve_ad_bgm(bgm_id: str) -> Path | None:
    for track in list_ad_bgm():
        if track["id"] == bgm_id:
            return Path(track["path"])
    return None


async def wait_for_generation(generation_id: str) -> dict[str, Any]:
    while True:
        generation = get_generation(generation_id)
        if generation["status"] == "succeeded":
            return generation
        if generation["status"] == "failed":
            raise RuntimeError(generation.get("error_message") or "Video generation failed")
        await asyncio.sleep(2)


async def review_ad_segment(
    project: dict[str, Any],
    plan: dict[str, Any],
    segment: dict[str, Any],
    frames: list[Path],
    *,
    current_duration_seconds: float,
    continuation_count: int,
) -> dict[str, Any]:
    user_prompt = json.dumps(
        {
            "overall_plan": {
                "title": plan.get("title"),
                "strategy": plan.get("strategy"),
            },
            "segment": segment,
            "current_duration_seconds": round(current_duration_seconds, 2),
            "target_duration_seconds": segment["duration_seconds"],
            "continuation_count": continuation_count,
            "maximum_continuations": 3,
            "instruction": "Review the supplied key frames and return JSON only.",
        },
        ensure_ascii=False,
    )
    try:
        return await request_ad_llm(
            system_prompt=load_ad_prompt("segment_reviewer_v1.md"),
            user_prompt=user_prompt,
            image_paths=frames,
        )
    except RuntimeError as error:
        return {
            "approved": True,
            "reason": f"Review fallback: {error}",
            "preserve": ["商品主体", "构图", "光线"],
            "should_continue": current_duration_seconds + 0.25 < float(segment["duration_seconds"]),
            "continue_reason": "当前片段尚未达到规划时长。",
            "continuation_prompt": segment["prompt"],
            "retry_prompt": segment["prompt"],
        }


async def plan_ad_segment_transition(
    project: dict[str, Any],
    plan: dict[str, Any],
    previous_segment: dict[str, Any],
    next_segment: dict[str, Any],
    previous_frames: list[Path],
) -> dict[str, Any]:
    user_prompt = json.dumps(
        {
            "overall_plan": {
                "title": plan.get("title"),
                "strategy": plan.get("strategy"),
            },
            "previous_segment": previous_segment,
            "next_segment": next_segment,
            "instruction": (
                "Use the supplied key frames from the previous generated segment. "
                "Decide whether the next segment should start as a video continuation."
            ),
        },
        ensure_ascii=False,
    )
    try:
        decision = await request_ad_llm(
            system_prompt=load_ad_prompt("segment_transition_v1.md"),
            user_prompt=user_prompt,
            image_paths=previous_frames[-2:],
        )
        return {
            "should_continue": bool(decision.get("should_continue")),
            "transition_type": str(decision.get("transition_type", "hard_cut"))[:80],
            "reason": str(decision.get("reason", ""))[:500],
            "preserve": [
                str(item)[:200] for item in decision.get("preserve", [])
            ][:8],
            "transition_prompt": str(
                decision.get("transition_prompt") or next_segment.get("prompt", "")
            )[:3000],
        }
    except RuntimeError as error:
        same_asset = (
            previous_segment.get("asset_index", -1) >= 0
            and previous_segment.get("asset_index") == next_segment.get("asset_index")
        )
        return {
            "should_continue": same_asset,
            "transition_type": "direct_continuation" if same_asset else "hard_cut",
            "reason": f"Transition fallback: {error}",
            "preserve": ["主体身份", "画幅", "光线方向"],
            "transition_prompt": next_segment.get("prompt", ""),
        }


async def ensure_ad_master(project_id: str, source_paths: list[Path]) -> str:
    project = ad_project_detail(project_id)
    existing_path = project.get("master_output_path")
    if existing_path and (MEDIA_DIR / existing_path).is_file():
        return existing_path

    project_dir = AD_WORK_DIR / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    normalized: list[Path] = []
    for index, source in enumerate(source_paths, start=1):
        target = project_dir / f"normalized-{index:02d}.mp4"
        await asyncio.to_thread(normalize_ad_clip, source, target)
        normalized.append(target)
    joined = project_dir / "joined.mp4"
    await asyncio.to_thread(concat_ad_clips, normalized, joined, project_dir)
    master_relative = Path("ad") / project_id / "master.mp4"
    master_path = MEDIA_DIR / master_relative
    master_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.copy2, joined, master_path)
    with database() as connection:
        connection.execute(
            "UPDATE ad_projects SET master_output_path = ? WHERE id = ?",
            (master_relative.as_posix(), project_id),
        )
    return master_relative.as_posix()


async def compose_ad_project(project_id: str, plan: dict[str, Any], source_paths: list[Path]) -> str:
    project = ad_project_detail(project_id)
    master_relative = await ensure_ad_master(project_id, source_paths)
    master_path = MEDIA_DIR / master_relative
    project_dir = AD_WORK_DIR / project_id
    duration = await asyncio.to_thread(probe_video_duration, master_path)
    voice_path: Path | None = None
    if project["voice_enabled"] and plan.get("voiceover_script"):
        voice_path = project_dir / "voice.mp3"
        await edge_tts.Communicate(
            plan["voiceover_script"], voice=project["voice_id"]
        ).save(str(voice_path))
    subtitle_path: Path | None = None
    if project["subtitle_enabled"] and plan.get("voiceover_script"):
        subtitle_path = project_dir / "subtitles.ass"
        write_ad_subtitles(plan["voiceover_script"], duration, subtitle_path)

    with database() as connection:
        next_version = int(
            connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS value FROM ad_final_versions WHERE project_id = ?",
                (project_id,),
            ).fetchone()["value"]
        )
    final_relative = Path("ad") / project_id / "versions" / f"version-{next_version:03d}.mp4"
    final_path = MEDIA_DIR / final_relative
    final_path.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg_executable(), "-y", "-i", str(master_path)]
    filters: list[str] = []
    audio_label: str | None = None
    input_index = 1
    if voice_path:
        command.extend(["-i", str(voice_path)])
        voice_duration = await asyncio.to_thread(probe_video_duration, voice_path)
        tempo = min(2.0, max(0.5, voice_duration / max(duration, 0.1)))
        filters.append(f"[{input_index}:a]atempo={tempo:.4f},apad,atrim=0:{duration:.3f}[voice]")
        audio_label = "voice"
        input_index += 1
    if project["bgm_enabled"]:
        bgm_path = resolve_ad_bgm(project.get("bgm_id", "default/ambient"))
        if bgm_path:
            command.extend(["-stream_loop", "-1", "-i", str(bgm_path)])
            filters.append(f"[{input_index}:a]atrim=0:{duration:.3f},afade=t=out:st={max(duration - 1.5, 0):.3f}:d=1.5,volume=0.15[bgm]")
            if audio_label:
                filters.append("[voice][bgm]amix=inputs=2:duration=first:dropout_transition=1[audio]")
                audio_label = "audio"
            else:
                audio_label = "bgm"
    video_filter = (
        "[0:v]subtitles=subtitles.ass[video]"
        if subtitle_path
        else "[0:v]null[video]"
    )
    filters.append(video_filter)
    if filters:
        command.extend(["-filter_complex", ";".join(filters), "-map", "[video]"])
    else:
        command.extend(["-map", "0:v"])
    if audio_label:
        command.extend(["-map", f"[{audio_label}]", "-c:a", "aac", "-b:a", "192k"])
    else:
        command.append("-an")
    command.extend([
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-r", "24", str(final_path),
    ])
    result = await asyncio.to_thread(
        subprocess.run,
        command,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(project_dir),
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not compose advertising video: {result.stderr[-600:]}")
    with database() as connection:
        connection.execute(
            """
            INSERT INTO ad_final_versions (
              id, project_id, version, output_path, voiceover_script,
              post_caption, hashtags_json, voice_enabled, subtitle_enabled,
              bgm_enabled, bgm_id, voice_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                project_id,
                next_version,
                final_relative.as_posix(),
                str(plan.get("voiceover_script", "")),
                str(plan.get("post_caption", "")),
                json.dumps(plan.get("hashtags", []), ensure_ascii=False),
                int(project["voice_enabled"]),
                int(project["subtitle_enabled"]),
                int(project["bgm_enabled"]),
                project.get("bgm_id", "default/ambient"),
                project["voice_id"],
                now(),
            ),
        )
    return final_relative.as_posix()


async def run_ad_project(project_id: str) -> None:
    active_run_id: str | None = None
    try:
        project = ad_project_detail(project_id)
        version = project.get("approved_plan_version")
        if not version or not project.get("plan_approved_at"):
            raise RuntimeError("Advertising plan has not been approved by the user")
        plan_record = next((item for item in project["plans"] if item["version"] == version), None)
        if plan_record is None:
            raise RuntimeError("Approved advertising plan was not found")
        plan = plan_record["plan"]
        assets = project["assets"]
        await set_ad_project_state(project_id, "generating_segments")
        source_paths: list[Path] = []
        previous_generation_id: str | None = None
        previous_segment_id: str | None = None
        previous_definition: dict[str, Any] | None = None
        previous_frames: list[Path] = []
        total = len(plan["segments"])
        replan_from_sequence = int(
            plan.get("replan_from_sequence")
            or plan_record.get("replan_from_sequence")
            or 0
        )
        successful_current = {
            int(segment["sequence_number"]): segment
            for segment in project["segments"]
            if segment["plan_id"] == plan_record["id"]
            and segment["status"] == "succeeded"
            and segment.get("output_path")
        }
        successful_prefix = {
            int(segment["sequence_number"]): segment
            for segment in project["segments"]
            if replan_from_sequence
            and int(segment["sequence_number"]) < replan_from_sequence
            and segment["status"] == "succeeded"
            and segment.get("output_path")
        }
        for sequence, definition in enumerate(plan["segments"], start=1):
            existing_segment = successful_current.get(sequence) or successful_prefix.get(
                sequence
            )
            if existing_segment:
                existing_output = MEDIA_DIR / existing_segment["output_path"]
                if existing_output.is_file() and existing_segment.get("generation_id"):
                    source_paths.append(existing_output)
                    previous_generation_id = existing_segment["generation_id"]
                    previous_segment_id = existing_segment["id"]
                    previous_definition = definition
                    frame_dir = AD_WORK_DIR / project_id / f"segment-{sequence:02d}"
                    previous_frames = await asyncio.to_thread(
                        extract_ad_keyframes, existing_output, frame_dir
                    )
                    continue
            active_run_id = await add_ad_run(
                project_id, "generating_segment", (sequence - 1) / max(total, 1),
                {"sequence": sequence, "total": total},
            )
            asset = (
                assets[definition["asset_index"]]
                if assets and definition["asset_index"] >= 0
                else None
            )
            segment_asset_id = asset["id"] if asset else None
            reference_asset_id = asset["asset_id"] if asset else None
            provider_id = project.get("video_provider_id", DEFAULT_PROVIDER_ID)
            provider = get_provider(provider_id)
            incoming_transition: dict[str, Any] | None = None
            should_continue_from_previous = False
            if (
                previous_generation_id
                and previous_definition
                and previous_frames
            ):
                incoming_transition = await plan_ad_segment_transition(
                    project,
                    plan,
                    previous_definition,
                    definition,
                    previous_frames,
                )
                should_continue_from_previous = bool(
                    incoming_transition.get("should_continue")
                    and provider_supports_continuation(provider)
                )
                if (
                    incoming_transition.get("should_continue")
                    and not should_continue_from_previous
                ):
                    incoming_transition["reason"] = (
                        f"{incoming_transition.get('reason', '')} "
                        f"Selected provider {provider.label} does not support video continuation."
                    ).strip()
                    incoming_transition["should_continue"] = False
            required_capability = (
                "image_to_video"
                if should_continue_from_previous
                and (
                    provider.kind == "wanx-video"
                    or provider.settings.get("continuation_mode") == "last_frame"
                )
                else "video_edit"
                if should_continue_from_previous
                else "image_to_video" if asset else "text_to_video"
            )
            provider = get_provider(provider_id, required_capability)
            target_seconds = float(definition["duration_seconds"])
            segment_id = uuid.uuid4().hex
            prompt = (
                str(incoming_transition.get("transition_prompt") or definition["prompt"])
                if incoming_transition
                else definition["prompt"]
            )
            with database() as connection:
                retry_count = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(retry_count), -1) + 1 AS value
                        FROM ad_segments
                        WHERE project_id = ? AND plan_id = ? AND sequence_number = ?
                        """,
                        (project_id, plan_record["id"], sequence),
                    ).fetchone()["value"]
                )
                connection.execute(
                    "INSERT INTO ad_segments (id, project_id, plan_id, sequence_number, asset_id, parent_segment_id, target_duration_seconds, prompt, retry_count, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')",
                    (
                        segment_id,
                        project_id,
                        plan_record["id"],
                        sequence,
                        segment_asset_id,
                        previous_segment_id if should_continue_from_previous else None,
                        target_seconds,
                        prompt,
                        retry_count,
                    ),
                )
            length = 49
            config = {
                "width": 288,
                "height": 512,
                "resolution": project.get("video_resolution"),
                "length": length,
                "fps": 8,
                "seed": int.from_bytes(os.urandom(8), "big"),
                "provider_id": provider.id,
                "provider_model": provider.model,
            }
            if should_continue_from_previous:
                config.update({"tail_frames": 8, "join_parent": False})
            generation = make_generation(
                mode="continue" if should_continue_from_previous else "image" if asset else "text",
                prompt=prompt,
                negative_prompt="blurry, distorted, flickering, text, watermark, duplicate product",
                reference_asset_id=reference_asset_id if not should_continue_from_previous else None,
                parent_generation_id=previous_generation_id if should_continue_from_previous else None,
                config=config,
            )
            with database() as connection:
                connection.execute(
                    "UPDATE ad_segments SET generation_id = ?, status = 'running' WHERE id = ?",
                    (generation["id"], segment_id),
                )
            await broadcast_ad_project(project_id)
            start_generation_task(generation["id"])
            generated = await wait_for_generation(generation["id"])
            output_path = MEDIA_DIR / generated["output_path"]
            current_duration = await asyncio.to_thread(probe_video_duration, output_path)
            frame_dir = AD_WORK_DIR / project_id / f"segment-{sequence:02d}"
            frames = await asyncio.to_thread(extract_ad_keyframes, output_path, frame_dir)
            await set_ad_project_state(project_id, "reviewing_segments")
            continuation_count = 0
            review = await review_ad_segment(
                project,
                plan,
                definition,
                frames,
                current_duration_seconds=current_duration,
                continuation_count=continuation_count,
            )
            while (
                bool(review.get("should_continue"))
                and current_duration + 0.25 < target_seconds
                and continuation_count < 3
                and provider_supports_continuation(provider)
            ):
                continuation = make_generation(
                    mode="continue",
                    prompt=str(review.get("continuation_prompt") or prompt),
                    negative_prompt="blurry, distorted, flickering, text, watermark, duplicate product",
                    parent_generation_id=generated["id"],
                    config={
                        **config,
                        "tail_frames": 8,
                        "join_parent": True,
                        "seed": int.from_bytes(os.urandom(8), "big"),
                    },
                )
                start_generation_task(continuation["id"])
                generated = await wait_for_generation(continuation["id"])
                output_path = MEDIA_DIR / generated["output_path"]
                current_duration = await asyncio.to_thread(probe_video_duration, output_path)
                continuation_count += 1
                with database() as connection:
                    connection.execute(
                        "UPDATE ad_segments SET generation_id = ? WHERE id = ?",
                        (generated["id"], segment_id),
                    )
                await broadcast_ad_project(project_id)
                frames = await asyncio.to_thread(
                    extract_ad_keyframes,
                    output_path,
                    frame_dir / f"continuation-{continuation_count}",
                )
                review = await review_ad_segment(
                    project,
                    plan,
                    definition,
                    frames,
                    current_duration_seconds=current_duration,
                    continuation_count=continuation_count,
                )
            if (
                bool(review.get("should_continue"))
                and current_duration + 0.25 < target_seconds
                and not provider_supports_continuation(provider)
            ):
                review["continue_reason"] = (
                    f"{review.get('continue_reason', '')} "
                    f"Selected provider {provider.label} does not support video continuation."
                ).strip()
                review["should_continue"] = False
            with database() as connection:
                if incoming_transition:
                    review["incoming_transition"] = incoming_transition
                connection.execute(
                    "UPDATE ad_segments SET status = 'succeeded', output_path = ?, review_json = ? WHERE id = ?",
                    (generated["output_path"], json.dumps(review, ensure_ascii=False), segment_id),
                )
            source_paths.append(output_path)
            previous_generation_id = generated["id"]
            previous_segment_id = segment_id
            previous_definition = definition
            previous_frames = frames
            await complete_ad_run(active_run_id)
            active_run_id = None
            await set_ad_project_state(project_id, "generating_segments")
        await set_ad_project_state(project_id, "composing_audio_video")
        active_run_id = await add_ad_run(project_id, "composing_audio_video", 0.95)
        final_path = await compose_ad_project(project_id, plan, source_paths)
        await complete_ad_run(active_run_id)
        active_run_id = None
        await set_ad_project_state(project_id, "completed", final_output_path=final_path)
    except asyncio.CancelledError:
        if active_run_id:
            await complete_ad_run(active_run_id, error="Stopped by user")
        await set_ad_project_state(project_id, "cancelled", error_message="Stopped by user")
        raise
    except Exception as error:
        if active_run_id:
            await complete_ad_run(active_run_id, error=str(error)[:1000])
        logger.exception("Advertising project %s failed", project_id)
        await set_ad_project_state(project_id, "failed", error_message=str(error)[:1000])


def start_ad_project_task(project_id: str) -> None:
    task = asyncio.create_task(run_ad_project(project_id))
    ad_project_tasks[project_id] = task
    task.add_done_callback(lambda _: ad_project_tasks.pop(project_id, None))


async def run_ad_recompose(project_id: str) -> None:
    try:
        project = ad_project_detail(project_id)
        version = project.get("approved_plan_version")
        plan_record = next(
            (item for item in project["plans"] if item["version"] == version), None
        )
        if plan_record is None:
            raise RuntimeError("Approved advertising plan was not found")
        source_paths = [
            MEDIA_DIR / item["output_path"]
            for item in project["segments"]
            if item["status"] == "succeeded" and item.get("output_path")
        ]
        if not source_paths:
            raise RuntimeError("Completed video segments were not found")
        await set_ad_project_state(project_id, "composing_audio_video")
        run_id = await add_ad_run(
            project_id, "recomposing_audio_video", 0.98, {"video_generation": False}
        )
        final_path = await compose_ad_project(
            project_id, plan_record["plan"], source_paths
        )
        await complete_ad_run(run_id)
        await set_ad_project_state(project_id, "completed", final_output_path=final_path)
    except asyncio.CancelledError:
        await set_ad_project_state(project_id, "cancelled", error_message="Stopped by user")
        raise
    except Exception as error:
        await set_ad_project_state(project_id, "failed", error_message=str(error)[:1000])


def start_ad_recompose_task(project_id: str) -> None:
    task = asyncio.create_task(run_ad_recompose(project_id))
    ad_project_tasks[project_id] = task
    task.add_done_callback(lambda _: ad_project_tasks.pop(project_id, None))


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title="SK2 Local Video Studio", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")


async def provider_availability() -> dict[str, bool]:
    availability = {provider.id: False for provider in PROVIDERS.values()}
    async with httpx.AsyncClient(timeout=4) as client:
        for provider in PROVIDERS.values():
            if not provider.enabled:
                continue
            if provider.kind == "comfyui":
                try:
                    response = await client.get(
                        f"{local_setting(provider, 'base_url')}/system_stats"
                    )
                    availability[provider.id] = response.is_success
                except httpx.HTTPError:
                    pass
            elif provider.kind == "agnes-video":
                try:
                    provider_api_key(provider)
                    availability[provider.id] = True
                except RuntimeError:
                    pass
            elif provider.kind == "wanx-video":
                try:
                    provider_api_key(provider)
                    availability[provider.id] = True
                except RuntimeError:
                    pass
    return availability


@app.get("/api/health")
async def health() -> dict[str, Any]:
    services: dict[str, bool] = {"comfyui": False, "ollama": False}
    provider_status = await provider_availability()
    services["comfyui"] = any(
        provider_status[provider.id]
        for provider in PROVIDERS.values()
        if provider.kind == "comfyui"
    )
    async with httpx.AsyncClient(timeout=4) as client:
        try:
            services["ollama"] = (await client.get(f"{OLLAMA_URL}/api/tags")).is_success
        except httpx.HTTPError:
            pass
    return {
        "services": services,
        "providers": provider_status,
        "queue_active": job_lock.locked(),
        "model_runtime": {
            "busy": model_lock.locked(),
            "activity": model_activity,
        },
    }


@app.get("/api/providers")
async def list_providers() -> dict[str, Any]:
    availability = await provider_availability()
    return {
        "default_provider_id": DEFAULT_PROVIDER_ID,
        "providers": [
            {**public_provider(provider), "available": availability[provider.id]}
            for provider in PROVIDERS.values()
        ],
    }


@app.get("/api/ad-settings")
async def get_ad_model_settings() -> dict[str, Any]:
    settings = ad_llm_settings()
    provider_id = settings["video_provider_id"]
    if provider_id not in PROVIDERS or not PROVIDERS[provider_id].enabled:
        provider_id = DEFAULT_PROVIDER_ID
    return {
        "video_provider_id": provider_id,
        "llm_base_url": settings["base_url"],
        "llm_model": settings["model"],
        "llm_api_key_configured": bool(settings["api_key"]),
        "llm_api": "responses",
    }


@app.put("/api/ad-settings")
async def update_ad_model_settings(
    request: UpdateAdModelSettingsRequest,
) -> dict[str, Any]:
    provider = get_provider(request.video_provider_id)
    if not provider.enabled:
        raise HTTPException(status_code=400, detail="Selected video provider is disabled")

    set_app_setting("ad_default_video_provider_id", provider.id)
    set_app_setting("ad_llm_base_url", request.llm_base_url.strip().rstrip("/"))
    set_app_setting("ad_llm_model", request.llm_model.strip())
    if request.llm_api_key and request.llm_api_key.strip():
        set_app_setting("ad_llm_api_key", request.llm_api_key.strip())
    return await get_ad_model_settings()


@app.post("/api/stop")
async def stop_all_operations() -> dict[str, Any]:
    async with stop_lock:
        with database() as connection:
            rows = connection.execute(
                """
                SELECT id FROM generations
                WHERE status IN ('queued', 'preparing', 'running')
                """
            ).fetchall()
        generation_ids = [row["id"] for row in rows]

        for generation_id in generation_ids:
            await set_generation_state(
                generation_id,
                "failed",
                progress=1.0,
                error_message="Stopped by user",
            )

        await asyncio.gather(
            *(
                interrupt_comfy_provider(provider)
                for provider in PROVIDERS.values()
                if provider.enabled and provider.kind == "comfyui"
            ),
            return_exceptions=True,
        )

        cancellable_tasks = [
            *generation_tasks.values(),
            *edit_parser_tasks,
            *ad_project_tasks.values(),
        ]
        for task in cancellable_tasks:
            if not task.done():
                task.cancel()
        if cancellable_tasks:
            await asyncio.gather(*cancellable_tasks, return_exceptions=True)

        await unload_ollama_model()
        return {
            "stopped_generations": generation_ids,
            "cancelled_tasks": len(cancellable_tasks),
        }


@app.post("/api/ad-projects")
async def create_ad_project(request: CreateAdProjectRequest) -> dict[str, Any]:
    project_id = uuid.uuid4().hex
    provider = get_provider(request.video_provider_id)
    resolution = provider_resolution(provider, request.video_resolution)
    with database() as connection:
        connection.execute(
            """
            INSERT INTO ad_projects (
              id, brief, target_duration_seconds, voice_enabled, subtitle_enabled,
              bgm_enabled, tts_provider, voice_id, video_provider_id, video_resolution,
              status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)
            """,
            (
                project_id,
                request.brief,
                request.target_duration_seconds,
                int(request.voice_enabled),
                int(request.subtitle_enabled),
                int(request.bgm_enabled),
                request.tts_provider,
                request.voice_id,
                provider.id,
                resolution,
                now(),
            ),
        )
    return ad_project_detail(project_id)


@app.get("/api/ad-projects/history")
async def ad_project_history() -> dict[str, Any]:
    with database() as connection:
        rows = connection.execute(
            """
            SELECT
              projects.id,
              projects.brief,
              projects.target_duration_seconds,
              projects.master_output_path,
              projects.final_output_path,
              projects.created_at,
              projects.completed_at,
              plans.plan_json,
              (
                SELECT COUNT(*)
                FROM ad_final_versions AS versions
                WHERE versions.project_id = projects.id
              ) AS final_version_count
            FROM ad_projects AS projects
            LEFT JOIN ad_plans AS plans
              ON plans.project_id = projects.id
              AND plans.version = projects.approved_plan_version
            WHERE projects.status = 'completed'
              AND projects.final_output_path IS NOT NULL
            ORDER BY COALESCE(projects.completed_at, projects.created_at) DESC
            LIMIT 100
            """
        ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        plan = json.loads(item["plan_json"]) if item.get("plan_json") else {}
        final_output_path = item.pop("final_output_path")
        item.pop("plan_json", None)
        item["title"] = str(plan.get("title") or item["brief"])[:120]
        item["output_url"] = f"/media/{final_output_path}"
        if item.get("master_output_path"):
            item["master_output_url"] = f"/media/{item['master_output_path']}"
        items.append(item)
    return {"items": items}


@app.get("/api/ad-projects/{project_id}")
async def ad_project(project_id: str) -> dict[str, Any]:
    return ad_project_detail(project_id)


@app.post("/api/ad-projects/{project_id}/assets")
async def upload_ad_assets(
    project_id: str, files: list[UploadFile] = File(...)
) -> dict[str, Any]:
    project = ad_project_detail(project_id)
    if project["status"] not in {"draft", "planning", "waiting_user_confirmation"}:
        raise HTTPException(status_code=409, detail="Assets cannot be changed after plan approval")
    if not files:
        raise HTTPException(status_code=422, detail="At least one image is required")
    if len(project["assets"]) + len(files) > 8:
        raise HTTPException(status_code=422, detail="A project supports at most 8 images")
    project_asset_dir = AD_MEDIA_DIR / project_id / "assets"
    project_asset_dir.mkdir(parents=True, exist_ok=True)
    for offset, file in enumerate(files):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise HTTPException(status_code=415, detail="Only PNG, JPG, JPEG, and WebP are supported")
        asset_id = uuid.uuid4().hex
        stored_path = project_asset_dir / f"{asset_id}{suffix}"
        stored_path.write_bytes(await file.read())
        ad_asset_id = uuid.uuid4().hex
        with database() as connection:
            connection.execute(
                "INSERT INTO assets (id, filename, stored_path, mime_type, created_at) VALUES (?, ?, ?, ?, ?)",
                (asset_id, file.filename or stored_path.name, str(stored_path), file.content_type or "", now()),
            )
            connection.execute(
                "INSERT INTO ad_assets (id, project_id, asset_id, filename, stored_path, sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ad_asset_id,
                    project_id,
                    asset_id,
                    file.filename or stored_path.name,
                    str(stored_path),
                    len(project["assets"]) + offset,
                    now(),
                ),
            )
    return ad_project_detail(project_id)


@app.post("/api/ad-projects/{project_id}/reference-video")
async def upload_reference_video(
    project_id: str, file: UploadFile = File(...)
) -> dict[str, Any]:
    project = ad_project_detail(project_id)
    if project["status"] not in {"draft", "planning", "waiting_user_confirmation"}:
        raise HTTPException(status_code=409, detail="Reference video cannot be changed after plan approval")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm", ".mkv"}:
        raise HTTPException(status_code=415, detail="Only MP4, MOV, WebM, and MKV reference videos are supported")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="Reference video is empty")
    if len(content) > 300 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Reference video must be 300 MB or smaller")
    reference_dir = AD_MEDIA_DIR / project_id / "reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    target = reference_dir / f"reference{suffix}"
    target.write_bytes(content)
    try:
        analysis = await analyze_reference_video(
            target, AD_WORK_DIR / project_id / "reference-analysis"
        )
    except RuntimeError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    relative_path = target.relative_to(MEDIA_DIR).as_posix()
    with database() as connection:
        connection.execute(
            """
            UPDATE ad_projects
            SET reference_video_path = ?, reference_analysis_json = ?, error_message = NULL
            WHERE id = ?
            """,
            (relative_path, json.dumps(analysis, ensure_ascii=False), project_id),
        )
    await broadcast_ad_project(project_id)
    return ad_project_detail(project_id)


async def create_ad_plan_version(
    project_id: str, *, feedback: str | None = None, from_segment: int | None = None
) -> dict[str, Any]:
    project = ad_project_detail(project_id)
    if project["status"] not in {"draft", "planning", "waiting_user_confirmation"}:
        raise HTTPException(status_code=409, detail="The approved project can no longer revise its plan")
    prior_plan = project["plans"][0]["plan"] if project["plans"] else None
    asset_summary = [{"index": index} for index, _ in enumerate(project["assets"])]
    asset_image_paths = ad_asset_image_paths(project)
    reference_video_frames = await ad_reference_video_frame_paths(project)
    target_duration = int(project["target_duration_seconds"])
    recommended_segment_count = ad_segment_count(target_duration)
    instruction = {
        "brief": project["brief"],
        "target_duration_seconds": target_duration,
        "voice_enabled": project["voice_enabled"],
        "voice_id": project["voice_id"],
        "voiceover_duration_guidance": (
            ad_voiceover_duration_guidance(target_duration)
            if project["voice_enabled"]
            else {
                "duration_seconds": target_duration,
                "instruction": "Voiceover is disabled. Return an empty voiceover_script.",
            }
        ),
        "assets": asset_summary,
        "asset_image_order": (
            "The first attached images are product assets in the same order as the "
            "asset indexes. Inspect their visible product, packaging, person, and scene "
            "details. Do not infer anything from file names."
            if asset_image_paths
            else "No product reference images are attached."
        ),
        "reference_video_frame_count": len(reference_video_frames),
        "reference_video_frame_order": (
            "The final attached images are key frames from the optional reference video. "
            "Use them together with reference_video_analysis only for high-level visual "
            "language, pacing, and camera rhythm. Do not copy identifiable people, "
            "branding, text, exact scenes, or shot sequence."
            if reference_video_frames
            else "No reference video is attached."
        ),
        "reference_video_analysis": project.get("reference_analysis"),
        "previous_plan": prior_plan,
        "user_feedback": feedback,
        "segment_count_guidance": {
            "recommended_count": recommended_segment_count,
            "recommended_segment_seconds": "2 to 10 seconds, varied by narrative beat",
        },
        "constraints": [
            "Do not use a video model. This is planning only.",
            (
                f"Use approximately {recommended_segment_count} short shots. "
                "Vary shot durations according to the advertising narrative rather than "
                "splitting them evenly. The segment durations must add up exactly to the requested duration."
            ),
            (
                "Use only the asset_index values supplied."
                if asset_summary
                else "No images were supplied. Set asset_index to -1 for every segment and write self-contained text-to-video prompts."
            ),
        ],
    }
    with database() as connection:
        connection.execute("UPDATE ad_projects SET status = 'planning', error_message = NULL WHERE id = ?", (project_id,))
    try:
        plan = await request_ad_llm(
            system_prompt=load_ad_prompt("planner_v1.md"),
            user_prompt=json.dumps(instruction, ensure_ascii=False),
            image_paths=asset_image_paths + reference_video_frames,
        )
    except RuntimeError:
        plan = fallback_ad_plan(project, len(project["assets"]))
    plan = normalize_ad_plan(plan, project, len(project["assets"]))
    if from_segment is not None and prior_plan:
        prefix_count = from_segment - 1
        prior_segments = list(prior_plan.get("segments", []))
        if prefix_count >= len(prior_segments):
            raise HTTPException(
                status_code=422,
                detail="The selected restart segment is outside the current plan",
            )
        prefix = [dict(segment) for segment in prior_segments[:prefix_count]]
        suffix = [dict(segment) for segment in plan["segments"][prefix_count:]]
        if not suffix:
            suffix = [dict(segment) for segment in prior_segments[prefix_count:]]
        remaining_duration = int(project["target_duration_seconds"]) - sum(
            int(segment["duration_seconds"]) for segment in prefix
        )
        for segment, seconds in zip(
            suffix,
            rebalance_ad_durations(
                [int(segment["duration_seconds"]) for segment in suffix],
                remaining_duration,
            ),
        ):
            segment["duration_seconds"] = seconds
        plan["segments"] = prefix + suffix
        plan["replan_from_sequence"] = from_segment
    with database() as connection:
        latest = connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS value FROM ad_plans WHERE project_id = ?", (project_id,)
        ).fetchone()["value"]
        version = int(latest) + 1
        plan_id = uuid.uuid4().hex
        connection.execute(
            """
            INSERT INTO ad_plans (
              id, project_id, version, plan_json, voiceover_script, post_caption,
              hashtags_json, prompt_bundle_version, parent_plan_id,
              replan_from_sequence, revision_note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'v1', ?, ?, ?, ?)
            """,
            (
                plan_id,
                project_id,
                version,
                json.dumps(plan, ensure_ascii=False),
                plan["voiceover_script"],
                plan["post_caption"],
                json.dumps(plan["hashtags"], ensure_ascii=False),
                project["plans"][0]["id"] if from_segment and project["plans"] else None,
                from_segment,
                feedback if from_segment else None,
                now(),
            ),
        )
        connection.execute(
            "UPDATE ad_projects SET status = 'waiting_user_confirmation' WHERE id = ?",
            (project_id,),
        )
    await broadcast_ad_project(project_id)
    return ad_project_detail(project_id)


@app.post("/api/ad-projects/{project_id}/plan")
async def create_ad_plan(project_id: str) -> dict[str, Any]:
    return await create_ad_plan_version(project_id)


@app.post("/api/ad-projects/{project_id}/plan-feedback")
async def revise_ad_plan(
    project_id: str, request: AdPlanFeedbackRequest
) -> dict[str, Any]:
    return await create_ad_plan_version(project_id, feedback=request.feedback)


@app.post("/api/ad-projects/{project_id}/plan-from-segment")
async def revise_ad_plan_from_segment(
    project_id: str, request: AdPlanFromSegmentRequest
) -> dict[str, Any]:
    return await create_ad_plan_version(
        project_id,
        feedback=request.feedback,
        from_segment=request.from_segment,
    )


@app.post("/api/ad-projects/{project_id}/plan-prompts")
async def update_ad_plan_prompts(
    project_id: str, request: AdPlanPromptUpdateRequest
) -> dict[str, Any]:
    project = ad_project_detail(project_id)
    if project["status"] != "waiting_user_confirmation":
        raise HTTPException(status_code=409, detail="Prompts can only be changed before plan approval")
    plan_record = next(
        (item for item in project["plans"] if item["version"] == request.version), None
    )
    if plan_record is None:
        raise HTTPException(status_code=404, detail="Plan version was not found")
    plan = dict(plan_record["plan"])
    segments = plan.get("segments")
    if not isinstance(segments, list) or len(segments) != len(request.prompts):
        raise HTTPException(status_code=422, detail="Prompt count must match the current segment count")

    updated_segments: list[dict[str, Any]] = []
    for index, (segment, prompt) in enumerate(zip(segments, request.prompts), start=1):
        clean_prompt = prompt.strip()
        if len(clean_prompt) < 5:
            raise HTTPException(
                status_code=422,
                detail=f"Segment {index} prompt must contain at least 5 characters",
            )
        if len(clean_prompt) > 3000:
            raise HTTPException(
                status_code=422,
                detail=f"Segment {index} prompt must not exceed 3000 characters",
            )
        updated_segments.append({**segment, "prompt": clean_prompt})
    plan["segments"] = updated_segments
    with database() as connection:
        connection.execute(
            "UPDATE ad_plans SET plan_json = ? WHERE id = ?",
            (json.dumps(plan, ensure_ascii=False), plan_record["id"]),
        )
    await broadcast_ad_project(project_id)
    return ad_project_detail(project_id)


@app.post("/api/ad-projects/{project_id}/plan-prompt-rewrite")
async def rewrite_ad_segment_prompt(
    project_id: str, request: AdSegmentPromptRewriteRequest
) -> dict[str, Any]:
    project = ad_project_detail(project_id)
    if project["status"] != "waiting_user_confirmation":
        raise HTTPException(status_code=409, detail="Prompts can only be changed before plan approval")
    plan_record = next(
        (item for item in project["plans"] if item["version"] == request.version), None
    )
    if plan_record is None:
        raise HTTPException(status_code=404, detail="Plan version was not found")
    plan = dict(plan_record["plan"])
    segments = plan.get("segments")
    if not isinstance(segments, list) or request.segment_index >= len(segments):
        raise HTTPException(status_code=422, detail="Segment index is outside the current plan")

    current_segment = dict(segments[request.segment_index])
    context_start = max(0, request.segment_index - 2)
    context_end = min(len(segments), request.segment_index + 3)
    segment_context = [
        {
            "segment_index": index + 1,
            "position": (
                "current"
                if index == request.segment_index
                else "previous"
                if index < request.segment_index
                else "next"
            ),
            "purpose": str(segment.get("purpose", "")),
            "motion": str(segment.get("motion", "")),
            "duration_seconds": segment.get("duration_seconds"),
            "prompt": (
                request.current_prompt.strip()
                if index == request.segment_index
                else str(segment.get("prompt", ""))
            ),
        }
        for index, segment in enumerate(segments[context_start:context_end], start=context_start)
    ]
    context_asset_indexes = list(dict.fromkeys(
        int(segment.get("asset_index", -1))
        for segment in segments[context_start:context_end]
        if isinstance(segment.get("asset_index"), int) and segment.get("asset_index", -1) >= 0
    ))
    context_asset_images = ad_asset_image_paths(project, context_asset_indexes)
    reference_video_frames = await ad_reference_video_frame_paths(project)
    try:
        result = await request_ad_llm(
            system_prompt=load_ad_prompt("segment_prompt_rewriter_v1.md"),
            user_prompt=json.dumps(
                {
                    "brief": project["brief"],
                    "overall_plan": {
                        "title": plan.get("title", ""),
                        "strategy": plan.get("strategy", ""),
                    },
                    "segment_index": request.segment_index + 1,
                    "segment": {
                        **current_segment,
                        "prompt": request.current_prompt.strip(),
                    },
                    "nearby_segment_context": segment_context,
                    "asset_image_order": (
                        "The first attached images are product assets in this asset index "
                        f"order: {context_asset_indexes}. Inspect the matching images and "
                        "do not infer product details from file names."
                    ),
                    "reference_video_frame_count": len(reference_video_frames),
                    "reference_video_frame_order": (
                        "The final attached images are optional reference-video key frames. "
                        "Use them only for high-level visual language and pacing."
                        if reference_video_frames
                        else "No reference-video key frames are attached."
                    ),
                    "user_instruction": request.instruction.strip(),
                },
                ensure_ascii=False,
            ),
            image_paths=context_asset_images + reference_video_frames,
        )
    except RuntimeError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    rewritten_prompt = str(result.get("prompt", "")).strip()
    if len(rewritten_prompt) < 5:
        raise HTTPException(status_code=422, detail="Advertising language model returned no usable prompt")
    if len(rewritten_prompt) > 3000:
        rewritten_prompt = rewritten_prompt[:3000]

    updated_segments = [dict(segment) for segment in segments]
    updated_segments[request.segment_index]["prompt"] = rewritten_prompt
    plan["segments"] = updated_segments
    with database() as connection:
        connection.execute(
            "UPDATE ad_plans SET plan_json = ? WHERE id = ?",
            (json.dumps(plan, ensure_ascii=False), plan_record["id"]),
        )
    await broadcast_ad_project(project_id)
    return ad_project_detail(project_id)


@app.post("/api/ad-projects/{project_id}/plan-prompts-rewrite")
async def rewrite_ad_plan_prompts(
    project_id: str, request: AdPlanPromptRewriteRequest
) -> dict[str, Any]:
    project = ad_project_detail(project_id)
    if project["status"] != "waiting_user_confirmation":
        raise HTTPException(status_code=409, detail="Prompts can only be changed before plan approval")
    plan_record = next(
        (item for item in project["plans"] if item["version"] == request.version), None
    )
    if plan_record is None:
        raise HTTPException(status_code=404, detail="Plan version was not found")
    plan = dict(plan_record["plan"])
    segments = plan.get("segments")
    if not isinstance(segments, list) or not segments:
        raise HTTPException(status_code=422, detail="The current plan has no segments")
    asset_image_paths = ad_asset_image_paths(project)
    reference_video_frames = await ad_reference_video_frame_paths(project)
    try:
        result = await request_ad_llm(
            system_prompt=load_ad_prompt("plan_prompt_rewriter_v1.md"),
            user_prompt=json.dumps(
                {
                    "brief": project["brief"],
                    "overall_plan": {
                        "title": plan.get("title", ""),
                        "strategy": plan.get("strategy", ""),
                    },
                    "segments": [
                        {
                            "segment_index": index + 1,
                            "purpose": str(segment.get("purpose", "")),
                            "motion": str(segment.get("motion", "")),
                            "duration_seconds": segment.get("duration_seconds"),
                            "prompt": str(segment.get("prompt", "")),
                        }
                        for index, segment in enumerate(segments)
                    ],
                    "asset_image_order": (
                        "The first attached images are product assets in the same order as "
                        "project asset indexes. Inspect them and preserve visible details; "
                        "do not infer product details from file names."
                    ),
                    "reference_video_frame_count": len(reference_video_frames),
                    "reference_video_frame_order": (
                        "The final attached images are optional reference-video key frames. "
                        "Use them only for high-level visual language and pacing."
                        if reference_video_frames
                        else "No reference-video key frames are attached."
                    ),
                    "user_instruction": request.instruction.strip(),
                },
                ensure_ascii=False,
            ),
            image_paths=asset_image_paths + reference_video_frames,
        )
    except RuntimeError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    prompts = result.get("prompts")
    if not isinstance(prompts, list) or len(prompts) != len(segments):
        raise HTTPException(
            status_code=422,
            detail="Advertising language model returned a prompt count that does not match the segment count",
        )

    updated_segments: list[dict[str, Any]] = []
    for index, (segment, prompt) in enumerate(zip(segments, prompts), start=1):
        clean_prompt = str(prompt).strip()
        if len(clean_prompt) < 5:
            raise HTTPException(
                status_code=422,
                detail=f"Advertising language model returned no usable prompt for segment {index}",
            )
        updated_segments.append({**segment, "prompt": clean_prompt[:3000]})
    plan["segments"] = updated_segments
    with database() as connection:
        connection.execute(
            "UPDATE ad_plans SET plan_json = ? WHERE id = ?",
            (json.dumps(plan, ensure_ascii=False), plan_record["id"]),
        )
    await broadcast_ad_project(project_id)
    return ad_project_detail(project_id)


@app.post("/api/ad-projects/{project_id}/plan-approve")
async def approve_ad_plan(
    project_id: str, request: AdPlanApprovalRequest
) -> dict[str, Any]:
    project = ad_project_detail(project_id)
    plan = next((item for item in project["plans"] if item["version"] == request.version), None)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan version was not found")
    if project["status"] != "waiting_user_confirmation":
        raise HTTPException(status_code=409, detail="Project is not awaiting plan confirmation")
    provider = get_provider(
        project.get("video_provider_id", DEFAULT_PROVIDER_ID),
        "image_to_video" if project["assets"] else "text_to_video",
    )
    ensure_payment_confirmation(provider, request.payment_confirmed)
    confirmed_at = now()
    with database() as connection:
        connection.execute(
            "UPDATE ad_plans SET approved_at = ? WHERE id = ?", (confirmed_at, plan["id"])
        )
        connection.execute(
            "UPDATE ad_projects SET status = 'approved', approved_plan_version = ?, plan_approved_at = ?, error_message = NULL WHERE id = ?",
            (request.version, confirmed_at, project_id),
        )
    await broadcast_ad_project(project_id)
    start_ad_project_task(project_id)
    return ad_project_detail(project_id)


@app.post("/api/ad-projects/{project_id}/generate")
async def generate_approved_ad(
    project_id: str, request: AdGenerationRequest
) -> dict[str, Any]:
    project = ad_project_detail(project_id)
    if not project.get("approved_plan_version") or not project.get("plan_approved_at"):
        raise HTTPException(status_code=409, detail="Confirm a plan before generating video")
    if project["status"] not in {"approved", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Project is already running or completed")
    provider = get_provider(
        project.get("video_provider_id", DEFAULT_PROVIDER_ID),
        "image_to_video" if project["assets"] else "text_to_video",
    )
    ensure_payment_confirmation(provider, request.payment_confirmed)
    plan = next(
        (
            item
            for item in project["plans"]
            if item["version"] == project["approved_plan_version"]
        ),
        None,
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="Approved plan was not found")
    failed_sequences = [
        int(segment["sequence_number"])
        for segment in project["segments"]
        if segment["plan_id"] == plan["id"] and segment["status"] != "succeeded"
    ]
    resume_from_sequence = min(failed_sequences) if failed_sequences else 1
    with database() as connection:
        connection.execute(
            """
            INSERT INTO ad_recovery_attempts (
              id, project_id, plan_id, resume_from_sequence, status, created_at
            ) VALUES (?, ?, ?, ?, 'queued', ?)
            """,
            (
                uuid.uuid4().hex,
                project_id,
                plan["id"],
                resume_from_sequence,
                now(),
            ),
        )
    start_ad_project_task(project_id)
    return ad_project_detail(project_id)


@app.get("/api/ad-projects/{project_id}/voices")
async def list_ad_voices(project_id: str) -> dict[str, Any]:
    ad_project_detail(project_id)
    return {"provider": "edge-tts", "voices": AD_VOICES}


@app.post("/api/ad-projects/{project_id}/voice-preview")
async def create_ad_voice_preview(
    project_id: str, request: AdVoicePreviewRequest
) -> dict[str, Any]:
    ad_project_detail(project_id)
    if request.voice_id not in {item["id"] for item in AD_VOICES}:
        raise HTTPException(status_code=422, detail="Unsupported voice")
    preview_dir = AD_MEDIA_DIR / project_id / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    target = preview_dir / f"{request.voice_id}.mp3"
    try:
        await edge_tts.Communicate(
            "你好，这是一段广告配音试听。清晰表达产品卖点，让内容更有吸引力。",
            voice=request.voice_id,
        ).save(str(target))
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Voice preview failed: {error}") from error
    return {"url": f"/media/ad/{project_id}/previews/{request.voice_id}.mp3"}


@app.get("/api/ad-projects/{project_id}/bgm")
async def list_project_bgm(project_id: str) -> dict[str, Any]:
    ad_project_detail(project_id)
    tracks = list_ad_bgm()
    return {
        "tracks": [
            {key: value for key, value in track.items() if key != "path"}
            for track in tracks
        ],
        "default_id": "default/ambient",
    }


@app.get("/api/ad-projects/{project_id}/bgm/{bgm_id:path}/audio")
async def project_bgm_audio(project_id: str, bgm_id: str) -> FileResponse:
    ad_project_detail(project_id)
    path = resolve_ad_bgm(bgm_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Background music was not found")
    return FileResponse(path, media_type="audio/mpeg", filename=path.name)


@app.post("/api/ad-projects/{project_id}/final-copy")
async def rewrite_ad_final_copy(
    project_id: str, request: AdFinalCopyRequest
) -> dict[str, Any]:
    project = ad_project_detail(project_id)
    if project["status"] != "completed":
        raise HTTPException(status_code=409, detail="Complete video generation before rewriting final copy")
    version = project.get("approved_plan_version")
    plan_record = next(
        (item for item in project["plans"] if item["version"] == version), None
    )
    if plan_record is None:
        raise HTTPException(status_code=404, detail="Approved plan was not found")
    try:
        return await write_ad_final_copy(project, plan_record["plan"], request.instruction)
    except RuntimeError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/ad-projects/{project_id}/final-edit")
async def edit_ad_final(
    project_id: str, request: AdFinalEditRequest
) -> dict[str, Any]:
    project = ad_project_detail(project_id)
    if project["status"] != "completed":
        raise HTTPException(status_code=409, detail="Complete video generation before editing the final audio and copy")
    version = project.get("approved_plan_version")
    plan_record = next(
        (item for item in project["plans"] if item["version"] == version), None
    )
    if plan_record is None:
        raise HTTPException(status_code=404, detail="Approved plan was not found")
    if request.bgm_id is not None and request.bgm_enabled is not False:
        if resolve_ad_bgm(request.bgm_id) is None:
            raise HTTPException(status_code=422, detail="Selected background music was not found")
    if request.voice_id is not None and request.voice_id not in {voice["id"] for voice in AD_VOICES}:
        raise HTTPException(status_code=422, detail="Unsupported voice")

    plan = dict(plan_record["plan"])
    if request.voiceover_script is not None:
        plan["voiceover_script"] = request.voiceover_script.strip()
    if request.post_caption is not None:
        plan["post_caption"] = request.post_caption.strip()
    if request.hashtags is not None:
        plan["hashtags"] = [item.strip() for item in request.hashtags if item.strip()][:10]
    if request.voice_enabled is True and not plan.get("voiceover_script"):
        raise HTTPException(status_code=422, detail="Voiceover text is required when voice is enabled")

    project_updates = {
        "voice_enabled": int(request.voice_enabled) if request.voice_enabled is not None else project["voice_enabled"],
        "subtitle_enabled": int(request.subtitle_enabled) if request.subtitle_enabled is not None else project["subtitle_enabled"],
        "bgm_enabled": int(request.bgm_enabled) if request.bgm_enabled is not None else project["bgm_enabled"],
        "bgm_id": request.bgm_id if request.bgm_id is not None else project.get("bgm_id", "default/ambient"),
        "voice_id": request.voice_id if request.voice_id is not None else project["voice_id"],
    }
    with database() as connection:
        connection.execute(
            """
            UPDATE ad_projects
            SET voice_enabled = ?, subtitle_enabled = ?, bgm_enabled = ?, bgm_id = ?, voice_id = ?, error_message = NULL
            WHERE id = ?
            """,
            (
                project_updates["voice_enabled"],
                project_updates["subtitle_enabled"],
                project_updates["bgm_enabled"],
                project_updates["bgm_id"],
                project_updates["voice_id"],
                project_id,
            ),
        )
        connection.execute(
            """
            UPDATE ad_plans
            SET plan_json = ?, voiceover_script = ?, post_caption = ?, hashtags_json = ?
            WHERE id = ?
            """,
            (
                json.dumps(plan, ensure_ascii=False),
                plan.get("voiceover_script", ""),
                plan.get("post_caption", ""),
                json.dumps(plan.get("hashtags", []), ensure_ascii=False),
                plan_record["id"],
            ),
        )
    await broadcast_ad_project(project_id)
    start_ad_recompose_task(project_id)
    return ad_project_detail(project_id)


@app.post("/api/ad-projects/{project_id}/stop")
async def stop_ad_project(project_id: str) -> dict[str, Any]:
    ad_project_detail(project_id)
    task = ad_project_tasks.get(project_id)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    with database() as connection:
        generation_rows = connection.execute(
            """
            SELECT generation_id FROM ad_segments
            WHERE project_id = ? AND generation_id IS NOT NULL
            """,
            (project_id,),
        ).fetchall()
    for row in generation_rows:
        generation_task = generation_tasks.get(row["generation_id"])
        if generation_task and not generation_task.done():
            generation_task.cancel()
    await set_ad_project_state(project_id, "cancelled", error_message="Stopped by user")
    return ad_project_detail(project_id)


@app.post("/api/ad-projects/{project_id}/return-to-plan")
async def return_ad_project_to_plan(project_id: str) -> dict[str, Any]:
    project = ad_project_detail(project_id)
    if project["status"] not in {
        "approved",
        "generating_segments",
        "reviewing_segments",
        "composing_audio_video",
        "failed",
        "cancelled",
    }:
        raise HTTPException(status_code=409, detail="Project is not in a state that can return to its plan")

    task = ad_project_tasks.get(project_id)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    with database() as connection:
        rows = connection.execute(
            "SELECT generation_id FROM ad_segments WHERE project_id = ? AND generation_id IS NOT NULL",
            (project_id,),
        ).fetchall()
    for row in rows:
        generation_task = generation_tasks.get(row["generation_id"])
        if generation_task and not generation_task.done():
            generation_task.cancel()
    await asyncio.gather(
        *(
            interrupt_comfy_provider(provider)
            for provider in PROVIDERS.values()
            if provider.enabled and provider.kind == "comfyui"
        ),
        return_exceptions=True,
    )
    with database() as connection:
        connection.execute(
            """
            UPDATE ad_projects
            SET status = 'waiting_user_confirmation',
                approved_plan_version = NULL,
                plan_approved_at = NULL,
                error_message = NULL,
                completed_at = NULL
            WHERE id = ?
            """,
            (project_id,),
        )
    await broadcast_ad_project(project_id)
    return ad_project_detail(project_id)


@app.get("/api/generations")
async def list_generations() -> list[dict[str, Any]]:
    with database() as connection:
        rows = connection.execute(
            "SELECT * FROM generations ORDER BY created_at DESC"
        ).fetchall()
    return [row_to_dict(row) for row in rows]


@app.get("/api/generations/{generation_id}")
async def generation_detail(generation_id: str) -> dict[str, Any]:
    return get_generation(generation_id)


@app.post("/api/assets")
async def create_asset(file: UploadFile = File(...)) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(status_code=415, detail="Only PNG, JPG, JPEG, and WebP are supported")
    asset_id = uuid.uuid4().hex
    filename = f"{asset_id}{suffix}"
    stored_path = UPLOAD_DIR / filename
    stored_path.write_bytes(await file.read())
    with database() as connection:
        connection.execute(
            "INSERT INTO assets (id, filename, stored_path, mime_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (asset_id, file.filename or filename, str(stored_path), file.content_type or "", now()),
        )
    return {"id": asset_id, "filename": file.filename or filename}


@app.post("/api/generations")
async def create_generation(request: CreateGenerationRequest) -> dict[str, Any]:
    mode: Literal["text", "image"] = "image" if request.reference_asset_id else "text"
    capability = "image_to_video" if mode == "image" else "text_to_video"
    provider = get_provider(request.provider_id, capability)
    resolution = provider_resolution(provider, request.resolution)
    ensure_payment_confirmation(provider, request.payment_confirmed)
    seed = request.seed if request.seed is not None else int.from_bytes(os.urandom(8), "big")
    generation = make_generation(
        mode=mode,
        prompt=request.prompt,
        negative_prompt=request.negative_prompt,
        reference_asset_id=request.reference_asset_id,
        config={
            "width": request.width,
            "height": request.height,
            "resolution": resolution,
            "length": request.length,
            "fps": request.fps,
            "seed": seed,
            "provider_id": provider.id,
            "provider_model": provider.model,
        },
    )
    start_generation_task(generation["id"])
    return generation


@app.post("/api/generations/{generation_id}/edits")
async def edit_generation(
    generation_id: str, request: EditGenerationRequest
) -> dict[str, Any]:
    parent = get_generation(generation_id)
    if parent["status"] != "succeeded":
        raise HTTPException(status_code=409, detail="Only a completed video can be edited")

    provider_id = request.provider_id or parent["config"].get(
        "provider_id", DEFAULT_PROVIDER_ID
    )
    provider = get_provider(provider_id, "video_edit")
    ensure_payment_confirmation(provider, request.payment_confirmed)
    try:
        edit_spec = await request_edit_spec(request.instruction)
    except asyncio.CancelledError as error:
        raise HTTPException(status_code=409, detail="Edit parsing was stopped") from error
    config = parent["config"]
    seed = request.seed if request.seed is not None else config["seed"]
    generation = make_generation(
        mode="edit",
        parent_generation_id=generation_id,
        prompt=request.instruction,
        negative_prompt=request.negative_prompt,
        edit_spec=edit_spec,
        config={
            **config,
            "seed": seed,
            "provider_id": provider.id,
            "provider_model": provider.model,
        },
    )
    start_generation_task(generation["id"])
    return generation


@app.post("/api/generations/{generation_id}/continuations")
async def continue_generation(
    generation_id: str, request: ContinueGenerationRequest
) -> dict[str, Any]:
    parent = get_generation(generation_id)
    if parent["status"] != "succeeded":
        raise HTTPException(status_code=409, detail="Only a completed video can be continued")
    if request.tail_frames >= request.length:
        raise HTTPException(
            status_code=422,
            detail="tail_frames must be smaller than the new video length",
        )

    provider_id = request.provider_id or parent["config"].get(
        "provider_id", DEFAULT_PROVIDER_ID
    )
    provider = get_provider(provider_id)
    if not provider_supports_continuation(provider):
        raise HTTPException(
            status_code=422,
            detail=f"Provider {provider_id} does not support video continuation",
        )
    ensure_payment_confirmation(provider, request.payment_confirmed)
    config = parent["config"]
    seed = request.seed if request.seed is not None else int.from_bytes(
        os.urandom(8), "big"
    )
    generation = make_generation(
        mode="continue",
        parent_generation_id=generation_id,
        prompt=request.prompt,
        negative_prompt=request.negative_prompt,
        config={
            **config,
            "length": request.length,
            "fps": request.fps,
            "tail_frames": request.tail_frames,
            "seed": seed,
            "provider_id": provider.id,
            "provider_model": provider.model,
        },
    )
    start_generation_task(generation["id"])
    return generation


@app.websocket("/api/events")
async def events(websocket: WebSocket) -> None:
    await websocket.accept()
    event_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        event_clients.discard(websocket)
