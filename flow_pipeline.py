import asyncio
import base64
import ctypes
import difflib
import json
import math
import ntpath
import hashlib
import logging
import os
import random
import re
import shutil
import struct
import subprocess
import threading
import tempfile
import time
import urllib.parse
import urllib.request
import unicodedata
import numpy as np
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from queue import Queue, Empty
from typing import Any

import psutil
import requests
from PIL import Image, ImageOps
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from adguard_rotator import rotate_adguard_windows_vpn
PIPELINE_VERSION = "2026-11-27-UNIFIED-MASTERING-CLEAN-EXPORT-V75"
FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

# ============================================================================
# CONFIG
# ============================================================================

APP_DIR = Path(__file__).resolve().parent
GUI_SETTINGS_FILE = APP_DIR / "flow_pipeline_gui_settings.json"
ACTIVE_PROJECT_MARKER_FILE = APP_DIR / "flow_pipeline_active_project.json"

INPUT_ROOT = Path(r"C:\TRANSLATE")
OUTPUT_ROOT = Path(r"H:\Мой диск\CONTENT")
FON_AUDIO_DIR = Path(r"J:\slideshow_project\fon_audio")

STATE_DIR_NAME = "_flow_state"
MEDIA_DIR_NAME = "_flow_media"
WORK_DIR_NAME = "_flow_work"
CHARACTER_REFERENCE_DIR_NAME = "character_references"
CHANNEL_HERO_REFERENCE_DIR_NAME = "etalon"
SUPPORTED_REFERENCE_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MAX_PRIMARY_CHARACTER_REFERENCES = 10
SCENE_PLAN_VERSION = "contextual-action-priority-v6-safe-children-pregnancy-adaptive-model-split-retry"
# A storyboard batch is never silently completed with local guesswork. Gemini/proxy
# outages are retried with resource-aware backoff; malformed short answers rotate models
# and split only missing clip IDs into smaller requests. Exhaustion stops safely.
SCENE_PLAN_MAX_API_ATTEMPTS = 12
SCENE_PLAN_RETRY_BASE_SECONDS = 15.0
SCENE_PLAN_RETRY_MAX_BACKOFF_SECONDS = 180.0
SCENE_PLAN_FORMAT_RETRIES_BEFORE_SPLIT = 2
SCENE_PLAN_MIN_BATCH_SIZE = 3
SCENE_PLAN_FORMAT_RETRY_DELAY_SECONDS = 1.5
# Channel-specific override: never affects other channels/etalon images.
CHANNEL_HERO_SKIN_OVERRIDES = {
    # The channel folder is named either "Mateo" or "Mateo Navarro" depending on the user's setup.
    # Exact normalized matching is used below; unrelated niches are not affected.
    "mateo": "PURE WHITE STYLIZED SKIN DESIGN, base fill sRGB #FFFFFF (RGB 255,255,255), not a natural human complexion; every visible area of bare skin on face, ears, neck, shoulders, arms, hands, legs, knees, feet, and any exposed torso/chest stays white; only very light neutral-grey shading (#F0F0F0) and dark linework are allowed; never use peach, pink, beige, tan, orange, brown, blue, cyan, teal, navy, violet, purple or saturated shadow colours on skin; lighting and colour grading must never recolour the skin",
    "mateo navarro": "PURE WHITE STYLIZED SKIN DESIGN, base fill sRGB #FFFFFF (RGB 255,255,255), not a natural human complexion; every visible area of bare skin on face, ears, neck, shoulders, arms, hands, legs, knees, feet, and any exposed torso/chest stays white; only very light neutral-grey shading (#F0F0F0) and dark linework are allowed; never use peach, pink, beige, tan, orange, brown, blue, cyan, teal, navy, violet, purple or saturated shadow colours on skin; lighting and colour grading must never recolour the skin",
}


def normalize_channel_skin_key(value: str) -> str:
    """Normalize a channel folder name for exact skin-override lookup."""
    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", without_marks).strip()


def get_channel_hero_skin_override(channel_name: str) -> str:
    """Return an explicitly configured channel-only skin rule; never partial-match other niches."""
    wanted = normalize_channel_skin_key(channel_name)
    for configured_name, prompt in CHANNEL_HERO_SKIN_OVERRIDES.items():
        if normalize_channel_skin_key(configured_name) == wanted:
            return str(prompt or "").strip()
    return ""

# ------------------------------------------------------------
# Flow browser
# ------------------------------------------------------------

FLOW_WINDOW_WIDTH = 1020
FLOW_WINDOW_HEIGHT = 780
# Normal on-screen fallback coordinates (used when off-screen mode is disabled).
FLOW_WINDOW_X = 1525
FLOW_WINDOW_Y = 605
# Keep the active Chrome window mostly outside the primary monitor while leaving
# a draggable piece of its top strip visible in the lower-right corner.
# Set mode to "normal" to restore the regular FLOW_WINDOW_X/FLOW_WINDOW_Y position.
FLOW_WINDOW_OFFSCREEN_MODE = "bottom_right"  # "bottom_right" or "normal"
FLOW_WINDOW_VISIBLE_GRAB_WIDTH = 300
FLOW_WINDOW_VISIBLE_GRAB_HEIGHT = 60

CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

FLOW_HOME_URL = "https://flow.google.com/"

# Slow-VPN startup: do not treat the navigation lifecycle event as proof that
# the Flow web app is ready. Wait for the actual Flow document/UI separately.
FLOW_PAGE_NAVIGATION_TIMEOUT_MS = 180_000
FLOW_PAGE_NAVIGATION_ATTEMPTS = 2
FLOW_PAGE_READY_TIMEOUT_SECONDS = 180.0
FLOW_PAGE_READY_POLL_SECONDS = 1.0
FLOW_PROJECT_LIST_WAIT_SECONDS = 120.0
FLOW_PROMPT_EDITOR_WAIT_SECONDS = 120.0
FLOW_PROMPT_BOX_WAIT_SECONDS = 90.0

FLOW_PROFILES = [
    # Все профили имеют уникальные user_data_dir и CDP-порты; все включённые профили поддерживают изображения и видео.
    {"name": "pro_01", "user_data_dir": r"C:\FlowAccounts\pro_01", "port": 9333, "enabled": True, "supports_video": True},
    {"name": "pro_02", "user_data_dir": r"C:\FlowAccounts\pro_02", "port": 9334, "enabled": True, "supports_video": True},
    {"name": "pro_03", "user_data_dir": r"C:\FlowAccounts\pro_03", "port": 9335, "enabled": True, "supports_video": True},
    {"name": "pro_04", "user_data_dir": r"C:\FlowAccounts\pro_04", "port": 9336, "enabled": True, "supports_video": True},
    {"name": "pro_01_test", "user_data_dir": r"C:\FlowAccounts\pro_01_test", "port": 9341, "enabled": True, "supports_video": True},
    {"name": "pro_01_clean", "user_data_dir": r"C:\FlowAccounts\pro_01_clean", "port": 9342, "enabled": True, "supports_video": True},
    {"name": "pro_02_clean", "user_data_dir": r"C:\FlowAccounts\pro_02_clean", "port": 9344, "enabled": True, "supports_video": True},
    {"name": "pro_06", "user_data_dir": r"C:\FlowAccounts\pro_06", "port": 9345, "enabled": True, "supports_video": True},
    {"name": "pro_07", "user_data_dir": r"C:\FlowAccounts\pro_07", "port": 9346, "enabled": True, "supports_video": True},
    {"name": "pro_08", "user_data_dir": r"C:\FlowAccounts\pro_08", "port": 9347, "enabled": True, "supports_video": True},
    {"name": "pro_05", "user_data_dir": r"C:\FlowAccounts\pro_05", "port": 9337, "enabled": True, "supports_video": False},
    {"name": "pro_09", "user_data_dir": r"C:\FlowAccounts\pro_09", "port": 9348, "enabled": True, "supports_video": True},
    {"name": "pro_10", "user_data_dir": r"C:\FlowAccounts\pro_10", "port": 9349, "enabled": True, "supports_video": True},
    {"name": "pro_11", "user_data_dir": r"C:\FlowAccounts\pro_11", "port": 9350, "enabled": True, "supports_video": True},
    {"name": "pro_12", "user_data_dir": r"C:\FlowAccounts\pro_12", "port": 9351, "enabled": True, "supports_video": True},
    {"name": "pro_13", "user_data_dir": r"C:\FlowAccounts\pro_13", "port": 9352, "enabled": False, "supports_video": False},
    {"name": "pro_14", "user_data_dir": r"C:\FlowAccounts\pro_14", "port": 9353, "enabled": True, "supports_video": True},
    {"name": "pro_15", "user_data_dir": r"C:\FlowAccounts\pro_15", "port": 9354, "enabled": True, "supports_video": True},
    {"name": "pro_16", "user_data_dir": r"C:\FlowAccounts\pro_16", "port": 9355, "enabled": True, "supports_video": True},
    {"name": "pro_17", "user_data_dir": r"C:\FlowAccounts\pro_17", "port": 9356, "enabled": True, "supports_video": True},
    {"name": "pro_18", "user_data_dir": r"C:\FlowAccounts\pro_18", "port": 9357, "enabled": True, "supports_video": True},
    {"name": "pro_19", "user_data_dir": r"C:\FlowAccounts\pro_19", "port": 9358, "enabled": True, "supports_video": True},
    {"name": "pro_20", "user_data_dir": r"C:\FlowAccounts\pro_20", "port": 9359, "enabled": True, "supports_video": False},
    {"name": "pro_21", "user_data_dir": r"C:\FlowAccounts\pro_21", "port": 9360, "enabled": True, "supports_video": True},
    {"name": "pro_22", "user_data_dir": r"C:\FlowAccounts\pro_22", "port": 9361, "enabled": True, "supports_video": True},
    {"name": "pro_23", "user_data_dir": r"C:\FlowAccounts\pro_23", "port": 9362, "enabled": True, "supports_video": True},
    {"name": "pro_24", "user_data_dir": r"C:\FlowAccounts\pro_24", "port": 9363, "enabled": True, "supports_video": True},
    {"name": "pro_25", "user_data_dir": r"C:\FlowAccounts\pro_25", "port": 9364, "enabled": True, "supports_video": True},
    {"name": "pro_26", "user_data_dir": r"C:\FlowAccounts\pro_26", "port": 9365, "enabled": True, "supports_video": False},
    {"name": "pro_27", "user_data_dir": r"C:\FlowAccounts\pro_27", "port": 9366, "enabled": True, "supports_video": True},
    {"name": "pro_28", "user_data_dir": r"C:\FlowAccounts\pro_28", "port": 9367, "enabled": True, "supports_video": True},
    {"name": "pro_29", "user_data_dir": r"C:\FlowAccounts\pro_29", "port": 9368, "enabled": True, "supports_video": True},
    {"name": "pro_30", "user_data_dir": r"C:\FlowAccounts\pro_30", "port": 9369, "enabled": True, "supports_video": True},
]

FLOW_PROXY_PORTS = []  # все окна Flow на одном порту с общим FLOW-POOL

def get_flow_proxy_port(profile: dict[str, Any]) -> int | None:
    if not FLOW_PROXY_PORTS:
        return None
    explicit = profile.get("proxy_port")
    if explicit:
        return int(explicit)
    return int(FLOW_PROXY_PORTS[0])

CLASH_API_URL = "http://127.0.0.1:9097"  # подставьте порт из External Controller
CLASH_API_SECRET = "flow1234"                    # подставьте Secret, если задан
CLASH_FLOW_GROUP = "FLOW-POOL"
CLASH_FLOW_PROXIES = ["MyProxy", "MyProxy2", "MyVDSina"]
_clash_flow_proxy_idx = 0
_clash_flow_lock = threading.Lock()


def rotate_clash_flow_proxy(reason: str = "rate limit") -> str | None:
    """Switch FLOW-POOL to the next proxy in Clash Verge and drop stale Flow sockets."""
    global _clash_flow_proxy_idx
    if not CLASH_FLOW_PROXIES:
        return None

    headers = {"Content-Type": "application/json"}
    if CLASH_API_SECRET:
        headers["Authorization"] = f"Bearer {CLASH_API_SECRET}"
    no_proxy = {"http": None, "https": None}

    with _clash_flow_lock:
        _clash_flow_proxy_idx = (_clash_flow_proxy_idx + 1) % len(CLASH_FLOW_PROXIES)
        target = CLASH_FLOW_PROXIES[_clash_flow_proxy_idx]

    try:
        r = requests.put(
            f"{CLASH_API_URL}/proxies/{CLASH_FLOW_GROUP}",
            headers=headers,
            json={"name": target},
            timeout=5,
            proxies=no_proxy,
        )
        if r.status_code not in (200, 204):
            log.warning("[Clash Rotate] Не удалось переключить %s -> %s (HTTP %s: %s)", CLASH_FLOW_GROUP, target, r.status_code, r.text[:200])
            return None

        # Закрываем только активные keep-alive соединения Flow, чтобы Chrome сразу пошёл через новый IP
        closed = 0
        try:
            conns = requests.get(f"{CLASH_API_URL}/connections", headers=headers, timeout=5, proxies=no_proxy).json().get("connections") or []
            for conn in conns:
                host = str((conn.get("metadata") or {}).get("host", "")).lower()
                if any(dom in host for dom in ("flow.google", "flow-content.google", "labs.google", "aisandbox-pa.googleapis.com")):
                    cid = conn.get("id")
                    if cid:
                        requests.delete(f"{CLASH_API_URL}/connections/{cid}", headers=headers, timeout=3, proxies=no_proxy)
                        closed += 1
        except Exception:
            pass

        log.info("🔄 [Clash Rotate] Переключил %s -> %s (причина: %s; сброшено Flow-соединений: %s)", CLASH_FLOW_GROUP, target, reason, closed)
        return target
    except Exception as exc:
        log.warning("[Clash Rotate] Ошибка обращения к API Clash Verge: %s", exc)
        return None

AUTO_LAUNCH_CHROME = True
MOVE_WINDOW_WHEN_SWITCHING = True

FORCE_FLOW_PROFILE_FOR_TEST = None

# Generation watchdogs. Technical stalls get bounded retries with the same prompt.
# Explicit Flow policy refusals are detected early and routed through Gemini's
# safe-prompt rewrite loop, with a separate maximum of 99 submitted prompt attempts.
FLOW_IMAGE_GENERATION_TIMEOUT = 180
FLOW_VIDEO_GENERATION_TIMEOUT = 360
FLOW_IMAGE_OUTPUT_READY_TIMEOUT = 120
FLOW_VIDEO_OUTPUT_READY_TIMEOUT = 240
FLOW_IMAGE_PRE_RETRY_GRACE_SECONDS = 5
FLOW_IMAGE_PRE_RETRY_SCAN_SECONDS = 7
FLOW_GENERATION_RETRIES_PER_PROFILE = 1  # one retry after the first attempt
FLOW_POLICY_REWRITE_MAX_ATTEMPTS = 99  # maximum prompt submissions while safely revising policy-refused prompts
FLOW_GENERATION_TIMEOUT = FLOW_VIDEO_GENERATION_TIMEOUT  # legacy/default timeout
IMAGE_QUALITY_RETRIES = 1  # regenerate once if the result contains obvious frame bars or is monochrome

# Optional vision-based review is disabled because repeated HTTP 503/timeouts were
# making review unreliable. Safe prompts, Flow/provider safety filters, technical
# image QA, and the separate text-based YouTube preflight remain enabled.
# Set both flags to True only after the vision-review API is confirmed operational.
ENABLE_YOUTUBE_CONTENT_SAFETY_REVIEW = False
YOUTUBE_CONTENT_REVIEW_MODEL = "gemini-3.8-flash"
YOUTUBE_CONTENT_REVIEW_VIDEO_FRAME_FRACTIONS = (0.10, 0.30, 0.50, 0.70, 0.90)
YOUTUBE_CONTENT_SAFETY_RETRIES = 1

# Monetization risk screening is distinct from Community Guidelines screening.
# It produces a project-local JSON report and never claims to predict YouTube's decision.
ENABLE_YOUTUBE_MONETIZATION_PREFLIGHT = True
YOUTUBE_SCRIPT_SIMILARITY_WARNING_THRESHOLD = 0.82
ENABLE_FINAL_VIDEO_SAFETY_REVIEW = False
FLOW_DOWNLOAD_TIMEOUT = 180
FLOW_UPSCALE_DOWNLOAD_TIMEOUT = 100  # images and videos: wait 100s for upscale, then fall back to native/original
FLOW_RATE_LIMIT_RECOVERY_PAUSE_SECONDS = 10.0  # wait after Flow cookie/session reset before retrying a throttled request
FLOW_MENU_RETRIES = 7
FLOW_TECHNICAL_RETRIES_PER_ACCOUNT = 2
FLOW_ACCOUNT_ATTEMPTS = 2  # two profiles max per target
FLOW_MAX_TOTAL_GENERATION_ATTEMPTS = 4  # technical/failover budget; explicit policy rewrites use their own 99-attempt cap
FLOW_SESSION_IMAGE_LIMIT = 20
FLOW_ROTATE_AFTER_EACH_SUCCESS = False  # keep the active profile until its quota for the current media type is reached
FLOW_QUOTA_RECHECK_INTERVAL_SECONDS = 10 * 60  # while waiting for any available profile; never before startup selection
FLOW_QUOTA_RECHECK_ERROR_SECONDS = 3 * 60
FLOW_QUOTA_WAIT_LOG_INTERVAL_SECONDS = 5 * 60
FLOW_SESSION_VIDEO_LIMIT = 10

# Start one fresh Flow Agent conversation for each newly opened Chrome profile,
# using the current History (forum) -> Create new session UI.
ENABLE_FLOW_NEW_SESSION_RESET = True
FLOW_NEW_SESSION_PER_BROWSER_OPEN = True
FLOW_NEW_SESSION_HISTORY_WAIT_SECONDS = 15.0
FLOW_NEW_SESSION_PANEL_POLL_SECONDS = 0.25
FLOW_NEW_SESSION_SETTLE_SECONDS = 0.8
FLOW_NEW_SESSION_UI_VERSION = "history-forum-create-session-v1"
FLOW_PROFILE_USAGE_STATE_FILE = APP_DIR / "flow_profile_session_usage.json"  # persist 20-image/10-video quotas across Python restarts while Chrome stays open

# Transient network failures are retried against the SAME endpoint before a proxy
# is rested. HTTP 403/429/5xx responses are API responses, not proof a proxy is bad.
# Number of total attempts on the same endpoint, including the first request.
# Proxy/connect failures usually fail quickly, so allow more retries. A read timeout
# usually means the endpoint connected but Google/its backend was slow; retry it fewer times.
PROXY_REQUEST_RETRIES = 5
PROXY_READ_TIMEOUT_ATTEMPTS = 2
PROXY_RETRY_BACKOFF_SECONDS = (0.8, 1.5, 2.5, 4.0)
PROXY_TIMEOUT_COOLDOWN_SECONDS = 180  # do not immediately reuse a proxy after a long response timeout
PROXY_CONNECT_COOLDOWN_SECONDS = 60

FLOW_CLIP_SECONDS = 8.0
INTRO_SHOT_COUNT = 8
INTRO_TARGET_SECONDS = FLOW_CLIP_SECONDS * INTRO_SHOT_COUNT  # about 64 seconds at 8s per Flow clip
INTRO_STORYBOARD_VERSION = "floating-intro-8-11-v1-whisper-silence-aligned"
ENABLE_FLOW_INTRO_DEFAULT = True

# ------------------------------------------------------------
# Montage parameters
# ------------------------------------------------------------

FPS = 30
HW_ENCODER = "h264_nvenc"
ENCODER_PRESET = "p5"

VOICE_VOLUME_GAIN = 1.0
MUSIC_VOLUME_RANGE = (0.18, 0.22)
SUBTITLE_FONT_SIZE = 25
SUBTITLE_MARGIN_V = 16
ENABLE_SUBTITLE_KARAOKE = False


# Legitimate creative variation for viewing quality and narrative pacing. These settings
# are not anti-detection measures and do not make repetitive material eligible for YPP.
# Restored visual-variation / Ken Burns parameters from the original montage.
SLIDE_MIN_DUR = 14.0
SLIDE_MAX_DUR = 16.0
AUDIO_PITCH_JITTER = (0.988, 1.012)
AUDIO_TEMPO_JITTER = (0.995, 1.005)
CROP_PERCENT_RANGE = (0.015, 0.045)
FILM_GRAIN_STRENGTH = (1, 2)
ENABLE_PARALLAX_FILM_GRAIN = True
FFMPEG_CLEAN_META_VIDEO = ["-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact"]
FFMPEG_CLEAN_META_AV = ["-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact"]
COLOR_CONTRAST_JITTER = (0.985, 1.025)
COLOR_BRIGHTNESS_JITTER = (-0.012, 0.018)
COLOR_SATURATION_JITTER = (0.96, 1.04)
VIDEO_BITRATE_RANGE = (9000, 11000)
ZOOM_SPEED_FACTOR_RANGE = (0.65, 0.95)
TRANSITION_DURATION_RANGE = (0.25, 0.40)
# Montage-only cross-dissolve between still-image slides. This does not alter
# the motion rendered inside any scene and preserves total timeline duration.
# A 0.30s dissolve is subtle enough for documentary pacing without a black flash.
SLIDE_CROSSFADE_SECONDS = 0.30
SLIDE_CROSSFADE_BATCH_SIZE = 12  # bound decoder count during per-slide-group rendering
TRANSITION_RENDER_VERSION = "semantic-mixed-cut-dissolve-fadeblack-v1"
MONTAGE_TRANSITION_VERSION = TRANSITION_RENDER_VERSION
SEMANTIC_TIMELINE_VERSION = "whisper-llm-pause-aligned-v1"
USE_DYNAMIC_SLIDE_INTERVALS = True

# 2.5D depth parallax is applied only to still-image scenes rendered into slides.
# Intro/Flow/Veo videos are concatenated as videos and are never processed here.
ENABLE_2_5D_PARALLAX = True
PARALLAX_RENDER_VERSION = "midas-2.5d-v18-zoom-only-balanced-in-out-native-source-single-resample-fhd"
PARALLAX_FALLBACK_RENDER_VERSION = "kenburns-fallback-zoom-only-v2"
PARALLAX_RENDER_WIDTH = 1920
PARALLAX_RENDER_HEIGHT = 1080
# Keep source imagery at its common generated-native resolution until the final FHD sample.
PARALLAX_CANVAS_WIDTH = 2752
PARALLAX_CANVAS_HEIGHT = 1536
PARALLAX_DEPTH_MODEL_MAX_SIDE = 960
PARALLAX_DEPTH_SMOOTH_SIGMA = 7.0  # smooth enough to avoid noisy depth boundaries
PARALLAX_DEPTH_RENDER_SMOOTH_SIGMA = 4.7  # preserve broad depth transitions while avoiding local jitter
PARALLAX_DEPTH_CONTRAST = 1.22  # modestly expand relative foreground/background separation
# Slightly stronger depth separation; not a huge jump, so silhouettes stay stable.
PARALLAX_DISPLACEMENT_X = 56.0
PARALLAX_DISPLACEMENT_Y = 28.0
PARALLAX_ZOOM_AMOUNT = 0.225
PARALLAX_PAN_AMOUNT_X = 30.0
PARALLAX_PAN_AMOUNT_Y = 10.0
PARALLAX_LIVING_DEPTH_STRENGTH = 0.34
PARALLAX_GENERAL_DEPTH_STRENGTH = 0.90
PARALLAX_LANDSCAPE_DEPTH_STRENGTH = 1.12
# Gentle, one-way yaw-like depth shift applied on top of zoom. These are deliberately
# small because a single photo has no information about surfaces hidden behind objects.
PARALLAX_CAMERA_TURN_X = 15.0
PARALLAX_CAMERA_TURN_Y = 4.0
# One-direction motion profiles: push-in, pull-out, side pass, diagonal/orbit-like pass.
# The side/orbit-like modes are still a 2.5D approximation, not a true 3D reconstruction.

# MiDaS DPT_Hybrid is loaded once and re-used for all images in this process.
_DEPTH_MODEL = None
_DEPTH_TRANSFORM = None
_DEPTH_DEVICE = None
_DEPTH_MODEL_ERROR: str | None = None
_PARALLAX_FALLBACK_LOGGED = False

# Keep natural environmental ambience from Flow intro clips at approximately 15%
# of its original amplitude beneath the main narration. The gain is a linear multiplier,
# not a percentage of perceived loudness: 0.15 = 15% amplitude (about -16.5 dB).
# The peak limiter is a safety ceiling, not an additional gain stage.
FLOW_INTRO_AUDIO_GAIN = 0.15
FLOW_INTRO_AUDIO_PEAK_LIMIT = 0.15  # cap the ambience peak at 15% full scale
ENABLE_BACKGROUND_MUSIC = False
FLOW_VIDEO_AUDIO_POLICY_MARKER = "FLOW VIDEO AUDIO POLICY —"
FLOW_VIDEO_AUDIO_POLICY = """FLOW VIDEO AUDIO POLICY — Prefer silence or only soft, natural location ambience (quiet wind, leaves, or room tone). No music, speech, singing, whistling, or sharp sound effects."""
# One bounded recovery path for Flow's audio backend failures. After Gemini rewrites
# the visual prompt, the retry asks for a completely silent visual-only clip rather
# than asking Flow to synthesize even ambient audio again.
FLOW_VIDEO_SILENT_AUDIO_POLICY_MARKER = "FLOW VIDEO SILENT AUDIO FALLBACK —"
FLOW_VIDEO_SILENT_AUDIO_POLICY = """FLOW VIDEO SILENT AUDIO FALLBACK — Create a VISUAL-ONLY silent video. Do not generate or include an audio track. No sound, audio, ambience, environmental sound, sound effects, music, soundtrack, speech, dialogue, voices, narration, singing, humming, whistling, or sound design. Keep the requested visible scene and motion; silence is intentional."""

# Intro clips are silent visual illustrations under the separately mixed narration.
# The rule is intentionally intro-only: main-story clips may depict dialogue when their
# storyboard explicitly requires it.
FLOW_INTRO_SILENT_VISUAL_POLICY_MARKER = "INTRO SILENT-VISUAL POLICY —"
FLOW_INTRO_SILENT_VISUAL_POLICY = """INTRO SILENT-VISUAL POLICY — This is a silent visual illustration, NOT a scene of people speaking. Every visible person's mouth stays gently and naturally CLOSED for the entire clip; lips remain still in every frame. No talking, no speaking, no whispering, no shouting, no singing, no humming, no mouthing words, no lip-sync, no conversational mouth movements, and no open-mouth expression. Do not show anyone mid-sentence or addressing another person. If the narration describes dialogue or a conversation, communicate it silently through listening, a restrained facial reaction with closed lips, a gesture, a meaningful glance, hands, objects, or an over-the-shoulder composition. Prefer profiles, three-quarter views, or wider compositions when a face is visible. Do not create a talking-head shot. Keep all facial expressions subtle and lips closed."""


def enforce_flow_intro_silent_visual_policy(prompt: str) -> str:
    """Append the no-speaking/no-lip-motion rule to intro prompts only."""
    base = str(prompt or "").strip()
    marker_pos = base.casefold().find(FLOW_INTRO_SILENT_VISUAL_POLICY_MARKER.casefold())
    if marker_pos >= 0:
        base = base[:marker_pos].rstrip()
    return f"{base}\n\n{FLOW_INTRO_SILENT_VISUAL_POLICY}".strip() if base else FLOW_INTRO_SILENT_VISUAL_POLICY


def enforce_flow_silent_video_audio(prompt: str) -> str:
    """Replace softer audio directives while preserving style/intro rules after them."""
    base = str(prompt or "").strip()
    # Remove only the directive lines, not everything after the marker: final style
    # locks or intro-specific visual rules may be appended later in the prompt.
    markers = (FLOW_VIDEO_SILENT_AUDIO_POLICY_MARKER.casefold(), FLOW_VIDEO_AUDIO_POLICY_MARKER.casefold())
    kept_lines = [line for line in base.splitlines() if not any(marker in line.casefold() for marker in markers)]
    base = "\n".join(kept_lines).strip()
    return f"{base}\n\n{FLOW_VIDEO_SILENT_AUDIO_POLICY}".strip() if base else FLOW_VIDEO_SILENT_AUDIO_POLICY


def enforce_flow_ambience_only_audio(prompt: str) -> str:
    """Append the normal ambience rule, unless audio recovery requires a silent clip.

    Remove an existing copy first so retries and resumed checkpoints do not grow the
    prompt indefinitely. A strict silent fallback always takes precedence over this
    normal ambience-only directive.
    """
    base = str(prompt or "").strip()
    if FLOW_VIDEO_SILENT_AUDIO_POLICY_MARKER.casefold() in base.casefold():
        return enforce_flow_silent_video_audio(base)
    marker_pos = base.casefold().find(FLOW_VIDEO_AUDIO_POLICY_MARKER.casefold())
    if marker_pos >= 0:
        base = base[:marker_pos].rstrip()
    return f"{base}\n\n{FLOW_VIDEO_AUDIO_POLICY}".strip() if base else FLOW_VIDEO_AUDIO_POLICY


FLOW_VIDEO_OUTPUT_MARKER = "MANDATORY VIDEO OUTPUT —"
FLOW_VIDEO_OUTPUT_DIRECTIVE = (
    "MANDATORY VIDEO OUTPUT — Generate an actual playable 8-second VIDEO, not a still image, "
    "poster, illustration-only result, or single frame. Show clear continuous motion throughout "
    "the full shot: animate the subject-specific physical action, at least one environmental detail, "
    "and one smooth deliberate camera movement. Use motion that fits this exact scene; preserve "
    "the subject, composition, selected visual style, and all safety/language constraints."
)
FLOW_VIDEO_OUTPUT_RETRY_DIRECTIVE = (
    "MANDATORY VIDEO OUTPUT — RETRY AFTER WRONG MEDIA TYPE. The previous result was an IMAGE and "
    "is not acceptable. This request requires a moving video clip from Flow's VIDEO task, not an "
    "image, poster, or single frame. Make continuous visible motion unmistakable from the first "
    "second through the last: subject-specific movement, subtle environmental movement, and one "
    "slow camera move. Keep the described scene and selected visual style; do not turn it into a "
    "static illustration."
)


def enforce_flow_video_output(prompt: str, retry_after_image: bool = False) -> str:
    """Make the requested media type explicit; preserve the stronger retry directive."""
    base = str(prompt or "").strip()
    has_directive = FLOW_VIDEO_OUTPUT_MARKER.casefold() in base.casefold()
    # The first submission only needs one directive. Preserve it on subsequent attempts;
    # replace it only when the UI actually returned an image instead of a video.
    if has_directive and not retry_after_image:
        return base
    if has_directive and retry_after_image:
        base = re.sub(
            r"(?im)^MANDATORY VIDEO OUTPUT —[^\r\n]*(?:\r?\n)?",
            "",
            base,
        ).strip()
    directive = FLOW_VIDEO_OUTPUT_RETRY_DIRECTIVE if retry_after_image else FLOW_VIDEO_OUTPUT_DIRECTIVE
    return f"{directive}\n\n{base}".strip() if base else directive

# Project-level visual style selector; realism remains the default.
VISUAL_STYLE_OPTIONS = {
    "realistic": "Реализм — документальная фотография (по умолчанию)",
    "colorful_painterly": "Красочная живопись — масляная иллюстрация",
    "cartoon_2d": "Мультяшный — красочная 2D-анимация / графический роман",
    "comic_explainer": "2D YouTube Storytime — comic explainer (как у конкурентов)",
}
ACTIVE_VISUAL_STYLE_ID = "realistic"


def normalize_visual_style(style_id: str | None) -> str:
    value = str(style_id or "realistic").strip().casefold()
    aliases = {
        "realism": "realistic", "photo": "realistic", "photorealistic": "realistic",
        "painting": "colorful_painterly", "oil_painting": "colorful_painterly",
        "illustration": "colorful_painterly", "colorful": "colorful_painterly",
        "cartoon": "cartoon_2d", "cartoon_2d": "cartoon_2d", "2d": "cartoon_2d",
        "2d_animation": "cartoon_2d", "cel_shaded": "cartoon_2d", "graphic_novel": "cartoon_2d",
        "comic": "cartoon_2d", "animated": "cartoon_2d",
        "comic_explainer": "comic_explainer", "storytime": "comic_explainer",
        "youtube_comic": "comic_explainer", "2d_explainer": "comic_explainer",
    }
    value = aliases.get(value, value)
    return value if value in VISUAL_STYLE_OPTIONS else "realistic"


def visual_style_label(style_id: str | None = None) -> str:
    return VISUAL_STYLE_OPTIONS[normalize_visual_style(style_id or ACTIVE_VISUAL_STYLE_ID)]


def visual_style_description(media_kind: str = "image", style_id: str | None = None) -> str:
    style = normalize_visual_style(style_id or ACTIVE_VISUAL_STYLE_ID)
    if style == "colorful_painterly":
        core = ("richly coloured hand-painted historical oil-painting illustration on canvas, visible confident brushstrokes, "
                "subtle impasto texture, luminous saturated pigments, warm golds, copper reds, deep blues and natural greens, "
                "dramatic chiaroscuro with readable shadow detail, unmistakably hand-painted surfaces, believable anatomy and recognisable faces")
        if media_kind == "video":
            return "gently animated " + core
        return core
    if style == "comic_explainer":
        core = ("polished 2D YouTube storytime explainer illustration, simplified rounded human character designs, "
                "confident thick dark outlines, clean readable linework, soft cel-shading with restrained smooth gradients, "
                "expressive faces and clear body language, warm cinematic key light with cool blue shadows when appropriate, "
                "balanced vivid colours, readable everyday environments with moderate detail, strong foreground/background separation, "
                "cinematic but simple storytelling composition, consistent character proportions and a clean animated-documentary look")
        if media_kind == "video":
            return "smoothly animated " + core
        if media_kind == "thumbnail":
            return "bold 2D YouTube storytime thumbnail illustration, simplified expressive character, thick dark outlines, soft cel shading, warm cinematic colours and one clear focal idea"
        return core
    if style == "cartoon_2d":
        core = ("vibrant 2D European animation and graphic-novel illustration, bold clean ink outlines, crisp contours, "
                "flat cel-shading with gentle gradient accents, expressive but consistent character designs, richly saturated colours, "
                "clear silhouettes, stylised yet coherent anatomy, illustrated backgrounds, clean comic-book rendering")
        if media_kind == "video":
            return "gently animated " + core
        if media_kind == "thumbnail":
            return "vibrant 2D graphic-novel thumbnail illustration, bold clean ink outlines, cel-shaded characters, saturated palette and strong focal composition"
        return core
    if media_kind == "video":
        return "photorealistic live-action historical documentary cinematography, natural lighting and authentic textures"
    if media_kind == "thumbnail":
        return "photorealistic historical documentary photography, clear focal subject and natural cinematic lighting"
    return "photorealistic full-colour historical documentary photography, natural lifelike colours and authentic textures"


def visual_style_rules(media_kind: str = "image", style_id: str | None = None) -> str:
    style = normalize_visual_style(style_id or ACTIVE_VISUAL_STYLE_ID)
    if style == "colorful_painterly":
        motion = " For video, gently animate the painted scene without changing its style." if media_kind == "video" else " Keep this as one still illustration, not a sequence or collage."
        return ("- VISUAL STYLE (override conflicting style wording): richly coloured classical oil-painting illustration, visible "
                "brushstrokes and tactile canvas/impasto texture, luminous saturated pigments, warm golds, copper reds, deep blues "
                "and natural greens, dramatic chiaroscuro with readable shadow detail, painterly realism. Keep faces recognisable, "
                "age accurate and anatomy natural." + motion + " Not photorealistic, not live-action, not a photograph, not 3D/CGI, not cartoon or anime.")
    if style == "comic_explainer":
        if media_kind == "video":
            extra = (" Animate with smooth restrained 2D motion; keep linework and facial identity stable, avoid frame flicker, morphing, "
                     "sliding facial features, and style changes.")
        elif media_kind == "thumbnail":
            extra = (" For a thumbnail, use one readable story-specific focal subject, a bold silhouette, clear facial expression, "
                     "warm/cool cinematic contrast and no typography or collage.")
        else:
            extra = " Use one still landscape illustration, not a sequence or collage."
        return ("- VISUAL STYLE (override conflicting style wording): polished 2D YouTube storytime / comic-explainer illustration; "
                "simplified rounded character designs, bold consistent dark outlines, clean readable linework, soft cel-shading with "
                "subtle gradient accents, expressive but coherent faces, warm cinematic lighting and cool shadow separation, vivid balanced "
                "colours, detailed-but-simplified backgrounds, strong readable silhouettes, medium/wide storytelling composition, full-bleed "
                "landscape 16:9. Preserve the same character identity and art direction across scenes. Not photorealistic, not an oil painting, "
                "not 3D/CGI, not anime or manga." + extra)
    if style == "cartoon_2d":
        if media_kind == "video":
            extra = (" Animate with restrained, smooth 2D motion; keep outlines stable, avoid flicker, face morphing, sliding facial features, "
                     "or switching art styles between frames.")
        elif media_kind == "thumbnail":
            extra = (" For a thumbnail, use one strong story-accurate focal subject, expressive but consistent faces, bold silhouette and "
                     "high readable contrast; no typography or collage.")
        else:
            extra = " Keep this as one still illustration, not a sequence or collage."
        return ("- VISUAL STYLE (override conflicting style wording): clean 2D European animation / graphic-novel illustration, "
                "bold consistent black or dark ink outlines, crisp contours, vibrant flat cel-shading with restrained gradient accents, "
                "rich saturated but controlled colours, clear silhouettes, stylised yet coherent anatomy, expressive and recognisable "
                "faces, consistent character design, layered illustrated backgrounds, full-bleed landscape 16:9 composition. Preserve each "
                "character's canonical age, face shape, hair, skin tone and distinguishing features. Not photorealistic, not an oil painting, "
                "not 3D/CGI, not anime or manga."
                + extra)
    if media_kind == "video":
        return "- VISUAL STYLE: photorealistic live-action historical documentary cinematography, natural lighting and authentic textures; no illustration, painting, CGI or cartoon style."
    return "- VISUAL STYLE: photorealistic full-colour historical documentary photography, natural lifelike colours, realistic textures and believable anatomy; no illustration, painting, CGI or cartoon style."


FLOW_FINAL_STYLE_LOCK_START = "ABSOLUTE FINAL VISUAL STYLE LOCK"
FLOW_FINAL_STYLE_LOCK_END = "END ABSOLUTE FINAL VISUAL STYLE LOCK"


def enforce_selected_visual_style(prompt: str, media_kind: str = "image") -> str:
    """Move the active project's style lock to the very end of a Flow prompt.

    Do not trust a generic style marker that may belong to stale/conflicting text.
    Replace only our own bounded lock and preserve scene/safety/language/audio content.
    """
    base = str(prompt or "").strip()
    base = re.sub(
        rf"\s*{re.escape(FLOW_FINAL_STYLE_LOCK_START)}.*?{re.escape(FLOW_FINAL_STYLE_LOCK_END)}\s*",
        "\n\n", base, flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    style = normalize_visual_style(ACTIVE_VISUAL_STYLE_ID)
    is_video = media_kind == "video"

    # Replace common *positive* stale photorealism cues before adding the lock.
    # Keep negated phrases such as "NOT photorealistic" unchanged.
    if style in {"colorful_painterly", "cartoon_2d", "comic_explainer"}:
        replacements = {
            "photorealistic live-action historical documentary cinematography": (
                "gently animated hand-painted oil-painting scene" if style == "colorful_painterly"
                else ("polished 2D storytime explainer scene" if style == "comic_explainer" else "restrained 2D illustrated scene")
            ),
            "photorealistic full-colour historical documentary photography": (
                "hand-painted oil illustration" if style == "colorful_painterly"
                else ("polished 2D YouTube storytime illustration" if style == "comic_explainer" else "2D graphic-novel illustration")
            ),
            "photorealistic historical documentary photography": (
                "hand-painted oil illustration" if style == "colorful_painterly"
                else ("polished 2D YouTube storytime illustration" if style == "comic_explainer" else "2D graphic-novel illustration")
            ),
            "photorealistic": ("painterly hand-painted" if style == "colorful_painterly" else ("polished 2D storytime illustrated" if style == "comic_explainer" else "stylised 2D illustrated")),
            "photo-realistic": ("painterly hand-painted" if style == "colorful_painterly" else ("polished 2D storytime illustrated" if style == "comic_explainer" else "stylised 2D illustrated")),
            "photographic": "painted" if style == "colorful_painterly" else "illustrated",
            "live-action": "painted live motion" if style == "colorful_painterly" else "2D animated",
            "documentary photography": "painted documentary illustration" if style == "colorful_painterly" else "2D documentary illustration",
        }
        for term, replacement in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
            pattern = re.compile(re.escape(term), re.IGNORECASE)
            def replace_if_positive(match, replacement=replacement, whole_text=base):
                prefix = whole_text[max(0, match.start() - 28):match.start()].casefold()
                if re.search(r"(?:not|never|no|avoid|without)\s+(?:(?:a|an|the)\s+)?$", prefix):
                    return match.group(0)
                return replacement
            base = pattern.sub(replace_if_positive, base)

    if style == "colorful_painterly":
        medium = "gently animated hand-painted oil-painting scene" if is_video else "single hand-painted oil illustration"
        details = (
            "This MUST visibly look like a PAINTING, not a photograph or live-action footage. "
            "Use clear broad hand-painted brushstrokes across faces, hair, clothing, architecture and background; "
            "visible tactile canvas/oil-paint texture; painterly edge transitions; deliberately painted highlights and shadows; "
            "rich saturated pigments and expressive painted shapes. Avoid photographic skin pores, camera-realistic microtexture, "
            "and lens-like photorealism. Every frame must remain in the same painted medium."
        )
    elif style == "comic_explainer":
        medium = "polished animated 2D YouTube storytime explainer scene" if is_video else "single polished 2D YouTube storytime explainer illustration"
        details = (
            "Use simplified rounded characters, bold consistent dark outlines, clean linework, soft cel-shading with subtle gradients, "
            "expressive but stable faces, warm key light and cool shadow separation, vivid balanced colours, and readable illustrated "
            "environments. Keep proportions and identity coherent across frames. Not photorealistic, not an oil painting, not 3D/CGI, "
            "not anime/manga. Keep the same clean 2D medium throughout."
        )
    elif style == "cartoon_2d":
        medium = "restrained 2D European animation / graphic-novel scene" if is_video else "single 2D European animation / graphic-novel illustration"
        details = (
            "This MUST visibly look like clean 2D drawn art: consistent ink outlines, clear contours, flat cel-shading, "
            "layered illustrated backgrounds and stable stylised character design. NOT photorealistic, NOT live-action, "
            "NOT an oil painting, NOT 3D/CGI, and NOT anime/manga. Keep the same drawn 2D medium in every frame."
        )
    else:
        medium = "photorealistic live-action documentary video" if is_video else "photorealistic documentary photograph"
        details = (
            "Use natural photographic lighting, believable physical textures and lifelike proportions. "
            "Do not switch to illustration, oil painting, cartoon, anime, or CGI."
        )

    lock = (
        f"{FLOW_FINAL_STYLE_LOCK_START} — {style.upper()}\n"
        f"Final style requirement: {medium}. {details}\n"
        f"{FLOW_FINAL_STYLE_LOCK_END}"
    )
    return f"{base}\n\n{lock}".strip() if base else lock


def append_selected_visual_style(prompt: str, media_kind: str = "image") -> str:
    """Apply the authoritative current-project style lock, replacing stale lock blocks."""
    return enforce_selected_visual_style(prompt, media_kind)


def style_format_requirements(style_id: str | None = None) -> str:
    style = normalize_visual_style(style_id or ACTIVE_VISUAL_STYLE_ID)
    if style == "colorful_painterly":
        return """MANDATORY FINAL IMAGE FORMAT — the selected painterly style takes priority over conflicting earlier wording:
- Full-colour, richly pigmented hand-painted oil illustration with visible brushstrokes and subtle canvas/impasto texture; not a photograph or 3D render.
- Use luminous saturated but period-appropriate colours; preserve gold, copper-red, blue, green and natural skin-tone variation. Never monochrome, grayscale, sepia, washed out, or nearly black-and-white.
- Landscape widescreen 16:9, edge-to-edge. No black bars, border, frame, film strip, camera UI, timestamp, vignette, or picture-in-picture.
- No text overlays, captions, logos, watermark or decorative typography. Physical writing must follow the narration-language rule."""
    if style == "comic_explainer":
        return """MANDATORY FINAL IMAGE FORMAT — the selected 2D storytime explainer style takes priority over conflicting earlier wording:
- Polished 2D YouTube storytime/comic-explainer illustration: simplified rounded characters, bold clean dark outlines, soft cel-shading with restrained gradients, readable expressions, warm cinematic lighting and balanced vivid colours. No photorealism, oil-painting texture, 3D/CGI, anime or manga.
- Keep character identity stable while allowing outfits to change when the story calls for it. Never monochrome, grayscale, sepia, washed-out, or near-monochrome.
- Landscape widescreen 16:9 edge-to-edge. No black bars, side mattes, border, frame, film strip, camera UI, timestamp, vignette or picture-in-picture.
- No captions, logos, watermarks or decorative typography. Physical writing must follow the narration-language rule."""
    if style == "cartoon_2d":
        return """MANDATORY FINAL IMAGE FORMAT — the selected cartoon style takes priority over conflicting earlier wording:
- Full-colour 2D European animation / graphic-novel illustration with bold clean contours, crisp linework, vibrant cel-shading and consistent stylised character designs. Do not make it photorealistic, painterly oil art, or 3D/CGI.
- Use vivid, readable colours and stable, natural character identity details. Never monochrome, grayscale, sepia, washed out, or near-monochrome.
- Landscape widescreen 16:9, edge-to-edge. No black bars, side mattes, border, frame, film strip, camera UI, timestamp, vignette, or picture-in-picture.
- No captions, logos, watermarks or decorative typography. Physical writing must follow the narration-language rule."""
    return """MANDATORY FINAL IMAGE FORMAT — these rules override any conflicting style cues above:
- A true full-colour photorealistic image with natural, clearly visible realistic colours and believable skin tones. Never black-and-white, monochrome, grayscale, sepia, duotone, washed-out or near-monochrome.
- Landscape widescreen 16:9 composition that fills the entire image canvas edge to edge. The scene itself occupies the whole frame.
- NO black bars, NO side mattes, NO letterboxing or pillarboxing, NO border, NO frame, NO picture-inside-a-picture, NO film-strip or sprocket holes, NO faux film still, NO camera UI, NO timestamp, NO markings along the edges, NO vignette.
- Do not draw a physical screen, framed photograph, cinema screen or image border. Show the scene directly as a clean full-bleed photograph.
- Contemporary clean colour rendition: natural contrast and colour, not an archival or black-and-white treatment. No added text overlays, captions, logos, or watermarks. Physical writing must follow the narration-language rule."""

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("flow_pipeline")

# ============================================================================
# FILE HELPERS
# ============================================================================

GEMINI_KEYS_FILE = APP_DIR / "gemini_keys.txt"
GROQ_KEYS_FILE = Path(os.environ.get("GROQ_KEYS_FILE", r"C:\Users\user\PycharmProjects\PythonProject\groq_keys.txt"))
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TEXT_MODEL = "openai/gpt-oss-120b"  # active model confirmed by the successful Groq run
GROQ_TEXT_CALL_BUDGET_SECONDS = 72.0
GROQ_TEXT_CONNECT_TIMEOUT_SECONDS = 5.0
GROQ_TEXT_READ_TIMEOUT_SECONDS = 35.0
# When Gemini exhausts its model cascade, one Groq fallback is made for the current
# text operation. The next operation probes Gemini again after its recovery cooldown.
GROQ_FALLBACK_ON_GEMINI_FAILURE = True
GROQ_FALLBACK_PROXY_WAIT_SECONDS = 180.0  # wait for a rotating proxy; never go direct
GROQ_KEY_RATE_LIMIT_COOLDOWN_SECONDS = 300.0
GROQ_KEY_AUTH_COOLDOWN_SECONDS = 900.0
GEMINI_PROXIES_FILE = APP_DIR / "gemini_proxies.txt"


def read_secret_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            result.append(line)
    return result


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_name(text: str, fallback: str = "Project") -> str:
    value = re.sub(r"[\\/*?:\"<>|]", "", text or "")
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:110] or fallback


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_duration(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        output = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            stderr=subprocess.DEVNULL,
            timeout=30,
        ).decode("utf-8", errors="ignore").strip()
        return float(output)
    except Exception:
        return None


def get_video_resolution(path: Path) -> tuple[int | None, int | None]:
    if not path.exists():
        return None, None
    try:
        output = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(path),
            ],
            stderr=subprocess.DEVNULL,
            timeout=30,
        ).decode("utf-8", errors="ignore")
        data = json.loads(output)
        stream = data.get("streams", [{}])[0]
        return stream.get("width"), stream.get("height")
    except Exception:
        return None, None


def is_valid_audio(path: Path) -> bool:
    d = get_duration(path)
    return d is not None and d > 0.1


def is_valid_video(path: Path, min_size: int = 100_000) -> bool:
    if not path.exists() or path.stat().st_size < min_size:
        return False
    width, height = get_video_resolution(path)
    duration = get_duration(path)
    return (
        width is not None
        and height is not None
        and width >= 640
        and height >= 360
        and duration is not None
        and duration > 0.1
    )


def is_valid_image(path: Path, min_size: int = 10_000) -> bool:
    if not path.exists() or path.stat().st_size < min_size:
        return False
    try:
        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:
        return False


PREVIEW_OUTPUT_WIDTH = 1280
PREVIEW_OUTPUT_HEIGHT = 720
PREVIEW_OUTPUT_SIZE = (PREVIEW_OUTPUT_WIDTH, PREVIEW_OUTPUT_HEIGHT)
PREVIEW_OUTPUT_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def is_preview_delivery_asset(path: Path) -> bool:
    """Return True for preview/thumbnail image files in the final delivery folder."""
    if not path.is_file() or path.suffix.casefold() not in PREVIEW_OUTPUT_SUFFIXES:
        return False
    stem = path.stem.casefold()
    return stem.startswith(("preview", "thumb", "thumbnail"))


def preview_image_has_delivery_size(path: Path) -> bool:
    """Check that a delivered preview is exactly 1280x720 pixels."""
    try:
        with Image.open(path) as image:
            oriented = ImageOps.exif_transpose(image)
            return oriented.size == PREVIEW_OUTPUT_SIZE
    except Exception:
        return False


def normalize_preview_image_1280x720(path: Path, apply_finishing: bool = True) -> bool:
    """Export a clean 1280x720 RGB preview; optionally apply the thumbnail finish."""
    temp_path: Path | None = None
    try:
        if not path.is_file():
            return False
        with Image.open(path) as source:
            oriented = ImageOps.exif_transpose(source)
            if "A" in oriented.getbands() or "transparency" in source.info:
                rgba = oriented.convert("RGBA")
                white_bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                rgb = Image.alpha_composite(white_bg, rgba).convert("RGB")
            else:
                rgb = oriented.convert("RGB")
            if apply_finishing:
                angle = random.choice((-1, 1)) * random.uniform(0.30, 0.55)
                rotated = rgb.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)
                edge = random.uniform(0.016, 0.024)
                left, top = int(rotated.width * edge), int(rotated.height * edge)
                right, bottom = max(left + 1, int(rotated.width * (1.0 - edge))), max(top + 1, int(rotated.height * (1.0 - edge)))
                cropped = rotated.crop((left, top, right, bottom))
                fitted = ImageOps.fit(cropped, PREVIEW_OUTPUT_SIZE, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
                pixels = np.asarray(fitted, dtype=np.float32) / 255.0
                pixels = np.power(np.clip(pixels, 0.0, 1.0), random.uniform(0.985, 1.015)) * 255.0
                pixels += np.random.normal(0.0, random.uniform(1.1, 1.7), pixels.shape).astype(np.float32)
                finished = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8), mode="RGB")
            else:
                fitted = ImageOps.fit(rgb, PREVIEW_OUTPUT_SIZE, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
                finished = Image.fromarray(np.asarray(fitted, dtype=np.uint8), mode="RGB")
        image_format = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP", ".bmp": "BMP"}.get(path.suffix.casefold())
        if image_format is None:
            log.warning("[Preview] Неподдерживаемый формат превью: %s", path)
            return False
        temp_path = path.with_name(f".{path.stem}.1280x720.tmp{path.suffix}")
        options: dict[str, Any] = {}
        if image_format == "JPEG":
            options.update({"quality": random.randint(90, 93), "optimize": True, "progressive": True})
        elif image_format == "PNG":
            options.update({"optimize": True, "compress_level": 6})
        elif image_format == "WEBP":
            options.update({"quality": 92, "method": 6})
        finished.save(temp_path, format=image_format, **options)
        with Image.open(temp_path) as check:
            check.verify()
        with Image.open(temp_path) as check:
            if check.size != PREVIEW_OUTPUT_SIZE or check.mode not in {"RGB", "L"}:
                raise RuntimeError(f"неверный экспорт {check.size}/{check.mode}")
            if check.info.get("icc_profile") or check.info.get("exif"):
                raise RuntimeError("в экспорт попали ICC/EXIF-метаданные")
        temp_path.replace(path)
        log.info("✅ [Preview] Чистый экспорт %sx%s (%s): %s", PREVIEW_OUTPUT_WIDTH, PREVIEW_OUTPUT_HEIGHT, "finished" if apply_finishing else "original", path.name)
        return True
    except Exception as exc:
        log.error("[Preview] Не удалось привести %s к 1280x720: %s", path, exc)
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass



# Character-reference similarity checks are intentionally disabled in the pipeline.
# Attached references guide character consistency; similarity must not block or delete
# otherwise valid generated scene assets.


def image_quality_issues(path: Path) -> list[str]:
    """Detect obvious black side bars and genuinely monochrome images.

    This intentionally uses conservative thresholds: a dark scene by itself
    is not rejected, but wide, nearly uniform black side mattes are.
    """
    if not path.exists():
        return ["файл изображения отсутствует"]
    try:
        with Image.open(path) as original:
            image = original.convert("RGB")
            width, height = image.size
            issues: list[str] = []
            ratio = width / max(1, height)
            if ratio < 1.68 or ratio > 1.88:
                issues.append(f"неширокий формат холста {ratio:.2f}:1")

            resampling = getattr(Image, "Resampling", Image).BILINEAR
            sample = image.resize((128, 72), resampling)
            pixels = sample.load()

            def band_stats(x0: int, y0: int, x1: int, y1: int) -> tuple[float, float, float]:
                luminance = []
                near_black = 0
                count = 0
                for yy in range(y0, y1):
                    for xx in range(x0, x1):
                        r, g, b = pixels[xx, yy]
                        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
                        luminance.append(lum)
                        if max(r, g, b) <= 18:
                            near_black += 1
                        count += 1
                if not count:
                    return 255.0, 255.0, 0.0
                mean = sum(luminance) / count
                variance = sum((v - mean) ** 2 for v in luminance) / count
                return mean, variance ** 0.5, near_black / count

            # 10% of the image edge is enough to detect the wide mattes in
            # Flow outputs while ignoring a few naturally dark pixels.
            edge_w = 13
            left = band_stats(0, 0, edge_w, 72)
            right = band_stats(128 - edge_w, 0, 128, 72)
            left_black = left[0] < 10 and left[1] < 8 and left[2] > 0.97
            right_black = right[0] < 10 and right[1] < 8 and right[2] > 0.97
            if left_black and right_black:
                issues.append("широкие чёрные полосы/рамка по бокам")

            # Evaluate saturation inside the image, excluding edges where
            # titles/film markings tend to appear. Only near-grayscale images
            # are flagged; snow/stone/dark scenes can remain naturally muted.
            x0, x1 = 18, 110
            y0, y1 = 4, 68
            hsv = sample.crop((x0, y0, x1, y1)).convert("HSV")
            sats = [sat for _, sat, _ in hsv.getdata()]
            mean_sat = sum(sats) / max(1, len(sats))
            colorful_share = sum(1 for sat in sats if sat >= 30) / max(1, len(sats))
            if mean_sat < 18 and colorful_share < 0.08:
                issues.append("изображение выглядит чёрно-белым/монохромным")

            return issues
    except Exception as exc:
        # Do not fail a whole project if a diagnostic cannot inspect an image.
        log.warning("[Image QA] Не удалось проверить %s: %s", path.name, exc)
        return []


def make_full_color_edge_to_edge_prompt(prompt: str, issues: list[str] | None = None) -> str:
    """Append image-format constraints while preserving the appended safety rules."""
    marker = "\n\nMANDATORY FINAL IMAGE FORMAT"
    safety_suffix = ""
    safety_pos = prompt.find(YOUTUBE_CONTENT_SAFETY_MARKER)
    format_pos = prompt.find(marker)
    if safety_pos >= 0 and format_pos >= 0 and safety_pos > format_pos:
        safety_suffix = prompt[safety_pos:].strip()
    base_prompt = prompt.split(marker, 1)[0].rstrip()
    reason = "; ".join(issues or [])
    correction = style_format_requirements()
    if reason:
        correction += f"\n\nCorrect the previous quality issue(s): {reason}."
    result = f"{base_prompt}\n\n{correction}"
    if safety_suffix:
        result += f"\n\n{safety_suffix}"
    return result


YOUTUBE_CONTENT_SAFETY_MARKER = "YOUTUBE CONTENT SAFETY CONSTRAINTS"
YOUTUBE_CONTENT_SAFETY_RULES = """YOUTUBE CONTENT SAFETY CONSTRAINTS — these rules override conflicting scene details:
- General-audience, non-graphic documentary presentation. Do not sensationalize suffering, injury, fear, cruelty, or death.
- Never depict a person deliberately hurting, threatening, terrorizing, or cruelly restraining an animal. Never show an animal visibly suffering, severely emaciated, bleeding, wounded, trapped in distress, or dead.
- If the narration mentions abuse, neglect, illness, injury, or violence, communicate the topic through a safe, non-graphic alternative: calm recovery, respectful veterinary/rescue care, a peaceful animal in a clean safe environment, an adult caregiver offering water/feed, relevant objects, or an empty setting. Do not depict the harmful act itself.
- Keep animals calm, physically intact, and not visibly distressed. Do not use close-ups of pain, injuries, blood, exposed tissue, or a helpless victim.
- Children, babies, and teenagers ARE ALLOWED when they are genuinely relevant to the story. Preserve the exact age and appearance stated in the story; never age a minor up or down. Show minors only fully clothed in calm, ordinary, age-appropriate, physically safe settings. For babies and very young children, include a calm responsible caregiver nearby when appropriate. Do not depict a child being threatened, chased, harmed, restrained, abandoned, frightened, injured, or placed near a hazard.
- Pregnant adult women ARE ALLOWED when relevant to the story. Clearly depict them as adults, fully clothed and respectfully, in calm ordinary settings. A visible pregnancy/baby bump is permitted; do not infer pregnancy when the story does not state it. Do not depict labour, childbirth, exposed anatomy, obstetric procedures, pregnancy complications, medical distress, or injury.
- STRICT FRAME-SAFETY RULE: any frame containing a child/minor or a pregnant adult must contain NO weapon of any kind anywhere in the frame (including held, holstered, sheathed, displayed, on a table, or in the background), NO blood, wounds, injuries, violent aftermath, threats, attacks, restraint, panic, or dangerous activity. Also exclude hazardous props/settings such as firearms, knives, explosives, dangerous machinery, fire, heights, road traffic, or open water when a child/minor is present. If the narration discusses violence, weapons, blood, childbirth, or danger, keep the vulnerable person out of that shot and use a separate neutral object/detail, calm safe aftermath, or peaceful setting instead.
- No graphic violence, gore, sexual content, threatening acts, or imitation of dangerous behavior in any scene. Benign family scenes and respectful, non-medical depictions of pregnancy are permitted.
- Preserve a factual, respectful documentary tone. No sensationalized thumbnail imagery, text, logos, watermarks, or decorative frames.
""".strip()


def apply_youtube_content_safety_rules(prompt: str, media_kind: str = "image") -> str:
    """Append clear, general-audience safety constraints to a visual prompt."""
    base = (prompt or "").strip()
    if YOUTUBE_CONTENT_SAFETY_MARKER in base:
        return base
    extra = YOUTUBE_CONTENT_SAFETY_RULES
    if media_kind == "video":
        extra += "\n- Motion must remain calm and non-threatening throughout the whole clip; do not animate harm, panic, pursuit, attack, or an animal in distress."
    elif media_kind == "thumbnail":
        extra += "\n- The thumbnail must not depict an animal in distress or use suffering as clickbait; prefer a calm subject, recovery/care context, or neutral setting."
    return f"{base}\n\n{extra}".strip()


VISUAL_TEXT_LANGUAGE_MARKER = "VISIBLE SCENE TEXT LANGUAGE:"

def narration_language_name(target_lang: str) -> str:
    """Human-readable language name used to guide generated scene lettering."""
    normalized = str(target_lang or "ru").strip().lower().split("-")[0]
    return {"ru": "Russian", "es": "Spanish", "pl": "Polish"}.get(normalized, "Russian")


def apply_visible_text_language_rule(prompt: str, target_lang: str = "ru") -> str:
    """Enforce one diegetic writing language, matching the selected narration language.

    This is deliberately stronger than a preference: quoted/source-language wording
    must not be copied into a sign or document unless it is in the narration language.
    """
    base = (prompt or "").strip()
    # Remove an older rule case-insensitively so stale Russian instructions cannot survive
    # when a saved project is resumed with Spanish/Polish selected.
    marker_match = re.search(re.escape(VISUAL_TEXT_LANGUAGE_MARKER), base, flags=re.IGNORECASE)
    if marker_match:
        base = base[:marker_match.start()].rstrip()

    normalized = str(target_lang or "ru").strip().lower().split("-")[0]
    language = narration_language_name(normalized)
    if normalized == "es":
        script_guard = (
            "- HARD LANGUAGE LOCK: physical in-scene writing must be natural Spanish only. "
            "Do not show Cyrillic letters, Russian words, Russian-language signs, or mixed Russian/Spanish text. "
            "If the source story contains a quoted phrase in another language, translate its meaning into Spanish "
            "for the visible object or leave the writing blank/non-legible. Never copy foreign wording verbatim."
        )
    elif normalized == "pl":
        script_guard = (
            "- HARD LANGUAGE LOCK: physical in-scene writing must be natural Polish only. "
            "Do not show Russian/Cyrillic lettering or mix languages. Translate quoted source wording into Polish "
            "or leave it blank/non-legible. Never copy foreign wording verbatim."
        )
    else:
        script_guard = (
            "- HARD LANGUAGE LOCK: physical in-scene writing must be natural Russian only. "
            "Do not mix in Spanish/Polish/English words. Translate quoted source wording into Russian "
            "or leave it blank/non-legible. Never copy foreign wording verbatim."
        )
    instruction = f"""{VISUAL_TEXT_LANGUAGE_MARKER} {language}
{script_guard}
- This applies to all physical writing within the depicted world: signs, notices, letters, documents, book covers, labels, packaging, screens, and shop fronts.
- If writing is not essential to the scene, show no readable writing. If the exact wording cannot be rendered reliably in {language}, use blank or non-legible marks.
- Do not add subtitles, captions, title cards, graphic overlays, logos, watermarks, or decorative typography. Never use text overlays to satisfy this rule."""
    return f"{base}\n\n{instruction}".strip()


SAFE_CHILD_CONTEXT_PATTERN = re.compile(
    r"\b(child(?:ren)?|kid(?:s)?|boy|girl|baby|babies|infant|toddler|newborn|teen(?:ager)?|minor|schoolchild)\b|"
    r"\b(dziec\w*|chłop\w*|chlop\w*|dziewczyn\w*|niemowl\w*|maluch\w*|nastolat\w*|ucze\w*|uczennic\w*)\b|"
    r"\b(реб[её]н\w*|дет\w*|мальч\w*|девоч\w*|младен\w*|малыш\w*|подрост\w*|школьник\w*|школьниц\w*)\b|"
    r"\b(niñ[oa]s?|niñas?|bebé|bebes|infante|adolescente)\b",
    re.IGNORECASE,
)
SAFE_PREGNANCY_CONTEXT_PATTERN = re.compile(
    r"\b(pregnan\w*|pregnancy|expectant mother|mother[- ]to[- ]be)\b|"
    r"\b(ciężarn\w*|ciezarn\w*|ciąży|ciazy|ciąża|ciaza)\b|"
    r"\b(беремен\w*|ожидающая ребёнка|ожидающая ребенка)\b|"
    r"\b(embarazad\w*|embarazo|gestante)\b",
    re.IGNORECASE,
)
SAFE_HARM_CONTEXT_PATTERN = re.compile(
    r"\b(violence|violent|weapon|weapons|gun|rifle|knife|blood|bleeding|wound|wounds|injur\w*|attack|attacked|threat|threatened|abuse|abused|assault|danger|dangerous|fire|explosion|explosive|childbirth|labour|labor|medical emergency)\b|"
    r"\b(przemoc\w*|broń|broni|bron\w*|pistolet\w*|karabin\w*|nóż|noża|nożem|noz\w*|krew|krwi|krw\w*|ran\w*|zran\w*|atak\w*|groźb\w*|grozb\w*|zagroż\w*|zagroz\w*|naduży\w*|naduzy\w*|niebezpiecz\w*|pożar\w*|pozar\w*|wybuch\w*|poród|porod\w*)\b|"
    r"\b(насили\w*|оружи\w*|пистолет|винтовк\w*|нож|кров\w*|ран\w*|травм\w*|напад\w*|угроз\w*|опасн\w*|пожар|взрыв|рождени\w*|роды)\b|"
    r"\b(violencia|arma\w*|pistola|cuchillo|sangre|herida\w*|lesi\w*|ataque|amenaz\w*|peligro|incendio|explosión|parto)\b",
    re.IGNORECASE,
)


def make_safe_regeneration_prompt(
    context_text: str = "",
    setting_context: dict[str, Any] | None = None,
    media_kind: str = "image",
) -> str:
    """Build a conservative replacement prompt while preserving safe, relevant vulnerable subjects."""
    context = str(context_text or "")
    animal_terms = re.compile(
        r"\b(horse|foal|pony|animal|dog|cat|cow|sheep|goat|bird|wildlife)\b|"
        r"(лошад|жереб|кон[ьи]|животн|собак|кошк|коров|овц|коз[аы]|птиц|звер|koń|koni|końmi|koza|krow|owc|pies|kot|zwierzę|zwierzet)",
        re.IGNORECASE,
    )
    is_animal_story = bool(animal_terms.search(context))
    is_child_story = bool(SAFE_CHILD_CONTEXT_PATTERN.search(context))
    is_pregnancy_story = bool(SAFE_PREGNANCY_CONTEXT_PATTERN.search(context))
    contains_harm_or_danger = bool(SAFE_HARM_CONTEXT_PATTERN.search(context))
    # When the current narration beat contains a hazard, a weapon, blood, violence,
    # childbirth or injury, keep the child/pregnant person OUT of the replacement frame.
    suppress_vulnerable_subjects = contains_harm_or_danger and (is_child_story or is_pregnancy_story)
    setting = ""
    if isinstance(setting_context, dict):
        safe_bits = [str(setting_context.get(k, "")).strip() for k in ("setting", "time_period", "color_palette")]
        setting = ", ".join(x for x in safe_bits if x and len(x) < 160)

    subjects: list[str] = []
    if suppress_vulnerable_subjects:
        subjects.append(
            "a quiet, empty, physically safe setting with story-relevant neutral objects only; no people visible, "
            "suggesting a peaceful transition or non-graphic aftermath without showing the dangerous event"
        )
    elif is_child_story and is_pregnancy_story:
        subjects.append(
            "the story-relevant, age-accurate child fully clothed in a peaceful everyday setting, with a calm responsible caregiver nearby, "
            "and the clearly adult pregnant woman fully clothed and respectfully depicted; both are safe, calm, unharmed, and not distressed"
        )
    elif not suppress_vulnerable_subjects:
        if is_child_story:
            subjects.append(
                "the story-relevant child at the exact age stated in the story, fully clothed and calmly engaged in an ordinary age-appropriate activity "
                "in a peaceful, physically safe setting; include a responsible caregiver nearby for a baby or very young child"
            )
        if is_pregnancy_story:
            subjects.append(
                "the story-relevant clearly adult pregnant woman, fully clothed and respectfully shown in a calm ordinary activity; "
                "a visible baby bump is permitted, with no medical or childbirth scene"
            )
    if is_animal_story and not suppress_vulnerable_subjects:
        subjects.append(
            "a calm, physically intact animal in a clean, safe stable or rescue environment, with gentle non-invasive care if relevant; "
            "the animal is settled and not visibly injured, emaciated, frightened, or distressed"
        )
    if not subjects:
        subjects.append(
            "a calm adult documentary subject in a safe environment, emphasizing neutral environmental details, "
            "relevant objects, respectful assistance, recovery, or a non-graphic aftermath rather than any harmful act"
        )

    subject = "; and ".join(subjects)
    style_desc = visual_style_description("video" if media_kind == "video" else "image")
    active_style = normalize_visual_style(ACTIVE_VISUAL_STYLE_ID)
    lighting_desc = ("painterly directional light, rich colour pigments, tactile brushwork"
                     if active_style == "colorful_painterly"
                     else ("warm cinematic key light, cool shadow separation, clean cel-shading" if active_style == "comic_explainer"
                           else "natural light, authentic textures"))
    base = (
        f"{style_desc} of {subject}. "
        f"{lighting_desc}, balanced composition, calm body language, no sensationalism. "
        f"Setting: {setting or 'a neutral, realistic documentary setting'}. "
        "Represent the story safely: no harmful act, threat, fear, visible injury, blood, weapon, dangerous object, "
        "violent aftermath, or hazardous situation in this frame. For any frame with a child or pregnant adult, "
        "show no weapon of any kind anywhere in the frame. Do not show labour, childbirth, exposed anatomy, "
        "or medical complications. Landscape 16:9, full frame, no captions, logos, watermark, border, or film-strip effects."
    )
    return apply_youtube_content_safety_rules(base, media_kind="video" if media_kind == "video" else "image")


def _flow_prompt_core(prompt: str) -> str:
    """Remove appended safety/style/language boilerplate before rewriting a refused shot."""
    value = str(prompt or "").strip()
    markers = [
        YOUTUBE_CONTENT_SAFETY_MARKER,
        VISUAL_TEXT_LANGUAGE_MARKER,
        FLOW_VIDEO_AUDIO_POLICY_MARKER,
        FLOW_VIDEO_SILENT_AUDIO_POLICY_MARKER,
        FLOW_INTRO_SILENT_VISUAL_POLICY_MARKER,
        FLOW_FINAL_STYLE_LOCK_START,
        "VISUAL STYLE (override conflicting style wording)",
        "MANDATORY FINAL IMAGE FORMAT",
    ]
    for marker in markers:
        match = re.search(re.escape(marker), value, flags=re.IGNORECASE)
        if match:
            value = value[:match.start()].rstrip()
    return value


def _infer_prompt_text_language(prompt: str) -> str:
    """Recover the selected diegetic writing language from an existing prompt."""
    match = re.search(
        re.escape(VISUAL_TEXT_LANGUAGE_MARKER) + r"\s*(Russian|Spanish|Polish)",
        str(prompt or ""), flags=re.IGNORECASE,
    )
    if not match:
        return "ru"
    return {"russian": "ru", "spanish": "es", "polish": "pl"}.get(match.group(1).lower(), "ru")


def _finish_rewritten_flow_prompt(prompt: str, original_prompt: str, media_kind: str) -> str:
    """Re-apply only concise safety/style/language constraints to the revised shot."""
    target_lang = _infer_prompt_text_language(original_prompt)
    core = _flow_prompt_core(prompt).strip().strip('`').strip()
    if not core:
        return ""

    style_prefix = visual_style_description("video" if media_kind == "video" else "image")
    if style_prefix.casefold() not in core.casefold():
        core = f"{style_prefix}. {core}"
    core = append_selected_visual_style(core, "video" if media_kind == "video" else "image")

    compact_safety = (
        "Tone: peaceful, calm, and suitable for a general audience; all visible characters are safe and at ease."
    )
    if compact_safety.casefold() not in core.casefold():
        core = f"{core}\n\n{compact_safety}"
    return apply_visible_text_language_rule(core, target_lang)


def make_policy_safe_fallback_prompt(
    original_prompt: str, media_kind: str = "video", rewrite_index: int = 1,
) -> str:
    """Create a concise, positive-only alternative if Gemini cannot rewrite a refusal."""
    context = _flow_prompt_core(original_prompt).casefold()
    setting_bits: list[str] = []
    if re.search(r"\b(snow|snowy|winter|blizzard|montana|снег|зима|метель)\b", context):
        setting_bits.append("a quiet winter landscape with soft snow and natural cold-weather light")
    if re.search(r"\b(stone cabin|cabin|homestead|stone home|house|дом|каменн)\b", context):
        setting_bits.append("a modest stone cabin or rural homestead")
    if re.search(r"\b(wagon|stagecoach|cart|carriage|телег|повозк|карет)\b", context):
        setting_bits.append("a stationary period-appropriate wooden wagon as a background prop")
    if re.search(r"\b(railway|railroad|train|station|железнодорож|поезд|станци)\b", context):
        setting_bits.append("a period railway station or quiet platform")
    if re.search(r"\b(horse|foal|pony|лошад|жереб)\b", context):
        subject = "a healthy horse standing calmly beside the location, with an adult caretaker nearby"
    elif re.search(r"\b(dog|cat|собак|кошк)\b", context):
        subject = "a calm household pet resting peacefully in a safe everyday setting"
    else:
        subject = "a quiet view of the location, with ordinary daily life in the background"
    setting = "; ".join(dict.fromkeys(setting_bits)) or "a historically grounded rural settlement"

    variations = [
        "Use a wide establishing composition focused on the location, landscape, weather, and architecture.",
        "Use a quiet detail composition of period-appropriate materials, clothing, tools, and nearby objects.",
        "Show an ordinary peaceful moment with relaxed body language and a simple, uncluttered background.",
        "Focus on the surroundings and everyday life, using restrained camera motion for video or one clear composition for an image.",
    ]
    variation = variations[(max(1, int(rewrite_index)) - 1) % len(variations)]
    media_label = "one coherent 8-second shot with gentle continuous motion" if media_kind == "video" else "one still full-frame image"
    safe_core = (
        f"{visual_style_description('video' if media_kind == 'video' else 'image')}. "
        f"{media_label}. Show {subject} at {setting}. {variation} "
        "Use period-appropriate surroundings, natural light, stable character appearance, and a peaceful documentary tone. "
        "Landscape 16:9, full frame, suitable for a general audience."
    )
    return _finish_rewritten_flow_prompt(safe_core, original_prompt, media_kind)


def rewrite_flow_prompt_after_policy_refusal(
    original_prompt: str,
    refusal_text: str = "",
    media_kind: str = "video",
    rewrite_index: int = 1,
) -> str:
    """Ask Gemini to make a genuinely safer prompt, not to circumvent Flow's safeguards."""
    original_core = _flow_prompt_core(original_prompt)
    style_name = visual_style_label(ACTIVE_VISUAL_STYLE_ID)
    refusal_excerpt = re.sub(r"\s+", " ", str(refusal_text or "")).strip()[-1400:]
    is_third_party_refusal = bool(detect_new_flow_third_party_refusal(refusal_excerpt))
    third_party_guidance = """
ADDITIONAL THIRD-PARTY CONTENT REQUIREMENTS:
- Make the replacement genuinely original; do not recreate or imitate any third-party protected character, franchise, film/series/game scene, branded universe, logo, costume, signature prop, catchphrase, artwork, or recognizable media design.
- If the original prompt names or clearly describes such protected material, remove that identity and replace it with a newly designed, generic character or neutral period-appropriate object/location. Do not preserve a near-identical likeness or distinctive combination of protected traits.
- Do not imitate a named living artist, studio, or specific contemporary copyrighted work. Keep only the broad narrative purpose and harmless action, expressed through original visual details.
- Prefer one ordinary action, one or two generic subjects, and a simple background. Avoid brand names, logos, trademarked symbols, and exact recreations.
""".strip() if is_third_party_refusal else ""
    rewrite_request = f"""You are revising a visual-generation prompt after Google Flow refused it for policy reasons.
Rewrite the prompt as a clearly policy-compliant, simpler alternative. This is not a request to bypass or disguise a prohibited request.

Requirements:
{third_party_guidance}
- Preserve the story's harmless narrative purpose, period, setting, relevant adult character identity, and selected art style ({style_name}) where feasible.
- Remove or replace the element that may have caused the refusal. If a scene implies harm, danger, intimidation, suffering, or another disallowed depiction, do NOT depict it indirectly or euphemistically; switch to a peaceful everyday moment, a neutral location/object shot, calm recovery/care, or a non-graphic aftermath.
- Keep actions ordinary, calm, non-graphic, and suitable for a general audience. Do not include visible harm, threatening action, or unsafe activity.
- Keep the output brief and concrete: {"one coherent 8-second shot" if media_kind == "video" else "one still image"}. Preserve useful character continuity and environmental details, but remove unnecessary cinematic complications.
- Do not mention policies, refusals, moderation, rules, or your reasoning in the final prompt. Do not instruct Flow to ignore safeguards.
- Return ONLY the rewritten prompt, with no preamble or quote marks.

CURRENT PROMPT:
{original_core[:6000]}

FLOW REFUSAL MESSAGE (diagnostic context only):
{refusal_excerpt[:1400]}
""".strip()

    rewritten = call_text_api(rewrite_request)
    if rewritten:
        rewritten = re.sub(r"^(?:revised\s+prompt|rewritten\s+prompt|video\s+prompt|image\s+prompt|prompt)\s*:\s*", "", rewritten.strip(), flags=re.IGNORECASE)
        rewritten = rewritten.strip().strip('`').strip().strip('"').strip()
        old_norm = re.sub(r"\s+", " ", original_core).casefold()
        new_norm = re.sub(r"\s+", " ", _flow_prompt_core(rewritten)).casefold()
        if len(new_norm) >= 30 and new_norm != old_norm:
            similarity = difflib.SequenceMatcher(None, old_norm, new_norm).ratio()
            if similarity < 0.94:
                finished = _finish_rewritten_flow_prompt(rewritten, original_prompt, media_kind)
                if finished:
                    return finished

    log.warning(
        "[Flow Policy Rewrite] Gemini не вернул достаточно отличающийся промпт; использую безопасный вариант №%s.",
        rewrite_index,
    )
    return make_policy_safe_fallback_prompt(original_prompt, media_kind, rewrite_index)


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def force_cuda_cleanup(reason: str = "") -> None:
    """Release unreachable Python objects and cached CUDA/CPU working-set memory.

    This is a best-effort cleanup, not a substitute for removing live references to
    models/tensors. Callers should set large objects to None before invoking it.
    On Windows, EmptyWorkingSet asks the OS to discard pageable, currently unused
    pages from this process's working set; it does not terminate anything.
    """
    import gc

    try:
        gc.collect()
    except Exception:
        pass

    cuda_cleaned = False
    try:
        import torch
        if torch.cuda.is_available():
            try:
                torch.cuda.synchronize()
            except Exception:
                pass
            torch.cuda.empty_cache()
            try:
                torch.cuda.ipc_collect()
            except Exception:
                pass
            # Synchronize/collection can make more Python objects unreachable.
            gc.collect()
            torch.cuda.empty_cache()
            cuda_cleaned = True
    except Exception as exc:
        if reason:
            log.debug("[Memory Cleanup] CUDA cleanup skipped (%s): %s", reason, exc)

    working_set_trimmed = False
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            kernel32.GetCurrentProcess.argtypes = []
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
            psapi.EmptyWorkingSet.restype = ctypes.c_int
            working_set_trimmed = bool(psapi.EmptyWorkingSet(kernel32.GetCurrentProcess()))
        except Exception:
            pass

    try:
        gc.collect()
    except Exception:
        pass

    if reason:
        log.info(
            "🧹 [Memory Cleanup] %s: gc=done | cuda=%s | windows_working_set_trim=%s",
            reason, "cleared" if cuda_cleaned else "n/a", "done" if working_set_trimmed else "n/a",
        )


# ==============================================================================
# ⚙️ GEMINI KEYS & PROXY ENGINE (ROUND-ROBIN + COOLDOWNS + FAILOVER)
# ==============================================================================

DEFAULT_GEMINI_API_KEYS = []

DEFAULT_GEMINI_PROXIES = []


def load_gemini_keys() -> list[str]:
    file_keys = read_secret_lines(GEMINI_KEYS_FILE)
    if file_keys:
        return file_keys
    return DEFAULT_GEMINI_API_KEYS


def _secret_file_candidates(filename: str) -> list[Path]:
    # Primary location is next to the script. Also check the parent and the
    # current working directory so PyCharm launch settings cannot silently
    # make the proxy/key files invisible.
    candidates = [
        APP_DIR / filename,
        APP_DIR.parent / filename,
        Path.cwd() / filename,
    ]
    result = []
    seen = set()
    for item in candidates:
        try:
            resolved = item.resolve()
        except Exception:
            resolved = item
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def _normalize_proxy_line(proxy: str) -> str:
    # requests needs a scheme for standard HTTP(S) proxies.
    value = proxy.strip()
    if not value:
        return value
    if '://' not in value:
        return f'http://{value}'
    return value


def load_gemini_proxies() -> list[str]:
    for candidate in _secret_file_candidates(GEMINI_PROXIES_FILE.name):
        file_proxies = read_secret_lines(candidate)
        if file_proxies:
            normalized = [_normalize_proxy_line(x) for x in file_proxies]
            log.info(f"[Proxy Engine] ✅ Загружено прокси: {len(normalized)} | файл={candidate}")
            return normalized

    log.warning(
        f"[Proxy Engine] ⚠️ Файл {GEMINI_PROXIES_FILE.name} не найден/пуст. "
        f"Искались пути: {', '.join(str(x) for x in _secret_file_candidates(GEMINI_PROXIES_FILE.name))}"
    )
    return DEFAULT_GEMINI_PROXIES


def load_groq_keys() -> list[str]:
    """Load Groq API keys from the user's configured absolute path or project fallbacks."""
    candidates = [GROQ_KEYS_FILE, *_secret_file_candidates(GROQ_KEYS_FILE.name)]
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = str(candidate.resolve()).casefold()
        except Exception:
            resolved = str(candidate).casefold()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            keys = read_secret_lines(candidate)
        except OSError as exc:
            log.warning("[Groq Key Engine] Не удалось прочитать %s: %s", candidate, exc)
            continue
        if keys:
            log.info("[Groq Key Engine] Загружено API-ключей: %s | файл=%s", len(keys), candidate)
            return keys
    log.warning(
        "[Groq Key Engine] Файл groq_keys.txt не найден или пуст. Проверены: %s",
        ", ".join(str(p) for p in candidates),
    )
    return []


USE_GEMINI_PROXY = True  # Shared rotating proxy pool for Gemini, Groq text requests, and Gemini TTS.
GEMINI_API_KEYS = load_gemini_keys()
GROQ_API_KEYS = load_groq_keys()
GEMINI_PROXIES_LIST = load_gemini_proxies()
ACTIVE_TEXT_PROVIDER = "gemini"  # Always defaults to Gemini unless changed in the startup menu.

GEMINI_AUDIO_MODEL = "gemini-3.8-flash-tts"
GEMINI_VOICE_NAME = "Algieba"
# Optional channel-specific Gemini / Extended Voice Library voice IDs.
CHANNEL_VOICE_OVERRIDES: dict[str, str] = {
    # "channel folder name": "Algieba",  # or an Extended Voice Library voice ID
}
ACTIVE_TTS_CHANNEL_NAME = ""


def _normalize_voice_channel_key(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).casefold()).strip()


def get_channel_tts_voice(channel_name: str) -> str:
    wanted = _normalize_voice_channel_key(channel_name)
    for configured_name, voice_id in CHANNEL_VOICE_OVERRIDES.items():
        if _normalize_voice_channel_key(configured_name) == wanted and str(voice_id or "").strip():
            return str(voice_id).strip()
    return GEMINI_VOICE_NAME


# Keep narration requests compact. Long TTS requests have occasionally returned
# a valid WAV container with a long silent tail, so split at sentence boundaries
# and keep each chunk at or below 900 characters.
MAX_AUDIO_CHUNK_SIZE = 900
TTS_MIN_ACTIVE_RATIO = 0.30
TTS_MAX_CONTIGUOUS_SILENCE_S = 8.0

# Gemini TTS edge-noise cleanup. Start trimming is adaptive (only if the first
# samples look like low-level broadband hiss). Tail trimming is also adaptive and
# never removes a fixed duration from every chunk, so spoken endings are preserved.
TTS_EDGE_CLEANUP_VERSION = "adaptive-hiss-trim-v5-3000char-speech-compressor-silence-guard"
TTS_EDGE_FRAME_MS = 5.0
TTS_START_HISS_SCAN_MAX_MS = 50.0
TTS_START_HISS_MIN_RUN_MS = 10.0
TTS_START_HISS_KEEP_MS = 5.0
TTS_TAIL_HISS_SCAN_MAX_MS = 220.0
TTS_TAIL_HISS_MIN_RUN_MS = 25.0
TTS_TAIL_HISS_KEEP_MS = 15.0
TTS_HISS_ABSOLUTE_RMS_MAX = 850.0  # int16 sample units after normalization
TTS_HISS_RELATIVE_RMS_MAX = 0.075  # relative to the active speech-frame RMS
TTS_HISS_HIGH_FREQUENCY_RATIO_MIN = 0.28  # broadband/high-frequency hiss heuristic
TTS_EDGE_FADE_MS = 4.0
# Remove only a tiny isolated impulse at the very beginning when speech starts
# much later. This is not a general trim and is capped at 3 ms.
TTS_START_CLICK_SCAN_MS = 3.0
TTS_START_CLICK_MIN_REL_PEAK = 0.20
TTS_START_CLICK_FOLLOWING_RMS_REL_MAX = 0.03
TTS_START_CLICK_GUARD_RMS_REL_MAX = 0.06
# Remove a loud, noise-like burst at the very end only when it is detached from
# speech by a clearly quiet gap. This targets TTS terminal hiss/static artifacts.
TTS_TAIL_DETACHED_NOISE_SCAN_MAX_MS = 300.0
TTS_TAIL_DETACHED_NOISE_GAP_MIN_MS = 80.0
TTS_TAIL_DETACHED_NOISE_MIN_MS = 40.0
TTS_TAIL_DETACHED_HF_RATIO_MIN = 0.28
TTS_TAIL_DETACHED_SPECTRAL_FLATNESS_MIN = 0.22
TTS_TAIL_DETACHED_ZERO_CROSSING_MIN = 0.13
VOICE_THREADS = 1

GEMINI_TEXT_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",  # Deprecated alias; current API routes it to gemini-3.8-flash.
    "gemini-3.6-flash",
    "gemini-3.5-flash",  # Deprecated alias; current API routes it to gemini-3.6-flash.
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]
# Keep the user's preferred ranking visible above, but do not call deprecated IDs
# whose current API endpoints redirect to another configured model: that would
# effectively retry the same backend after a slow/failing request.
GEMINI_MODEL_REDIRECTS = {
    "gemini-3.7-flash": "gemini-3.8-flash",
    "gemini-3.5-flash": "gemini-3.6-flash",
}
_current_gemini_text_model_idx = 0

# Model routing starts at the strongest configured model and falls down the list
# on errors/timeouts. Health is process-local: every new launch re-tests the strongest
# model instead of permanently blacklisting a model that may become available later.
GEMINI_TEXT_CALL_BUDGET_SECONDS = 72.0
GEMINI_TEXT_CONNECT_TIMEOUT_SECONDS = 5.0
GEMINI_TEXT_READ_TIMEOUT_SECONDS = 22.0
GEMINI_MODEL_SLOW_RESPONSE_SECONDS = 18.0
GEMINI_MODEL_SLOW_STREAK_LIMIT = 2
GEMINI_MODEL_FAILURE_STREAK_LIMIT = 2
GEMINI_MODEL_FAILURE_COOLDOWN_SECONDS = 90.0
GEMINI_MODEL_TIMEOUT_COOLDOWN_SECONDS = 120.0
GEMINI_MODEL_RATE_LIMIT_COOLDOWN_SECONDS = 75.0
GEMINI_ROUTER_RECOVERY_COOLDOWN_SECONDS = 120.0  # use Groq while Gemini models recover
_GEMINI_ROUTER_LOCK = threading.RLock()
_GEMINI_ROUTER_COOLDOWN_UNTIL = 0.0
_GEMINI_ROUTER_LAST_FAILURE = ""
_GEMINI_MODEL_HEALTH_LOCK = threading.RLock()
_GEMINI_MODEL_HEALTH: dict[str, dict[str, Any]] = {
    name: {
        "successes": 0, "failures": 0, "consecutive_failures": 0,
        "slow_streak": 0, "avg_latency": None, "cooldown_until": 0.0,
        "unsupported": False, "last_error": "",
    }
    for name in GEMINI_TEXT_MODELS
}

_gemini_dead_keys = set()
_gemini_key_cooldowns = {}
_gemini_key_index = 0
_gemini_key_lock = threading.RLock()

_gemini_proxy_index = 0
_gemini_proxy_cooldowns = {}
_gemini_proxy_in_use = set()
_gemini_proxy_lock = threading.RLock()

_groq_dead_keys: set[str] = set()
_groq_key_cooldowns: dict[str, float] = {}
_groq_key_index = 0
_groq_key_lock = threading.RLock()

# Per-thread telemetry lets a storyboard retry avoid the model that just returned
# malformed rows without changing the public string return value of call_gemini_api.
_gemini_call_context = threading.local()


def _get_proxy_label(proxy: str | None) -> str:
    if not proxy:
        return "Direct (No Proxy)"
    return proxy.split("@")[-1] if "@" in proxy else proxy


def _put_proxy_on_cooldown(proxy: str, reason: str, duration_sec: int = 180):
    label = _get_proxy_label(proxy)
    with _gemini_proxy_lock:
        _gemini_proxy_cooldowns[proxy] = time.time() + duration_sec
    log.warning(f"[Proxy Engine] ⏳ {label} на отдыхе {duration_sec}с (Причина: {reason})")


def _acquire_next_proxy(*, max_wait_seconds: float | None = None, log_unavailable: bool = True) -> str | None:
    global _gemini_proxy_index
    if not USE_GEMINI_PROXY or not GEMINI_PROXIES_LIST:
        return None

    while True:
        now = time.time()
        with _gemini_proxy_lock:
            total = len(GEMINI_PROXIES_LIST)
            for _ in range(total):
                candidate = GEMINI_PROXIES_LIST[_gemini_proxy_index % total]
                _gemini_proxy_index += 1
                if candidate in _gemini_proxy_in_use:
                    continue
                if _gemini_proxy_cooldowns.get(candidate, 0) > now:
                    continue
                _gemini_proxy_in_use.add(candidate)
                return candidate

            active_cooldowns = [t for p, t in _gemini_proxy_cooldowns.items() if t > now]
            wait_time = max(2, int(min(active_cooldowns) - now)) if active_cooldowns else 5

        if max_wait_seconds is not None and wait_time > max(0.0, float(max_wait_seconds)):
            if log_unavailable:
                log.warning(
                    "[Proxy Engine] Все прокси заняты/на паузе (%sс до повторной проверки); возвращаю отсутствие прокси. Прямое подключение не выполняется автоматически.",
                    wait_time,
                )
            return None
        log.warning(f"[Proxy Engine] ⏳ Все прокси в кулдауне. Пауза {wait_time}с...")
        time.sleep(min(wait_time, 15))


def _acquire_required_proxy(service: str, *, max_wait_seconds: float = 0.0) -> str | None:
    """Acquire a configured proxy or fail closed; cloud API calls must never go direct."""
    if not USE_GEMINI_PROXY or not GEMINI_PROXIES_LIST:
        log.error(
            "[Proxy Engine] %s: прокси не настроены/отключены. Прямое подключение запрещено; запрос не отправляю.",
            service,
        )
        return None

    wait_budget = max(0.0, float(max_wait_seconds))
    deadline = time.monotonic() + wait_budget
    while True:
        proxy = _acquire_next_proxy(max_wait_seconds=0, log_unavailable=False)
        if proxy:
            return proxy
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning(
                "[Proxy Engine] %s: свободный прокси не появился за %.0fс. Прямой маршрут запрещён.",
                service, wait_budget,
            )
            return None
        time.sleep(min(3.0, remaining))


def _release_proxy(proxy: str | None):
    if not proxy:
        return
    with _gemini_proxy_lock:
        _gemini_proxy_in_use.discard(proxy)


def _post_with_transient_retries(
    url: str,
    *,
    retry_limit: int | None = None,
    read_timeout_attempts: int | None = None,
    **kwargs,
):
    """Retry transient socket/proxy errors with a caller-specific bounded budget.

    HTTP status responses are returned immediately. A read timeout is not retried by
    callers that need to abandon a slow model promptly.
    """
    proxy_map = kwargs.get("proxies")
    proxy_url = (proxy_map.get("https") or proxy_map.get("http")) if isinstance(proxy_map, dict) else None
    label = _get_proxy_label(proxy_url)
    tries = max(1, int(retry_limit if retry_limit is not None else PROXY_REQUEST_RETRIES))
    read_tries = max(1, int(read_timeout_attempts if read_timeout_attempts is not None else PROXY_READ_TIMEOUT_ATTEMPTS))
    last_exc = None
    for attempt in range(1, tries + 1):
        try:
            return requests.post(url, **kwargs)
        except (
            requests.exceptions.ProxyError,
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ReadTimeout,
            requests.exceptions.ConnectionError,
        ) as exc:
            last_exc = exc
            allowed_attempts = min(tries, read_tries) if isinstance(exc, requests.exceptions.ReadTimeout) else tries
            if attempt >= allowed_attempts:
                break
            delay = PROXY_RETRY_BACKOFF_SECONDS[min(attempt - 1, len(PROXY_RETRY_BACKOFF_SECONDS) - 1)]
            log.warning(
                "[Proxy Engine] Temporary %s via %s; retrying same endpoint %s/%s in %.1fs",
                type(exc).__name__, label, attempt + 1, allowed_attempts, delay,
            )
            time.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("HTTP POST failed without an exception")


def _kill_gemini_key(key: str, reason: str, is_temporary: bool = False, cooldown_minutes: int = 30):
    key_tag = f"...{key[-8:]}"
    if is_temporary:
        unlock_time = time.time() + (cooldown_minutes * 60)
        _gemini_key_cooldowns[key] = unlock_time
        log.warning(f"[Key Engine] ⏳ Ключ {key_tag} на паузе {cooldown_minutes} мин. ({reason})")
    else:
        _gemini_dead_keys.add(key)
        alive = len(GEMINI_API_KEYS) - len(_gemini_dead_keys)
        log.warning(f"[Key Engine] 🚫 Ключ {key_tag} исключен навсегда ({reason}). Живых: {alive}/{len(GEMINI_API_KEYS)}")


def _get_next_gemini_key(*, max_wait_seconds: float | None = None) -> str | None:
    global _gemini_key_index
    with _gemini_key_lock:
        now = time.time()
        active = [
            k for k in GEMINI_API_KEYS
            if k not in _gemini_dead_keys and _gemini_key_cooldowns.get(k, 0) <= now
        ]
        if active:
            key = active[_gemini_key_index % len(active)]
            _gemini_key_index += 1
            return key

        if len(_gemini_dead_keys) >= len(GEMINI_API_KEYS):
            log.error("[Key Engine] ❌ Все ключи Gemini исключены.")
            return None

        next_unlock = min(t for k, t in _gemini_key_cooldowns.items() if k not in _gemini_dead_keys)
        sleep_sec = max(1, int(next_unlock - now))

    if max_wait_seconds is not None and sleep_sec > max(0.0, float(max_wait_seconds)):
        log.warning("[Key Engine] Все ключи временно заняты/на паузе (%sс до ближайшего). Возвращаю управление без ожидания.", sleep_sec)
        return None
    if sleep_sec > 45:
        log.warning(f"[Key Engine] ⏳ Ключи на длительном кулдауне ({sleep_sec}с) — отдаём дефолтный шаблон.")
        return None

    log.warning(f"[Key Engine] ⏳ Ожидание ключей: {sleep_sec}с...")
    time.sleep(sleep_sec)

    with _gemini_key_lock:
        active = [
            k for k in GEMINI_API_KEYS
            if k not in _gemini_dead_keys and _gemini_key_cooldowns.get(k, 0) <= time.time()
        ]
        if active:
            key = active[_gemini_key_index % len(active)]
            _gemini_key_index += 1
            return key

    return None


# ==============================================================================
# 🧩 GEMINI TEXT API
# ==============================================================================

def _gemini_model_state(model_name: str) -> dict[str, Any]:
    with _GEMINI_MODEL_HEALTH_LOCK:
        return dict(_GEMINI_MODEL_HEALTH.setdefault(model_name, {
            "successes": 0, "failures": 0, "consecutive_failures": 0,
            "slow_streak": 0, "avg_latency": None, "cooldown_until": 0.0,
            "unsupported": False, "last_error": "",
        }))


def _record_gemini_model_success(model_name: str, elapsed: float) -> None:
    now = time.time()
    with _GEMINI_MODEL_HEALTH_LOCK:
        state = _GEMINI_MODEL_HEALTH.setdefault(model_name, {})
        state["successes"] = int(state.get("successes", 0)) + 1
        state["consecutive_failures"] = 0
        old_avg = state.get("avg_latency")
        state["avg_latency"] = float(elapsed) if old_avg is None else (0.70 * float(old_avg) + 0.30 * float(elapsed))
        state["slow_streak"] = int(state.get("slow_streak", 0)) + 1 if elapsed >= GEMINI_MODEL_SLOW_RESPONSE_SECONDS else 0
        state["last_error"] = ""
        state["cooldown_until"] = 0.0
        if int(state["slow_streak"]) >= GEMINI_MODEL_SLOW_STREAK_LIMIT:
            state["cooldown_until"] = now + GEMINI_MODEL_TIMEOUT_COOLDOWN_SECONDS
            log.warning(
                "[Gemini Model Router] %s отвечает медленно несколько раз (EWMA %.1fs); пауза модели на %.0fс, пока используем следующую.",
                model_name, float(state["avg_latency"]), GEMINI_MODEL_TIMEOUT_COOLDOWN_SECONDS,
            )


def _record_gemini_model_failure(
    model_name: str,
    reason: str,
    *,
    cooldown_seconds: float | None = None,
    unsupported: bool = False,
) -> None:
    now = time.time()
    reason_short = re.sub(r"\s+", " ", str(reason or "unknown failure"))[:240]
    with _GEMINI_MODEL_HEALTH_LOCK:
        state = _GEMINI_MODEL_HEALTH.setdefault(model_name, {})
        state["failures"] = int(state.get("failures", 0)) + 1
        state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
        state["last_error"] = reason_short
        if unsupported:
            state["unsupported"] = True
            state["cooldown_until"] = 0.0
        elif cooldown_seconds is not None:
            state["cooldown_until"] = max(float(state.get("cooldown_until", 0.0)), now + max(1.0, float(cooldown_seconds)))
        elif int(state["consecutive_failures"]) >= GEMINI_MODEL_FAILURE_STREAK_LIMIT:
            state["cooldown_until"] = now + GEMINI_MODEL_FAILURE_COOLDOWN_SECONDS

    if unsupported:
        log.warning(
            "[Gemini Model Router] Модель %s недоступна для текущего API/ключа и будет пропущена до следующего запуска: %s",
            model_name, reason_short,
        )
    elif cooldown_seconds is not None:
        log.warning(
            "[Gemini Model Router] %s уходит на паузу %.0fс после ошибки: %s",
            model_name, cooldown_seconds, reason_short,
        )
    else:
        log.warning(
            "[Gemini Model Router] Сбой модели %s (%s подряд): %s",
            model_name, _gemini_model_state(model_name).get("consecutive_failures", 0), reason_short,
        )


def _gemini_model_is_unavailable_response(status_code: int, response_text: str, model_name: str) -> bool:
    lowered = str(response_text or "").casefold()
    if status_code == 404:
        return True
    patterns = (
        "model not found", "unknown model", "unknown name", "unsupported model", "model is not supported",
        "not found for api version", "does not exist", "models/" + model_name.casefold() + " is not found",
        "is not found for generatecontent", "is not supported for generatecontent",
        "model is unavailable", "model unavailable", "unknown model name",
        f"{model_name.casefold()} is not found", f"{model_name.casefold()} is not supported",
    )
    return any(pattern in lowered for pattern in patterns)


def _effective_gemini_model_order() -> list[str]:
    """Normalize deprecated aliases and deduplicate endpoints while preserving priority."""
    effective: list[str] = []
    for configured in GEMINI_TEXT_MODELS:
        target = GEMINI_MODEL_REDIRECTS.get(configured, configured)
        if target not in effective:
            effective.append(target)
    return effective


def _ordered_gemini_models_for_call(exclude_models: set[str] | None = None) -> list[str]:
    """Keep configured quality order while skipping cooldown, unknown and caller-excluded models."""
    now = time.time()
    effective_order = _effective_gemini_model_order()
    excluded = set(exclude_models or ())
    with _GEMINI_MODEL_HEALTH_LOCK:
        supported = [
            model for model in effective_order
            if not _GEMINI_MODEL_HEALTH.setdefault(model, {}).get("unsupported", False)
            and model not in excluded
        ]
        usable = [
            model for model in supported
            if float(_GEMINI_MODEL_HEALTH.setdefault(model, {}).get("cooldown_until", 0.0) or 0.0) <= now
        ]
        if usable:
            return usable
        if not supported:
            return []
        # Do not hammer endpoints that the router has just marked unavailable.
        # call_text_api will use its one-shot Groq fallback until a Gemini model cooldown expires.
        cooldowns = [
            float(_GEMINI_MODEL_HEALTH.setdefault(model, {}).get("cooldown_until", 0.0) or 0.0)
            for model in supported
        ]
        next_retry = max(0.0, min(cooldowns) - now) if cooldowns else 0.0
        log.info(
            "[Gemini Model Router] Все поддерживаемые модели на cooldown; Gemini пропускается на %.0fс, текущие текстовые задачи передаются Groq.",
            next_retry,
        )
        return []


def call_gemini_api(prompt: str, *, exclude_models: set[str] | None = None) -> str | None:
    """Use strongest available text model first; quickly fall back on slow/failed models.

    Unsupported model IDs are skipped for this process after a clear 400/404 model error.
    Repeated server errors or slow successes trigger a temporary cooldown. The router has
    a total per-call budget so one unavailable model cannot stall the whole pipeline.
    """
    global _current_gemini_text_model_idx, _GEMINI_ROUTER_COOLDOWN_UNTIL, _GEMINI_ROUTER_LAST_FAILURE
    _gemini_call_context.last_model = None
    call_started = time.monotonic()

    with _GEMINI_ROUTER_LOCK:
        router_wait = max(0.0, _GEMINI_ROUTER_COOLDOWN_UNTIL - time.time())
        router_reason = _GEMINI_ROUTER_LAST_FAILURE
    if router_wait > 0:
        log.info(
            "[Gemini Circuit] Gemini временно на паузе ещё %.0fс после общего сбоя (%s); для этой операции будет использован Groq fallback.",
            router_wait, router_reason or "temporary provider outage",
        )
        return None

    # Fail closed before selecting a model/key: Google API requests are never sent directly.
    if not USE_GEMINI_PROXY or not GEMINI_PROXIES_LIST:
        log.error("[Gemini Text] Нет настроенного прокси; прямые Gemini-запросы запрещены.")
        return None

    deadline = call_started + float(GEMINI_TEXT_CALL_BUDGET_SECONDS)
    models = _ordered_gemini_models_for_call(exclude_models)
    if not models:
        reason = "все поддерживаемые модели исключены для текущего retry" if exclude_models else "ни одна модель не доступна"
        log.warning("[Gemini Model Router] %s.", reason)
        return None

    last_errors: list[str] = []
    attempted_model_requests = 0
    for model_name in models:
        remaining = deadline - time.monotonic()
        if remaining < 4.0:
            log.warning("[Gemini Model Router] Достигнут лимит времени %.0fс на одну операцию; прекращаю перебор моделей.", GEMINI_TEXT_CALL_BUDGET_SECONDS)
            break

        # API key restrictions/quota often affect only one key, not the model. On 403/429
        # try one more key for the same high-priority model before descending to a smaller one.
        for key_attempt in range(1, 3):
            remaining = deadline - time.monotonic()
            if remaining < 4.0:
                break
            api_key = _get_next_gemini_key(max_wait_seconds=0)
            if not api_key:
                log.warning("[Gemini Text] Нет свободного API-ключа; останавливаю вызов без ожидания очереди ключей.")
                return None

            # Gemini text must use an explicitly configured proxy. If none is free,
            # stop this provider attempt and let call_text_api use the Groq fallback.
            proxy = _acquire_required_proxy("Gemini Text", max_wait_seconds=0.0)
            if not proxy:
                last_errors.append("no proxy available (direct route disabled)")
                log.warning("[Gemini Text] Нет свободного прокси; Gemini не отправляется напрямую.")
                return None
            p_label = _get_proxy_label(proxy)
            proxies = {"http": proxy, "https": proxy}
            key_tag = f"...{api_key[-8:]}"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
            headers = {"Content-Type": "application/json"}
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.6, "topP": 0.9, "maxOutputTokens": 8192},
            }
            connect_timeout = min(GEMINI_TEXT_CONNECT_TIMEOUT_SECONDS, max(2.0, remaining * 0.15))
            read_timeout = min(GEMINI_TEXT_READ_TIMEOUT_SECONDS, max(3.0, remaining - connect_timeout - 0.5))
            request_started = time.monotonic()
            attempted_model_requests += 1
            try:
                r = _post_with_transient_retries(
                    f"{url}?key={api_key}",
                    headers=headers,
                    json=payload,
                    timeout=(connect_timeout, read_timeout),
                    proxies=proxies,
                    retry_limit=2,
                    read_timeout_attempts=1,
                )
                elapsed = time.monotonic() - request_started
                response_text = str(getattr(r, "text", "") or "")

                if r.status_code == 200:
                    try:
                        data = r.json()
                    except Exception as exc:
                        _record_gemini_model_failure(model_name, f"invalid JSON response: {exc}")
                        last_errors.append(f"{model_name}: invalid JSON")
                        break
                    candidates = data.get("candidates", []) if isinstance(data, dict) else []
                    result_text = ""
                    if isinstance(candidates, list) and candidates:
                        content = candidates[0].get("content", {}) if isinstance(candidates[0], dict) else {}
                        parts = content.get("parts", []) if isinstance(content, dict) else []
                        if isinstance(parts, list):
                            result_text = "\n".join(str(part.get("text", "")) for part in parts if isinstance(part, dict) and part.get("text")).strip()
                    if result_text:
                        _record_gemini_model_success(model_name, elapsed)
                        with _GEMINI_ROUTER_LOCK:
                            _GEMINI_ROUTER_COOLDOWN_UNTIL = 0.0
                            _GEMINI_ROUTER_LAST_FAILURE = ""
                        _current_gemini_text_model_idx = GEMINI_TEXT_MODELS.index(model_name)
                        _gemini_call_context.last_model = model_name
                        router_position = models.index(model_name) + 1
                        note = "" if router_position == 1 else f"; fallback #{router_position}"
                        log.info(
                            "✅ [Gemini Text] %s%s %.2fs (%s символов, proxy=%s)",
                            model_name, note, elapsed, len(result_text), p_label,
                        )
                        return result_text
                    feedback = data.get("promptFeedback", {}) if isinstance(data, dict) else {}
                    reason = feedback.get("blockReason", "empty candidates/parts") if isinstance(feedback, dict) else "empty candidates/parts"
                    _record_gemini_model_failure(model_name, f"empty response: {reason}")
                    last_errors.append(f"{model_name}: empty response ({reason})")
                    break

                if _gemini_model_is_unavailable_response(r.status_code, response_text, model_name):
                    _record_gemini_model_failure(model_name, f"HTTP {r.status_code}: {response_text[:180]}", unsupported=True)
                    last_errors.append(f"{model_name}: model ID unavailable")
                    break

                if r.status_code == 429:
                    _kill_gemini_key(api_key, "лимит 429", is_temporary=True, cooldown_minutes=5)
                    if key_attempt < 2:
                        log.warning("[Gemini Text] HTTP 429 на ключе %s для %s; проверяю тот же приоритетный endpoint на другом ключе (%s/2).", key_tag, model_name, key_attempt + 1)
                        continue
                    _record_gemini_model_failure(model_name, f"HTTP 429 on two keys: {response_text[:180]}", cooldown_seconds=GEMINI_MODEL_RATE_LIMIT_COOLDOWN_SECONDS)
                    last_errors.append(f"{model_name}: rate limited")
                    break

                if r.status_code == 403:
                    detail = re.sub(r"\s+", " ", response_text)[:220]
                    _kill_gemini_key(api_key, f"HTTP 403 (API access): {detail}", is_temporary=True, cooldown_minutes=15)
                    if key_attempt < 2:
                        log.warning("[Gemini Text] HTTP 403 для ключа %s; пробую тот же endpoint %s на другом ключе.", key_tag, model_name)
                        continue
                    # No model-level blacklist for a key/project permission error.
                    last_errors.append(f"{model_name}: API key/project permission denied")
                    break

                if r.status_code == 400 and "api_key_invalid" in response_text.casefold():
                    _kill_gemini_key(api_key, "невалидный ключ 400", is_temporary=False)
                    if key_attempt < 2:
                        continue
                    last_errors.append(f"{model_name}: invalid API key")
                    break

                reason = f"HTTP {r.status_code}: {response_text[:180]}"
                _record_gemini_model_failure(model_name, reason)
                last_errors.append(f"{model_name}: HTTP {r.status_code}")
                break

            except requests.exceptions.ReadTimeout as exc:
                if proxy:
                    _put_proxy_on_cooldown(proxy, "Gemini text read timeout; endpoint did not answer inside model budget", duration_sec=PROXY_TIMEOUT_COOLDOWN_SECONDS)
                _record_gemini_model_failure(
                    model_name, f"ReadTimeout after {time.monotonic() - request_started:.1f}s",
                    cooldown_seconds=GEMINI_MODEL_TIMEOUT_COOLDOWN_SECONDS,
                )
                last_errors.append(f"{model_name}: ReadTimeout")
                break
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ProxyError, requests.exceptions.ConnectionError) as exc:
                if proxy:
                    _put_proxy_on_cooldown(proxy, f"{type(exc).__name__} during Gemini text call", duration_sec=PROXY_CONNECT_COOLDOWN_SECONDS)
                log.warning("[Gemini Text] Сеть/прокси не ответили для %s: %s; перехожу к следующему endpoint без ожидания полной серии повторов.", model_name, type(exc).__name__)
                last_errors.append(f"{model_name}: {type(exc).__name__}")
                break
            except Exception as exc:
                _record_gemini_model_failure(model_name, f"{type(exc).__name__}: {exc}")
                last_errors.append(f"{model_name}: {type(exc).__name__}")
                log.warning("[Gemini Text] Ошибка модели %s: %s", model_name, exc)
                break
            finally:
                _release_proxy(proxy)

        if time.monotonic() >= deadline:
            break
        # Tiny inter-model pause gives the key/proxy state a scheduling turn without
        # introducing multi-second sleeps between fallback models.
        time.sleep(0.15)

    if last_errors:
        error_summary = "; ".join(last_errors[-8:])
        log.error(
            "[Gemini Model Router] Не удалось выполнить операцию за %.1fс; проверенные модели: %s",
            time.monotonic() - call_started, error_summary,
        )
        if attempted_model_requests > 0:
            with _GEMINI_ROUTER_LOCK:
                _GEMINI_ROUTER_COOLDOWN_UNTIL = max(
                    _GEMINI_ROUTER_COOLDOWN_UNTIL,
                    time.time() + GEMINI_ROUTER_RECOVERY_COOLDOWN_SECONDS,
                )
                _GEMINI_ROUTER_LAST_FAILURE = error_summary[:220]
            log.warning(
                "[Gemini Circuit] Все проверенные Gemini-модели не ответили. На %.0fс переключаю текстовые задачи на Groq; после паузы снова проверю Gemini.",
                GEMINI_ROUTER_RECOVERY_COOLDOWN_SECONDS,
            )
    return None


def _get_next_groq_key(*, max_wait_seconds: float | None = 0) -> str | None:
    """Round-robin Groq key selection with temporary cooldowns and invalid-key quarantine."""
    global _groq_key_index
    with _groq_key_lock:
        now = time.time()
        active = [
            key for key in GROQ_API_KEYS
            if key not in _groq_dead_keys and float(_groq_key_cooldowns.get(key, 0.0) or 0.0) <= now
        ]
        if active:
            key = active[_groq_key_index % len(active)]
            _groq_key_index += 1
            return key

        viable = [key for key in GROQ_API_KEYS if key not in _groq_dead_keys]
        if not viable:
            log.error("[Groq Key Engine] Нет доступных Groq API-ключей: список пуст или все ключи признаны невалидными.")
            return None
        unlocks = [float(_groq_key_cooldowns.get(key, 0.0) or 0.0) for key in viable]
        sleep_seconds = max(1.0, min(unlocks) - now) if unlocks else 1.0

    if max_wait_seconds is not None and sleep_seconds > max(0.0, float(max_wait_seconds)):
        log.warning("[Groq Key Engine] Все ключи временно на паузе ещё примерно %.0fс.", sleep_seconds)
        return None
    time.sleep(min(sleep_seconds, 15.0))
    return _get_next_groq_key(max_wait_seconds=0)


def _cooldown_groq_key(key: str, reason: str, duration_seconds: float, *, permanent: bool = False) -> None:
    tag = f"...{key[-8:]}"
    with _groq_key_lock:
        if permanent:
            _groq_dead_keys.add(key)
            _groq_key_cooldowns.pop(key, None)
            log.error("[Groq Key Engine] Ключ %s исключён как невалидный: %s | активных=%s/%s", tag, reason, len(GROQ_API_KEYS) - len(_groq_dead_keys), len(GROQ_API_KEYS))
        else:
            _groq_key_cooldowns[key] = time.time() + max(1.0, float(duration_seconds))
            log.warning("[Groq Key Engine] Ключ %s на паузе %.0fс: %s", tag, duration_seconds, reason)


def call_groq_api(prompt: str, *, proxy_wait_seconds: float = GROQ_FALLBACK_PROXY_WAIT_SECONDS) -> str | None:
    """Text-only Groq completion with key rotation; proxy is mandatory, never direct."""
    if not GROQ_API_KEYS:
        log.error("[Groq] Нет API-ключей. Создай groq_keys.txt и добавь по одному ключу на строку: %s", GROQ_KEYS_FILE)
        return None
    if not USE_GEMINI_PROXY or not GEMINI_PROXIES_LIST:
        log.error("[Groq] Нет настроенного прокси. Прямой Groq-запрос запрещён; fallback не отправляется.")
        return None

    call_started = time.monotonic()
    wait_budget = max(0.0, float(proxy_wait_seconds))
    deadline = call_started + wait_budget + float(GROQ_TEXT_CALL_BUDGET_SECONDS)
    last_errors: list[str] = []
    attempted_keys: set[str] = set()
    proxy_attempt_budget = min(3, len(GEMINI_PROXIES_LIST))
    fatal_model_error = False
    proxy_unavailable = False
    proxy_wait_used = False

    while time.monotonic() < deadline and len(attempted_keys) < len(GROQ_API_KEYS):
        key = _get_next_groq_key(max_wait_seconds=0)
        if not key or key in attempted_keys:
            break
        attempted_keys.add(key)
        key_tag = f"...{key[-8:]}"

        # A network failure tries the next proxy with the SAME Groq key first.
        # API-level 401/403/429 responses rotate the key instead of blaming the proxy.
        for proxy_attempt in range(1, proxy_attempt_budget + 1):
            if time.monotonic() >= deadline:
                break
            remaining_proxy_wait = 0.0 if proxy_wait_used else max(0.0, deadline - time.monotonic() - GROQ_TEXT_CALL_BUDGET_SECONDS)
            proxy = _acquire_required_proxy(
                "Groq Text",
                max_wait_seconds=remaining_proxy_wait,
            )
            if not proxy:
                last_errors.append("no proxy available (direct route disabled)")
                log.warning("[Groq] Нет доступного прокси; прямой запрос не выполняю.")
                proxy_unavailable = True
                break
            proxy_wait_used = True
            proxy_label = _get_proxy_label(proxy)
            proxy_map = {"http": proxy, "https": proxy}
            remaining = max(4.0, deadline - time.monotonic())
            timeout = (
                min(GROQ_TEXT_CONNECT_TIMEOUT_SECONDS, max(2.0, remaining * 0.15)),
                min(GROQ_TEXT_READ_TIMEOUT_SECONDS, max(3.0, remaining - GROQ_TEXT_CONNECT_TIMEOUT_SECONDS - 0.5)),
            )
            try:
                response = _post_with_transient_retries(
                    GROQ_API_URL,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": GROQ_TEXT_MODEL,
                        "messages": [{"role": "user", "content": str(prompt or "")}],
                        "temperature": 0.6,
                        "max_completion_tokens": 8192,
                        "stream": False,
                    },
                    timeout=timeout,
                    proxies=proxy_map,
                    retry_limit=2,
                    read_timeout_attempts=1,
                )
                elapsed = time.monotonic() - call_started
                body_text = str(getattr(response, "text", "") or "")

                if response.status_code == 200:
                    try:
                        data = response.json()
                        choices = data.get("choices", []) if isinstance(data, dict) else []
                        message = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
                        content = message.get("content", "") if isinstance(message, dict) else ""
                        if isinstance(content, list):
                            content = "\n".join(
                                str(part.get("text", "")) for part in content
                                if isinstance(part, dict) and part.get("text")
                            )
                        result = str(content or "").strip()
                    except Exception as exc:
                        last_errors.append(f"invalid response JSON: {type(exc).__name__}")
                        log.warning("[Groq] Не удалось разобрать JSON-ответ: %s", exc)
                        break

                    if result:
                        _gemini_call_context.last_model = f"groq:{GROQ_TEXT_MODEL}"
                        log.info(
                            "✅ [Groq Text] %s ответил за %.2fс (%s символов, ключ=%s, proxy=%s)",
                            GROQ_TEXT_MODEL, elapsed, len(result), key_tag, proxy_label,
                        )
                        return result
                    last_errors.append("empty completion content")
                    log.warning("[Groq] Модель вернула пустой content (ключ=%s).", key_tag)
                    break

                if response.status_code == 401:
                    _cooldown_groq_key(key, f"HTTP 401: {body_text[:180]}", 0, permanent=True)
                    last_errors.append("HTTP 401 invalid API key")
                    break
                if response.status_code == 403:
                    _cooldown_groq_key(key, f"HTTP 403: {body_text[:180]}", GROQ_KEY_AUTH_COOLDOWN_SECONDS)
                    last_errors.append("HTTP 403 access denied")
                    break
                if response.status_code == 429:
                    _cooldown_groq_key(key, f"HTTP 429 rate limit: {body_text[:180]}", GROQ_KEY_RATE_LIMIT_COOLDOWN_SECONDS)
                    last_errors.append("HTTP 429 rate limited")
                    break
                if response.status_code in {400, 404}:
                    # An unsupported/retired model ID is not fixed by rotating a key or proxy.
                    last_errors.append(f"HTTP {response.status_code}: {body_text[:260]}")
                    log.error(
                        "[Groq] Модель %s отклонила запрос (HTTP %s). Смена ключа/прокси это не исправит: %s",
                        GROQ_TEXT_MODEL, response.status_code, re.sub(r"\s+", " ", body_text)[:260],
                    )
                    fatal_model_error = True
                    break
                if response.status_code >= 500:
                    last_errors.append(f"HTTP {response.status_code}")
                    log.warning("[Groq] Сервер вернул HTTP %s (ключ=%s, proxy=%s); пробую следующий маршрут.", response.status_code, key_tag, proxy_label)
                    if proxy is None:
                        break
                    continue

                last_errors.append(f"HTTP {response.status_code}: {body_text[:180]}")
                log.error("[Groq] Неожиданный HTTP %s: %s", response.status_code, body_text[:220])
                break

            except requests.exceptions.ReadTimeout as exc:
                if proxy:
                    _put_proxy_on_cooldown(proxy, "Groq text read timeout", duration_sec=PROXY_TIMEOUT_COOLDOWN_SECONDS)
                last_errors.append(f"ReadTimeout via {proxy_label}")
                log.warning("[Groq] ReadTimeout через %s; %s/%s попытка с другим маршрутом.", proxy_label, proxy_attempt, proxy_attempt_budget)
                if proxy is None:
                    break
                continue
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ProxyError, requests.exceptions.ConnectionError) as exc:
                if proxy:
                    _put_proxy_on_cooldown(proxy, f"{type(exc).__name__} during Groq text call", duration_sec=PROXY_CONNECT_COOLDOWN_SECONDS)
                last_errors.append(f"{type(exc).__name__} via {proxy_label}")
                log.warning("[Groq] Сетевая ошибка через %s: %s; пробую следующий прокси с тем же ключом.", proxy_label, type(exc).__name__)
                if proxy is None:
                    break
                continue
            except Exception as exc:
                last_errors.append(f"{type(exc).__name__}: {exc}")
                log.exception("[Groq] Неожиданная ошибка запроса.")
                break
            finally:
                _release_proxy(proxy)

        if fatal_model_error or proxy_unavailable:
            break

    log.error(
        "[Groq] Не удалось выполнить текстовый запрос за %.1fс; проверено ключей=%s/%s; ошибки=%s",
        time.monotonic() - call_started, len(attempted_keys), len(GROQ_API_KEYS), "; ".join(last_errors[-8:]) or "no usable key",
    )
    return None


def normalize_text_provider(provider: str | None) -> str:
    value = str(provider or "gemini").strip().casefold()
    return value if value in {"gemini", "groq"} else "gemini"


def set_active_text_provider(provider: str | None) -> str:
    """Set the selected text-model backend; Gemini remains the default."""
    global ACTIVE_TEXT_PROVIDER
    ACTIVE_TEXT_PROVIDER = normalize_text_provider(provider)
    return ACTIVE_TEXT_PROVIDER


def call_text_api(prompt: str, *, exclude_models: set[str] | None = None) -> str | None:
    """Use the selected provider; when Gemini fails, make one Groq fallback for this operation.

    ACTIVE_TEXT_PROVIDER remains Gemini. Thus the next independent text operation probes
    Gemini again after its circuit/model cooldown, and only falls back to Groq if needed.
    """
    if ACTIVE_TEXT_PROVIDER == "groq":
        _gemini_call_context.last_model = None
        return call_groq_api(prompt)

    gemini_result = call_gemini_api(prompt, exclude_models=exclude_models)
    if gemini_result and gemini_result.strip():
        return gemini_result

    if not GROQ_FALLBACK_ON_GEMINI_FAILURE:
        return None
    if not GROQ_API_KEYS:
        log.error("[Text Provider] Gemini не ответил, но Groq fallback отключён: нет Groq API-ключей.")
        return None

    log.warning(
        "[Text Provider] Gemini не ответил для текущей текстовой операции; делаю один Groq fallback. Следующая операция снова сначала проверит Gemini."
    )
    groq_result = call_groq_api(prompt, proxy_wait_seconds=GROQ_FALLBACK_PROXY_WAIT_SECONDS)
    if groq_result and groq_result.strip():
        log.info("[Text Provider] ✅ Текущая операция выполнена через Groq fallback; выбранный провайдер Gemini не меняется.")
        return groq_result
    log.error("[Text Provider] Не ответили ни Gemini, ни одноразовый Groq fallback для текущей текстовой операции.")
    return None


# ==============================================================================
# 🎙️️ GEMINI TTS (DSP S-CURVE SMOOTHING + DC OFFSET REMOVAL)
# ==============================================================================

def split_text_into_chunks(text: str, max_chunk_size=MAX_AUDIO_CHUNK_SIZE) -> list[str]:
    if not text:
        return []

    # Убираем разметку, длинные тире и многоточия, провоцирующие зависание генератора
    cleaned = text.replace("**", "").replace("*", "").replace("...", ".")
    cleaned = re.sub(r"—+", "-", cleaned)
    cleaned = re.sub(r"[\t\r\n]+", " ", cleaned.strip())
    cleaned = re.sub(r"\s{2,}", " ", cleaned)

    sentences = re.split(r"(?<=[.!?])(?:\s+|$)", cleaned)
    sentences = [s.strip() for s in sentences if s.strip()]

    # A single long sentence must not bypass MAX_AUDIO_CHUNK_SIZE. Split it at
    # clause punctuation first, then at word boundaries as a final fallback.
    bounded_sentences: list[str] = []
    for sentence in sentences:
        if len(sentence) <= max_chunk_size:
            bounded_sentences.append(sentence)
            continue
        clauses = [part.strip() for part in re.split(r"(?<=[,;:])\s+", sentence) if part.strip()]
        for clause in clauses:
            if len(clause) <= max_chunk_size:
                bounded_sentences.append(clause)
                continue
            fragment = ""
            for word in clause.split():
                candidate = f"{fragment} {word}".strip()
                if fragment and len(candidate) > max_chunk_size:
                    bounded_sentences.append(fragment)
                    fragment = word
                else:
                    fragment = candidate
            if fragment:
                bounded_sentences.append(fragment)
    sentences = bounded_sentences

    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for sentence in sentences:
        sentence_size = len(sentence) + (1 if current else 0)
        if current and size + sentence_size > max_chunk_size:
            chunks.append(" ".join(current))
            current = []
            size = 0

        current.append(sentence)
        size += sentence_size

    if current:
        chunks.append(" ".join(current))

    return chunks

def _audio_frame_hiss_metrics(frame, rate: int) -> tuple[float, float]:
    """Return (RMS, high-frequency power ratio) for a short mono int16 frame."""
    import numpy as np

    if frame.size == 0:
        return 0.0, 0.0
    values = frame.astype(np.float32, copy=False)
    values = values - float(np.mean(values))
    rms = float(np.sqrt(np.mean(values * values)))
    if rms < 1.0:
        return rms, 0.0
    # A Hann window prevents edge discontinuities from looking like broadband noise.
    window = np.hanning(len(values)).astype(np.float32)
    spectrum = np.fft.rfft(values * window)
    power = np.abs(spectrum) ** 2
    total = float(np.sum(power))
    if total <= 1e-9:
        return rms, 0.0
    freqs = np.fft.rfftfreq(len(values), d=1.0 / float(rate))
    high = float(np.sum(power[freqs >= 3500.0]))
    return rms, high / total


def _detect_edge_hiss_trim_samples(audio, rate: int) -> tuple[int, int]:
    """Conservatively identify low-level broadband hiss at the very edges.

    This is deliberately not a general silence remover. It scans only a short
    leading/trailing window, requires multiple low-level high-frequency frames,
    and preserves a small section at the boundary as a guard against trimming a
    quiet initial/final consonant.
    """
    import numpy as np

    if audio.size < int(rate * 0.1):
        return 0, 0

    frame_size = max(64, int(rate * TTS_EDGE_FRAME_MS / 1000.0))
    full_frames = [audio[i:i + frame_size] for i in range(0, len(audio) - frame_size + 1, frame_size)]
    if len(full_frames) < 5:
        return 0, 0

    rms_values = np.asarray([float(np.sqrt(np.mean((f.astype(np.float32) - np.mean(f)) ** 2))) for f in full_frames])
    active = rms_values[rms_values > 100.0]
    if active.size == 0:
        return 0, 0
    speech_reference_rms = float(np.percentile(active, 70))
    rms_threshold = min(float(TTS_HISS_ABSOLUTE_RMS_MAX), speech_reference_rms * float(TTS_HISS_RELATIVE_RMS_MAX))
    if rms_threshold <= 0:
        return 0, 0

    def is_hiss(frame) -> bool:
        rms, high_ratio = _audio_frame_hiss_metrics(frame, rate)
        return rms <= rms_threshold and high_ratio >= float(TTS_HISS_HIGH_FREQUENCY_RATIO_MIN)

    max_head_frames = int(math.ceil(TTS_START_HISS_SCAN_MAX_MS / TTS_EDGE_FRAME_MS)) if TTS_START_HISS_SCAN_MAX_MS > 0 else 0
    min_head_frames = max(1, int(math.ceil(TTS_START_HISS_MIN_RUN_MS / TTS_EDGE_FRAME_MS)))
    head_count = 0
    for i in range(min(max_head_frames, len(full_frames))):
        if is_hiss(full_frames[i]):
            head_count += 1
        else:
            break
    trim_head = 0
    if head_count >= min_head_frames:
        run_samples = head_count * frame_size
        keep_samples = int(rate * TTS_START_HISS_KEEP_MS / 1000.0)
        trim_head = max(0, run_samples - keep_samples)

    max_tail_frames = int(math.ceil(TTS_TAIL_HISS_SCAN_MAX_MS / TTS_EDGE_FRAME_MS)) if TTS_TAIL_HISS_SCAN_MAX_MS > 0 else 0
    min_tail_frames = max(1, int(math.ceil(TTS_TAIL_HISS_MIN_RUN_MS / TTS_EDGE_FRAME_MS)))
    tail_count = 0
    for offset in range(1, min(max_tail_frames, len(full_frames)) + 1):
        if is_hiss(full_frames[-offset]):
            tail_count += 1
        else:
            break
    trim_tail = 0
    if tail_count >= min_tail_frames:
        run_samples = tail_count * frame_size
        keep_samples = int(rate * TTS_TAIL_HISS_KEEP_MS / 1000.0)
        trim_tail = max(0, run_samples - keep_samples)

    # Never trim so much that the chunk becomes unexpectedly short.
    max_total = max(0, len(audio) - int(rate * 0.15))
    trim_head = min(trim_head, max_total)
    trim_tail = min(trim_tail, max(0, max_total - trim_head))
    return int(trim_head), int(trim_tail)


def _detect_start_click_trim_samples(audio, rate: int) -> int:
    """Detect a tiny isolated impulse before speech, without trimming a normal onset."""
    import numpy as np

    scan_n = int(rate * TTS_START_CLICK_SCAN_MS / 1000.0)
    after_n = int(rate * 0.030)
    guard_n = int(rate * 0.080)
    if scan_n < 8 or len(audio) < guard_n or after_n <= scan_n:
        return 0

    first = np.asarray(audio[:scan_n], dtype=np.float32)
    following = np.asarray(audio[scan_n:after_n], dtype=np.float32)
    guard = np.asarray(audio[scan_n:guard_n], dtype=np.float32)
    if not first.size or not following.size or not guard.size:
        return 0

    frame_n = max(1, int(rate * 0.020))
    rms_frames = [
        float(np.sqrt(np.mean(np.asarray(audio[i:i + frame_n], dtype=np.float32) ** 2)))
        for i in range(0, len(audio) - frame_n + 1, frame_n)
    ]
    active = np.asarray([value for value in rms_frames if value > 0.01], dtype=np.float32)
    if active.size < 3:
        return 0
    speech_reference = float(np.percentile(active, 60))

    first_peak = float(np.max(np.abs(first)))
    first_rms = float(np.sqrt(np.mean(first ** 2)))
    following_rms = float(np.sqrt(np.mean(following ** 2)))
    guard_rms = float(np.sqrt(np.mean(guard ** 2)))

    isolated_impulse = (
        first_peak >= max(0.025, speech_reference * TTS_START_CLICK_MIN_REL_PEAK)
        and first_rms >= max(0.008, speech_reference * 0.05)
        and following_rms <= max(0.002, speech_reference * TTS_START_CLICK_FOLLOWING_RMS_REL_MAX)
        and guard_rms <= max(0.004, speech_reference * TTS_START_CLICK_GUARD_RMS_REL_MAX)
    )
    return scan_n if isolated_impulse else 0


def _frame_noise_metrics(frame, rate: int) -> tuple[float, float, float, float]:
    """Return RMS, high-frequency ratio, spectral flatness, and zero-crossing rate."""
    import numpy as np

    values = np.asarray(frame, dtype=np.float32)
    if values.size < 32:
        return 0.0, 0.0, 0.0, 0.0
    values = values - float(np.mean(values))
    rms = float(np.sqrt(np.mean(values ** 2)))
    nfft = 1
    while nfft < len(values):
        nfft <<= 1
    spectrum = np.abs(np.fft.rfft(values * np.hanning(len(values)), n=nfft)) ** 2 + 1e-20
    frequencies = np.fft.rfftfreq(nfft, d=1.0 / float(rate))
    total = float(np.sum(spectrum))
    if total <= 1e-12:
        return rms, 0.0, 0.0, 0.0
    high_ratio = float(np.sum(spectrum[frequencies >= 3500.0]) / total)
    flatness = float(np.exp(np.mean(np.log(spectrum))) / np.mean(spectrum))
    zcr = float(np.mean(np.signbit(values[1:]) != np.signbit(values[:-1])))
    return rms, high_ratio, flatness, zcr


def _detect_detached_terminal_noise_samples(audio, rate: int) -> int:
    """Trim a short loud broadband burst only if a quiet gap separates it from speech."""
    import numpy as np

    frame_n = max(128, int(rate * 0.010))  # 10 ms frames for stable spectral estimates
    frame_count = len(audio) // frame_n
    max_frames = int(math.ceil(TTS_TAIL_DETACHED_NOISE_SCAN_MAX_MS / 10.0))
    if frame_count < 20:
        return 0

    frames = [np.asarray(audio[i * frame_n:(i + 1) * frame_n], dtype=np.float32) for i in range(frame_count)]
    metrics = [_frame_noise_metrics(frame, rate) for frame in frames]
    rms_values = np.asarray([row[0] for row in metrics], dtype=np.float32)
    active_rms = rms_values[rms_values > 0.01]
    if active_rms.size < 5:
        return 0
    speech_reference = float(np.percentile(active_rms, 60))
    quiet_threshold = max(0.002, speech_reference * 0.03)

    def noise_like(row):
        rms, high_ratio, flatness, zcr = row
        return (
            rms > quiet_threshold
            and high_ratio >= 0.20
            and flatness >= 0.16
            and zcr >= 0.10
        )

    scan_start = max(0, frame_count - max_frames)
    start = frame_count - 1
    while start >= scan_start and noise_like(metrics[start]):
        start -= 1
    first_noise = start + 1
    burst_frames = frame_count - first_noise
    burst_ms = burst_frames * 10.0
    if burst_frames <= 0 or burst_ms < TTS_TAIL_DETACHED_NOISE_MIN_MS:
        return 0

    # Require a real quiet gap immediately before the terminal burst.
    quiet_frames = 0
    idx = first_noise - 1
    while idx >= 0 and rms_values[idx] <= quiet_threshold:
        quiet_frames += 1
        idx -= 1
    quiet_gap_ms = quiet_frames * 10.0
    if quiet_gap_ms < TTS_TAIL_DETACHED_NOISE_GAP_MIN_MS:
        return 0

    burst_metrics = metrics[first_noise:]
    med_hf = float(np.median([row[1] for row in burst_metrics]))
    med_flat = float(np.median([row[2] for row in burst_metrics]))
    med_zcr = float(np.median([row[3] for row in burst_metrics]))
    if not (
        med_hf >= TTS_TAIL_DETACHED_HF_RATIO_MIN
        and med_flat >= TTS_TAIL_DETACHED_SPECTRAL_FLATNESS_MIN
        and med_zcr >= TTS_TAIL_DETACHED_ZERO_CROSSING_MIN
    ):
        return 0

    trim = len(audio) - first_noise * frame_n
    max_trim = int(rate * TTS_TAIL_DETACHED_NOISE_SCAN_MAX_MS / 1000.0)
    return min(max(0, trim), max_trim)


def _decode_gemini_tts_audio_payload(audio_bytes: bytes, mime_type: str = "") -> tuple[bytes, int, int, int]:
    """Decode Gemini TTS output safely, stripping a WAV container when present.

    The generateContent TTS endpoint can return a complete WAV payload. Treating
    its RIFF header as PCM creates a tiny click at the beginning and can expose
    trailing container metadata as noise. Headerless PCM remains supported.
    Output is normalized to mono PCM16 for the existing pipeline.
    """
    import io
    import wave
    import numpy as np

    if not audio_bytes:
        raise ValueError("Gemini TTS returned empty audio data")

    if len(audio_bytes) >= 12 and audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE":
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            channels = int(wf.getnchannels())
            rate = int(wf.getframerate())
            sample_width = int(wf.getsampwidth())
            if wf.getcomptype() != "NONE":
                raise ValueError(f"Unsupported compressed WAV payload: {wf.getcomptype()}")
            frames = wf.readframes(wf.getnframes())
        if sample_width != 2:
            raise ValueError(f"Unsupported Gemini TTS WAV sample width: {sample_width} bytes")
        samples = np.frombuffer(frames, dtype="<i2")
        if channels > 1:
            if samples.size % channels:
                samples = samples[:samples.size - (samples.size % channels)]
            samples = samples.reshape(-1, channels).astype(np.int32).mean(axis=1)
            samples = np.clip(samples, -32768, 32767).astype(np.int16)
        else:
            samples = samples.astype(np.int16, copy=False)
        log.debug("[TTS Audio] Decoded WAV container: mime=%s rate=%s channels=%s samples=%s", mime_type, rate, channels, len(samples))
        return samples.astype("<i2", copy=False).tobytes(), 1, rate, 2

    # Legacy/headerless response is raw 16-bit signed little-endian PCM.
    if len(audio_bytes) % 2:
        log.warning("[TTS Audio] Headerless PCM had an odd byte count; dropping the final byte (mime=%s).", mime_type)
        audio_bytes = audio_bytes[:-1]
    return audio_bytes, 1, 24000, 2


def _tts_speech_quality(pcm_data: bytes, rate: int = 24000, sample_width: int = 2) -> dict:
    """Reject empty TTS and abnormal long-silence outputs before saving a chunk."""
    import numpy as np

    empty = {
        "ok": False, "duration": 0.0, "rms": 0.0, "p95_frame_rms": 0.0,
        "active_ratio": 0.0, "peak": 0.0, "max_silence_s": 0.0,
    }
    if sample_width != 2 or not pcm_data:
        return empty
    samples = np.frombuffer(pcm_data, dtype="<i2").astype(np.float32)
    if samples.size == 0:
        return empty

    duration = samples.size / float(rate)
    frame_n = max(1, int(rate * 0.020))
    usable = (samples.size // frame_n) * frame_n
    if usable >= frame_n:
        frames = samples[:usable].reshape(-1, frame_n)
        frame_rms = np.sqrt(np.mean(frames * frames, axis=1))
    else:
        frame_rms = np.asarray([np.sqrt(np.mean(samples * samples))], dtype=np.float32)

    overall_rms = float(np.sqrt(np.mean(samples * samples)))
    p95 = float(np.percentile(frame_rms, 95)) if frame_rms.size else 0.0
    peak = float(np.max(np.abs(samples)))
    active_threshold = max(120.0, p95 * 0.12)
    active_mask = frame_rms >= active_threshold
    active_ratio = float(np.mean(active_mask)) if frame_rms.size else 0.0

    # Measure the longest continuous low-energy gap. Normal brief pauses are OK;
    # multi-second gaps inside a spoken chunk strongly indicate a defective result.
    inactive = ~active_mask
    longest_frames = 0
    current_frames = 0
    for is_inactive in inactive:
        if bool(is_inactive):
            current_frames += 1
            if current_frames > longest_frames:
                longest_frames = current_frames
        else:
            current_frames = 0
    max_silence_s = float(longest_frames * frame_n / float(rate))

    ok = (
        duration >= 0.8
        and peak >= 600.0
        and p95 >= 120.0
        and overall_rms >= 80.0
        and active_ratio >= TTS_MIN_ACTIVE_RATIO
        and max_silence_s <= TTS_MAX_CONTIGUOUS_SILENCE_S
    )
    return {
        "ok": bool(ok), "duration": duration, "rms": overall_rms,
        "p95_frame_rms": p95, "active_ratio": active_ratio, "peak": peak,
        "max_silence_s": max_silence_s,
    }

def save_wav(filename: Path, pcm_data: bytes, channels=1, rate=24000, sample_width=2, target_rms=0.15):
    """Save Gemini TTS PCM, trimming only detected edge hiss and preserving speech.

    A tiny adaptive head trim can remove opening hiss. The tail is trimmed only
    when a short run of quiet, high-frequency broadband noise is detected; unlike
    the previous fixed 220 ms cut, voiced endings and most final consonants remain.
    Edge fades are short and do not perform silence removal.
    """
    import wave
    import numpy as np

    if not pcm_data:
        raise ValueError("Gemini TTS returned empty PCM data")

    audio = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
    if len(audio) == 0:
        raise ValueError("Gemini TTS returned an empty audio buffer")

    # Remove a constant DC offset without deleting any part of the spoken signal.
    audio -= np.mean(audio)

    # Normalize before edge classification so thresholds are consistent across chunks.
    rms = float(np.sqrt(np.mean(audio ** 2))) if len(audio) else 0.0
    if rms > 1e-4:
        audio *= (target_rms * 32767.0) / rms

    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak > 32767.0:
        audio *= 32767.0 / peak

    trim_head, low_level_tail_trim = _detect_edge_hiss_trim_samples(audio, rate)
    start_click_trim = _detect_start_click_trim_samples(audio, rate)
    detached_tail_trim = _detect_detached_terminal_noise_samples(audio, rate)
    # These artifacts occupy the same edge regions, so use the largest safe trim
    # per edge rather than summing overlapping detections.
    trim_head = max(trim_head, start_click_trim)
    trim_tail = max(low_level_tail_trim, detached_tail_trim)
    trim_head = min(trim_head, max(0, len(audio) - int(rate * 0.15)))
    trim_tail = min(trim_tail, max(0, len(audio) - trim_head - int(rate * 0.15)))
    if trim_head or trim_tail:
        end_index = len(audio) - trim_tail if trim_tail else len(audio)
        audio = audio[trim_head:end_index]
        log.info(
            "[TTS Audio Cleanup] %s: start=%.1f ms (isolated-click=%s), end=%.1f ms (low-level-hiss=%s, detached-noise=%s); kept %.2f s",
            filename.name,
            trim_head * 1000.0 / rate,
            "yes" if start_click_trim else "no",
            trim_tail * 1000.0 / rate,
            "yes" if low_level_tail_trim else "no",
            "yes" if detached_tail_trim else "no",
            len(audio) / float(rate),
        )

    # A very short 4 ms fade prevents clicks without shaving a word ending.
    edge = min(int(TTS_EDGE_FADE_MS * rate / 1000.0), len(audio) // 4)
    if edge > 1:
        ramp_in = np.linspace(0.0, 1.0, edge, dtype=np.float32)
        ramp_out = np.linspace(1.0, 0.0, edge, dtype=np.float32)
        audio[:edge] *= ramp_in
        audio[-edge:] *= ramp_out

    cleaned_bytes = np.clip(audio, -32768, 32767).astype(np.int16).tobytes()
    with wave.open(str(filename), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(rate)
        wf.writeframes(cleaned_bytes)


def translate_script_for_narration(text: str, target_lang: str, cache_dir: Path) -> str:
    """Translate a narration script when Spanish is selected; cache by source hash."""
    if target_lang != "es":
        return text
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cache_path = cache_dir / "narration_es_translated.txt"
    meta_path = cache_dir / "narration_es_translated.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        cached = cache_path.read_text(encoding="utf-8")
        if meta.get("source_sha256") == source_hash and cached.strip():
            log.info("[Translation] Reutilizando traducción española en caché.")
            return cached
    except Exception:
        pass

    # Split by sentence/paragraph without losing order, so each request stays compact.
    cleaned = text.strip()
    units = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n{2,}", cleaned) if part.strip()]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for unit in units:
        if current and size + len(unit) + 1 > 3800:
            chunks.append(" ".join(current))
            current, size = [], 0
        current.append(unit)
        size += len(unit) + 1
    if current:
        chunks.append(" ".join(current))

    translated_chunks: list[str] = []
    ensure_directory(cache_dir)
    for index, chunk in enumerate(chunks, start=1):
        part_cache = cache_dir / f"narration_es_part_{index:04d}_{hashlib.sha256(chunk.encode('utf-8')).hexdigest()[:10]}.txt"
        if part_cache.exists() and part_cache.read_text(encoding="utf-8").strip():
            translated_chunks.append(part_cache.read_text(encoding="utf-8").strip())
            continue
        prompt = f"""
Translate the following documentary narration into natural, neutral Latin American Spanish for spoken voiceover.
Preserve every factual detail, names, order, and meaning. Do not summarize, omit, embellish, add headings, add commentary, or invent details.
Keep the tone calm and documentary. Return only the translated Spanish narration.

SOURCE TEXT:
{chunk}
""".strip()
        result = call_text_api(prompt)
        if not result or not result.strip():
            raise RuntimeError(f"No se pudo traducir al español el fragmento {index}/{len(chunks)}")
        translated = result.strip()
        part_cache.write_text(translated, encoding="utf-8")
        translated_chunks.append(translated)
        log.info("[Translation] Spanish narration part %s/%s translated.", index, len(chunks))

    translated_text = "\n\n".join(translated_chunks).strip()
    if not translated_text:
        raise RuntimeError("La traducción española quedó vacía")
    cache_path.write_text(translated_text, encoding="utf-8")
    atomic_write_json(meta_path, {"source_sha256": source_hash, "target_lang": "es", "created_at": now_iso()})
    return translated_text


def text_to_speech_gemini(text: str, out_file: Path, chunk_id: int, total_chunks: int) -> bool:
    voice_name = get_channel_tts_voice(ACTIVE_TTS_CHANNEL_NAME)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_AUDIO_MODEL}:generateContent"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{
            "parts": [{
                "text": text,
                "speech_metadata": {
                    "style": "Calm, clear documentary narration. Keep a consistent medium speaking pace and even volume, with natural brief pauses at punctuation. Speak only the supplied transcript verbatim; do not add long pauses or extra sounds.",
                },
            }]
        }],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice_name}
                }
            },
            "temperature": 0.5,
        },
    }

    max_attempts = 999
    silent_marker = out_file.with_suffix(out_file.suffix + ".silent_attempts.json")

    def read_silent_count() -> int:
        try:
            data = json.loads(silent_marker.read_text(encoding="utf-8"))
            return int(data.get("count", 0))
        except Exception:
            return 0

    def write_silent_count(count: int, metrics: dict, mime_type: str) -> None:
        try:
            atomic_write_json(silent_marker, {
                "count": count,
                "duration_s": round(float(metrics.get("duration", 0.0)), 3),
                "rms": round(float(metrics.get("rms", 0.0)), 3),
                "p95_frame_rms": round(float(metrics.get("p95_frame_rms", 0.0)), 3),
                "active_ratio": round(float(metrics.get("active_ratio", 0.0)), 4),
                "max_silence_s": round(float(metrics.get("max_silence_s", 0.0)), 3),
                "mime_type": mime_type,
                "updated_at": now_iso(),
            })
        except Exception as exc:
            log.warning("[TTS Audio] Could not persist silent-output diagnostic: %s", exc)

    if not USE_GEMINI_PROXY or not GEMINI_PROXIES_LIST:
        log.error(
            "❌ [TTS] Для Gemini TTS обязателен настроенный прокси. "
            "Прямые запросы запрещены; чанк не отправляю."
        )
        return False

    for attempt in range(1, max_attempts + 1):
        api_key = _get_next_gemini_key()
        if not api_key:
            log.error("❌ [TTS] Нет доступного Gemini API key.")
            return False

        proxy = _acquire_next_proxy()
        if not proxy:
            log.error("[TTS] Не удалось получить прокси; прямой запрос запрещён, чанк останавливается.")
            return False
        p_label = _get_proxy_label(proxy)
        proxies = {"http": proxy, "https": proxy}
        key_tag = f"...{api_key[-8:]}"

        log.info(
            f"🔄 [TTS] Попытка {attempt}/{max_attempts} | "
            f"key={key_tag} | proxy={p_label}"
        )

        start_t = time.time()
        try:
            r = _post_with_transient_retries(
                f"{url}?key={api_key}",
                headers=headers,
                json=payload,
                timeout=50,
                proxies=proxies,
            )
            elapsed = time.time() - start_t
            body_lower = r.text.lower()

            if r.status_code == 200:
                data = r.json()
                inline_data = data["candidates"][0]["content"]["parts"][0]["inlineData"]
                encoded_audio = base64.b64decode(inline_data["data"])
                mime_type = str(inline_data.get("mimeType", "unknown"))
                try:
                    pcm_bytes, pcm_channels, pcm_rate, pcm_width = _decode_gemini_tts_audio_payload(encoded_audio, mime_type)
                except Exception as decode_exc:
                    log.warning("[TTS Audio] Could not decode TTS payload (%s): %s; retrying with another request.", mime_type, decode_exc)
                    continue

                metrics = _tts_speech_quality(pcm_bytes, pcm_rate, pcm_width)
                if not metrics["ok"]:
                    silent_count = read_silent_count() + 1
                    write_silent_count(silent_count, metrics, mime_type)
                    log.warning(
                        "⚠️ [TTS Audio] Чанк %s/%s вернул подозрительно тихое/пустое аудио: "
                        "duration=%.2fs rms=%.1f p95=%.1f active=%.1f%% max-silence=%.2fs peak=%.1f mime=%s "
                        "(bad-audio response #%s; попытка %s/%s). Не принимаю этот результат и продолжаю попытки, "
                        "чтобы не вставлять тишину/обрезанную озвучку в итоговый ролик.",
                        chunk_id + 1, total_chunks, metrics["duration"], metrics["rms"],
                        metrics["p95_frame_rms"], metrics["active_ratio"] * 100.0,
                        metrics.get("max_silence_s", 0.0), metrics["peak"], mime_type,
                        silent_count, attempt, max_attempts,
                    )
                    try:
                        out_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                    # Do not stop at the third bad response. This chunk gets the full
                    # max_attempts budget (currently 99); only verified audio is accepted.
                    time.sleep(1.0)
                    continue

                try:
                    out_file.unlink(missing_ok=True)
                except Exception:
                    pass
                save_wav(out_file, pcm_bytes, channels=pcm_channels, rate=pcm_rate, sample_width=pcm_width)
                actual_duration = get_duration(out_file)
                spoken_words = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
                # Duration alone is not enough: the waveform must contain meaningful voice activity.
                minimum_duration = max(0.8, len(spoken_words) * 0.18)
                if (
                    actual_duration is None
                    or (len(spoken_words) >= 12 and actual_duration < minimum_duration)
                ):
                    log.warning(
                        "⚠️ [TTS] Чанк %s/%s подозрительно короткий: %.2fs при %s словах "
                        "(минимум %.2fs); повторяю запрос, чтобы не терять фрагменты текста.",
                        chunk_id + 1, total_chunks, actual_duration or 0.0,
                        len(spoken_words), minimum_duration,
                    )
                    try:
                        out_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                    continue
                try:
                    silent_marker.unlink(missing_ok=True)
                except Exception:
                    pass
                log.info(
                    f"✅ [Gemini TTS] Чанк {chunk_id + 1}/{total_chunks} готов "
                    f"за {elapsed:.2f}с (duration={actual_duration:.2f}s, speech-active={metrics['active_ratio']:.1%}, "
                    f"max-silence={metrics.get('max_silence_s', 0.0):.2f}s, mime={mime_type}, proxy={p_label})"
                )
                return True

            if r.status_code == 400:
                if "api_key_invalid" in body_lower or "api key not valid" in body_lower:
                    _kill_gemini_key(
                        api_key,
                        "невалидный ключ 400",
                        is_temporary=False,
                    )
                    log.info(
                        f"➡️ [TTS] 400 invalid key {key_tag} → следующий ключ"
                    )
                    continue

                if "user location is not supported" in body_lower:
                    log.error(
                        f"❌ [TTS] Gemini отклонил запрос по региону: "
                        f"key={key_tag} | proxy={p_label} | {r.text[:500]}"
                    )
                    log.error(
                        "❌ [TTS] Это не ошибка failover ключей: API сообщает, "
                        "что текущая точка подключения/регион не поддерживается."
                    )
                    return False

                log.warning(
                    f"⚠️ [TTS] 400 за {elapsed:.2f}s | key={key_tag} | "
                    f"proxy={p_label} | {r.text[:500]}"
                )
                continue

            if r.status_code == 429:
                _kill_gemini_key(
                    api_key,
                    "TTS лимит 429",
                    is_temporary=True,
                    cooldown_minutes=5,
                )
                log.info(
                    f"➡️ [TTS] 429: key={key_tag} на cooldown 5 мин → следующий ключ"
                )
                continue

            if r.status_code == 403:
                _kill_gemini_key(
                    api_key,
                    "TTS 403",
                    is_temporary=True,
                    cooldown_minutes=15,
                )
                log.info(
                    f"➡️ [TTS] 403: key={key_tag} на cooldown 15 мин; "
                    f"proxy={p_label} НЕ блокируется"
                )
                continue

            log.warning(
                f"⚠️ [TTS] HTTP {r.status_code} за {elapsed:.2f}s | "
                f"key={key_tag} | proxy={p_label} | {r.text[:500]}"
            )
            continue

        except requests.exceptions.ProxyError as e:
            elapsed = time.time() - start_t
            if proxy:
                _put_proxy_on_cooldown(
                    proxy,
                    f"TTS ProxyError after {PROXY_REQUEST_RETRIES} attempts",
                    duration_sec=PROXY_CONNECT_COOLDOWN_SECONDS,
                )
            log.warning(
                f"⚠️ [TTS] ProxyError за {elapsed:.2f}s | key={key_tag} | "
                f"proxy={p_label} | {e}"
            )
            continue

        except requests.exceptions.ReadTimeout as e:
            elapsed = time.time() - start_t
            if proxy:
                _put_proxy_on_cooldown(
                    proxy,
                    f"TTS ReadTimeout after {PROXY_READ_TIMEOUT_ATTEMPTS} attempts (upstream may be slow)",
                    duration_sec=PROXY_TIMEOUT_COOLDOWN_SECONDS,
                )
            log.warning(
                f"⚠️ [TTS] ReadTimeout за {elapsed:.2f}s | key={key_tag} | "
                f"proxy={p_label} | {e}"
            )
            continue

        except requests.exceptions.ConnectTimeout as e:
            elapsed = time.time() - start_t
            if proxy:
                _put_proxy_on_cooldown(
                    proxy,
                    f"TTS ConnectTimeout after {PROXY_REQUEST_RETRIES} attempts",
                    duration_sec=PROXY_CONNECT_COOLDOWN_SECONDS,
                )
            log.warning(
                f"⚠️ [TTS] ConnectTimeout за {elapsed:.2f}s | key={key_tag} | "
                f"proxy={p_label} | {e}"
            )
            continue

        except requests.exceptions.ConnectionError as e:
            elapsed = time.time() - start_t
            if proxy:
                _put_proxy_on_cooldown(
                    proxy,
                    f"TTS ConnectionError after {PROXY_REQUEST_RETRIES} retries",
                    duration_sec=PROXY_CONNECT_COOLDOWN_SECONDS,
                )
            log.warning(
                f"⚠️ [TTS] ConnectionError за {elapsed:.2f}s | key={key_tag} | "
                f"proxy={p_label} | {e}"
            )
            continue

        except Exception as e:
            elapsed = time.time() - start_t
            log.exception(
                f"❌ [TTS] Неожиданная ошибка за {elapsed:.2f}s | "
                f"key={key_tag} | proxy={p_label}: {e}"
            )
            continue

        finally:
            _release_proxy(proxy)

        time.sleep(0.8)

    log.error(
        f"❌ [TTS] Чанк {chunk_id + 1}/{total_chunks}: "
        f"исчерпаны {max_attempts} попыток"
    )
    return False


def tts_chunk_path(chunk_dir: Path, chunk_index: int, text: str) -> Path:
    """Use a text hash in each chunk filename so changed scripts cannot reuse stale audio."""
    cache_payload = f"{TTS_EDGE_CLEANUP_VERSION}\0{text}"
    digest = hashlib.sha256(cache_payload.encode("utf-8")).hexdigest()[:12]
    return chunk_dir / f"chunk_{chunk_index:04d}_{digest}.wav"


def audio_worker_gemini(q: Queue, chunk_list: list[str], chunk_dir: Path, retry_cnt: dict, total_chunks: int):
    while True:
        try:
            idx = q.get_nowait()
        except Empty:
            break

        text = chunk_list[idx]
        chunk_path = tts_chunk_path(chunk_dir, idx, text)

        if chunk_path.exists() and is_valid_audio(chunk_path):
            q.task_done()
            continue

        success = text_to_speech_gemini(text, chunk_path, idx, total_chunks)
        if not success:
            retry_cnt[idx] += 1
            fails = retry_cnt[idx]
            silent_marker = chunk_path.with_suffix(chunk_path.suffix + ".silent_attempts.json")
            silent_count = 0
            try:
                silent_count = int(json.loads(silent_marker.read_text(encoding="utf-8")).get("count", 0))
            except Exception:
                pass
            if silent_count > 0:
                # text_to_speech_gemini already consumed its full 99-attempt budget
                # before returning after bad audio; do not accidentally start another
                # worker-level retry cycle and exceed the per-chunk limit.
                log.error(
                    "[TTS] Чанк %s: после полного лимита попыток качественная озвучка не получена; "
                    "пропуск запрещён, сборка итогового аудио завершится ошибкой. Диагностика: %s",
                    idx + 1, silent_marker,
                )
            elif fails < 15:
                time.sleep(5 if fails < 3 else 15)
                q.put(idx)
        else:
            time.sleep(2.0)

        q.task_done()


def master_voice_audio(input_wav: Path, output_wav: Path) -> bool:
    """Master narration in one FFmpeg pass before Whisper transcription."""
    if not input_wav.is_file():
        log.error("[Voice Master] Нет исходного аудио: %s", input_wav)
        return False
    ensure_directory(output_wav.parent)
    temp_output = output_wav.with_name(output_wav.stem + ".mastering.tmp.wav")
    temp_output.unlink(missing_ok=True)
    pitch, tempo = random.uniform(*AUDIO_PITCH_JITTER), random.uniform(*AUDIO_TEMPO_JITTER)
    ap1, ap2 = random.uniform(600.0, 950.0), random.uniform(2100.0, 2900.0)
    eq1, eq2 = random.uniform(-0.8, 0.8), random.uniform(-0.8, 0.8)
    chain = (f"aresample=44100,asetrate=44100*{pitch:.5f},aresample=44100,atempo={tempo / pitch:.5f},"
        "highpass=f=65,lowpass=f=15000,"
        f"allpass=f={ap1:.1f}:width_type=q:w=1.0,allpass=f={ap2:.1f}:width_type=q:w=1.0,"
        f"equalizer=f=350:t=q:w=1.0:g={eq1:.3f},equalizer=f=3200:t=q:w=1.0:g={eq2:.3f},"
        "acompressor=threshold=0.12:ratio=2.0:attack=20:release=250:makeup=1.0:knee=2.0:detection=rms,loudnorm=I=-16:TP=-1.5:LRA=4.5")
    fc = f"[0:a]{chain}[voice];anoisesrc=color=pink:sample_rate=44100:amplitude=0.0007[room];[voice][room]amix=inputs=2:duration=first:normalize=0:dropout_transition=0[mix]"
    cmd = [FFMPEG_BIN, "-y", "-i", str(input_wav), "-filter_complex", fc, "-map", "[mix]", "-vn", "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:a", "+bitexact", str(temp_output)]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900)
        if not is_valid_audio(temp_output):
            raise RuntimeError("mastered WAV failed validation")
        temp_output.replace(output_wav)
        log.info("[Voice Master] 44.1 kHz mono PCM готов; pitch=%.5f tempo=%.5f", pitch, tempo)
        return True
    except Exception as exc:
        log.error("[Voice Master] Ошибка обработки %s: %s", input_wav.name, exc)
        temp_output.unlink(missing_ok=True)
        return False


def generate_audio_gemini_sync(text: str, temp_dir: Path, output_file: Path) -> bool:
    if not text.strip():
        return False
    chunks = split_text_into_chunks(text)
    total_c = len(chunks)
    log.info(f"📢 [TTS] Озвучка Gemini TTS ({get_channel_tts_voice(ACTIVE_TTS_CHANNEL_NAME)}). Чанков: {total_c}")

    chunk_dir = temp_dir / "voice_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    # Allow a fresh process run to retry chunks that failed the silence guard previously.
    for i, chunk_text in enumerate(chunks):
        marker = tts_chunk_path(chunk_dir, i, chunk_text).with_suffix(".wav.silent_attempts.json")
        try:
            marker.unlink(missing_ok=True)
        except Exception:
            pass

    q = Queue()
    for i in range(total_c):
        q.put(i)
    retry_counts = defaultdict(int)

    threads = []
    for _ in range(max(1, VOICE_THREADS)):
        t = threading.Thread(target=audio_worker_gemini, args=(q, chunks, chunk_dir, retry_counts, total_c))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    valid_chunks = [
        tts_chunk_path(chunk_dir, i, chunks[i])
        for i in range(total_c)
        if tts_chunk_path(chunk_dir, i, chunks[i]).exists() and is_valid_audio(tts_chunk_path(chunk_dir, i, chunks[i]))
    ]
    if len(valid_chunks) != total_c:
        log.error(f"❌ [TTS] Готово {len(valid_chunks)}/{total_c} чанков.")
        return False

    raw_audio = temp_dir / "audio_raw.wav"
    list_file = temp_dir / "chunks_list.txt"

    def ffconcat_quote(path: Path) -> str:
        # Экранируем апострофы в именах папок и файлов для FFmpeg.
        return "'" + path.as_posix().replace("'", "'\\''") + "'"

    with open(list_file, "w", encoding="utf-8", newline="\n") as f:
        for p in valid_chunks:
            f.write(f"file {ffconcat_quote(p)}\n")

    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(raw_audio)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    log.info("[TTS Audio] Applying unified voice mastering before Whisper.")
    if not master_voice_audio(raw_audio, output_file):
        return False

    shutil.rmtree(chunk_dir, ignore_errors=True)
    return is_valid_audio(output_file)


# ==============================================================================
# 🎙️ WHISPER TRANSCRIPTION
# ==============================================================================

def transcribe_audio(
    audio_file: Path,
    subtitle_file: Path,
    segments_json: Path,
    lang_code: str,
) -> list[dict[str, Any]]:
    import stable_whisper

    log.info("🎙️ [Whisper] Загружаем turbo (%s)...", lang_code.upper())
    started = time.time()
    model = None
    result = None
    segment_data: list[dict[str, Any]] = []

    try:
        model = stable_whisper.load_model("turbo", device="cuda")
        result = model.transcribe(
            str(audio_file),
            language=lang_code,
            word_timestamps=True,
            fp16=True,
        )

        ensure_directory(subtitle_file.parent)
        # Whisper timestamps are already measured against the exact TTS audio.
        # Do not shift subtitles ahead of the speech: even a small negative offset
        # makes the captions visibly lead the current slide/audio.
        result.to_ass(
            str(subtitle_file),
            karaoke=ENABLE_SUBTITLE_KARAOKE,
            highlight_color="00FFFF" if ENABLE_SUBTITLE_KARAOKE else "FFFFFF",
            primary_color="FFFFFF",
            timing_offset=0.0,
        )

        for segment in result.segments:
            words = []
            if hasattr(segment, "words") and segment.words:
                for word in segment.words:
                    words.append({
                        "start": word.start,
                        "end": word.end,
                        "word": word.word,
                    })

            segment_data.append({
                "start": segment.start,
                "end": segment.end,
                "text": segment.text,
                "words": words,
            })

        segments_json.write_text(
            json.dumps(segment_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    finally:
        # Explicitly drop BOTH the model and the transcription result before
        # collecting garbage; previously result could still keep arrays/tensors
        # alive when the CUDA cache was emptied. Cleanup also runs on exceptions.
        result = None
        model = None
        force_cuda_cleanup("после Whisper-транскрибации")

    log.info("✅ [Whisper] Готов за %.1fs", time.time() - started)
    return segment_data

def load_segments(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def transcript_text(segments: list[dict[str, Any]]) -> str:
    return " ".join(
        str(s.get("text", "")).strip()
        for s in segments
        if str(s.get("text", "")).strip()
    ).strip()


def audit_narration_coverage(expected_text: str, recognized_text: str, report_path: Path) -> dict[str, Any]:
    """Compare source narration with Whisper transcript and report possible omissions.

    This is diagnostic only: ASR spelling/name errors can reduce the score, so it
    never blocks rendering. It helps distinguish true TTS omissions from normal
    transcription differences.
    """
    def tokens(value: str) -> list[str]:
        return re.findall(r"[^\W_]+", (value or "").casefold(), flags=re.UNICODE)

    expected = tokens(expected_text)
    recognized = tokens(recognized_text)
    matcher = difflib.SequenceMatcher(None, expected, recognized, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    coverage = matched / max(1, len(expected))
    similarity = matcher.ratio()

    possible_gaps: list[dict[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag not in {"delete", "replace"} or i1 == i2:
            continue
        left = " ".join(expected[max(0, i1 - 7):i1])
        omitted = " ".join(expected[i1:i2])
        right = " ".join(expected[i2:min(len(expected), i2 + 7)])
        possible_gaps.append({"left_context": left, "expected_words": omitted, "right_context": right})
        if len(possible_gaps) >= 30:
            break

    report = {
        "created_at": now_iso(),
        "expected_word_count": len(expected),
        "recognized_word_count": len(recognized),
        "matched_expected_word_count": matched,
        "expected_word_coverage": round(coverage, 4),
        "sequence_similarity": round(similarity, 4),
        "status": "REVIEW" if coverage < 0.82 else "PASS",
        "possible_missing_or_misrecognized_spans": possible_gaps,
        "note": "Whisper may misrecognize names or wording; compare the listed spans with the audio before deciding that words were omitted.",
    }
    try:
        atomic_write_json(report_path, report)
    except Exception as exc:
        log.warning("[Narration Integrity] Не удалось сохранить отчёт: %s", exc)

    if coverage < 0.82:
        log.warning(
            "⚠️ [Narration Integrity] Вероятное расхождение сценария и распознанной озвучки: "
            "coverage=%.1f%%, expected=%s words, recognized=%s words. Report: %s",
            coverage * 100, len(expected), len(recognized), report_path,
        )
    else:
        log.info(
            "✅ [Narration Integrity] Сценарий/озвучка сверены: coverage=%.1f%% (%s/%s слов). Report: %s",
            coverage * 100, matched, len(expected), report_path,
        )
    return report


def get_text_for_timeslot(
    segments: list[dict[str, Any]],
    start_time: float,
    end_time: float,
) -> str:
    words: list[str] = []

    for segment in segments:
        for word in segment.get("words", []):
            ws = word.get("start")
            we = word.get("end")
            if ws is None or we is None:
                continue
            if max(start_time, ws) < min(end_time, we):
                words.append(str(word.get("word", "")))

        if not segment.get("words"):
            ss = segment.get("start", 0)
            se = segment.get("end", 0)
            if max(start_time, ss) < min(end_time, se):
                words.append(str(segment.get("text", "")))

    text = ""
    for word in words:
        word = word.strip()
        if not word:
            continue
        if text and not word.startswith((",", ".", "!", "?", ":", ";", "%", ")")):
            text += " "
        text += word
    return text.strip()


# ==============================================================================
# 🧠 GEMINI DIRECTING & DUAL PROMPT GENERATORS
# ==============================================================================

def analyze_characters(transcription: str) -> dict[str, Any]:
    prompt = f"""
You are a documentary casting director.
Extract the recurring named people and important animals from the story, up to 8 people and 3 animals.
Respect each person's stated age, including children; include pregnant adult characters only when the story explicitly says so, and describe people/animals only in safe, ordinary contexts.
Give concise realistic descriptions of face/head, skin tone or coat, hair/mane, eyes, body, base wardrobe/equipment, and distinctive features.
Return ONLY valid JSON.

{{"Character Name": "age-accurate appearance exactly as stated in the story, realistic attire, detailed features"}}

TEXT:
{transcription[:15000]}
""".strip()

    res = call_text_api(prompt)
    if not res:
        return {}

    try:
        match = re.search(r"\{.*\}", res, re.DOTALL)
        return json.loads(match.group(0)) if match else {}
    except Exception:
        return {}



def analyze_context(transcription: str) -> dict[str, Any]:
    """Extract stable documentary setting details for visual prompts.

    Returns an empty dictionary when the API is unavailable or the response
    is not valid JSON, allowing the pipeline to use its existing fallbacks.
    """
    prompt = f"""
Extract realistic documentary setting information from the story.
Return ONLY valid JSON with these keys:
{{
  "setting": "specific place and environmental details",
  "time_period": "historical era, year, or season when stated",
  "mood": "restrained documentary atmosphere",
  "color_palette": "natural, consistent dominant colours"
}}

Do not invent facts that contradict the story. Use concise values.

STORY:
{transcription[:15000]}
""".strip()

    response = call_text_api(prompt)
    if not response:
        return {}

    try:
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if not match:
            log.warning("[Context Analysis] Gemini response did not contain a JSON object")
            return {}
        data = json.loads(match.group(0))
        if not isinstance(data, dict):
            return {}
        allowed = {"setting", "time_period", "mood", "color_palette"}
        return {key: str(value).strip() for key, value in data.items() if key in allowed and value is not None}
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        log.warning("[Context Analysis] Could not parse setting context: %s", exc)
        return {}


def analyze_character_bible(transcription: str) -> dict[str, Any]:
    """Create a project-local visual canon for recurring people and animals.

    Facts explicitly stated by the story must be preserved. For details not stated,
    the model may choose one plausible visual design once; that choice is then locked
    and reused by all prompts for this project.
    """
    prompt = f"""
You are a documentary casting director creating a CONTINUITY BIBLE for a fictional/historical story.
Return ONLY valid JSON with this schema:
{{
  "version": 5,
  "characters": [
    {{
      "id": "STABLE_UPPER_SNAKE_CASE_ID",
      "narrative_role": "protagonist, narrator, supporting, antagonist, or background",
      "visual_reference_priority": "primary, supporting, or minor",
      "name": "Name Exactly As In Story",
      "aliases": ["exact story spelling", "Latin/English transliteration if the story uses another script", "specific recurring role label used by prompts"],
      "kind": "human or animal (choose exactly one)",
      "age_or_stage": "exact story age if stated; otherwise not specified",
      "face_or_head": "stable facial/head geometry and distinguishing traits",
      "skin_tone_or_coat": "stable skin tone for a human or coat colour for an animal",
      "hair_or_mane": "stable hair/braid/beard or mane/tail description",
      "eyes": "stable eye colour/features when applicable",
      "build_or_body": "stable build/body/markings; preserve pregnancy only if explicitly stated in the story and only as a respectful, non-medical adult appearance",
      "wardrobe_or_equipment": "default/base clothing and equipment plus scene-specific outfit changes when story, weather, time, or activity calls for them",
      "distinctive_features": "permanent marks, scars, freckles, socks/white blaze etc.",
      "continuity_lock": "one concise identity description that excludes wardrobe and other scene-dependent details"
    }}
  ]
}}

Rules:
- Include up to 8 important recurring named people and up to 3 important recurring animals. Include children with their actual story age; do not age them up. Include pregnant adult characters when story-relevant and explicitly stated; preserve pregnancy as a temporary story state without adding medical assumptions. Animals are characters too.
- Identify the single central PROTAGONIST explicitly with narrative_role="protagonist". If the story is narrated in first person, identify the narrator/"I" as the protagonist even if the script never gives that person a proper name. In that case include a character entry named "The Narrator" or a concise role-based name, and add grounded aliases for the narrator's specific occupation/role used in visuals (e.g. "the young mechanic" only if the story supports it). Do not create a narrator if the story is actually about a different central person.
- Use narrative_role="narrator" only when the first-person narrator is not the central protagonist; otherwise use "protagonist". Other entries must be supporting, antagonist, or background.
- For visual_reference_priority, mark the central protagonist primary. Supporting for recurring secondary characters; minor for brief/background characters. Mark no more than 10 recurring, visually important characters as primary. Prefer the central protagonist plus only frequently recurring named characters whose faces must remain consistent. This field is for reference-image generation, not for deciding whether a character appears in the story.
- Prioritize explicit facts from the story. Do not contradict stated age, hair colour, clothing details, animal coat or markings.
- When visual facts are unstated, choose one plausible, naturalistic design and use that exact choice consistently thereafter. Do not write alternatives such as "brown or blond".
- Lock identity traits: skin tone, face shape, eye colour, hair colour/style, age, build, and permanent marks. Treat wardrobe/equipment as a default reference, not as identity. Follow explicit outfit changes in the story; also allow sensible changes for clear time jumps, weather, location, occupation, or activity. Keep the outfit consistent across adjacent shots in the same continuous event and never change it arbitrarily. For animals, coat colour and markings are fixed; tack, blankets, or harness may vary when the story requires it.
- Do not add characters that aren't in the story. If a detail is not applicable, use "not applicable"; if a fact is unclear, choose one stable visual choice rather than changing it scene by scene.
- Keep descriptions practical for stable visual prompts in the selected style ({visual_style_label()}), not literary or metaphorical.
- Add 2-6 useful aliases per character: the exact name spelling from the script, Latin/English transliterations when needed, recurring short names, and distinctive role labels such as "the seamstress" or "the mine owner". Avoid generic aliases like only "woman", "man", "horse", or "child" that could match unrelated subjects.

STORY:
{transcription[:18000]}
""".strip()
    response = call_text_api(prompt)
    if not response:
        return {"version": 5, "characters": []}
    try:
        match = re.search(r"\{.*\}", response, re.DOTALL)
        data = json.loads(match.group(0)) if match else {}
        entries = data.get("characters", []) if isinstance(data, dict) else []
        if not isinstance(entries, list):
            return {"version": 5, "characters": []}
        cleaned = []
        for entry in entries:
            if not isinstance(entry, dict) or not str(entry.get("name", "")).strip():
                continue
            name = str(entry["name"]).strip()
            stable_id = re.sub(r"[^A-Z0-9]+", "_", str(entry.get("id", name.upper()))).strip("_")
            fields = {
                key: re.sub(r"\s+", " ", str(entry.get(key, "not specified")).strip())
                for key in (
                    "age_or_stage", "face_or_head", "skin_tone_or_coat", "hair_or_mane",
                    "eyes", "build_or_body", "wardrobe_or_equipment", "distinctive_features", "continuity_lock"
                )
            }
            priority = str(entry.get("visual_reference_priority", "supporting")).strip().casefold()
            if priority not in {"primary", "supporting", "minor"}:
                priority = "supporting"
            narrative_role = str(entry.get("narrative_role", "supporting")).strip().casefold()
            if narrative_role not in {"protagonist", "narrator", "supporting", "antagonist", "background"}:
                narrative_role = "protagonist" if priority == "primary" else "supporting"
            raw_aliases = entry.get("aliases", [])
            if isinstance(raw_aliases, str):
                raw_aliases = re.split(r"[;|,]", raw_aliases)
            aliases: list[str] = []
            for alias in [name, str(entry.get("id", "")), str(entry.get("id", "")).replace("_", " "), *(raw_aliases if isinstance(raw_aliases, list) else [])]:
                alias = re.sub(r"\s+", " ", str(alias or "")).strip().strip("-• ")
                if alias and alias.casefold() not in {x.casefold() for x in aliases}:
                    aliases.append(alias)
            cleaned.append({
                "id": stable_id or re.sub(r"\W+", "_", name.upper()),
                "name": name,
                "aliases": aliases[:12],
                "kind": "animal" if "animal" in str(entry.get("kind", "human")).lower() else "human",
                "narrative_role": narrative_role,
                "visual_reference_priority": priority,
                **fields,
            })
        primary_count = 0
        for item in cleaned:
            if item.get("visual_reference_priority") == "primary":
                primary_count += 1
                if primary_count > MAX_PRIMARY_CHARACTER_REFERENCES:
                    item["visual_reference_priority"] = "supporting"
        return {"version": 5, "characters": cleaned[:11]}
    except Exception as exc:
        log.warning("[Character Bible] Не удалось разобрать JSON: %s", exc)
        return {"version": 5, "characters": []}


# Built from the current project's character bible. This lets the scene lock match
# English prompts against names written in Cyrillic/other scripts without changing
# the canonical names used in the saved bible.
_CHARACTER_ALIAS_INDEX: dict[str, list[str]] = {}
_CHARACTER_AMBIGUOUS_NAME_TOKENS: set[str] = set()


def _ambiguous_character_name_tokens(entries: list[dict[str, Any]]) -> set[str]:
    """Find name tokens shared by different characters (e.g. a family surname).

    Shared tokens must not identify a character on their own. Full names and stable
    IDs remain valid matches, while a shared surname such as WHITTAKER cannot cause
    Rose's scene to select Thomas as well.
    """
    owners: dict[str, set[int]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        values = [str(entry.get("name", "")), str(entry.get("id", ""))]
        aliases = entry.get("aliases", [])
        if isinstance(aliases, str):
            aliases = re.split(r"[;|,]", aliases)
        if isinstance(aliases, list):
            values.extend(str(value or "") for value in aliases)
        tokens: set[str] = set()
        for value in values:
            for token in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", value):
                token = token.casefold()
                if len(token) >= 4:
                    tokens.add(token)
        for token in tokens:
            owners.setdefault(token, set()).add(index)
    return {token for token, character_indexes in owners.items() if len(character_indexes) > 1}


CHANNEL_HERO_SKIN_LOCK_START = "ABSOLUTE CHANNEL HERO SKIN COLOUR LOCK START"
CHANNEL_HERO_SKIN_LOCK_END = "ABSOLUTE CHANNEL HERO SKIN COLOUR LOCK END"


def append_channel_hero_skin_lock(
    prompt: str, hero_entry: dict[str, Any] | None, media_kind: str = "image"
) -> str:
    """Append a strong, channel-specific neutral-white skin rule for the attached hero only.

    This is deliberately opt-in: it applies only to a character entry locked to a channel
    etalon with an explicit channel_skin_tone_override. Other channels/characters are untouched.
    """
    if not isinstance(hero_entry, dict) or not hero_entry.get("channel_reference_locked"):
        return str(prompt or "").strip()
    override = str(hero_entry.get("channel_skin_tone_override", "") or "").strip()
    if not override:
        return str(prompt or "").strip()

    base = str(prompt or "").strip()
    base = re.sub(
        rf"\s*{re.escape(CHANNEL_HERO_SKIN_LOCK_START)}.*?{re.escape(CHANNEL_HERO_SKIN_LOCK_END)}\s*",
        "\n\n", base, flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    lock = (
        f"{CHANNEL_HERO_SKIN_LOCK_START}\n"
        "This rule applies ONLY to the protagonist shown in the attached channel etalon configured for this channel, never to supporting characters or any other channel. "
        "ABSOLUTE DESIGN REQUIREMENT: this protagonist has deliberately PURE WHITE SKIN, not a realistic human complexion. Use pure white sRGB #FFFFFF (RGB 255,255,255) as the actual flat base fill for every visible area of bare skin. It must read white immediately, not merely pale/fair. "
        "Apply this to the complete visible body: face, ears, neck, shoulders, arms, hands, fingers, legs, knees, feet, and any exposed chest or torso. No exposed skin area may use natural peach, pink, beige, tan, orange, brown or realistic flesh colour. "
        "Skin shading is restricted to extremely subtle neutral grey no darker than approximately #F0F0F0, plus normal dark linework. No blush, warm tint, blue/cyan/teal/navy cast, purple/violet/magenta cast, or saturated coloured bounce light on skin. "
        "Lighting, weather, sunset, indoor lamps, cool shadows and colour grading may change the environment and clothing, but MUST NOT change the white skin design. White skin is a fixed character-design colour, not a lighting-dependent complexion. "
        "For VIDEO: preserve white skin in EVERY frame throughout motion, including exposed arms and legs; never gradually drift to realistic peach/beige/blue skin. For THUMBNAILS: every visible skin region must remain pure white. "
        "Preserve the character's identity and clean 2D linework. This is the character's canonical stylized design, not a mask, paint, costume or temporary effect. "
        f"Channel-specific reference rule: {override}.\n"
        f"{CHANNEL_HERO_SKIN_LOCK_END}"
    )
    return f"{base}\n\n{lock}".strip() if base else lock


def character_bible_prompt_map(bible: dict[str, Any], fallback: dict[str, Any] | None = None) -> dict[str, str]:
    """Flatten structured character entries into canonical prompt descriptions and a matching alias index."""
    global _CHARACTER_ALIAS_INDEX, _CHARACTER_AMBIGUOUS_NAME_TOKENS
    result: dict[str, str] = {}
    _CHARACTER_ALIAS_INDEX = {}
    entries = bible.get("characters", []) if isinstance(bible, dict) else []
    _CHARACTER_AMBIGUOUS_NAME_TOKENS = _ambiguous_character_name_tokens(
        [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []
    )
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            raw_aliases = entry.get("aliases", [])
            if isinstance(raw_aliases, str):
                raw_aliases = re.split(r"[;|,]", raw_aliases)
            aliases: list[str] = []
            for alias in [name, str(entry.get("id", "")), str(entry.get("id", "")).replace("_", " "), *(raw_aliases if isinstance(raw_aliases, list) else [])]:
                alias = re.sub(r"\s+", " ", str(alias or "")).strip().strip("-• ")
                if alias and alias.casefold() not in {a.casefold() for a in aliases}:
                    aliases.append(alias)
            _CHARACTER_ALIAS_INDEX[name] = aliases[:12]
            if entry.get("channel_reference_locked"):
                skin_override = str(entry.get("channel_skin_tone_override", "") or "").strip()
                skin_rule = (" CHANNEL-SPECIFIC ABSOLUTE SKIN COLOUR LOCK: neutral near-white porcelain/ivory base #F7F5F2 on face, ears, neck, arms and hands; low saturation, neutral light-grey shadows only; no blue/cyan/teal/violet/purple or peach/beige/tan/brown colour cast from lighting. The skin remains white regardless of environment lighting. " + skin_override) if skin_override else ""
                result[name] = (
                    f"CANONICAL CHARACTER ID {entry.get('id', name)}; narrative role={entry.get('narrative_role', 'protagonist')}; "
                    f"story age/stage={entry.get('age_or_stage', 'not specified')}. "
                    "CHANNEL ETALON IDENTITY LOCK: the attached image from the channel's etalon folder is the authoritative source for "
                    "this protagonist's face/head geometry, skin tone, hair, eyes, apparent age, proportions, and distinguishing features. "
                    "If inferred written appearance details conflict with the attached image, follow the image. Do not replace this identity with a side character."
                    + skin_rule + " "
                    "WARDROBE BASELINE LOCK FOR THIS PROJECT: the clothing and accessories visibly present in the selected channel etalon image define this protagonist's default outfit for this entire project. The attached image, not the story's generic wardrobe prose, is the source of truth. "
                    "If narration or a generated scene prompt repeats a habitual uniform or equipment description that conflicts with the selected etalon, treat it as background biography and ignore the conflicting outfit detail for this shot. Do not silently revert to previously used clothes. "
                    "Only change clothes when THIS specific scene explicitly depicts a clothing change, a clearly different outfit, a formal occasion, or a meaningful time/weather/location/activity change; do not treat repeated descriptions of the usual uniform as a change event. "
                    "Keep the chosen outfit stable across adjacent shots of the same event. Do not add a polo, screwdriver, cap, logo, tool or other accessory unless it appears in the selected etalon or this scene clearly requires it. "
                    "The face and identity traits remain fixed regardless of wardrobe. The reference is not permission to show the protagonist in every scene; only show the character when the scene cast decision explicitly includes them."
                )
            else:
                result[name] = (
                    f"CANONICAL CHARACTER ID {entry.get('id', name)}; type={entry.get('kind', 'human')}; "
                    f"age/stage={entry.get('age_or_stage', 'not specified')}; "
                    f"face/head={entry.get('face_or_head', 'not specified')}; "
                    f"skin tone/coat={entry.get('skin_tone_or_coat', 'not specified')}; "
                    f"hair/mane={entry.get('hair_or_mane', 'not specified')}; "
                    f"eyes={entry.get('eyes', 'not specified')}; "
                    f"build/body={entry.get('build_or_body', 'not specified')}; "
                    f"default/base wardrobe and equipment reference={entry.get('wardrobe_or_equipment', 'not specified')}; "
                    f"permanent features={entry.get('distinctive_features', 'not specified')}. "
                    "IDENTITY LOCK: keep face/head geometry, skin tone or coat, hair/mane, eyes, age/stage, body type, and permanent marks consistent. "
                    "Wardrobe/equipment above is a default reference, not an identity lock. Follow explicit outfit changes and changes justified by story time, weather, location, occupation, or activity. "
                    "Preserve the same outfit across adjacent shots of the same continuous event; do not change clothing arbitrarily. "
                    "Animal coat colours and markings are permanent; tack/equipment may change when narratively appropriate."
                )
    if not result and isinstance(fallback, dict):
        for name, desc in fallback.items():
            if str(name).strip() and str(desc).strip():
                display_name = str(name).strip()
                result[display_name] = str(desc)
                _CHARACTER_ALIAS_INDEX[display_name] = [display_name]
    return result




def _character_alias_candidates(
    entry: dict[str, Any], all_entries: list[dict[str, Any]] | None = None
) -> list[str]:
    """Names and stable IDs used in scene prompts; exclude generic role words."""
    raw: list[str] = []
    name = str(entry.get("name", "")).strip()
    stable_id = str(entry.get("id", "")).strip()
    if name:
        raw.append(name)
    if stable_id:
        raw.extend([stable_id, stable_id.replace("_", " ")])
    aliases = entry.get("aliases", [])
    if isinstance(aliases, str):
        aliases = re.split(r"[;|,]", aliases)
    if isinstance(aliases, list):
        raw.extend(str(value or "").strip() for value in aliases)

    generic = {
        "woman", "man", "girl", "boy", "child", "person", "horse", "animal",
        "seamstress", "miner", "mine owner", "daughter", "mother", "the woman",
        "the man", "the girl", "the child", "конь", "лошадь", "женщина", "мужчина",
        "девочка", "дочь", "ребёнок", "ребенок", "швея", "шахтёр", "шахтер",
    }
    ambiguous_tokens = _ambiguous_character_name_tokens(all_entries or [entry])
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in raw:
        alias = re.sub(r"\s+", " ", str(value or "").strip()).strip("-• ")
        if not alias or alias.casefold() in generic or alias.casefold() in seen:
            continue
        # An alias consisting only of a shared name token is ambiguous and unsafe.
        alias_tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", alias)
        if len(alias_tokens) == 1 and alias_tokens[0].casefold() in ambiguous_tokens:
            continue
        seen.add(alias.casefold())
        cleaned.append(alias)
    # Include distinctive name parts to handle prompts that use only a first name
    # or Russian declensions (e.g. "Клара" in the bible, "Клару" in the script).
    for base in (name, stable_id.replace("_", " ")):
        for token in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", base):
            if (len(token) >= 4 and token.casefold() not in generic
                    and token.casefold() not in ambiguous_tokens and token.casefold() not in seen):
                seen.add(token.casefold())
                cleaned.append(token)
    return cleaned


def _alias_pattern(alias: str) -> str:
    """Build a bounded alias regex without losing underscores in stable IDs."""
    normalized = re.sub(r"\s+", " ", str(alias or "").strip())
    escaped = re.escape(normalized).replace(r"\ ", r"\s+")
    return r"(?<![\w])" + escaped + r"(?:['’]s)?(?![\w])"


def _alias_count(text: str, alias: str) -> int:
    normalized = re.sub(r"\s+", " ", str(alias or "").strip())
    if len(normalized) < 3:
        return 0
    haystack = re.sub(r"\s+", " ", str(text or "")).casefold()
    variants = [normalized]
    if "_" in normalized:
        variants.append(normalized.replace("_", " "))
    patterns = [_alias_pattern(value) for value in variants]
    # A one-token Cyrillic name may be inflected in the Russian narration. Match
    # the stem only for proper-name tokens, never for generic role labels.
    for value in variants:
        if re.fullmatch(r"[А-Яа-яЁё]+", value, flags=re.I) and len(value) >= 4:
            # Cyrillic names commonly change endings in Russian narration: Клара →
            # Клару, Сайлас → Сайласу/Сайласа, Калеб → Калеба. Match the proper-name
            # token plus a small set of inflection endings, not arbitrary surrounding text.
            patterns.append(
                r"(?<![\w])" + re.escape(value.casefold())
                + r"(?:а|я|у|ю|е|и|ы|ом|ем|ой|ей|ов|ев)?(?![\w])"
            )
            stem = value
            if stem[-1].casefold() in "аоуыяеиюэёь" and len(stem) >= 5:
                stem = stem[:-1]
            if len(stem) >= 4:
                patterns.append(r"(?<![\w])" + re.escape(stem.casefold()) + r"[а-яё]*(?![\w])")
    return max((len(re.findall(pattern, haystack, flags=re.I)) for pattern in patterns), default=0)


def _character_mention_count(
    text: str, entry: dict[str, Any], all_entries: list[dict[str, Any]] | None = None
) -> int:
    """Count mentions using names, stable IDs, aliases, and Russian name inflections."""
    return max((_alias_count(text, alias) for alias in _character_alias_candidates(entry, all_entries)), default=0)


def select_primary_reference_characters(
    bible: dict[str, Any], story_text: str = "", limit: int = MAX_PRIMARY_CHARACTER_REFERENCES
) -> list[dict[str, Any]]:
    """Choose the most important recurring cast, balancing explicit priority and actual recurrence."""
    entries = bible.get("characters", []) if isinstance(bible, dict) else []
    if not isinstance(entries, list) or limit <= 0:
        return []
    characters = [item for item in entries if isinstance(item, dict) and str(item.get("name", "")).strip()]
    cap = min(limit, MAX_PRIMARY_CHARACTER_REFERENCES)

    ranked: list[tuple[float, int, int, dict[str, Any]]] = []
    for index, item in enumerate(characters):
        count = _character_mention_count(story_text, item, characters)
        priority = str(item.get("visual_reference_priority", "")).strip().casefold()
        is_primary = priority in {"primary", "main", "protagonist", "главный", "главная", "главные"}
        # Explicit primary status helps, but cannot automatically push a frequently
        # recurring child/companion below a lightly mentioned side character.
        if not is_primary and count < (1 if not any(
            str(c.get("visual_reference_priority", "")).strip().casefold()
            in {"primary", "main", "protagonist", "главный", "главная", "главные"}
            for c in characters
        ) else 2):
            continue
        score = float(count) + (1.0 if is_primary else 0.0)
        ranked.append((score, count, -index, item))

    ranked.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    selected = [row[3] for row in ranked[:cap]]
    return selected

def character_reference_prompt(entry: dict[str, Any], setting_context: dict[str, Any] | None = None) -> str:
    """Create a single-subject, neutral reference portrait prompt from the locked bible."""
    name = str(entry.get("name", "the main character")).strip()
    kind = str(entry.get("kind", "human")).casefold()
    identity = "; ".join(
        f"{label}: {entry.get(key, 'not specified')}"
        for key, label in (
            ("age_or_stage", "age/stage"), ("face_or_head", "face/head geometry"),
            ("skin_tone_or_coat", "skin tone/coat"), ("hair_or_mane", "hair/mane"),
            ("eyes", "eyes"), ("build_or_body", "build/body"),
            ("distinctive_features", "permanent distinctive features"),
        )
        if str(entry.get(key, "")).strip() and str(entry.get(key, "")).strip().casefold() not in {"not specified", "not applicable"}
    )
    era = str((setting_context or {}).get("time_period", "the story's authentic time period"))
    setting = str((setting_context or {}).get("setting", "a simple neutral studio background"))
    if kind == "animal":
        subject = "one animal only, full head and upper body clearly visible"
    else:
        subject = "one person only, head-and-shoulders portrait, face unobstructed and clearly visible"
    selected_style = visual_style_description("image")
    return (
        f"Create a CHARACTER IDENTITY REFERENCE portrait in this project's selected visual style ({selected_style}) for the fictional story character {name}. "
        f"Show {subject}. This is a clean identity reference, not a story scene: centered composition, neutral calm expression, "
        "eyes visible, natural anatomy and skin/fur texture, clear even illumination, plain uncluttered neutral background, "
        "no other people or animals, no collage, no split panels, no props covering the face. "
        f"CANONICAL IDENTITY — preserve exactly: {identity or str(entry.get('continuity_lock', 'Use one stable, plausible identity'))}. "
        f"Story period: {era}. Setting/visual context: {setting}. "
        "Use simple, era-appropriate clothing only if applicable. No text, labels, captions, watermark, logo, decorative frame, or letterboxing. "
        "One image with one subject only; prioritize a clearly readable, repeatable face/head identity over dramatic styling. "
        f"\n\n{visual_style_rules('image')}"
    )


def strip_character_identity_lock_sections(prompt: str) -> str:
    """Remove previously appended character/reference mapping blocks from a stored scene prompt.

    Older checkpoints persisted scene prompts with contextual identity blocks that could mention
    characters not actually visible in the shot. Preserve the scene and quality/safety instructions,
    while removing the character list itself so reference selection is based on the visible scene.
    """
    text = str(prompt or "")
    terminators = (
        "MANDATORY FINAL IMAGE FORMAT",
        "YOUTUBE CONTENT SAFETY CONSTRAINTS",
        "VISIBLE SCENE TEXT LANGUAGE",
        "CANONICAL VISUAL REFERENCE INSTRUCTION",
    )
    while True:
        start = text.casefold().find("canonical character identity lock")
        if start < 0:
            break
        lowered = text.casefold()
        ends = [lowered.find(label.casefold(), start + len("canonical character identity lock")) for label in terminators]
        ends = [position for position in ends if position >= 0]
        if ends:
            end = min(ends)
            text = text[:start].rstrip() + "\n\n" + text[end:].lstrip()
        else:
            text = text[:start].rstrip()
            break
    # The dynamic reference map is always appended at the end; do not keep it in a checkpoint.
    visual_map = text.casefold().find("canonical visual reference instruction")
    if visual_map >= 0:
        text = text[:visual_map].rstrip()
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _scene_subject_text(text: str) -> str:
    """Keep only the actual visual description, excluding boilerplate and Agent reply text."""
    cleaned = strip_character_identity_lock_sections(str(text or ""))
    cut_markers = (
        "MANDATORY FINAL IMAGE FORMAT",
        "YOUTUBE CONTENT SAFETY CONSTRAINTS",
        "VISIBLE SCENE TEXT LANGUAGE",
        "CANONICAL VISUAL REFERENCE INSTRUCTION",
        "Option 1",
        "Option 2",
        "I've generated",
        "I’ve generated",
        "I've created",
        "I’ve created",
    )
    lowered = cleaned.casefold()
    positions = [lowered.find(marker.casefold()) for marker in cut_markers if lowered.find(marker.casefold()) >= 0]
    if positions:
        cleaned = cleaned[:min(positions)]
    return re.sub(r"\s+", " ", cleaned).strip()


def _is_detail_only_scene(text: str) -> bool:
    """Detect macro/detail shots where face portraits would be a distracting reference.

    This only suppresses human portrait attachments when a tight crop is explicitly
    about hands, fabric, tools, or an object and there is no indication that a face
    or head is visible. Full person shots and face portraits remain eligible.
    """
    value = _scene_subject_text(text).casefold()
    tight = re.search(r"\b(macro|close[- ]?up|extreme close|tight close|detail shot|крупн(?:ый|ого|ом) план|макросъ[её]мк)\b", value)
    detail_subject = re.search(
        r"\b(hands?|fingers?|palms?|wrists?|fabric|cloth|weave|fibers?|stitches?|needle|coins?|sewing machine|gears?|treadle|wooden wall|planks?|sign|hands? holding|рук(?:а|и|у|ой|ами)|пальц(?:ы|ев|ами)|ткань|шов|игл(?:а|ы|у|ой)|монет(?:а|ы|у|ами)|шестерн(?:я|и|ей)|педаль|дос(?:ка|ки|ок)|вывеск(?:а|и|у))\b",
        value,
    )
    face_visible = re.search(
        r"\b(face|faces|portrait|head and shoulders|visible face|eyes visible|full face|facial features|лицо|лица|портрет|голова и плечи|видны глаза)\b",
        value,
    )
    full_subject = re.search(r"\b(standing|stands|sitting|sits|walking|walks|full[- ]body|medium shot|wide shot|whole person|в полный рост|стоит|сидит|ид[её]т)\b", value)
    return bool(tight and detail_subject and not face_visible and not full_subject)


def _text_mentions_character(text: str, entry: dict[str, Any]) -> bool:
    return any(_alias_count(text, alias) > 0 for alias in _character_alias_candidates(entry))


def find_channel_hero_reference(
    project_folder: Path, preferred_path: str | Path | None = None
) -> Path | None:
    """Choose one etalon image for this project; reuse the saved pick on resumes.

    A new project randomly selects one valid image from the channel's etalon folder.
    The selected path is persisted in the project's character bible/state, so restarts
    do not randomly switch the protagonist's outfit mid-project.
    """
    channel_dir = Path(project_folder).parent
    etalon_dir = channel_dir / CHANNEL_HERO_REFERENCE_DIR_NAME
    if not etalon_dir.is_dir():
        return None
    candidates: list[Path] = []
    try:
        for candidate in etalon_dir.iterdir():
            if candidate.is_file() and candidate.suffix.casefold() in SUPPORTED_REFERENCE_IMAGE_SUFFIXES and is_valid_image(candidate):
                candidates.append(candidate.resolve())
    except OSError as exc:
        log.warning("[Hero Etalon] Не удалось прочитать %s: %s", etalon_dir, exc)
        return None
    if not candidates:
        log.warning("[Hero Etalon] Папка существует, но не содержит валидного изображения: %s", etalon_dir)
        return None
    candidates.sort(key=lambda item: item.name.casefold())

    preferred = None
    if preferred_path:
        try:
            candidate = Path(preferred_path).resolve()
            if candidate in candidates and candidate.parent == etalon_dir.resolve():
                preferred = candidate
        except Exception:
            preferred = None
    if preferred is not None:
        chosen = preferred
        log.info("[Hero Etalon] Возобновляю сохранённый выбор образа: %s", chosen.name)
    elif len(candidates) == 1:
        chosen = candidates[0]
    else:
        chosen = random.choice(candidates)
        log.info(
            "[Hero Etalon] Для нового проекта случайно выбран образ %s из %s вариантов; выбор закрепляется до завершения проекта.",
            chosen.name, len(candidates),
        )
    log.info("[Hero Etalon] Эталон главного героя: %s", chosen)
    return chosen


def apply_channel_hero_reference_to_bible(
    bible: dict[str, Any],
    story_text: str,
    project_folder: Path,
    fallback_characters: dict[str, Any] | None = None,
) -> Path | None:
    """Bind one channel-level image to the story's single central protagonist.

    The character role comes from the story bible. The image only overrides visual
    identity; it never changes which person is the protagonist or the scene action.
    """
    entries = bible.get("characters", []) if isinstance(bible, dict) else []
    if not isinstance(entries, list):
        entries = []
        bible["characters"] = entries

    # Prefer this project's previously selected etalon so restart/resume cannot change identity.
    preferred_hero_path: str | Path | None = None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("channel_reference_locked"):
            preferred_hero_path = entry.get("reference_image_path") or None
            if preferred_hero_path:
                break
    if not preferred_hero_path:
        try:
            saved_state_path = Path(project_folder) / STATE_DIR_NAME / "state.json"
            if saved_state_path.is_file():
                saved_state = json.loads(saved_state_path.read_text(encoding="utf-8", errors="replace"))
                saved_analysis = saved_state.get("analysis", {}) if isinstance(saved_state, dict) else {}
                if isinstance(saved_analysis, dict):
                    preferred_hero_path = saved_analysis.get("channel_hero_reference_path") or None
                    saved_bible = saved_analysis.get("character_bible", {})
                    for saved_entry in saved_bible.get("characters", []) if isinstance(saved_bible, dict) else []:
                        if isinstance(saved_entry, dict) and saved_entry.get("channel_reference_locked"):
                            preferred_hero_path = saved_entry.get("reference_image_path") or preferred_hero_path
                            break
        except Exception as exc:
            log.debug("[Hero Etalon] Не удалось прочитать сохранённый выбор: %s", exc)
    hero_path = find_channel_hero_reference(project_folder, preferred_hero_path)

    if not hero_path:
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("channel_reference_locked"):
                continue
            old_path = str(entry.get("reference_image_path", ""))
            if old_path and Path(old_path).name.casefold() == (Path(old_path).parent / Path(old_path).name).name.casefold():
                # Only remove the channel lock/path when it points into a channel etalon directory.
                try:
                    old_resolved = Path(old_path).resolve()
                    if old_resolved.parent.name.casefold() == CHANNEL_HERO_REFERENCE_DIR_NAME.casefold():
                        entry.pop("reference_image_path", None)
                        entry.pop("reference_identity_sha256", None)
                except Exception:
                    pass
            entry.pop("channel_reference_locked", None)
            entry.pop("channel_reference_source", None)
        return None

    if not entries and isinstance(fallback_characters, dict):
        for index, (name, description) in enumerate(list(fallback_characters.items())[:8]):
            entries.append({
                "id": re.sub(r"[^A-Z0-9]+", "_", str(name).upper()).strip("_") or f"CHARACTER_{index+1}",
                "name": str(name).strip(),
                "aliases": [str(name).strip()],
                "kind": "human",
                "narrative_role": "protagonist" if index == 0 else "supporting",
                "visual_reference_priority": "primary" if index == 0 else "supporting",
                "age_or_stage": "not specified",
                "face_or_head": "follow the channel etalon image for the protagonist" if index == 0 else "stable face/head geometry",
                "skin_tone_or_coat": "follow the channel etalon image for the protagonist" if index == 0 else "stable skin tone",
                "hair_or_mane": "follow the channel etalon image for the protagonist" if index == 0 else "stable hairstyle",
                "eyes": "not specified",
                "build_or_body": "not specified",
                "wardrobe_or_equipment": "adapt clothing naturally to the scene",
                "distinctive_features": "follow the channel etalon image for the protagonist" if index == 0 else "not specified",
                "continuity_lock": str(description)[:500],
            })

    if not entries:
        # Conservative fallback for a first-person story when the text model is temporarily
        # unavailable. Normally the version-5 bible creates a more specific role/alias entry.
        entries.append({
            "id": "CHANNEL_PROTAGONIST",
            "name": "The Narrator",
            "aliases": ["the narrator", "the protagonist", "main character"],
            "kind": "human",
            "narrative_role": "protagonist",
            "visual_reference_priority": "primary",
            "age_or_stage": "as stated in the story",
            "face_or_head": "determined by the attached channel etalon image",
            "skin_tone_or_coat": "determined by the attached channel etalon image",
            "hair_or_mane": "determined by the attached channel etalon image",
            "eyes": "determined by the attached channel etalon image",
            "build_or_body": "as appropriate to the story",
            "wardrobe_or_equipment": "adapt clothing naturally to the scene",
            "distinctive_features": "determined by the attached channel etalon image",
            "continuity_lock": "The story's central narrator/protagonist, with appearance anchored to the channel etalon.",
        })

    role_keys = {"protagonist", "narrator", "main character", "central character", "lead", "главный герой", "главная героиня", "рассказчик"}
    protagonist_candidates = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        role = re.sub(r"[_-]+", " ", str(entry.get("narrative_role", "")).strip().casefold())
        priority = str(entry.get("visual_reference_priority", "")).strip().casefold()
        score = _character_mention_count(story_text, entry, entries)
        if role in role_keys:
            role_score = 100
        elif role in {"supporting", "antagonist", "background"}:
            role_score = 0
        elif priority in {"primary", "main", "protagonist", "главный", "главная", "главные"}:
            role_score = 50
        else:
            role_score = 10
        protagonist_candidates.append((role_score, score, -index, entry))

    protagonist_candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    chosen = protagonist_candidates[0][3] if protagonist_candidates else None
    if not isinstance(chosen, dict):
        return hero_path

    # Keep existing side-character reference files, but only one entry is locked to this channel image.
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry is chosen:
            continue
        if entry.get("channel_reference_locked"):
            previous = str(entry.get("reference_image_path", ""))
            try:
                if previous and Path(previous).resolve() == hero_path.resolve():
                    entry.pop("reference_image_path", None)
                    entry.pop("reference_identity_sha256", None)
            except Exception:
                pass
        entry.pop("channel_reference_locked", None)
        entry.pop("channel_reference_source", None)

    chosen["narrative_role"] = "protagonist"
    chosen["visual_reference_priority"] = "primary"
    chosen["channel_reference_locked"] = True
    chosen["channel_reference_source"] = "channel_etalon"
    chosen["reference_image_path"] = str(hero_path)
    channel_name = Path(project_folder).parent.name
    skin_override = get_channel_hero_skin_override(channel_name)
    if skin_override:
        chosen["channel_skin_tone_override"] = skin_override
        chosen["skin_tone_or_coat"] = skin_override
        log.info("[Hero Etalon] Применён локальный skin override для канала %s; на другие каналы он не влияет.", channel_name)
    else:
        chosen.pop("channel_skin_tone_override", None)
    try:
        chosen["reference_identity_sha256"] = sha256_file(hero_path)
    except Exception:
        chosen["reference_identity_sha256"] = hashlib.sha256(str(hero_path).encode("utf-8")).hexdigest()
    aliases = chosen.get("aliases", [])
    if isinstance(aliases, str):
        aliases = re.split(r"[;|,]", aliases)
    if not isinstance(aliases, list):
        aliases = []
    # Reuse only grounded aliases already supplied by the story analysis; add role labels as last-resort matching.
    for alias in (str(chosen.get("name", "")).strip(), "the narrator", "the protagonist", "main character"):
        if alias and alias.casefold() not in {str(value).casefold() for value in aliases}:
            aliases.append(alias)
    chosen["aliases"] = aliases[:12]
    log.info(
        "[Hero Etalon] Привязываю %s к главному герою истории '%s'; лицо и выбранная одежда задают базовый образ проекта, смена одежды разрешена только по конкретному сюжету.",
        hero_path.name, chosen.get("name", "The Narrator"),
    )
    bible["version"] = 5
    return hero_path


def _scene_cast_mode(*texts: str) -> str | None:
    """Read the explicit CAST marker emitted by the versioned storyboard planner."""
    raw = " ".join(str(value or "") for value in texts)
    match = re.search(r"\bCAST\s*=\s*(NONE|PROTAGONIST|SUPPORTING|BOTH)\b", raw, re.I)
    return match.group(1).upper() if match else None


def references_for_scene(bible: dict[str, Any], *context_texts: str) -> list[Path]:
    """Attach character references only when the explicit storyboard cast requires them."""
    cast_mode = _scene_cast_mode(*context_texts)
    entries = bible.get("characters", []) if isinstance(bible, dict) else []
    if not isinstance(entries, list):
        return []
    if cast_mode == "NONE":
        return []

    # CAST=PROTAGONIST means attach the channel etalon even if the generated prose
    # happened to call the narrator "the mechanic" rather than by the bible's alias.
    forced_hero_paths: list[Path] = []
    if cast_mode in {"PROTAGONIST", "BOTH"}:
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("channel_reference_locked"):
                continue
            raw_path = str(entry.get("reference_image_path", "")).strip()
            path = Path(raw_path) if raw_path else None
            if path and is_valid_image(path):
                forced_hero_paths.append(path)
                break
        if cast_mode == "PROTAGONIST" and forced_hero_paths:
            return forced_hero_paths
        # If no channel etalon exists, fall through to legacy matching.

    combined = " ".join(_scene_subject_text(str(value or "")) for value in context_texts)
    selected: list[Path] = list(forced_hero_paths)
    seen_paths: set[str] = {str(path.resolve()).casefold() for path in forced_hero_paths}
    detail_only = _is_detail_only_scene(combined)
    if cast_mode == "SUPPORTING":
        # Supporting-only shot is never allowed to inherit the channel hero reference.
        entries = [entry for entry in entries if not (isinstance(entry, dict) and entry.get("channel_reference_locked"))]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if cast_mode in {"SUPPORTING", "BOTH"} and entry.get("channel_reference_locked"):
            # BOTH can add the protagonist back only if its alias is visible in the scene prompt.
            if cast_mode != "BOTH":
                continue
        priority = str(entry.get("visual_reference_priority", "")).strip().casefold()
        if priority not in {"primary", "main", "protagonist", "главный", "главная", "главные"}:
            continue
        if detail_only and str(entry.get("kind", "human")).casefold() != "animal":
            continue
        if not any(_alias_count(combined, alias) > 0 for alias in _character_alias_candidates(entry, entries)):
            continue
        raw_path = str(entry.get("reference_image_path", "")).strip()
        if not raw_path:
            continue
        path = Path(raw_path)
        key = str(path.resolve()).casefold()
        if key in seen_paths or not is_valid_image(path):
            continue
        selected.append(path)
        seen_paths.add(key)
        if len(selected) >= min(MAX_PRIMARY_CHARACTER_REFERENCES, 4):
            break
    return selected


def add_reference_usage_instruction(
    prompt: str, bible: dict[str, Any], image_paths: list[Path] | None
) -> str:
    """Rebuild only visible characters' text locks and map ordered attachments to distinct identities."""
    base = strip_character_identity_lock_sections(prompt or "")
    # A portrait reference/face lock is counterproductive for a tight crop of hands,
    # fabric, tools or an object. Keep the scene prompt literal and avoid adding face cues.
    if _is_detail_only_scene(base) and not image_paths:
        return base
    character_map = character_bible_prompt_map(bible)
    base = append_relevant_character_locks(base, character_map, base, max_characters=MAX_PRIMARY_CHARACTER_REFERENCES)
    if not image_paths:
        return base

    wanted_paths = [str(Path(path).resolve()).casefold() for path in image_paths]
    entries = bible.get("characters", []) if isinstance(bible, dict) else []
    entry_by_path: dict[str, dict[str, Any]] = {}
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            raw_path = str(entry.get("reference_image_path", "")).strip()
            if raw_path:
                entry_by_path[str(Path(raw_path).resolve()).casefold()] = entry

    mappings: list[str] = []
    names: list[str] = []
    for index, (path_key, raw_path) in enumerate(zip(wanted_paths, image_paths), start=1):
        entry = entry_by_path.get(path_key, {})
        name = str(entry.get("name", Path(raw_path).stem)).strip()
        if name and name not in names:
            names.append(name)
        traits = []
        for key, label in (
            ("age_or_stage", "age"),
            ("face_or_head", "face"),
            ("skin_tone_or_coat", "skin/coat"),
            ("hair_or_mane", "hair/mane"),
            ("eyes", "eyes"),
            ("build_or_body", "build"),
            ("distinctive_features", "distinctive features"),
        ):
            value = str(entry.get(key, "")).strip()
            if value and value.casefold() not in {"not specified", "not applicable", "unknown"}:
                traits.append(f"{label}: {value}")
        if entry.get("channel_reference_locked"):
            skin_override = str(entry.get("channel_skin_tone_override", "") or "").strip()
            skin_rule = f" Apply this channel-specific skin rule: {skin_override}." if skin_override else ""
            legacy_wardrobe = str(entry.get("wardrobe_or_equipment", "") or "").strip()
            legacy_wardrobe_rule = (
                f" The story bible's usual outfit/equipment text is only narrative background and must be ignored wherever it conflicts with the selected image: {legacy_wardrobe}."
                if legacy_wardrobe and legacy_wardrobe.casefold() not in {"not specified", "not applicable", "unknown"} else ""
            )
            detail = ("the channel etalon image is the authoritative source for this protagonist's face, head shape, skin tone, hair, eyes, "
                      "apparent age, proportions, and defining features. The visible clothing and accessories in the selected etalon are the DEFAULT WARDROBE for this entire project. "
                      "The attached image overrides repeated/generic clothing prose in the story bible, narration and generated scene prompt; do not revert to a usual uniform when it conflicts with this image. "
                      "Only the current scene's explicit outfit-change event or a clearly different story context can override the default. Keep clothes stable in adjacent shots of the same event."
                      + legacy_wardrobe_rule
                      + " Use this protagonist only because the current storyboard CAST marker explicitly includes them; do not add them to object or environment shots."
                      + skin_rule)
        else:
            detail = "; ".join(traits) if traits else str(entry.get("continuity_lock", "stable canonical appearance"))
        kind = "animal" if str(entry.get("kind", "human")).casefold() == "animal" else "person"
        mappings.append(
            f"ATTACHED REFERENCE IMAGE #{index} — filename '{Path(raw_path).name}' belongs only to {name} ({kind}; canonical ID {entry.get('id', name)}). "
            f"Recognize this identity by these traits: {detail}. Do not assign this face/body to another character."
        )

    instruction = (
        "CANONICAL VISUAL REFERENCE MAP — attachment order is the order listed below.\n"
        + "\n".join(mappings)
        + "\n\nEach attached image is a different, exclusive character identity. If two or more mapped characters appear in the same shot, "
          "show them as separate individuals and match each identity only to its own reference; never swap, merge, average, or duplicate their faces. "
          "Use these references only for characters who are explicitly visible in the current scene description. An object-only or environment-only "
          "shot must remain object-only/environment-only even when character references are available. The scene prompt controls action, composition, and setting. "
          "For the channel-locked protagonist, the selected etalon outfit is the project's default. The image wins over conflicting wardrobe descriptions generated from the story; do not redraw a repeatedly mentioned uniform or accessory when the selected etalon shows different clothing. Only a clear outfit change depicted in this exact scene may override it. "
          "The image controls stable identity and default wardrobe; do not force clothing/accessories from unrelated earlier descriptions. Do not substitute newly designed faces for the mapped characters."
    )
    result = base + "\n\n" + instruction
    # Apply the channel-only skin colour lock at the final prompt boundary, after style/identity text.
    for path_key in wanted_paths:
        entry = entry_by_path.get(path_key, {})
        if isinstance(entry, dict) and entry.get("channel_reference_locked") and entry.get("channel_skin_tone_override"):
            result = append_channel_hero_skin_lock(result, entry, "image")
            break
    return result


def append_relevant_character_locks(
    prompt: str,
    character_map: dict[str, Any] | None,
    *context_texts: str,
    max_characters: int = MAX_PRIMARY_CHARACTER_REFERENCES,
) -> str:
    """Append canonical continuity constraints for only the characters relevant to this shot.

    The alias index supports English prompts while canonical names remain in the original
    story script. Role aliases are required to be distinctive to reduce false matches.
    """
    if not isinstance(character_map, dict) or not character_map:
        return (prompt or "").strip()
    prompt_text = re.sub(r"\s+", " ", (prompt or "")).casefold()
    context_text = re.sub(r"\s+", " ", " ".join(str(x or "") for x in context_texts)).casefold()
    combined = f" {prompt_text} {context_text} "
    selected: list[tuple[str, str]] = []
    selected_ids: set[str] = set()

    def mention_matches(alias: str) -> bool:
        alias = re.sub(r"\s+", " ", str(alias or "")).strip().casefold()
        if len(alias) < 2:
            return False
        if re.search(r"(?<![\w])" + re.escape(alias) + r"(?:['’]s)?(?![\w])", combined, re.I):
            return True
        tokens = re.findall(r"[a-zа-яё0-9]+", alias, flags=re.I)
        # A short single name like Rose may be used possessively (Rose's).
        # Avoid matching tiny/common tokens such as "Li" or "Al".
        distinctive = [
            tok for tok in tokens
            if len(tok) >= 4 and tok.casefold() not in _CHARACTER_AMBIGUOUS_NAME_TOKENS
        ]
        return any(
            re.search(r"(?<![\w])" + re.escape(tok) + r"(?:['’]s)?(?![\w])", combined, re.I)
            for tok in distinctive[:2]
        )

    for name, description in character_map.items():
        name_str = str(name).strip()
        desc_str = str(description).strip()
        if not name_str or not desc_str:
            continue
        aliases = _CHARACTER_ALIAS_INDEX.get(name_str, [name_str])
        if not any(mention_matches(alias) for alias in aliases):
            continue
        # Deduplicate in case several aliases or duplicate name forms refer to one canon.
        identity_key = re.sub(r"\W+", "", name_str.casefold())
        if identity_key in selected_ids:
            continue
        selected_ids.add(identity_key)
        selected.append((name_str, desc_str))
        if len(selected) >= max_characters:
            break

    if not selected:
        return (prompt or "").strip()
    lines = [
        "CANONICAL CHARACTER IDENTITY LOCK — selected identities below are fixed. "
        "Never redesign face, skin tone, hair colour/style, eyes, age, body type, or permanent animal coat/markings. "
        "For a channel-etalon-locked protagonist, the outfit and accessories in the selected etalon are this project's default wardrobe and override repeated/general outfit descriptions in the narration that conflict with it. "
        "Change that default only when the current scene clearly depicts a different outfit or a meaningful story time/weather/location/activity change; keep outfits stable across adjacent shots of one continuous event. "
        "For all other characters, follow their own story/reference wardrobe. Do not add other named characters unless the scene explicitly requires them."
    ]
    for name, description in selected:
        lines.append(f"{name}: {description}")
    return (prompt or "").strip() + "\n\n" + "\n".join(lines)



# Stage 3 planning constants
INTRO_MIN_CLIPS = 8
INTRO_MAX_CLIPS = 11
INTRO_SHORT_VIDEO_THRESHOLD_SEC = 420.0
INTRO_SHORT_VIDEO_MAX_SHARE = 0.38
INTRO_SHORT_MIN_CLIPS = 5
ENABLE_MAIN_VIDEO_INSERTS = True
MAIN_VIDEO_INSERT_MIN_DUR = 7.2
MAIN_VIDEO_INSERT_MAX_DUR = 9.2
MAIN_VIDEO_INSERT_TARGET_DUR = 8.0

def plan_floating_intro(segments, audio_duration):
    """Choose N and the exact opening endpoint from Whisper sentence/pause boundaries."""
    duration=max(0.0,float(audio_duration or 0.0))
    if duration<INTRO_SHORT_VIDEO_THRESHOLD_SEC:
        upper=max(INTRO_SHORT_MIN_CLIPS,min(INTRO_MIN_CLIPS,int((duration*INTRO_SHORT_VIDEO_MAX_SHARE)//8.0)))
        lower=INTRO_SHORT_MIN_CLIPS
    else:
        lower,upper=INTRO_MIN_CLIPS,INTRO_MAX_CLIPS
    # Five clips need at least four 8s windows plus a 6.8s final window.
    physically_possible=max(1,int(max(0.0,duration-6.8)//8.0)+1)
    upper=min(upper,physically_possible)
    lower=min(lower,upper)
    words=[]
    for segment in segments or []:
        if not isinstance(segment,dict): continue
        for word in (segment.get("words") or [segment]):
            try:
                start=float(word.get("start",segment.get("start",0.0)))
                end=float(word.get("end",segment.get("end",0.0)))
                token=str(word.get("word",word.get("text","")))
                if end>start: words.append((start,end,token))
            except Exception: pass
    words.sort()
    boundaries=[]
    for index,(_,end,token) in enumerate(words):
        next_start=words[index+1][0] if index+1<len(words) else duration
        pause=max(0.0,next_start-end)
        if re.search(r"[.!?…][\"'”’»)]*$",token) or pause>=0.35:
            boundaries.append(min(duration,end+pause*0.5))
    options=[]
    for count in range(lower,upper+1):
        low=8.0*(count-1)+6.8
        high=min(duration,8.0*(count-1)+9.0)
        valid=[x for x in boundaries if low<=x<=high]
        if valid:
            best=min(valid,key=lambda x:abs(x-count*8.0))
            options.append((abs(best-count*8.0),count,best))
    if options:
        _,count,intro_end=min(options,key=lambda x:(x[0],x[1]))
    else:
        count=max(1,min(upper,round(duration/64.0*8.0)))
        intro_end=min(duration,8.0*count)
        if intro_end<8.0*(count-1)+6.8 and count>1:
            count=max(1,min(count,int((duration-6.8)//8.0)+1))
            intro_end=min(duration,8.0*count)
    intro_end=round(max(0.0,intro_end),3)
    durations=[8.0]*max(0,count-1)
    durations.append(round(intro_end-sum(durations),3))
    if durations[-1]<6.8 and count>1:
        shift=round(6.8-durations[-1],3)
        durations[-2]=round(durations[-2]-shift,3)
        durations[-1]=6.8
    durations[-1]=round(intro_end-sum(durations[:-1]),3)
    return {"clip_count":len(durations),"clip_durations":durations,"intro_end_time":round(sum(durations),3)}


def _nearest_whisper_cut(segments, target, lo, hi):
    cuts=[]
    for segment in segments or []:
        if not isinstance(segment,dict): continue
        for word in (segment.get("words") or [segment]):
            try:
                end=float(word.get("end",segment.get("end",0)))
                if lo<=end<=hi: cuts.append(end)
            except Exception: pass
    return min(cuts,key=lambda x:abs(x-target)) if cuts else min(hi,max(lo,target))


def assign_main_video_insert_slots(main_clips, segments, intro_end_time, audio_duration):
    if not ENABLE_MAIN_VIDEO_INSERTS or not main_clips or any(x.get("video_insert_assigned") for x in main_clips):
        return main_clips
    remaining=max(0.0,float(audio_duration)-float(intro_end_time))
    zones=([(0.12,0.30),(0.70,0.90)] if remaining<360 else
           ([(0.08,0.22),(0.28,0.43),(0.58,0.73),(0.80,0.94)] if remaining<=900 else
            [(0.05,0.18),(0.22,0.36),(0.43,0.58),(0.64,0.78),(0.82,0.96)]))
    candidates=[(i,c) for i,c in enumerate(main_clips) if float(c["end"])-float(c["start"])>=MAIN_VIDEO_INSERT_MIN_DUR]
    selected=[]
    for lo,hi in zones:
        target=float(intro_end_time)+remaining*(lo+hi)/2
        eligible=[(i,c) for i,c in candidates if i not in selected and all(abs(i-j)>=3 for j in selected)]
        if eligible: selected.append(min(eligible,key=lambda x:abs((float(x[1]["start"])+float(x[1]["end"]))/2-target))[0])
    try:
        prompt="Choose scene indexes for video inserts; prefer movement, travel, nature/weather, crowds and location changes; avoid static dialogue. Return JSON integer array.\n"+"\n".join(f'{i}: {c["start"]:.2f}-{c["end"]:.2f} {str(c.get("text",""))[:180]}' for i,c in candidates[:80])
        match=re.search(r"\[[\s\S]*?\]",str(call_text_api(prompt) or ""))
        proposed=json.loads(match.group(0)) if match else []
        valid=[int(x) for x in proposed if isinstance(x,int) and any(x==i for i,_ in candidates)]
        if valid: selected=valid[:len(zones)]
    except Exception as exc: log.warning("[Veo Inserts] LLM selection failed; using zone fallback: %s",exc)
    remainder_by_index={}
    for i in selected:
        clip=main_clips[i]; start=float(clip["start"]); end=float(clip["end"])
        if end-start>MAIN_VIDEO_INSERT_MAX_DUR:
            cut=_nearest_whisper_cut(segments,start+MAIN_VIDEO_INSERT_TARGET_DUR,start+MAIN_VIDEO_INSERT_MIN_DUR,min(end-5.5,start+MAIN_VIDEO_INSERT_MAX_DUR))
            if cut-start<MAIN_VIDEO_INSERT_MIN_DUR: continue
            remainder=dict(clip)
            remainder["start"]=round(cut,3); remainder["end"]=round(end,3); remainder["duration"]=round(end-cut,3)
            remainder["media_type"]="image"; remainder["done"]=False; remainder["video_insert_assigned"]=True
            remainder["output_path"]=str(Path(clip["output_path"]).with_name(f"scene_insert_remainder_{i+1:04d}.mp4"))
            remainder["remainder_of_scene"]=i+1
            remainder_by_index[i]=remainder
            clip["end"]=round(cut,3); clip["duration"]=round(cut-start,3)
            clip["text"]=get_text_for_timeslot(segments,start,cut)
        if MAIN_VIDEO_INSERT_MIN_DUR<=float(clip["end"])-float(clip["start"])<=MAIN_VIDEO_INSERT_MAX_DUR:
            clip["media_type"]="video"
    expanded=[]
    for i,clip in enumerate(main_clips):
        expanded.append(clip)
        if i in remainder_by_index: expanded.append(remainder_by_index[i])
    cursor=float(intro_end_time)
    for index,clip in enumerate(expanded):
        duration=max(0.05,float(clip["end"])-float(clip["start"]))
        clip["start"]=round(cursor,3); clip["end"]=round(cursor+duration,3); clip["duration"]=round(duration,3)
        clip["index"]=index; clip["media_type"]="video" if clip.get("media_type")=="video" else "image"
        clip["video_insert_assigned"]=True; cursor=clip["end"]
    if expanded:
        delta=round(float(audio_duration)-float(expanded[-1]["end"]),3)
        expanded[-1]["end"]=round(float(audio_duration),3)
        expanded[-1]["duration"]=round(expanded[-1]["duration"]+delta,3)
    return expanded


def master_main_video_insert(raw_path, output_path, target_duration):
    if not is_valid_video(raw_path): return False
    temp=output_path.with_name(output_path.stem+".master_tmp.mp4"); temp.unlink(missing_ok=True)
    target=float(target_duration)
    vf=f"setpts=PTS-STARTPTS,scale=1920:1080:force_original_aspect_ratio=increase:flags=lanczos,crop=1920:1080,eq=contrast=1.01:saturation=1.02,noise=alls=1:allf=t+u,fps={FPS},tpad=stop_mode=clone:stop_duration=1,trim=duration={target:.3f},setsar=1"
    base=["-y","-i",str(raw_path),"-map","0:v:0","-an","-vf",vf,"-t",f"{target:.3f}","-r",str(FPS),"-pix_fmt","yuv420p","-video_track_timescale","15360","-movflags","+faststart",*FFMPEG_CLEAN_META_VIDEO]
    for encoder in (["-c:v",HW_ENCODER,"-preset",ENCODER_PRESET,"-profile:v","high"],["-c:v","libx264","-preset","fast","-crf","18","-profile:v","high"]):
        try:
            temp.unlink(missing_ok=True)
            subprocess.run(base+encoder+[str(temp)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=600)
            actual=get_duration(temp)
            if is_valid_video(temp) and not has_audio_stream(temp) and actual is not None and abs(actual-target)<=0.12:
                temp.replace(output_path); return True
        except Exception as exc: log.warning("[Veo Inserts] Mastering failed: %s",exc)
    temp.unlink(missing_ok=True); return False


def build_intro_narration_windows(
    narration_segments: list[dict[str, Any]] | None,
    shot_count: int = INTRO_SHOT_COUNT,
    window_seconds: float = FLOW_CLIP_SECONDS,
    clip_durations: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Map Whisper's actual word timestamps onto fixed intro-shot time windows.

    This is based on the exact generated narration audio, not on equal character/word
    counts from the source script. Each intro clip therefore receives only the words
    that are spoken while that clip is on screen.
    """
    windows: list[dict[str, Any]] = []
    durations = [float(x) for x in clip_durations[:shot_count]] if isinstance(clip_durations, list) and len(clip_durations) == shot_count else [float(window_seconds)] * shot_count
    boundaries = []
    cursor = 0.0
    for index, duration in enumerate(durations):
        start = cursor
        end = cursor + duration
        boundaries.append((start, end))
        cursor = end
        windows.append({
            "shot": index + 1,
            "start": round(start, 3),
            "end": round(end, 3),
            "text": "",
        })

    if not isinstance(narration_segments, list):
        return windows

    words_by_window: list[list[str]] = [[] for _ in range(shot_count)]
    fallback_segments_by_window: list[list[str]] = [[] for _ in range(shot_count)]

    for segment in narration_segments:
        if not isinstance(segment, dict):
            continue
        try:
            seg_start = float(segment.get("start", 0.0))
            seg_end = float(segment.get("end", seg_start))
        except (TypeError, ValueError):
            continue
        if not (seg_end >= 0 and seg_start < cursor):
            continue

        raw_words = segment.get("words")
        if isinstance(raw_words, list) and raw_words:
            for word_item in raw_words:
                if not isinstance(word_item, dict):
                    continue
                token = str(word_item.get("word", "") or "")
                if not token.strip():
                    continue
                try:
                    word_start = float(word_item.get("start", seg_start))
                    word_end = float(word_item.get("end", word_start))
                except (TypeError, ValueError):
                    word_start, word_end = seg_start, seg_end
                # Assign a boundary-spanning word to the shot where it begins; in
                # particular, do not drop the final spoken word just because its end
                # timestamp extends a fraction past the 64-second intro boundary.
                anchor_time = max(0.0, word_start)
                window_index = next((i for i, (left, right) in enumerate(boundaries) if left <= anchor_time < right), -1)
                if 0 <= window_index < shot_count:
                    words_by_window[window_index].append(token)
        else:
            # Segment-only fallback: assign the whole segment to the window containing
            # its midpoint, rather than duplicating the same sentence across shots.
            text = str(segment.get("text", "") or "").strip()
            if text:
                midpoint = max(0.0, (seg_start + seg_end) / 2.0)
                window_index = next((i for i, (left, right) in enumerate(boundaries) if left <= midpoint < right), -1)
                if 0 <= window_index < shot_count:
                    fallback_segments_by_window[window_index].append(text)

    for index, window in enumerate(windows):
        if words_by_window[index]:
            # stable-whisper's word tokens preserve leading spaces and punctuation.
            window["text"] = "".join(words_by_window[index]).strip()
        elif fallback_segments_by_window[index]:
            window["text"] = " ".join(fallback_segments_by_window[index]).strip()

    return windows


def _format_intro_timecode(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000)))
    minutes, rem_ms = divmod(total_ms, 60_000)
    secs, millis = divmod(rem_ms, 1000)
    return f"{minutes:02d}:{secs:02d}.{millis:03d}"


def generate_intro_storyboard_gemini(
    opening_text: str,
    characters: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    target_lang: str = "ru",
    timed_windows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Create linked opening shots matching the persisted floating-intro windows."""
    chars = "\n".join(
        f"- {name}: {desc}" for name, desc in (characters or {}).items()
    ) or "Story-accurate figures in authentic attire matching the era."

    setting = json.dumps(context or {}, ensure_ascii=False)
    visual_language = narration_language_name(target_lang)
    shot_count = len(timed_windows) if isinstance(timed_windows, list) and timed_windows else INTRO_SHOT_COUNT
    windows = timed_windows if isinstance(timed_windows, list) and len(timed_windows) == shot_count else []
    if windows:
        timed_narration_block = "\n".join(
            f"SHOT {int(item.get('shot', i + 1))} | {_format_intro_timecode(float(item.get('start', i * FLOW_CLIP_SECONDS)))}-"
            f"{_format_intro_timecode(float(item.get('end', (i + 1) * FLOW_CLIP_SECONDS)))} | "
            f"EXACT WORDS SPOKEN DURING THIS SHOT: {str(item.get('text', '')).strip() or '[NO SPOKEN WORDS IN THIS WINDOW]'}"
            for i, item in enumerate(windows)
        )
    else:
        timed_narration_block = (
            "Whisper word timestamps are unavailable. Stay conservative and map the opening narration "
            "chronologically; never invent specific actions or events.\n" + str(opening_text or "")[:2200]
        )

    prompt = f"""
You are an elite visual director working in this selected style: {visual_style_description("video")}.
Create EXACTLY {shot_count} consecutive cinematic video prompts for Google Flow.
Each generated clip must match its supplied Whisper time window; the complete intro duration is the sum of those windows.

CRITICAL NARRATION-TO-IMAGE SYNCHRONIZATION:
Each numbered shot corresponds ONLY to its own fixed time window and exact spoken words below.
Do not illustrate the whole opening in every shot. Do not use information from later narration before it is spoken.
Do not insert generic establishing shots, stock scenes, random characters, or symbolic images that do not communicate the assigned words.
Identify the concrete subject/action/place/object/relationship conveyed by that exact excerpt and show it clearly during that shot.
If the excerpt is abstract, use a restrained visual metaphor that preserves its exact meaning; do not invent a separate event.
If no words are spoken in a window, create only a quiet continuity shot consistent with the already established scene and do not introduce a new plot event.
The eight clips must progress chronologically with the narration. Different compositions are welcome, but semantic accuracy is more important than visual variety.

ERA & SETTING:
{setting}

CHARACTERS & VISUAL IDENTITY:
{chars}

EXACT TIME-CODED NARRATION (from Whisper word timestamps on the actual voice track):
{timed_narration_block}

CAST CLASSIFICATION — required for every shot:
- `NONE`: only environment, objects, vehicle, documents, money, hands/POV detail, or visual metaphor; no protagonist visible.
- `PROTAGONIST`: the story's central narrator/main character is physically visible and necessary in this shot.
- `SUPPORTING`: only a secondary character is visible; the channel protagonist must not appear.
- `BOTH`: the protagonist and a specific secondary character are both visibly needed.
First-person narration or mentioning the narrator does NOT by itself mean they should appear. Do not use the protagonist as a recurring presenter and do not put them in every shot. Prefer `NONE` for setting, object, money, car, document, or environmental shots.
The `cast` metadata controls which reference image Python attaches to Flow before video generation; it is not text to include in the visual prompt.

RULES:
{visual_style_rules("video")}
- Match the exact historical era, wardrobe, environment, and atmosphere from the setting and opening text.
- Never use modern or contemporary elements unless the story explicitly takes place today.
- Keep identity, face, hair, skin tone, and permanent animal markings consistent across all eight shots. Keep wardrobe consistent within a continuous moment; change clothing only when the narration, time jump, weather, location, or activity clearly calls for it.
- Motion is continuous, restrained, and physically coherent while the selected painted/illustrated/photographic medium remains unchanged throughout all frames.
- Respect each named character's canonical age. Children may appear only when the story calls for them and only in ordinary, safe, non-graphic scenes; never age them up or down. No text overlays, subtitles, logos, watermarks, decorative borders, or letterboxing.
- If physical writing is genuinely needed inside the depicted scene (for example a sign, notice, paper, or shop sign), any readable wording must be in {visual_language}, the same language as the narration; never default to English. If not needed, show no readable writing.
- General-audience visual safety: never depict a person harming an animal, a visibly suffering or severely emaciated animal, blood, open wounds, graphic injury, or death. If such topics appear in the narration, show a calm recovery/rescue/veterinary-care alternative or a neutral setting; do not depict the harmful act.
- No graphic violence, cruelty, or sensationalized suffering.
- AUDIO: ONLY very soft natural location ambience (gentle wind, footsteps, fabric movement, room tone, mild weather). Absolutely NO dialogue, voice-over, music, melody, humming, whistling, shrill bird calls, squeals, screeches, metallic squeaks, sharp whooshes, sudden stingers, abrupt impacts, or piercing high tones. Keep ambience smooth and quiet; prefer near-silence to harsh or unnatural sound.
- SILENT VISUAL PERFORMANCE (CRITICAL): The intro is a silent visual illustration under a separate narrator. Every visible person's mouth must remain gently closed and their lips completely still for all frames. Never show anyone talking, whispering, shouting, singing, mouthing words, lip-syncing, or moving their lips as if speaking. Do not show a character mid-sentence or addressing another character. When the narration quotes or describes dialogue, show the listener's silent reaction, a restrained glance, a small closed-mouth expression, hands/objects, an over-the-shoulder view, or another visual consequence instead of depicting the act of speaking. Prefer profile, three-quarter, or wider compositions; avoid talking-head close-ups. This rule overrides any dialogue/action described in the narration for the visual performance only. The scene may still show the relevant people and story action, but all mouths stay closed and still.

Return ONLY a valid JSON array containing exactly {shot_count} objects. Every object must contain:
- `shot`: integer 1 to {shot_count}
- `cast`: exactly one of `NONE`, `PROTAGONIST`, `SUPPORTING`, `BOTH`
- `prompt`: a complete Flow video prompt; do not put a CAST marker inside the prompt
Example shape:
[
  {{"shot": 1, "cast": "NONE", "prompt": "..."}},
  {{"shot": 2, "cast": "NONE", "prompt": "..."}},
  {{"shot": 3, "cast": "PROTAGONIST", "prompt": "..."}},
  {{"shot": 4, "cast": "NONE", "prompt": "..."}},
  {{"shot": 5, "cast": "SUPPORTING", "prompt": "..."}},
  {{"shot": 6, "cast": "NONE", "prompt": "..."}},
  {{"shot": 7, "cast": "BOTH", "prompt": "..."}},
  {{"shot": 8, "cast": "NONE", "prompt": "..."}}
]
""".strip()

    res = call_text_api(prompt)
    if res:
        try:
            match = re.search(r"\[.*\]", res, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                if isinstance(data, list) and len(data) == shot_count:
                    # Respect the model's explicit shot IDs instead of assuming its
                    # JSON array order can never be shuffled.
                    ordered_data: list[dict[str, Any] | None] = [None] * shot_count
                    for fallback_index, raw_item in enumerate(data, start=1):
                        if not isinstance(raw_item, dict):
                            raise ValueError("Storyboard item is not an object")
                        try:
                            shot_number = int(raw_item.get("shot", fallback_index))
                        except (TypeError, ValueError):
                            shot_number = fallback_index
                        if not 1 <= shot_number <= shot_count or ordered_data[shot_number - 1] is not None:
                            raise ValueError(f"Storyboard has invalid/duplicate shot number: {shot_number}")
                        ordered_data[shot_number - 1] = raw_item
                    if any(item is None for item in ordered_data):
                        raise ValueError("Storyboard does not contain every shot number")
                    data = [item for item in ordered_data if item is not None]
                    normalized: list[dict[str, Any]] = []
                    for index, item in enumerate(data, start=1):
                        if not isinstance(item, dict) or not str(item.get("prompt", "")).strip():
                            raise ValueError("Storyboard shot is missing a prompt")
                        raw_prompt = str(item["prompt"]).strip()
                        cast_mode = str(item.get("cast", "NONE") or "NONE").strip().upper()
                        if cast_mode not in {"NONE", "PROTAGONIST", "SUPPORTING", "BOTH"}:
                            raise ValueError(f"Storyboard shot {index} has invalid cast value: {cast_mode!r}")
                        locked_prompt = append_relevant_character_locks(raw_prompt, characters or {}, raw_prompt, max_characters=MAX_PRIMARY_CHARACTER_REFERENCES)
                        locked_prompt = append_selected_visual_style(locked_prompt, "video")
                        window = windows[index - 1] if windows else {}
                        normalized.append({
                            "shot": index,
                            "cast": cast_mode,
                            "narration_start": float(window.get("start", (index - 1) * FLOW_CLIP_SECONDS)),
                            "narration_end": float(window.get("end", index * FLOW_CLIP_SECONDS)),
                            "narration_excerpt": str(window.get("text", "") or ""),
                            "prompt": enforce_flow_intro_silent_visual_policy(
                                enforce_flow_ambience_only_audio(
                                    apply_visible_text_language_rule(
                                        apply_youtube_content_safety_rules(locked_prompt, "video"), target_lang
                                    )
                                )
                            ),
                        })
                    return normalized
        except Exception as exc:
            log.warning("[Intro] Не удалось разобрать раскадровку из восьми кадров: %s", exc)

    # Deterministic fallback remains tied to the exact narration windows. It no longer
    # creates a generic establishing montage that can contradict the spoken words.
    era_hint = str((context or {}).get("time_period", "the story's stated time period"))
    place_hint = str((context or {}).get("setting", "the story's stated setting"))
    style_prefix = visual_style_description("video")
    fallback: list[dict[str, Any]] = []
    for index in range(shot_count):
        window = windows[index] if windows else {}
        excerpt = str(window.get("text", "") or "").strip()
        if excerpt:
            shot_core = (
                f"Show a visually clear, literal-but-cinematic interpretation of this exact spoken narration "
                f"for this assigned narration interval: {excerpt}. Depict only the people, object, action, place, "
                f"or relationship conveyed by these words, in {place_hint}, {era_hint}. "
                "Do not add another event or unrelated cutaway. Use one restrained continuous action."
            )
        else:
            shot_core = (
                f"Create a quiet continuity shot in {place_hint}, {era_hint}, consistent with the immediately "
                "preceding narrated moment. Do not introduce a new character, object, or plot event."
            )
        prompt_core = f"{style_prefix}; {shot_core}"
        cast_mode = "NONE"
        if characters:
            first_character = next(iter(characters), "")
            if first_character and re.search(rf"(?<!\w){re.escape(str(first_character))}(?!\w)", excerpt, re.IGNORECASE):
                cast_mode = "PROTAGONIST"
        shot = {
            "shot": index + 1,
            "cast": cast_mode,
            "narration_start": float(window.get("start", index * FLOW_CLIP_SECONDS)),
            "narration_end": float(window.get("end", (index + 1) * FLOW_CLIP_SECONDS)),
            "narration_excerpt": excerpt,
            "prompt": prompt_core,
        }
        shot["prompt"] = append_relevant_character_locks(
            shot["prompt"], characters or {}, shot["prompt"], max_characters=MAX_PRIMARY_CHARACTER_REFERENCES
        )
        shot["prompt"] = append_selected_visual_style(shot["prompt"], "video")
        shot["prompt"] = enforce_flow_intro_silent_visual_policy(
            enforce_flow_ambience_only_audio(
                apply_visible_text_language_rule(
                    apply_youtube_content_safety_rules(shot["prompt"], "video"), target_lang
                )
            )
        )
        fallback.append(shot)
    return fallback


def _scene_requires_visible_person(text: str, context_before: str = "", context_after: str = "", character_bible: dict[str, Any] | None = None) -> bool:
    """High-confidence cue that a scene contains a person acting or speaking.

    This is deliberately narrower than a mere mention of a human in the narration:
    it targets dialogue, interactions, and observable person-led actions so an
    environment-only storyboard decision cannot erase an explicitly narrated beat.
    """
    current = _scene_subject_text(str(text or ""))
    surrounding = " ".join(x for x in (context_before, current, context_after) if x)
    current_l = current.casefold()
    combined_l = surrounding.casefold()

    speech_re = re.compile(
        r"\b(?:said|says|say|asked|asks|replied|replies|answered|answers|whispered|whispers|"
        r"shouted|shouts|called|calls|told|tells|explained|explains|muttered|murmured|argued|"
        r"говорил\w*|говорит|сказал\w*|сказала\w*|сказали|спросил\w*|спросила\w*|"
        r"ответил\w*|ответила\w*|прошептал\w*|прошептала\w*|крикнул\w*|крикнула\w*|"
        r"объяснил\w*|объяснила\w*|позвал\w*|позвала\w*|произнёс\w*|произнес\w*|"
        r"dijo|dijeron|decía|decia|preguntó|pregunto|preguntaba|respondió|respondio|"
        r"contestó|contesto|susurró|susurro|gritó|grito|llamó|llamo|explicó|explico|"
        r"comentó|comento|murmuró|murmuro|reclamó|reclamo|powiedział|powiedzial|powiedziała|powiedziala|powiedzieli|mówił|mowil|mówiła|mowila|zapytał|zapytal|zapytała|zapytala|odpowiedział|odpowiedzial|odpowiedziała|odpowiedziala|szepnął|szepnal|szepnęła|szepnela|krzyknął|krzyknal|krzyknęła|krzyknela|oznajmił|oznajmil|odparł|odparl)\b",
        re.I,
    )
    human_re = re.compile(
        r"\b(?:person|people|man|men|woman|women|boy|girl|sheriff|rancher|cowboy|cowgirl|"
        r"mechanic|boss|worker|customer|cashier|doctor|teacher|mother|father|son|daughter|"
        r"husband|wife|friend|stranger|neighbor|neighbour|officer|suitor|farmer|he|she|they|"
        r"him|her|them|i|me|my|we|us|yo|me|mí|mi|nosotros|él|ella|ellos|ellas|hombre|"
        r"mujer|persona|personas|alguien|él|ella|ellos|ellas|yo|nosotros|cliente|cajera|"
        r"mecánico|mecánica|jefe|trabajador|médico|doctor|madre|padre|hijo|hija|marido|"
        r"esposa|amigo|amiga|vecino|vecina|policía|vaquero|sheriff|kobieta|kobiety|kobietą|kobietę|mężczyzna|mezczyzna|mężczyzny|człowiek|czlowiek|ludzie|dziecko|dzieci|chłopiec|chlopiec|chłopca|dziewczynka|dziewczynę|dziewczyna|ciężarna|ciezarna|matka|matkę|ojciec|syn|córka|corka|babcia|dziadek|ранчер|ковбой|шериф|"
        r"человек|люди|мужчина|женщина|парень|девушка|мальчик|девочка|он|она|они|его|её|"
        r"ее|ему|ей|я|мне|меня|мы|нас|рассказчик|прохожий|покупатель|кассир|механик|"
        r"начальник|работник|врач|учитель|мать|отец|сын|дочь|муж|жена|друг|подруга|сосед|"
        r"соседка|офицер|фермер)\b",
        re.I,
    )
    action_re = re.compile(
        r"\b(?:walked|walks|entered|enters|opened|opens|closed|closes|looked|looks|turned|turns|"
        r"stood|stands|sat|sits|rose|rises|came|comes|arrived|arrives|left|leaves|took|takes|"
        r"gave|gives|handed|hands|held|holds|pointed|points|nodded|nods|smiled|smiles|cried|cries|"
        r"grabbed|grabs|reached|reaches|approached|approaches|picked|picks|carried|carries|"
        r"смотрел\w*|посмотрел\w*|увидел\w*|вош[её]л\w*|вошла|открыл\w*|закрыл\w*|"
        r"подош[её]л\w*|подошла|приш[её]л\w*|пришла|уш[её]л\w*|ушла|стоял\w*|стояла|"
        r"сел\w*|села|встал\w*|встала|взял\w*|взяла|отдал\w*|отдала|передал\w*|передала|"
        r"протянул\w*|протянула|показал\w*|показала|кивнул\w*|кивнула|улыбнул\w*|улыбнулась|"
        r"обнял\w*|обняла|держал\w*|держала|положил\w*|положила|повернулся|повернулась|"
        r"entré|entre|entró|entro|abrí|abri|abrió|abrio|miré|mire|miró|miro|se sentó|se sento|"
        r"se acercó|se acerco|llegó|llego|caminó|camino|tomó|tomo|entregó|entrego|levantó|levanto|"
        r"señaló|senalo|asintió|asintio|sonrió|sonrio|sostuvo|sostiene|salió|salio|volvió|volvio|szedł|szedl|szła|szla|poszedł|poszedl|poszła|poszla|weszła|weszla|wszedł|wszedl|usiadł|usiadl|usiadła|usiadla|stanął|stanela|stanęła|spojrzał|spojrzal|spojrzała|spojrzala|patrzył|patrzyl|patrzyła|patrzyla|podał|podal|podała|podala|wziął|wzial|wzięła|wziela|trzymał|trzymal|trzymała|trzymala|przytulił|przytulil|przytuliła|przytulila|odszedł|odszedl|odeszła|odeszla|przyszedł|przyszedl|przyszła|przyszla)\b",
        re.I,
    )

    speech = bool(speech_re.search(current_l))
    humans = bool(human_re.search(current_l))
    actions = bool(action_re.search(current_l))
    quotes = bool(re.search(r'["“”«»„].{2,}["“”«»„]', current))
    vulnerable_subject_is_safe = (
        bool(SAFE_CHILD_CONTEXT_PATTERN.search(current))
        or bool(SAFE_PREGNANCY_CONTEXT_PATTERN.search(current))
    ) and not bool(SAFE_HARM_CONTEXT_PATTERN.search(current))
    if vulnerable_subject_is_safe:
        return True

    # The story bible can identify a person by their name/aliases even when the
    # narration only uses a role (for example, "the sheriff" or "the narrator").
    matched_character = False
    entries = character_bible.get("characters", []) if isinstance(character_bible, dict) else []
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and _text_mentions_character(current, entry):
                matched_character = True
                break

    if speech and (humans or matched_character or quotes or not re.search(r"\b(?:the report|the sign|el informe|el cartel|отчёт|отчет|табличка|вывеска)\b", current_l, re.I)):
        return True
    if actions and (humans or matched_character):
        return True
    # Strong interaction phrases sometimes omit the speaker's name in the current slot;
    # use adjacent context only to disambiguate, while requiring the action in this slot.
    interaction_re = re.compile(
        r"\b(?:talked to|spoke to|argued with|met with|looked at him|looked at her|handed him|handed her|"
        r"разговаривал с|разговаривала с|поговорил с|поговорила с|встретил\w*|встретила|"
        r"спорил с|спорила с|передал ему|передала ей|сказал ему|сказала ей|"
        r"habló con|hablo con|hablaron|se reunió con|se reunio con|le dijo|le preguntó|le pregunto)\b",
        re.I,
    )
    if interaction_re.search(current_l) and (humans or matched_character or human_re.search(combined_l)):
        return True
    return False


def _infer_scene_cast_mode(
    text: str,
    context_before: str = "",
    context_after: str = "",
    character_bible: dict[str, Any] | None = None,
) -> str:
    """Infer a safe CAST marker when Gemini omits one or contradicts clear action/dialogue."""
    current = _scene_subject_text(str(text or ""))
    combined = " ".join(x for x in (context_before[-350:], current, context_after[:350]) if x)
    current_l = current.casefold()
    combined_l = combined.casefold()
    first_person_action = bool(re.search(
        r"\b(?:i|me|my|we|us|yo|me|mi|nosotros|я|мне|меня|мы|нас)\b.{0,100}"
        r"\b(?:said|asked|replied|answered|looked|opened|entered|took|gave|held|walked|"
        r"сказал\w*|спросил\w*|ответил\w*|посмотрел\w*|открыл\w*|вош[её]л\w*|взял\w*|"
        r"достал\w*|подош[её]л\w*|встретил\w*|dije|pregunté|pregunte|respondí|respondi|"
        r"miré|mire|abrí|abri|entré|entre|tomé|tome)\b",
        current_l,
        re.I,
    ))
    has_dialogue = bool(re.search(
        r"\b(?:said|asked|replied|answered|told|говорил\w*|сказал\w*|спросил\w*|ответил\w*|"
        r"dijo|preguntó|pregunto|respondió|respondio|contestó|contesto|susurró|susurro|powiedział|powiedzial|powiedziała|powiedziala|powiedzieli|mówił|mowil|mówiła|mowila|zapytał|zapytal|zapytała|zapytala|odpowiedział|odpowiedzial|odpowiedziała|odpowiedziala|szepnął|szepnal|szepnęła|szepnela)\b",
        current_l,
        re.I,
    ))
    has_second_person = bool(re.search(
        r"\b(?:he|she|him|her|they|another man|another woman|the sheriff|the rancher|the mechanic|"
        r"он|она|они|ему|ей|его|её|ее|другой мужчина|другая женщина|шериф|ранчер|механик|"
        r"él|ella|ellos|ellas|le|su compañero|su compañera|el hombre|la mujer|on|ona|oni|jego|jej|mu|kobieta|mężczyzna|dziecko|dzieci|matka|ojciec)\b",
        combined_l,
        re.I,
    ))

    entries = character_bible.get("characters", []) if isinstance(character_bible, dict) else []
    protagonist_match_current = False
    supporting_match_current = False
    protagonist_match_context = False
    supporting_match_context = False
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("narrative_role", "supporting")).casefold()
            is_protagonist = (
                role in {"protagonist", "narrator", "main character", "central character", "главный герой", "главная героиня", "рассказчик"}
                or bool(entry.get("channel_reference_locked"))
            )
            if _text_mentions_character(current, entry):
                if is_protagonist:
                    protagonist_match_current = True
                else:
                    supporting_match_current = True
            elif _text_mentions_character(combined, entry):
                # Neighbouring narration can resolve a pronoun, but should not cause
                # a character from the previous/next beat to be inserted automatically.
                if is_protagonist:
                    protagonist_match_context = True
                else:
                    supporting_match_context = True

    protagonist_match = protagonist_match_current or (not supporting_match_current and protagonist_match_context)
    supporting_match = supporting_match_current or (not protagonist_match_current and supporting_match_context)
    has_second_person_current = bool(re.search(
        r"\b(?:he|she|him|her|they|another man|another woman|the sheriff|the rancher|the mechanic|"
        r"он|она|они|ему|ей|его|её|ее|другой мужчина|другая женщина|шериф|ранчер|механик|"
        r"él|ella|ellos|ellas|le|su compañero|su compañera|el hombre|la mujer|on|ona|oni|jego|jej|mu|kobieta|mężczyzna|dziecko|dzieci|matka|ojciec)\b",
        current_l,
        re.I,
    ))
    interaction_current = bool(re.search(
        r"\b(?:talked to|spoke to|argued with|met with|handed him|handed her|asked him|asked her|"
        r"разговаривал с|разговаривала с|поговорил с|поговорила с|спорил с|спорила с|передал ему|передала ей|"
        r"сказал ему|сказала ей|habló con|hablo con|hablaron|le dijo|le preguntó|le pregunto|rozmawiał z|rozmawiala z|rozmawiała z|rozmawial z|rozmawiał z|rozmawiali z|spotkał|spotkal|spotkała|spotkala|zapytał go|zapytal go|powiedział jej|powiedzial jej|powiedziała mu|powiedziala mu)\b",
        current_l,
        re.I,
    ))
    visible_exchange = has_dialogue and (has_second_person_current or interaction_current)

    if (first_person_action or protagonist_match_current) and supporting_match_current:
        return "BOTH"
    if first_person_action:
        return "BOTH" if visible_exchange else "PROTAGONIST"
    if protagonist_match_current:
        return "BOTH" if visible_exchange else "PROTAGONIST"
    if supporting_match_current:
        return "BOTH" if visible_exchange else "SUPPORTING"
    if protagonist_match:
        return "BOTH" if visible_exchange else "PROTAGONIST"
    if supporting_match:
        return "BOTH" if visible_exchange else "SUPPORTING"
    if has_dialogue:
        # If the narrator is speaking to someone, preserve the interaction instead of
        # falling back to an empty room. Otherwise show the speaking person.
        return "BOTH" if has_second_person else "SUPPORTING"
    return "SUPPORTING"


def _fallback_scene_plan(
    text: str,
    context_before: str = "",
    context_after: str = "",
    character_bible: dict[str, Any] | None = None,
) -> str:
    if _scene_requires_visible_person(text, context_before, context_after, character_bible):
        mode = _infer_scene_cast_mode(text, context_before, context_after, character_bible)
        description = (
            "show the actual person or people performing the concrete action or interaction narrated in this beat; "
            "preserve the current story event and the correct character roles"
        )
    else:
        mode = "NONE"
        clean = _scene_subject_text(text)[:240]
        description = f"faithfully illustrate the specific concrete subject of this narration: {clean}; use the relevant place or object only when it is the real focal point"
    return f"CAST={mode} | {description}, {visual_style_description('image')}, landscape 16:9"


def _scene_plan_recovery_wait(attempt_number: int) -> tuple[float | None, str]:
    """Wait for the earliest recoverable key/model resources for the selected provider."""
    now = time.time()
    waits: list[tuple[str, float]] = []

    if ACTIVE_TEXT_PROVIDER == "groq":
        with _groq_key_lock:
            viable_keys = [key for key in GROQ_API_KEYS if key not in _groq_dead_keys]
            if not viable_keys:
                return None, "все Groq API-ключи невалидны или файл groq_keys.txt пуст"
            ready_keys = [key for key in viable_keys if float(_groq_key_cooldowns.get(key, 0.0) or 0.0) <= now]
            if not ready_keys:
                unlocks = [float(_groq_key_cooldowns.get(key, 0.0) or 0.0) for key in viable_keys]
                waits.append(("Groq API keys", max(0.0, min(unlocks) - now)))
        backoff = min(
            SCENE_PLAN_RETRY_MAX_BACKOFF_SECONDS,
            SCENE_PLAN_RETRY_BASE_SECONDS * (2 ** max(0, min(attempt_number - 1, 4))),
        )
        if not waits:
            return backoff, f"Groq keys formally available; backoff {backoff:.0f}s"
        resource_wait = max(wait for _, wait in waits)
        detail = ", ".join(f"{name}: {wait:.0f}s" for name, wait in waits)
        return max(backoff, resource_wait), detail

    with _gemini_key_lock:
        viable_keys = [key for key in GEMINI_API_KEYS if key not in _gemini_dead_keys]
        if not viable_keys:
            return None, "все Gemini API-ключи окончательно исключены или список ключей пуст"
        ready_keys = [key for key in viable_keys if float(_gemini_key_cooldowns.get(key, 0.0) or 0.0) <= now]
        if not ready_keys:
            unlocks = [float(_gemini_key_cooldowns.get(key, 0.0) or 0.0) for key in viable_keys]
            waits.append(("API keys", max(0.0, min(unlocks) - now)))

    with _GEMINI_MODEL_HEALTH_LOCK:
        supported_models = [
            model for model in _effective_gemini_model_order()
            if not _GEMINI_MODEL_HEALTH.setdefault(model, {}).get("unsupported", False)
        ]
        if not supported_models:
            return None, "все настроенные Gemini-модели помечены как неподдерживаемые"
        ready_models = [
            model for model in supported_models
            if float(_GEMINI_MODEL_HEALTH.setdefault(model, {}).get("cooldown_until", 0.0) or 0.0) <= now
        ]
        if not ready_models:
            unlocks = [
                float(_GEMINI_MODEL_HEALTH.setdefault(model, {}).get("cooldown_until", 0.0) or 0.0)
                for model in supported_models
            ]
            waits.append(("models", max(0.0, min(unlocks) - now)))

    if USE_GEMINI_PROXY and GEMINI_PROXIES_LIST:
        with _gemini_proxy_lock:
            ready_proxies = [
                proxy for proxy in GEMINI_PROXIES_LIST
                if proxy not in _gemini_proxy_in_use
                and float(_gemini_proxy_cooldowns.get(proxy, 0.0) or 0.0) <= now
            ]
            if not ready_proxies:
                unlocks = [
                    float(_gemini_proxy_cooldowns.get(proxy, 0.0) or 0.0)
                    for proxy in GEMINI_PROXIES_LIST
                    if proxy not in _gemini_proxy_in_use
                ]
                if unlocks:
                    waits.append(("proxies", max(0.0, min(unlocks) - now)))

    backoff = min(
        SCENE_PLAN_RETRY_MAX_BACKOFF_SECONDS,
        SCENE_PLAN_RETRY_BASE_SECONDS * (2 ** max(0, min(attempt_number - 1, 4))),
    )
    if not waits:
        return backoff, f"ресурсы формально доступны; backoff {backoff:.0f}s"

    # A valid retry requires all resource classes to have at least one available
    # member, therefore wait for the slowest of their earliest unlock times.
    resource_wait = max(wait for _, wait in waits)
    detail = ", ".join(f"{name}: {wait:.0f}s" for name, wait in waits)
    return max(backoff, resource_wait), detail


def generate_scene_plan(
    all_segments_text: list[str],
    scene_contexts: list[dict[str, str]] | None = None,
    character_bible: dict[str, Any] | None = None,
) -> list[str]:
    """Create a complete storyboard; rotate models and split malformed batches, never guess scenes.

    Temporary API/proxy failures are retried with resource-aware backoff. A non-empty but
    malformed/short response is treated differently: the next attempt excludes the model
    that produced it, and repeated format failures split the missing clip IDs into smaller
    requests (20 -> 10 -> 5 -> 2/3). No local placeholder scenes are created.
    """
    if not all_segments_text:
        return []
    scene_contexts = scene_contexts or [{} for _ in all_segments_text]
    if len(scene_contexts) < len(all_segments_text):
        scene_contexts = list(scene_contexts) + [{} for _ in range(len(all_segments_text) - len(scene_contexts))]

    plans: list[str] = [""] * len(all_segments_text)
    initial_batch_size = 20
    initial_batches = [
        list(range(batch_start, min(len(all_segments_text), batch_start + initial_batch_size)))
        for batch_start in range(0, len(all_segments_text), initial_batch_size)
    ]

    def build_prompt(clip_indices: list[int]) -> str:
        blocks: list[str] = []
        for idx in clip_indices:
            context = scene_contexts[idx] if isinstance(scene_contexts[idx], dict) else {}
            before = re.sub(r"\s+", " ", str(context.get("before", "") or ""))[-260:]
            after = re.sub(r"\s+", " ", str(context.get("after", "") or ""))[:260]
            current = re.sub(r"\s+", " ", str(all_segments_text[idx] or ""))[:900]
            blocks.append(
                f"[CLIP {idx + 1}]\n"
                f"PREVIOUS CONTEXT (continuity only): {before or '(none)'}\n"
                f"CURRENT NARRATION (this determines the shot): {current or '(transcription unclear)'}\n"
                f"NEXT CONTEXT (continuity only): {after or '(none)'}"
            )

        return f"""
You are a precise storyboard director creating one visual plan per narration clip for this selected style: {visual_style_description("image")}.
For each clip, describe the actual event, people, objects and setting that best match the CURRENT NARRATION. The image must illustrate what the narrator is saying, not just the general theme.

Return exactly one line for every clip, preserving its numeric ID and this format exactly:
[CLIP 1] CAST=PROTAGONIST | one concise, concrete visual description
Use each input clip's actual number, not the example number. Output no commentary and never merge clips.
Allowed markers:
- CAST=PROTAGONIST: the story's main narrator/protagonist is visibly performing or experiencing the current beat.
- CAST=SUPPORTING: a named/identifiable secondary character is visibly performing the current beat; keep the protagonist out of frame.
- CAST=BOTH: the current beat genuinely shows the protagonist interacting with a relevant secondary character.
- CAST=NONE: a specific object, place, animal, document, vehicle, or environment is the actual focal subject, and no human action/dialogue is happening in this beat.

DIRECTORIAL PRIORITIES:
1. Narration-to-image fidelity is the top priority. If the current narration describes a person speaking, answering, asking, meeting, entering, opening something, handing something over, looking at someone, reacting, or performing another visible action, show the relevant person(s) and that action. Do not replace a narrated dialogue/action with an empty room, generic scenery, or unrelated symbolic object.
2. Use PREVIOUS/NEXT CONTEXT only to resolve pronouns, identify who is speaking, and maintain continuity. Do not replace the current beat with an event that occurs only in another clip.
3. First-person wording alone does not require the narrator to be visible. However, a first-person physical action, spoken line, interaction, or important emotional reaction normally should be shown with the protagonist.
4. Object-only or environment-only scenes are welcome when the narration's focal point really is an object/place, a description of a landscape, a financial figure, a document, or an abstract concept with no active person-led event. Do NOT aim for a quota of people-free scenes. Do NOT force a human into a true object detail.
5. For dialogue, show the speaker and, when the wording implies an exchange or response, the other participant. Preserve story roles; do not substitute an unrelated person.
6. Keep characters age-appropriate, respect the story's setting and time, and use the selected style consistently. Children/minors and pregnant adult women are allowed when the narration genuinely calls for them; preserve their actual story age/state and show them only in peaceful, fully clothed, physically safe, non-graphic everyday scenes. Never place a child or pregnant adult in a threatening, violent, hazardous, medical-emergency, or distressing situation. Any frame containing a child or pregnant adult must contain no weapons of any kind, blood, wounds, injuries, threats, or violent aftermath. Do not age children up or down, and do not infer pregnancy if it is not stated. No invented major events, arbitrary characters, black bars, grayscale or sepia.
7. When the current narration beat is specifically about a child/minor or a pregnant adult, make that person visible if the beat can be shown safely; do not automatically replace them with an unrelated adult or scenery. Use CAST=PROTAGONIST, SUPPORTING, or BOTH according to the actual character role. If the beat itself involves danger, violence, blood, weapons, injury, childbirth, or medical distress, do NOT show the vulnerable person in that frame; choose a separate neutral object/detail or peaceful empty setting instead.
8. Keep every line compact: one clip ID, one CAST marker, and one concrete sentence. Do not add headings, explanations, summaries, JSON, or markdown fences.

{visual_style_rules("image")}

CLIPS:
{chr(10).join(blocks)}
""".strip()

    def parse_rows(response: str, requested_indices: list[int], already_valid: dict[int, tuple[str, str]]) -> dict[int, tuple[str, str]]:
        parsed_this_attempt: dict[int, tuple[str, str]] = {}
        requested_ids = {idx + 1 for idx in requested_indices}
        for raw_line in str(response or "").splitlines():
            line = raw_line.strip().strip("`* ")
            if not line:
                continue
            id_match = re.search(r"(?:\[\s*)?(?:CLIP|SCENE|SLIDE)\s*#?\s*(\d+)\s*\]?", line, re.I)
            clip_id = int(id_match.group(1)) if id_match else None
            marker = re.search(r"CAST\s*=\s*(NONE|PROTAGONIST|SUPPORTING|BOTH)\s*\|\s*(.+)$", line, re.I)
            if not marker:
                continue
            if clip_id is None:
                lead_number = re.match(r"^\s*(\d{1,4})[.):\- ]+", line)
                if lead_number:
                    clip_id = int(lead_number.group(1))
            if clip_id is None:
                # Accept a missing label only when the response line is otherwise a
                # valid storyboard row; assign it to the next requested missing ID.
                unassigned = [
                    n for n in sorted(requested_ids)
                    if (n - 1) not in already_valid and (n - 1) not in parsed_this_attempt
                ]
                if unassigned:
                    clip_id = unassigned[0]
            if clip_id is None or clip_id not in requested_ids:
                continue
            mode = marker.group(1).upper()
            description = re.sub(r"\s+", " ", marker.group(2)).strip().strip("-:; ")
            if description:
                parsed_this_attempt[clip_id - 1] = (mode, description)
        return parsed_this_attempt

    def process_clip_indices(clip_indices: list[int], depth: int = 0) -> dict[int, tuple[str, str]]:
        accepted: dict[int, tuple[str, str]] = {}
        excluded_models: set[str] = set()
        consecutive_format_failures = 0
        last_failure = "Gemini returned no usable response"
        prompt = build_prompt(clip_indices)
        expected = len(clip_indices)

        for api_attempt in range(1, SCENE_PLAN_MAX_API_ATTEMPTS + 1):
            response = call_text_api(prompt, exclude_models=excluded_models)
            model_used = getattr(_gemini_call_context, "last_model", None)
            if ACTIVE_TEXT_PROVIDER == "groq" and model_used is None:
                model_used = f"groq:{GROQ_TEXT_MODEL}"
            if response:
                rows = parse_rows(response, clip_indices, accepted)
                for idx, row in rows.items():
                    accepted.setdefault(idx, row)

            missing_indices = [idx for idx in clip_indices if idx not in accepted]
            if not missing_indices:
                return accepted

            if response:
                consecutive_format_failures += 1
                last_failure = (
                    f"malformed/short storyboard response from {model_used or 'unknown model'}; "
                    f"response_chars={len(response)}, valid_rows={len(accepted)}/{expected}, "
                    f"missing_clip_ids={[idx + 1 for idx in missing_indices]}"
                )
                if model_used and ACTIVE_TEXT_PROVIDER == "gemini":
                    excluded_models.add(model_used)
                supported_count = 0
                if ACTIVE_TEXT_PROVIDER == "gemini":
                    with _GEMINI_MODEL_HEALTH_LOCK:
                        for model_name in _effective_gemini_model_order():
                            if not _GEMINI_MODEL_HEALTH.setdefault(model_name, {}).get("unsupported", False):
                                supported_count += 1
                if supported_count and len(excluded_models) >= supported_count:
                    log.info("[Scene Plan Retry] All supported models were tried for malformed output; starting a fresh model cycle.")
                    excluded_models.clear()

                if (
                    consecutive_format_failures >= SCENE_PLAN_FORMAT_RETRIES_BEFORE_SPLIT
                    and len(missing_indices) > SCENE_PLAN_MIN_BATCH_SIZE
                ):
                    midpoint = (len(missing_indices) + 1) // 2
                    child_groups = [missing_indices[:midpoint], missing_indices[midpoint:]]
                    log.warning(
                        "[Scene Plan Retry] Batch clips %s-%s returned malformed output %s times; "
                        "splitting only missing clips into smaller requests: %s.",
                        min(clip_indices) + 1, max(clip_indices) + 1,
                        consecutive_format_failures,
                        [f"{group[0] + 1}-{group[-1] + 1} ({len(group)} clips)" for group in child_groups if group],
                    )
                    for group in child_groups:
                        accepted.update(process_clip_indices(group, depth + 1))
                    return accepted

                if api_attempt < SCENE_PLAN_MAX_API_ATTEMPTS:
                    log.warning(
                        "[Scene Plan Retry] %s; retrying selected provider before splitting if needed. "
                        "No placeholder scenes will be used.", last_failure,
                    )
                    time.sleep(SCENE_PLAN_FORMAT_RETRY_DELAY_SECONDS)
                    continue
            else:
                consecutive_format_failures = 0
                last_failure = f"{ACTIVE_TEXT_PROVIDER.upper()} API returned None (key/proxy unavailable or timed out)"
                if excluded_models:
                    # A transport outage is not evidence that the previously malformed
                    # model is bad permanently. Once the router returns None, clear the
                    # temporary format exclusions before the cooldown-aware retry cycle.
                    log.info("[Scene Plan Retry] API unavailable; clearing temporary malformed-response model exclusions before recovery retry.")
                    excluded_models.clear()

            if api_attempt >= SCENE_PLAN_MAX_API_ATTEMPTS:
                break

            wait_seconds, wait_reason = _scene_plan_recovery_wait(api_attempt)
            if wait_seconds is None:
                raise RuntimeError(
                    f"Storyboard clips {min(clip_indices) + 1}-{max(clip_indices) + 1} cannot be completed: "
                    f"{last_failure}; retry stopped because {wait_reason}. No fallback scenes were generated."
                )
            log.warning(
                "[Scene Plan Retry] Clips %s-%s: valid=%s/%s, attempt %s/%s; %s. "
                "Waiting %.0fs before retry. No placeholder scenes will be used.",
                min(clip_indices) + 1, max(clip_indices) + 1, len(accepted), expected,
                api_attempt, SCENE_PLAN_MAX_API_ATTEMPTS, last_failure, wait_seconds,
            )
            log.info("[Scene Plan Retry] Recovery status: %s", wait_reason)
            time.sleep(wait_seconds)

        missing_indices = [idx for idx in clip_indices if idx not in accepted]
        if missing_indices:
            raise RuntimeError(
                f"Storyboard clips {min(clip_indices) + 1}-{max(clip_indices) + 1} remain incomplete "
                f"after {SCENE_PLAN_MAX_API_ATTEMPTS} attempts; missing clip IDs={[idx + 1 for idx in missing_indices]}. "
                f"Last issue: {last_failure}. Project checkpoint preserved; no guessed scenes were generated."
            )
        return accepted

    for batch_number, clip_indices in enumerate(initial_batches, start=1):
        log.info(
            "[Scene Plan] Starting storyboard batch %s/%s: clips %s-%s (%s clips).",
            batch_number, len(initial_batches), clip_indices[0] + 1, clip_indices[-1] + 1, len(clip_indices),
        )
        parsed = process_clip_indices(clip_indices)
        if len(parsed) != len(clip_indices):
            missing_ids = [idx + 1 for idx in clip_indices if idx not in parsed]
            raise RuntimeError(
                f"Internal storyboard validation failed for batch {batch_number}; missing IDs={missing_ids}. "
                "No guessed scenes were generated."
            )

        for global_index in clip_indices:
            clip_number = global_index + 1
            current = str(all_segments_text[global_index] or "")
            context = scene_contexts[global_index] if isinstance(scene_contexts[global_index], dict) else {}
            before = str(context.get("before", "") or "")
            after = str(context.get("after", "") or "")
            mode, description = parsed[global_index]
            if mode == "NONE" and _scene_requires_visible_person(current, before, after, character_bible):
                corrected_mode = _infer_scene_cast_mode(current, before, after, character_bible)
                log.warning(
                    "[Scene Plan] Clip %s: selected LLM chose CAST=NONE despite narrated action/dialogue; changing to CAST=%s.",
                    clip_number, corrected_mode,
                )
                mode = corrected_mode
                description = (
                    "show the actual person or people performing the action or dialogue in this exact narration beat; "
                    + description
                )
            plans[global_index] = f"CAST={mode} | {description}"

        log.info(
            "[Scene Plan] Storyboard batch %s/%s processed: clips %s-%s, valid %s rows=%s/%s.",
            batch_number, len(initial_batches), clip_indices[0] + 1, clip_indices[-1] + 1,
            ACTIVE_TEXT_PROVIDER.upper(), len(parsed), len(clip_indices),
        )

    return plans


def generate_video_prompt(
    clip_index: int,
    segment_text: str,
    ctx_before: str,
    ctx_after: str,
    plan_entry: str,
    main_characters: dict[str, Any],
    general_context: dict[str, Any],
    target_lang: str = "ru",
) -> str:
    visual_language = narration_language_name(target_lang)
    chars = "\n".join(
        f"- {name}: {desc}"
        for name, desc in main_characters.items()
    ) or "Story-relevant people in authentic attire, preserving each person's exact story age."

    prompt = f"""
You are a literal visual director writing ONE Google Flow video prompt in this selected style: {visual_style_description("video")}.
Create one coherent 8-second cinematic shot for the CURRENT STORY SEGMENT.

GLOBAL CHARACTER CONTINUITY:
{chars}

SETTING:
{json.dumps(general_context, ensure_ascii=False)}

SHOT PLAN:
{plan_entry}

TEXT BEFORE:
{ctx_before[:700]}

CURRENT SEGMENT:
{segment_text[:1200]}

TEXT AFTER:
{ctx_after[:700]}

RULES:
{visual_style_rules("video")}
- Keep colour, lighting, texture and motion consistent with the selected style.
- Lock identity from the canonical bible: face shape, skin tone, hair colour/style, eyes, age, build, and permanent features must not drift. Use the listed outfit as the default, but follow explicit clothing changes and sensible changes required by the story's time, weather, location, or activity. Preserve the outfit across adjacent shots of the same event; do not vary it randomly.
- Children/minors and pregnant adult women may appear when explicitly relevant to the current story beat. Preserve each child's exact story age; never age them up or down. Show children fully clothed in peaceful, ordinary, physically safe scenes, with a responsible caregiver for babies/very young children. Pregnant characters must be clearly adult, fully clothed, respectfully depicted, and not shown in labour, childbirth, medical complications, or distress. Any frame with a child or pregnant adult must contain no weapon of any kind, blood, wound, injury, threat, attack, violent aftermath, or hazardous activity.
- Only show subjects relevant to the current segment.
- Never depict deliberate harm to animals, an animal in visible pain/distress, severe emaciation, blood, wounds, graphic injury, death, or any harmful act. If the story mentions these topics, show calm recovery, respectful non-graphic care, an uninjured animal in a safe place, neutral setting details, or a non-graphic aftermath.
- Describe continuous realistic motion during the entire 8 seconds, but never animate harm, panic, attack, pursuit, or distress.
- Include deliberate camera movement: slow push, pan, track, crane or handheld documentary movement.
- Keep facial identity, wardrobe and environment coherent with the global context.
- No split screen, no subtitles or graphic text overlays, no watermark, no logos, no borders, no letterboxing, no black bars, no film-strip edges, no date, no timecode, no decorative overlay.
- If physical writing is essential within the scene, all readable wording must be in {visual_language}, the narration language; never default to English. Otherwise keep writing out of view.
- AUDIO: preserve only subtle natural environmental ambience (wind, footsteps, fabric, wood creaks, rain, room tone) where appropriate. Absolutely no dialogue, conversations, spoken words, narrator, voice-over, singing, vocalizations, or music of any kind. No cinematic score or rhythmic soundtrack. Ambience must remain quiet and natural.
- Do not switch visual styles mid-clip. Preserve the selected style consistently.

Return ONLY the final video prompt, 70-120 words.
""".strip()

    res = call_text_api(prompt)
    if res:
        generated = re.sub(r"^(prompt|video prompt):\s*", "", res.strip(), flags=re.I)
        generated = append_relevant_character_locks(
            generated, main_characters, segment_text, plan_entry, ctx_before[-500:], ctx_after[:500], max_characters=MAX_PRIMARY_CHARACTER_REFERENCES
        )
        generated = append_selected_visual_style(generated, "video")
        return enforce_flow_ambience_only_audio(
            apply_visible_text_language_rule(
                apply_youtube_content_safety_rules(generated, "video"), target_lang
            )
        )

    fallback = (
        f"{visual_style_description('video').capitalize()} inspired by the story context, "
        f"{plan_entry}. Period-appropriate lighting, story-accurate subjects, "
        f"subtle continuous environmental motion, slow controlled camera movement, "
        f"authentic textures and stable composition. If harm is discussed, show only calm recovery or neutral context, not the harmful act."
    )
    fallback = append_relevant_character_locks(
        fallback, main_characters, segment_text, plan_entry, ctx_before[-500:], ctx_after[:500], max_characters=MAX_PRIMARY_CHARACTER_REFERENCES
    )
    fallback = append_selected_visual_style(fallback, "video")
    return enforce_flow_ambience_only_audio(
        apply_visible_text_language_rule(
            apply_youtube_content_safety_rules(fallback, "video"), target_lang
        )
    )


def generate_image_prompt(
    clip_index: int,
    segment_text: str,
    ctx_before: str,
    ctx_after: str,
    plan_entry: str,
    main_characters: dict[str, Any],
    general_context: dict[str, Any],
    target_lang: str = "ru",
    defer_character_locks: bool = False,
) -> str:
    visual_language = narration_language_name(target_lang)
    chars = "\n".join(
        f"- {name}: {desc}"
        for name, desc in main_characters.items()
    ) or "Story-relevant people in authentic attire, preserving each person's exact story age."

    prompt = f"""
You are a historical visual artist writing ONE Google Flow image prompt in this selected style: {visual_style_description("image")}.
Create one coherent image for the CURRENT STORY SEGMENT.

GLOBAL CHARACTER CONTINUITY:
{chars}

SETTING:
{json.dumps(general_context, ensure_ascii=False)}

SHOT PLAN:
{plan_entry}

TEXT BEFORE:
{ctx_before[:700]}

CURRENT SEGMENT:
{segment_text[:1200]}

TEXT AFTER:
{ctx_after[:700]}

RULES:
{visual_style_rules("image")}
- Clear focal subject, richly readable colours, detailed surfaces, believable anatomy and stable composition.
- Keep identity identical to the canonical character bible: face, skin tone/coat, hair/mane, eyes, age, build, and permanent markings are locked. Treat base wardrobe/equipment as a reference rather than an immutable uniform; follow explicit changes and sensible changes demanded by story time, weather, location, or activity. Keep clothing stable across shots in the same event and do not vary it arbitrarily.
- Children/minors and pregnant adult women may appear when the current story beat genuinely requires them. Preserve each child's exact story age; never age them up or down. Show children fully clothed in peaceful, ordinary, physically safe scenes, with a responsible caregiver for babies/very young children. Pregnant characters must be clearly adult, fully clothed, respectfully depicted, and not shown in labour, childbirth, medical complications, or distress. Any frame with a child or pregnant adult must contain no weapon of any kind anywhere in frame, no blood, wound, injury, threat, attack, violent aftermath, or hazardous activity.
- CAST MARKER IS BINDING: {plan_entry.split('|', 1)[0].strip() if '|' in plan_entry else 'CAST=UNSPECIFIED'}. If it is CAST=NONE, show NO visible people at all (not even the protagonist in the background); illustrate with objects, environment, POV hands or a story-specific visual metaphor. If CAST=SUPPORTING, show only the relevant secondary character(s) and do not show the protagonist. If CAST=PROTAGONIST, show the etalon-backed protagonist only because the storyboard determined that this beat needs them. If CAST=BOTH, show the protagonist and the specifically relevant secondary person as separate people.
- First-person narration does NOT automatically put the narrator on screen. Text before/after is continuity context and must not replace the current beat. If the CURRENT SEGMENT describes dialogue, an exchange between people, a person entering/opening/handing/looking/reacting, or another concrete person-led action, show the relevant person(s) and action even if the broader topic is money, debt, work, or reflection. Prefer object/environment/POV scenes only when the current beat has no meaningful person-led action or dialogue.
- If a character is allowed by the CAST marker, name that character explicitly and obey their canonical identity; do not add other characters casually.
- Never depict deliberate harm to animals, visible animal suffering, severe emaciation, blood, open wounds, graphic injury, or a dead animal. If the narration mentions harm or illness, depict a calm recovery/care context, an uninjured animal in a safe setting, relevant objects, or neutral environment instead.
- Clear focal subject, stable and balanced widescreen composition.
- The image must fill a landscape 16:9 canvas edge to edge.
- Strictly a single STILL image: DO NOT describe motion, seconds, or camera movement.
- Absolutely no monochrome, grayscale, black-and-white, sepia, blue-tinted monochrome or desaturated film look.
- Absolutely no black side bars, black top/bottom bars, borders, frames, film strips, sprocket holes, camera UI, timestamps, subtitles, or graphic text overlays.
- If a physical sign, notice, label, paper, book, or screen is essential to the current scene, any readable text must be in {visual_language}, the narration language; never use English by default. If writing is not relevant, do not include readable words.
- Do not add artificial film grain, vignette, borders, or faux archival treatment unless demanded by the selected style.
- Do not switch visual styles within the project; follow the selected style consistently.

Return ONLY the final image prompt, 40-75 words.
""".strip()

    res = call_text_api(prompt)
    if res:
        base_prompt = re.sub(r"^(prompt|image prompt):\s*", "", res.strip(), flags=re.I)
    else:
        base_prompt = (
            f"{visual_style_description('image').capitalize()} of {segment_text[:350]}, "
            f"{plan_entry}. Rich readable colours, clear focal subject, "
            f"edge-to-edge 16:9 composition, no frames or monochrome treatment."
        )
    # Prompt prefetch runs in a worker thread. Defer the mutable, global alias-index
    # based character-lock expansion until the main scene loop has the final shot text.
    # The normal generation path retains the previous behaviour by default.
    if not defer_character_locks and _scene_cast_mode(plan_entry) != "NONE":
        base_prompt = append_relevant_character_locks(
            base_prompt, main_characters, plan_entry, max_characters=MAX_PRIMARY_CHARACTER_REFERENCES
        )
    formatted = make_full_color_edge_to_edge_prompt(base_prompt)
    return apply_visible_text_language_rule(
        apply_youtube_content_safety_rules(formatted, "image"), target_lang
    )


def get_thumbnail_prompt_1(text: str, target_lang: str = "ru") -> str | None:
    res = call_text_api(
        f"Create a serious YouTube documentary thumbnail prompt in this selected style: {visual_style_description('thumbnail')}. Story: '{text[:1200]}'. "
        f"{visual_style_rules('thumbnail')} Clear focal subject, textless, max 45 words. "
        "Do not depict abuse, animal distress, blood, wounds, graphic injury, cruelty, death, or suffering as clickbait; prefer calm care/recovery or a neutral setting."
    )
    return apply_visible_text_language_rule(
        apply_youtube_content_safety_rules(append_selected_visual_style(res.strip(), "thumbnail"), "thumbnail"), target_lang
    ) if res else None


def get_thumbnail_prompt_2(
    image_name: str,
    target_lang: str = "ru",
    story_text: str = "",
    title: str = "",
) -> str | None:
    """Prompt a close, story-aware variation of the attached source thumbnail.

    The actual source image is attached to Flow later; Gemini receives its filename plus
    the title/narration context and must not invent a wholesale replacement composition.
    """
    story_excerpt = re.sub(r"\s+", " ", str(story_text or "")).strip()[:1800]
    title_text = re.sub(r"\s+", " ", str(title or "")).strip()[:180]
    source_stem = Path(str(image_name or "")).stem.casefold()
    reference_label = "FALLBACK SOURCE IMAGE" if re.match(r"scene[_-]?\d+", source_stem) else "ORIGINAL YOUTUBE PREVIEW"
    request = (
        f"Write ONE image-generation prompt for a close visual variation of the ATTACHED {reference_label}. "
        f"The source image ({reference_label}) will be attached separately to the image generator, so explicitly instruct the image model to use it as the PRIMARY composition template. "
        "Preserve its overall scene concept, camera angle, crop, subject placement and relative scale, main silhouettes, visual hierarchy, background layout, dominant colour palette and lighting direction. "
        "It must look recognizably like the same thumbnail at first glance, not a different generic scene. "
        "Use the story context only to make factual details accurate: adjust props, clothes or small background details when they conflict with the actual narration, while keeping the original composition and layout as close as possible. "
        "Do not replace the scene with a completely different location/action merely because it seems more dramatic. "
        f"Selected style: {visual_style_description('thumbnail')}. {visual_style_rules('thumbnail')} "
        "Full-colour 16:9, textless, no logos or watermarks, max 70 words. No clickbait and no events that contradict the story.\n"
        f"Video title: {title_text or '(not provided)'}\n"
        f"Story context (for factual correction only, not for replacing the composition): {story_excerpt or '(not provided)'}\n"
        f"Original preview filename for reference: {image_name}"
    )
    res = call_text_api(request)
    if not res:
        res = (
            f"Use the attached {reference_label} as the PRIMARY visual template and create a close, recognizably related variation. "
            "Preserve the same camera angle, crop, subject placement and scale, main silhouettes, background layout, dominant colours, lighting direction and visual hierarchy. "
            "Keep the same overall visual idea; only adjust specific props, clothing or minor details that conflict with the actual story. Do not replace it with a different generic scene. "
            f"Story context: {title_text}. {story_excerpt[:500]}. "
            f"Selected style: {visual_style_description('thumbnail')}. Full-colour 16:9, textless, no logos/watermarks."
        )
    # Add a deterministic composition lock after the model-written prompt so it cannot be
    # diluted by generic wording. The actual source image will be the first attached reference.
    composition_lock = (
        f"\n\nSOURCE COMPOSITION LOCK: Attached image 1 is the {reference_label} and is the PRIMARY visual reference. "
        "Create a close variation of that image, not a new concept. Keep the same camera viewpoint, crop, subject count and placement, subject size, key silhouettes, background geometry, dominant palette, light direction and overall balance. "
        "Story context may change only the details that are factually wrong; preserve the visual composition as much as possible. "
        "Attached image 2, if present, is the identity reference for the protagonist only and must not override the original thumbnail's composition."
    )
    result = apply_visible_text_language_rule(
        apply_youtube_content_safety_rules(append_selected_visual_style(res.strip() + composition_lock, "thumbnail"), "thumbnail"),
        target_lang,
    )
    return result


def get_clickable_thumbnail_prompt(story_text: str, title: str, target_lang: str = "ru") -> str:
    """Create a compelling but accurate thumbnail prompt tied to this story, not misleading clickbait."""
    excerpt = (story_text or "")[:4500]
    res = call_text_api(
        f"Create ONE compelling, high-click-through YouTube documentary thumbnail prompt in this selected style: "
        f"{visual_style_description('thumbnail')}. Video title: {title[:180]!r}. "
        "Use the story excerpt below to identify its most distinctive real conflict, discovery, choice, or psychological insight. "
        "Show one instantly readable focal subject and a strong visual contrast; dramatic but credible lighting; vivid, clear composition; "
        "the image must be accurate to the story and must not imply events or outcomes that are not in the narration. "
        f"{visual_style_rules('thumbnail')} Textless, no logos/watermarks/borders, max 45 words.\n\nSTORY EXCERPT:\n{excerpt}"
    )
    if not res:
        res = (
            f"A compelling, story-faithful YouTube documentary thumbnail about {title[:180]}, "
            f"{visual_style_description('thumbnail')}, one strong focal subject, vivid contrast, expressive but credible mood, "
            "clean 16:9 composition, textless, no logos or watermarks."
        )
    return apply_visible_text_language_rule(
        apply_youtube_content_safety_rules(append_selected_visual_style(res.strip(), "thumbnail"), "thumbnail"), target_lang
    )

def get_thumbnail_prompt_3(title: str, target_lang: str = "ru") -> str | None:
    res = call_text_api(
        f"Create a serious YouTube documentary thumbnail prompt in this selected style: {visual_style_description('thumbnail')} for title '{title}'. "
        f"{visual_style_rules('thumbnail')} One clear focal subject, textless, max 45 words. "
        "Do not depict animal suffering, abuse, blood, wounds, graphic injury, or death; show calm recovery or a neutral setting where relevant."
    )
    return apply_visible_text_language_rule(
        apply_youtube_content_safety_rules(append_selected_visual_style(res.strip(), "thumbnail"), "thumbnail"), target_lang
    ) if res else None

# ============================================================================
# FLOW EXCEPTIONS
# ============================================================================


class FlowTechnicalError(RuntimeError):
    pass


class FlowContentSafetyReviewError(RuntimeError):
    """A visual asset did not pass the pre-publication content-safety review."""


class FlowMonetizationReviewError(RuntimeError):
    """A high-confidence monetization/community-policy risk needs editing before continuing."""


class FlowGenerationRejectedError(FlowTechnicalError):
    """Flow explicitly displayed a content-policy refusal for this request."""


class FlowGenerationFailedError(FlowTechnicalError):
    """Flow displayed a generic generation-failed card; retry as a transient failure."""


class FlowAudioGenerationError(FlowTechnicalError):
    """Flow's audio synthesis failed while no usable video result was produced."""


class FlowWrongMediaKindError(FlowTechnicalError):
    """Flow returned a new image while the pipeline explicitly requested a video."""


def _normalize_flow_policy_text(text: str) -> str:
    """Normalize localized Flow UI messages so punctuation, diacritics, and casing do not hide refusals."""
    value = str(text or "").casefold().replace("ё", "е")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


# Flow now sometimes rejects video generations with a third-party content-provider
# notice and a refund message instead of the older "prompt may violate our policies"
# wording. Keep this separate and specific so ordinary status text is not misclassified.
FLOW_THIRD_PARTY_REFUSAL_MARKERS = (
    # English wording shown on the current Flow Failed card.
    "interests of third party content providers",
    "interest of third party content providers",
    "due to interests of third party content providers",
    "due to the interests of third party content providers",
    "third party rights holders",
    "interests of third party rights holders",
    # Common Russian translations.
    "интересов сторонних поставщиков контента",
    "интересы сторонних поставщиков контента",
    "в интересах сторонних поставщиков контента",
    "из за интересов сторонних поставщиков контента",
    "интересов сторонних провайдеров контента",
    "интересы сторонних провайдеров контента",
    "интересов сторонних правообладателей",
    "в интересах сторонних правообладателей",
    # Polish and Spanish UI equivalents.
    "interesow dostawcow tresci stron trzecich",
    "interesy dostawcow tresci stron trzecich",
    "intereses de proveedores de contenido de terceros",
    "interes de los proveedores de contenido de terceros",
)


def detect_new_flow_third_party_refusal(current_text: str, baseline_text: str = "") -> str | None:
    """Detect a newly added third-party-rights/provider refusal card in Flow."""
    current = _normalize_flow_policy_text(current_text)
    baseline = _normalize_flow_policy_text(baseline_text)
    for marker in FLOW_THIRD_PARTY_REFUSAL_MARKERS:
        if current.count(marker) > baseline.count(marker):
            return marker

    # Fallback for wording variations: require both the third-party/provider concept
    # and a generation-refusal or refund cue, rather than matching generic mentions.
    combos = (
        ("third party", "content provider", "refund"),
        ("third party", "content providers", "cant generate"),
        ("сторонних", "контента", "вернут"),
        ("сторонних", "правообладател", "не могу сгенерировать"),
    )
    for combo in combos:
        if all(part in current for part in combo) and not all(part in baseline for part in combo):
            return "localized third-party content-provider refusal: " + " + ".join(combo)
    return None



FLOW_RATE_LIMIT_MARKERS = (
    # Exact Russian wording shown in the supplied Flow screenshot.
    "превышена допустимая частота запросов",
    "превышен допустимый лимит запросов",
    "делайте паузы между запросами",
    "паузы между запросами",
    "слишком много запросов",
    # English UI variants, including the newer Agent toast shown in the supplied screenshot.
    "rate limit exceeded",
    "you have exceeded the rate limit",
    "too many requests",
    "space out your requests",
    "please slow down your requests",
    "requests are happening too quickly",
    "you are asking too fast",
    "youre asking too fast",
    "please slow down and try again",
    "you are going too fast",
    "youre going too fast",
    # Spanish UI variants (accents are removed by _normalize_flow_policy_text).
    "demasiadas solicitudes",
    "has superado el limite de solicitudes",
    "se ha superado el limite de solicitudes",
    "espacia tus solicitudes",
    "estas pidiendo demasiado rapido",
    "estas solicitando demasiado rapido",
    "por favor ve mas despacio e intentalo de nuevo",
    # Polish UI variants.
    "przekroczono dopuszczalna czestotliwosc zadan",
    "przekroczono limit zadan",
    "zbyt wiele zadan",
    "rozloz zadania w czasie",
    "pytasz zbyt szybko",
    "prosze zwolnij i sprobuj ponownie",
    # Russian variants of the newly shown short Agent warning.
    "вы слишком быстро отправляете запросы",
    "вы запрашиваете слишком быстро",
    "вы слишком часто обращаетесь",
    "пожалуйста замедлитесь и попробуйте снова",
)


def detect_new_flow_rate_limit(current_text: str, baseline_text: str = "") -> str | None:
    """Detect a newly added Flow request-throttling card across localized UIs."""
    current = _normalize_flow_policy_text(current_text)
    baseline = _normalize_flow_policy_text(baseline_text)

    for marker in FLOW_RATE_LIMIT_MARKERS:
        if current.count(marker) > baseline.count(marker):
            return marker

    # Conservative combinations cover wording changes without matching a generic
    # "try again later" notice or unrelated Gemini/API rate-limit log text.
    combinations = (
        ("частота запросов", "паузы между запросами"),
        ("слишком много запросов", "повторите попытку позже"),
        ("too many requests", "try again later", "space out"),
        ("rate limit", "space out your requests"),
        ("rate limit", "requests", "try again later"),
        ("you are asking too fast", "slow down"),
        ("youre asking too fast", "slow down"),
        ("asking too fast", "try again"),
        ("please slow down", "try again"),
        ("demasiadas solicitudes", "intentalo de nuevo"),
        ("limite de solicitudes", "espacia"),
        ("zbyt wiele zadan", "ponownie pozniej"),
        ("czestotliwosc zadan", "rozloz zadania"),
        ("слишком быстро", "попробуйте снова"),
    )
    for combo in combinations:
        if all(part in current for part in combo) and not all(part in baseline for part in combo):
            return "localized Flow request-rate limit: " + " + ".join(combo)
    return None


def detect_new_flow_policy_refusal(current_text: str, baseline_text: str = "") -> str | None:
    """Return a newly displayed policy-refusal marker across English/Russian/Polish/Spanish UIs."""
    third_party_marker = detect_new_flow_third_party_refusal(current_text, baseline_text)
    if third_party_marker:
        return "third-party content-provider restriction: " + third_party_marker

    current = _normalize_flow_policy_text(current_text)
    baseline = _normalize_flow_policy_text(baseline_text)
    markers = (
        # Specific policy language, not generic retry instructions (audio failures also say "try another prompt").
        "this request may violate our policies",
        "this request may violate our policy",
        "this prompt may violate our policies",
        "this prompt may violate our policy",
        # Exact wording shown by current Flow refusal cards (the older detector
        # covered "may" but missed the equally common "might" variant).
        "this prompt might violate our policies",
        "this prompt might violate our policy",
        "this generation might violate our policies",
        "this generation might violate our policy",
        "this request might violate our policies",
        "this request might violate our policy",
        "this generation may violate our policies",
        "this generation may violate our policy",
        "похоже этот запрос может нарушать наши правила",
        "этот запрос может нарушать наши правила",
        "этот промпт может нарушать наши правила",
        "этот запрос нарушает наши правила",
        "этот промпт нарушает наши правила",
        "похоже этот контент нарушает наши правила",
        "этот контент нарушает наши правила",
        "нарушает правила сообщества",
        "контент не соответствует нашим правилам",
        "не соответствует правилам сообщества",
        "содержимое нарушает наши правила",
        "политике безопасности",
        "запрещенный контент",
        "недопустимый контент",
        "нарушение правил",
        # Common localized equivalents of an explicit request/prompt-policy refusal.
        "to zadanie moze naruszac nasze zasady",
        "to polecenie moze naruszac nasze zasady",
        "ta generacja moze naruszac nasze zasady",
        "esta solicitud puede infringir nuestras politicas",
        "esta peticion puede infringir nuestras politicas",
        "esta generacion podria infringir nuestras politicas",
        "esta generacion puede infringir nuestras politicas",
    )
    for marker in markers:
        if current.count(marker) > baseline.count(marker):
            return marker

    # Some UI translations vary the sentence but retain the same distinctive combination.
    combo_checks = (
        ("контент", "нарушает", "правил"),
        ("generacion", "politicas"),
        ("generacja", "zasady"),
    )
    for combo in combo_checks:
        if all(part in current for part in combo) and not all(part in baseline for part in combo):
            return "localized policy refusal: " + " + ".join(combo)
    return None


class FlowPolicyBlockError(RuntimeError):
    pass


class FlowSuspiciousActivityError(FlowPolicyBlockError):
    """Flow's profile-specific suspicious-activity / extension warning."""


class FlowQuotaError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    """Stream a file hash so cached review results only apply to unchanged media."""
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def review_images_for_youtube(
    image_paths: list[Path],
    context: str = "",
    media_kind: str = "image",
) -> dict[str, Any]:
    """Use Gemini vision as an advisory pre-publication screen for generated visuals.

    Only an explicit REJECT is a production blocker; REVIEW and API/network errors
    are recorded for later inspection and do not stop the project. This cannot
    guarantee YouTube policy compliance and never disables provider safety filters.
    """
    if not ENABLE_YOUTUBE_CONTENT_SAFETY_REVIEW:
        return {"decision": "SKIPPED", "reason": "Content-safety review disabled in configuration", "reviewed_at": now_iso()}
    if not USE_GEMINI_PROXY or not GEMINI_PROXIES_LIST:
        return {"decision": "REVIEW", "reason": "Gemini content review requires a configured proxy; direct requests are disabled", "reviewed_at": now_iso()}
    usable_paths = [Path(item) for item in image_paths if Path(item).is_file() and is_valid_image(Path(item), min_size=100)]
    if not usable_paths:
        return {"decision": "REVIEW", "reason": "No valid image/frame was available; content was not screened and manual inspection is recommended", "reviewed_at": now_iso()}

    try:
        from io import BytesIO
        parts: list[dict[str, Any]] = [{"text": (
            "You are a conservative pre-publication visual safety reviewer for a general-audience YouTube documentary. "
            "Inspect the supplied image(s), not just the textual context. Return ONLY JSON with keys decision, reason, flags. "
            "decision must be PASS, REJECT, or REVIEW. PASS only when the visible imagery is clearly non-graphic and does not depict abuse or severe distress. "
            "REJECT if any image clearly shows a person intentionally harming or terrorizing an animal; an animal visibly suffering, severely emaciated, injured, bleeding, trapped in distress, or dead; graphic injuries or gore; a violent attack; explicit sexual nudity; or other clearly prohibited graphic/violent imagery. "
            "Also assess advertiser suitability separately from likely removal: use REVIEW when a young-looking human or animal is visibly distressed, when shocking imagery, strong profanity in visible text, sexualized imagery, drug/tobacco use, a dangerous act, threatening weapon use, or hateful imagery may make the scene unsuitable for some ads. A calm, fully clothed child in an ordinary safe scene, or a respectfully depicted fully clothed pregnant adult, is allowed and is NOT by itself a reason to REJECT or REVIEW. Do not label a neutral educational discussion as a violation solely because its topic is sensitive. "
            "Ordinary healthy animals, calm rescue/recovery, and non-graphic veterinary care should PASS. "
            "Use REVIEW for ambiguity, uncertain visible context, or if you cannot confidently assess the frames. Do not assume abuse merely because an animal is present. "
            "If multiple images/frames are supplied, assess all of them and reject/review the set if any one is concerning. "
            f"Media kind: {media_kind}. Context (for interpretation only): {(context or '')[:1800]}"
        )}]
        for image_path in usable_paths:
            with Image.open(image_path) as source:
                frame = source.convert("RGB")
                frame.thumbnail((1280, 1280), getattr(Image, "Resampling", Image).LANCZOS)
                buffer = BytesIO()
                frame.save(buffer, format="JPEG", quality=82, optimize=True)
                encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": encoded}})
    except Exception as exc:
        return {"decision": "REVIEW", "reason": f"Could not prepare media for content review: {type(exc).__name__}: {exc}", "reviewed_at": now_iso()}

    for attempt in range(1, 3):
        api_key = _get_next_gemini_key()
        if not api_key:
            return {"decision": "REVIEW", "reason": "No Gemini API key is available for content review", "reviewed_at": now_iso()}
        proxy = _acquire_next_proxy()
        if not proxy:
            return {"decision": "REVIEW", "reason": "No proxy available for Gemini content review; direct requests are disabled", "reviewed_at": now_iso()}
        proxies = {"http": proxy, "https": proxy}
        model_name = YOUTUBE_CONTENT_REVIEW_MODEL
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 700, "responseMimeType": "application/json"},
        }
        try:
            response = requests.post(
                f"{url}?key={api_key}",
                headers={"Content-Type": "application/json"},
                json=payload, timeout=50, proxies=proxies,
            )
            if response.status_code == 200:
                body = response.json()
                candidates = body.get("candidates") or []
                if not candidates:
                    return {"decision": "REVIEW", "reason": "Gemini returned no review candidate; content was not conclusively screened, manual inspection recommended", "reviewed_at": now_iso(), "model": model_name}
                text_parts = candidates[0].get("content", {}).get("parts", [])
                response_text = "\n".join(str(item.get("text", "")) for item in text_parts if item.get("text"))
                match = re.search(r"\{.*\}", response_text, re.DOTALL)
                if not match:
                    return {"decision": "REVIEW", "reason": "Gemini review response was not valid JSON; content was not conclusively screened, manual inspection recommended", "reviewed_at": now_iso(), "model": model_name}
                try:
                    parsed = json.loads(match.group(0))
                except json.JSONDecodeError:
                    return {"decision": "REVIEW", "reason": "Gemini review JSON could not be parsed; content was not conclusively screened, manual inspection recommended", "reviewed_at": now_iso(), "model": model_name}
                decision = str(parsed.get("decision", "REVIEW")).upper().strip()
                if decision not in {"PASS", "REJECT", "REVIEW"}:
                    decision = "REVIEW"
                result = {
                    "decision": decision,
                    "reason": str(parsed.get("reason", "No reason supplied"))[:1000],
                    "flags": parsed.get("flags", []),
                    "reviewed_at": now_iso(),
                    "model": model_name,
                    "frames_reviewed": len(usable_paths),
                }
                log.info("[Content Safety] %s: %s (%s)", decision, ", ".join(p.name for p in usable_paths), result["reason"][:200])
                return result

            body_lower = response.text.lower()
            if response.status_code == 429:
                _kill_gemini_key(api_key, "content review rate limit 429", is_temporary=True, cooldown_minutes=5)
            elif response.status_code == 403:
                _kill_gemini_key(api_key, "content review 403", is_temporary=True, cooldown_minutes=15)
            else:
                log.warning("[Content Safety] Review API returned HTTP %s: %s", response.status_code, response.text[:300])
                if response.status_code in (400, 404) or "model" in body_lower and "not found" in body_lower:
                    return {"decision": "REVIEW", "reason": f"Content-review API error HTTP {response.status_code}; content was not conclusively screened, manual inspection recommended", "reviewed_at": now_iso(), "model": model_name}
        except requests.exceptions.RequestException as exc:
            if proxy:
                _put_proxy_on_cooldown(proxy, f"content review {type(exc).__name__}", duration_sec=90)
            log.warning("[Content Safety] Review request failed: %s", type(exc).__name__)
        except Exception as exc:
            log.warning("[Content Safety] Unexpected review error: %s", type(exc).__name__)
        finally:
            _release_proxy(proxy)
        time.sleep(0.8)

    return {"decision": "REVIEW", "reason": "Content-review service unavailable after retries; content was not screened, manual inspection recommended", "reviewed_at": now_iso(), "model": YOUTUBE_CONTENT_REVIEW_MODEL}


def review_image_with_cache(record: dict[str, Any], path: Path, context: str, cache_field: str = "content_safety_review") -> dict[str, Any]:
    """Reuse a PASS only when the exact same image bytes were previously reviewed."""
    digest = sha256_file(path)
    previous = record.get(cache_field, {})
    if isinstance(previous, dict) and previous.get("decision") == "PASS" and previous.get("sha256") == digest:
        return previous
    result = review_images_for_youtube([path], context=context, media_kind="image")
    result["sha256"] = digest
    record[cache_field] = result
    return result


def review_video_for_youtube(path: Path, context: str = "", temp_dir: Path | None = None) -> dict[str, Any]:
    """Review three representative frames from a generated intro clip."""
    if not ENABLE_YOUTUBE_CONTENT_SAFETY_REVIEW:
        return {"decision": "SKIPPED", "reason": "Content-safety review disabled in configuration", "reviewed_at": now_iso()}
    duration = get_duration(path)
    if duration is None or duration <= 0.1:
        return {"decision": "REVIEW", "reason": "Video duration unavailable; content cannot be screened", "reviewed_at": now_iso()}
    frame_root = ensure_directory(temp_dir or Path(tempfile.gettempdir()) / "flow_content_safety")
    sample_dir = Path(tempfile.mkdtemp(prefix="review_frames_", dir=str(frame_root)))
    frames: list[Path] = []
    try:
        for index, fraction in enumerate(YOUTUBE_CONTENT_REVIEW_VIDEO_FRAME_FRACTIONS, start=1):
            frame_path = sample_dir / f"frame_{index:02d}.jpg"
            timestamp = min(max(0.0, duration * fraction), max(0.0, duration - 0.05))
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{timestamp:.3f}", "-i", str(path), "-frames:v", "1", "-q:v", "3", str(frame_path)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=35,
            )
            if is_valid_image(frame_path, min_size=100):
                frames.append(frame_path)
        if len(frames) != len(YOUTUBE_CONTENT_REVIEW_VIDEO_FRAME_FRACTIONS):
            return {"decision": "REVIEW", "reason": "Could not extract all required intro-video review frames", "reviewed_at": now_iso()}
        return review_images_for_youtube(frames, context=context, media_kind="intro video keyframes")
    except Exception as exc:
        return {"decision": "REVIEW", "reason": f"Video frame review failed: {type(exc).__name__}: {exc}", "reviewed_at": now_iso()}
    finally:
        shutil.rmtree(sample_dir, ignore_errors=True)


# ============================================================================
# YOUTUBE MONETIZATION PREFLIGHT (AD SUITABILITY + CHANNEL ORIGINALITY)
# ============================================================================


def _script_shingles(text: str, size: int = 5) -> set[tuple[str, ...]]:
    """Build word shingles for a lightweight, local near-duplicate script check."""
    words = [w for w in re.findall(r"[^\W_]+", (text or "").lower(), flags=re.UNICODE) if len(w) > 2]
    if len(words) < size:
        return set()
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}


def find_similar_channel_scripts(project_folder: Path, current_text: str, max_files: int = 200) -> dict[str, Any]:
    """Detect near-identical scripts in sibling project folders; this is a warning, not a verdict."""
    current_tokens = re.findall(r"[^\W_]+", (current_text or "").lower(), flags=re.UNICODE)
    current_shingles = _script_shingles(current_text)
    if len(current_tokens) < 90 or not current_shingles:
        return {"status": "NOT_ENOUGH_TEXT", "closest_matches": []}

    matches: list[dict[str, Any]] = []
    inspected = 0
    try:
        siblings = sorted((p for p in project_folder.parent.iterdir() if p.is_dir()), key=lambda p: p.name.lower())
    except Exception:
        return {"status": "UNAVAILABLE", "closest_matches": []}

    for sibling in siblings:
        try:
            candidates = sorted(sibling.glob("*.txt"), key=lambda p: p.name.lower())
        except Exception:
            continue
        for candidate in candidates:
            if candidate.resolve() == (project_folder / candidate.name).resolve() and sibling.resolve() == project_folder.resolve():
                continue
            if inspected >= max_files:
                break
            inspected += 1
            try:
                other_text = candidate.read_text(encoding="utf-8", errors="ignore")
                other_tokens = re.findall(r"[^\W_]+", other_text.lower(), flags=re.UNICODE)
                if len(other_tokens) < 90:
                    continue
                other_shingles = _script_shingles(other_text)
                if not other_shingles:
                    continue
                similarity = len(current_shingles & other_shingles) / max(1, len(current_shingles | other_shingles))
                if similarity >= 0.30:
                    matches.append({
                        "file": str(candidate),
                        "jaccard_5word_shingles": round(similarity, 4),
                        "word_count": len(other_tokens),
                    })
            except Exception:
                continue
        if inspected >= max_files:
            break

    matches.sort(key=lambda item: item["jaccard_5word_shingles"], reverse=True)
    closest = matches[:5]
    highest = closest[0]["jaccard_5word_shingles"] if closest else 0.0
    status = "POSSIBLE_NEAR_DUPLICATE" if highest >= YOUTUBE_SCRIPT_SIMILARITY_WARNING_THRESHOLD else ("SIMILARITY_TO_REVIEW" if highest >= 0.60 else "NO_STRONG_MATCH_FOUND")
    return {"status": status, "threshold": YOUTUBE_SCRIPT_SIMILARITY_WARNING_THRESHOLD, "files_inspected": inspected, "closest_matches": closest}


def review_youtube_monetization_text(narration: str, title: str = "", mode: str = "script") -> dict[str, Any]:
    """Ask Gemini for an advisory policy-risk review of narration/title; not a YouTube approval."""
    if not ENABLE_YOUTUBE_MONETIZATION_PREFLIGHT:
        return {"decision": "SKIPPED", "reason": "Monetization preflight disabled in configuration", "recommendations": []}

    prompt = f"""You are a conservative YouTube pre-publication policy and advertiser-suitability reviewer.
Return ONLY valid JSON with these keys:
{{"decision":"PASS|REVIEW|HIGH_RISK","policy_areas":["..."],"reason":"...","recommendations":["..."],"safer_title_suggestion":"...","ai_disclosure_check":"..."}}

Review mode: {mode}
TITLE: {title[:500]}
NARRATION / CONTEXT (may be truncated):
{(narration or "")[:11000]}

Rules for your assessment:
- Separate three topics: (1) likely Community Guidelines/removal risk, (2) advertiser suitability / limited ads, and (3) channel-level YPP originality / inauthentic-content risk.
- A script-only review cannot judge unseen visuals, audio, copyright/licensing, factual accuracy, channel history, or the complete channel. Say so in the reason when relevant.
- Do NOT mark a video risky merely because it neutrally discusses illness, injury, crime, animal rescue, war, or another sensitive subject. Consider whether it is contextual, educational, documentary, non-graphic, and not sensationalized.
- HIGH_RISK only when the supplied wording clearly appears to glorify, encourage, or graphically sensationalize severe harm/animal abuse, explicit sexual content, hateful abuse, dangerous wrongdoing, or similar high-confidence policy concerns; or uses severe profanity/shocking terms as clickbait in the title.
- REVIEW for uncertainty or topics that may warrant human review for advertiser suitability; PASS only when no material concern is apparent from the supplied text.
- For a concerning title, suggest a factual, non-sensational alternative that preserves the actual subject. Do not invent claims.
- Check whether the story sounds generic, repetitive, mass-produced, misleading, or lacking original educational/commentary value. Do not claim this can be decided from one script alone.
- Photorealistic AI-generated scenes that depict realistic events which did not actually happen may require the creator to select YouTube Studio's AI-use disclosure during upload. Mention this as a checklist item, not as a reason to reject the video.
- Do not suggest metadata spoofing, deceptive labeling, keyword stuffing, or tiny edits meant to evade detection.
""".strip()

    raw = call_text_api(prompt)
    if not raw:
        return {
            "decision": "REVIEW",
            "policy_areas": ["review_service_unavailable"],
            "reason": "Gemini did not return a usable response; manual review is recommended.",
            "recommendations": ["Check the narration, title, thumbnail, generated visuals, and channel-level originality manually."],
            "safer_title_suggestion": "",
            "ai_disclosure_check": "Review YouTube Studio's AI-use setting if the video depicts realistic synthetic scenes that did not occur.",
        }

    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("No JSON object in model response")
        parsed = json.loads(match.group(0))
        decision = str(parsed.get("decision", "REVIEW")).upper().strip()
        if decision not in {"PASS", "REVIEW", "HIGH_RISK"}:
            decision = "REVIEW"
        return {
            "decision": decision,
            "policy_areas": parsed.get("policy_areas", []) if isinstance(parsed.get("policy_areas", []), list) else [],
            "reason": str(parsed.get("reason", "No reason supplied"))[:1800],
            "recommendations": parsed.get("recommendations", []) if isinstance(parsed.get("recommendations", []), list) else [],
            "safer_title_suggestion": str(parsed.get("safer_title_suggestion", ""))[:300],
            "ai_disclosure_check": str(parsed.get("ai_disclosure_check", "Review YouTube Studio's AI-use setting when relevant."))[:600],
            "reviewed_at": now_iso(),
        }
    except Exception as exc:
        return {
            "decision": "REVIEW",
            "policy_areas": ["review_response_unparseable"],
            "reason": f"Could not parse monetization review response ({type(exc).__name__}); manual review is recommended.",
            "recommendations": ["Review the text and title manually before publication."],
            "safer_title_suggestion": "",
            "ai_disclosure_check": "Review YouTube Studio's AI-use setting if the video depicts realistic synthetic scenes that did not occur.",
            "reviewed_at": now_iso(),
        }


def build_youtube_monetization_preflight(project_folder: Path, script_path: Path, text: str, title: str = "") -> dict[str, Any]:
    script_review = review_youtube_monetization_text(text, title=title, mode="script and title" if title else "script")
    similarity = find_similar_channel_scripts(project_folder, text)
    report = {
        "checked_at": now_iso(),
        "pipeline_version": PIPELINE_VERSION,
        "notice": "Advisory preflight only. It is not an official YouTube decision or guarantee of monetization.",
        "source_script": str(script_path),
        "script_review": script_review,
        "channel_script_similarity": similarity,
        "publication_checklist": [
            "Confirm that the narration and visuals add original story, research, explanation, or perspective rather than only repeating a template.",
            "Check that all music, images, clips, voices, and other assets are owned, licensed, or otherwise permitted.",
            "Review the title, thumbnail, description, and first seconds for misleading or sensationalized presentation.",
            "If photorealistic AI-generated scenes depict realistic events that did not actually occur, review the AI-use disclosure in YouTube Studio.",
        ],
    }
    if similarity.get("status") == "POSSIBLE_NEAR_DUPLICATE":
        report["originality_warning"] = "A script in a sibling project is highly similar. Confirm this episode has a genuinely distinct narrative, facts, commentary, and viewer value. Small visual/audio changes alone do not make repetitive content original."
    return report


def save_youtube_monetization_preflight(project_folder: Path, report: dict[str, Any]) -> Path:
    report_path = project_folder / "youtube_monetization_preflight.json"
    atomic_write_json(report_path, report)
    return report_path


# ============================================================================
# FLOW ACCOUNT MANAGER
# ============================================================================


def unique_enabled_flow_profiles(
    profiles: list[dict[str, Any]],
    *,
    warn_duplicates: bool = True,
) -> list[dict[str, Any]]:
    """Keep one entry per real Chrome user-data directory and CDP port.

    Different names/ports do not create independent Google sessions when the same
    Chrome user-data directory is reused. Keeping duplicates would cause Chrome
    profile-lock conflicts and misleading quota counters.
    """
    unique: list[dict[str, Any]] = []
    by_user_dir: dict[str, dict[str, Any]] = {}
    by_port: dict[int, dict[str, Any]] = {}
    by_name: set[str] = set()

    for raw_profile in profiles:
        if not isinstance(raw_profile, dict) or not raw_profile.get("enabled", True):
            continue
        profile = dict(raw_profile)
        name = str(profile.get("name", "")).strip()
        user_dir_raw = str(profile.get("user_data_dir", "")).strip()
        try:
            port = int(profile.get("port"))
        except (TypeError, ValueError):
            port = -1
        if not name or not user_dir_raw or port <= 0:
            if warn_duplicates:
                log.warning(
                    "[Flow Profiles] Игнорирую некорректную запись профиля: name=%r user_data_dir=%r port=%r",
                    name, user_dir_raw, profile.get("port"),
                )
            continue

        expanded = os.path.expandvars(os.path.expanduser(user_dir_raw)).replace("/", "\\")
        user_dir_key = ntpath.normcase(ntpath.normpath(expanded)).rstrip("\\")
        if name.casefold() in by_name:
            if warn_duplicates:
                log.warning("[Flow Profiles] Повторное имя профиля %s проигнорировано.", name)
            continue
        existing_dir = by_user_dir.get(user_dir_key)
        if existing_dir is not None:
            if warn_duplicates:
                capability_note = ""
                if bool(existing_dir.get("supports_video", True)) != bool(profile.get("supports_video", True)):
                    capability_note = " Capability flags differ, but the same Chrome session cannot be split into two accounts."
                log.warning(
                    "[Flow Profiles] %s пропущен: user_data_dir совпадает с %s (%s). Разные имя и порт не создают отдельный аккаунт.%s "
                    "Чтобы использовать отдельный аккаунт, назначь ему отдельную папку C:\\FlowAccounts\\%s и войди в Flow в этом профиле.",
                    name, existing_dir['name'], user_dir_raw, capability_note, name,
                )
            continue
        existing_port = by_port.get(port)
        if existing_port is not None:
            if warn_duplicates:
                log.warning(
                    "[Flow Profiles] %s пропущен: CDP port %s уже назначен профилю %s.",
                    name, port, existing_port["name"],
                )
            continue

        unique.append(profile)
        by_name.add(name.casefold())
        by_user_dir[user_dir_key] = profile
        by_port[port] = profile

    if warn_duplicates and len(unique) != sum(
        1 for p in profiles if isinstance(p, dict) and p.get("enabled", True)
    ):
        configured = sum(1 for p in profiles if isinstance(p, dict) and p.get("enabled", True))
        log.warning(
            "[Flow Profiles] Конфигурация содержит %s активных записей, но только %s уникальных Chrome-профилей; "
            "ротация будет идти по уникальным профилям.", configured, len(unique),
        )
    return unique


@dataclass
class FlowAccountState:
    name: str
    port: int
    status: str = "READY"
    failures: int = 0
    paused_until: float = 0.0
    last_error: str = ""
    jobs_done: int = 0
    images_done: int = 0
    videos_done: int = 0
    session_images: int = 0
    session_videos: int = 0
    quota_detected_at: float = 0.0
    quota_next_probe_at: float = 0.0


class FlowAccountManager:
    """Round-robin Flow browser/session manager.

    Session/cycle policy:
      - keep using the active browser profile after each successful media item;
      - switch only when that profile reaches the quota for the requested media type;
      - image and video quotas are independent: 20 images AND 10 videos;
      - reaching the video quota does not consume the image quota, or vice versa;
      - The per-profile supports_video flag controls video eligibility independently
        from image eligibility; the configured FLOW_PROFILES order is preserved.
    """

    def __init__(self, profiles: list[dict[str, Any]], startup_profile: str | None = None):
        self.profiles = unique_enabled_flow_profiles(profiles, warn_duplicates=True)
        if not self.profiles:
            raise RuntimeError("Не включён ни один Flow profile в FLOW_PROFILES.")
        self.startup_profile = str(startup_profile).strip() if startup_profile else None
        self.startup_profile_pending = bool(self.startup_profile)
        # Per-round usage survives Chrome reopen and Python restart. It prevents a
        # profile that just used its 20-image/10-video quota from being reselected
        # merely because closing Chrome reset the per-opening counter.
        self.round_number = 1
        self.round_usage = {
            p["name"]: {"images": 0, "videos": 0, "exhausted": False}
            for p in self.profiles
        }

        self.states = {
            p["name"]: FlowAccountState(name=p["name"], port=int(p["port"]))
            for p in self.profiles
        }
        self.index = 0
        self.lock = asyncio.Lock()
        self.active_profile_name: str | None = None
        self.active_controller = None
        self.current_project_state = None
        self.generation_attempts_by_target: dict[str, int] = {}
        self.usage_state_path = FLOW_PROFILE_USAGE_STATE_FILE
        self.browser_instance_ids: dict[str, str | None] = {}
        self.last_project_stop_reason: str | None = None
        self.last_quota_kind: str | None = None
        self._load_persistent_session_usage()

    @staticmethod
    def _browser_instance_id(profile: dict[str, Any]) -> str | None:
        """Stable for one running Chrome process; changes after Chrome is reopened."""
        pids = find_chrome_process_for_port(int(profile["port"]))
        if not pids:
            return None
        tokens = []
        for pid in pids:
            try:
                proc = psutil.Process(pid)
                tokens.append(f"{pid}@{proc.create_time():.3f}")
            except Exception:
                continue
        return "|".join(sorted(tokens)) or None

    def _read_usage_document(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.usage_state_path.read_text(encoding="utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _load_persistent_session_usage(self) -> None:
        document = self._read_usage_document()
        records = document.get("profiles", {}) if isinstance(document.get("profiles", {}), dict) else {}
        try:
            self.round_number = max(1, int(document.get("round_number", 1)))
        except Exception:
            self.round_number = 1
        changed = False
        for profile in self.profiles:
            name = profile["name"]
            state = self.states[name]
            current_id = self._browser_instance_id(profile)
            record = records.get(name, {}) if isinstance(records.get(name, {}), dict) else {}
            saved_id = record.get("browser_instance_id")
            # Flow Agent quotas survive Python restarts. Recheck them immediately
            # when the profile is next needed; never clear the flag on a timer alone.
            try:
                saved_quota_time = float(record.get("quota_detected_at", 0.0) or 0.0)
            except (TypeError, ValueError):
                saved_quota_time = 0.0
            if str(record.get("status", "")).upper() == "QUOTA_LIMIT" or saved_quota_time > 0:
                state.status = "QUOTA_LIMIT"
                state.quota_detected_at = saved_quota_time or time.time()
                state.quota_next_probe_at = 0.0  # immediately eligible for a read-only probe this run
                state.last_error = str(record.get("last_error", "Flow Agent quota detected"))
            saved_round_images = max(0, int(record.get("round_images", 0) or 0))
            saved_round_videos = max(0, int(record.get("round_videos", 0) or 0))
            # Older builds set round_exhausted as soon as EITHER quota was hit.
            # Migrate that flag: a profile is globally exhausted only after BOTH
            # independent media quotas have been consumed.
            self.round_usage[name] = {
                "images": saved_round_images,
                "videos": saved_round_videos,
                "exhausted": (
                    saved_round_images >= FLOW_SESSION_IMAGE_LIMIT
                    and saved_round_videos >= FLOW_SESSION_VIDEO_LIMIT
                ),
            }
            self.browser_instance_ids[name] = current_id
            if current_id is not None and saved_id == current_id:
                state.session_images = max(0, int(record.get("session_images", 0) or 0))
                state.session_videos = max(0, int(record.get("session_videos", 0) or 0))
                log.info(
                    "[Flow Account] Restored counters for live browser %s: images=%s/%s videos=%s/%s",
                    name, state.session_images, FLOW_SESSION_IMAGE_LIMIT,
                    state.session_videos, FLOW_SESSION_VIDEO_LIMIT,
                )
            else:
                # If Chrome isn't running, or its process identity changed, this is
                # a new browser opening. The per-opening counters safely start at zero.
                state.session_images = 0
                state.session_videos = 0
                changed = True
        if changed or not self.usage_state_path.exists():
            self._save_persistent_session_usage()

    def _save_persistent_session_usage(self) -> None:
        try:
            prior = self._read_usage_document()
            records = prior.get("profiles", {}) if isinstance(prior.get("profiles", {}), dict) else {}
            for profile in self.profiles:
                name = profile["name"]
                state = self.states[name]
                records[name] = {
                    "browser_instance_id": self.browser_instance_ids.get(name),
                    "session_images": int(state.session_images),
                    "session_videos": int(state.session_videos),
                    "round_images": int(self.round_usage.get(name, {}).get("images", 0)),
                    "round_videos": int(self.round_usage.get(name, {}).get("videos", 0)),
                    "round_exhausted": bool(self.round_usage.get(name, {}).get("exhausted", False)),
                    "status": "QUOTA_LIMIT" if state.status == "QUOTA_LIMIT" or state.quota_detected_at > 0 else "READY",
                    "quota_detected_at": float(state.quota_detected_at or 0.0),
                    "quota_next_probe_at": float(state.quota_next_probe_at or 0.0),
                    "last_error": state.last_error[:500] if state.status == "QUOTA_LIMIT" else "",
                    "updated_at": now_iso(),
                }
            atomic_write_json(self.usage_state_path, {"schema": 2, "round_number": self.round_number, "profiles": records, "updated_at": now_iso()})
        except Exception as exc:
            log.warning("[Flow Account] Не удалось сохранить счётчики сессии: %s", exc)

    def refresh_browser_session_identity(self, profile: dict[str, Any]) -> None:
        name = profile["name"]
        current_id = self._browser_instance_id(profile)
        prior_id = self.browser_instance_ids.get(name)
        if current_id != prior_id:
            state = self.states[name]
            state.session_images = 0
            state.session_videos = 0
            self.browser_instance_ids[name] = current_id
            log.info(
                "[Flow Account] Browser process changed for %s; live-session counters reset to 0/0; persistent media quotas are preserved.",
                name,
            )
            self._save_persistent_session_usage()

    @staticmethod
    def is_pro(profile: dict[str, Any]) -> bool:
        # Prefer an explicit capability flag. Legacy/custom entries without it
        # retain the previous name-based behaviour for backwards compatibility.
        if "supports_video" in profile:
            return bool(profile.get("supports_video"))
        return profile["name"].lower().startswith("pro")

    def profile_by_name(self, name: str) -> dict[str, Any]:
        for profile in self.profiles:
            if profile["name"] == name:
                return profile
        raise KeyError(name)

    def can_do(self, profile: dict[str, Any], kind: str) -> bool:
        return kind == "image" or self.is_pro(profile)

    def media_quota_limit(self, kind: str) -> int:
        if kind == "image":
            return FLOW_SESSION_IMAGE_LIMIT
        if kind == "video":
            return FLOW_SESSION_VIDEO_LIMIT
        raise ValueError(f"Unsupported Flow media kind: {kind}")

    def media_quota_used(self, profile_name: str, kind: str) -> int:
        """Persistent per-round usage, reconciled with the live browser counters."""
        usage = self.round_usage.get(profile_name, {})
        state = self.states[profile_name]
        if kind == "image":
            return max(int(usage.get("images", 0)), int(state.session_images))
        if kind == "video":
            return max(int(usage.get("videos", 0)), int(state.session_videos))
        raise ValueError(f"Unsupported Flow media kind: {kind}")

    def media_quota_reached(self, profile_name: str, kind: str) -> bool:
        return self.media_quota_used(profile_name, kind) >= self.media_quota_limit(kind)

    def session_limit_reached(self, profile_name: str) -> bool:
        """True only after BOTH media-specific limits have been reached."""
        return (
            self.media_quota_reached(profile_name, "image")
            and self.media_quota_reached(profile_name, "video")
        )

    def available_profiles(self, kind: str | None = None) -> list[dict[str, Any]]:
        result=[]
        now=time.time()
        for profile in self.profiles:
            state=self.states[profile["name"]]
            if state.paused_until > now:
                continue
            if state.status not in {"READY", "WORKING"}:
                continue
            if kind and not self.can_do(profile, kind):
                continue
            # Each media type consumes its own allowance. A profile that has
            # used 10 videos can still contribute up to 20 images, and vice versa.
            if kind and self.media_quota_reached(profile["name"], kind):
                continue
            if not kind and self.session_limit_reached(profile["name"]):
                continue
            result.append(profile)
        return result

    async def choose_profile(self, kind: str, preferred: str | None = None) -> dict[str, Any]:
        async with self.lock:
            now = time.time()
            # Recover cooldown states after their timer has expired.
            for configured_profile in self.profiles:
                self.refresh_browser_session_identity(configured_profile)
                state = self.states[configured_profile["name"]]
                if state.status == "PAUSED" and state.paused_until <= now:
                    state.status = "READY"
                    state.last_error = ""

            # Keep the currently active browser until it reaches its limit.
            if self.active_profile_name:
                active = self.profile_by_name(self.active_profile_name)
                if (
                    self.can_do(active, kind)
                    and not self.media_quota_reached(self.active_profile_name, kind)
                    and self.states[self.active_profile_name].paused_until <= time.time()
                    and self.states[self.active_profile_name].status in {"READY", "WORKING"}
                ):
                    self.states[self.active_profile_name].status = "WORKING"
                    return active

            candidates = self.available_profiles(kind)
            if not candidates:
                # Reset a round only after every currently eligible account has
                # consumed its cycle quota. Never reset just because one profile's
                # Chrome process restarted or the saved scene prefers that profile.
                eligible = [
                    p for p in self.profiles
                    if self.can_do(p, kind)
                    and self.states[p["name"]].paused_until <= now
                    and self.states[p["name"]].status in {"READY", "WORKING"}
                ]
                def quota_consumed(profile: dict[str, Any]) -> bool:
                    return self.media_quota_reached(profile["name"], kind)

                # Roll over only the requested media type. For example, after all
                # video-capable accounts have used 10 videos, restart the video
                # allowance without erasing their still-independent image usage.
                if eligible and all(quota_consumed(p) for p in eligible):
                    self.round_number += 1
                    for profile in eligible:
                        name = profile["name"]
                        state = self.states[name]
                        usage = self.round_usage.setdefault(
                            name, {"images": 0, "videos": 0, "exhausted": False}
                        )
                        usage[kind + "s"] = 0
                        usage["exhausted"] = (
                            int(usage.get("images", 0)) >= FLOW_SESSION_IMAGE_LIMIT
                            and int(usage.get("videos", 0)) >= FLOW_SESSION_VIDEO_LIMIT
                        )
                        # Close browser sessions before reusing this media allowance.
                        try:
                            active_controller_profile = getattr(getattr(self.active_controller, "profile", None), "get", lambda *_: None)("name")
                            if self.active_controller is not None and active_controller_profile == name:
                                await self.active_controller.disconnect(close_chrome=True)
                                self.active_controller = None
                                self.active_profile_name = None
                            else:
                                await asyncio.to_thread(close_chrome_profile_process, profile)
                        except Exception as exc:
                            log.warning("[Flow Round] Не удалось штатно закрыть профиль %s при завершении круга: %s", name, exc)
                        state.session_images = 0
                        state.session_videos = 0
                        state.status = "READY"
                        state.paused_until = 0.0
                        self.browser_instance_ids[name] = None
                    log.info(
                        "🔄 [Flow] Квота типа '%s' исчерпана всеми доступными профилями; "
                        "обнулена только эта квота, остальные счётчики сохранены. Новый круг=%s.",
                        kind, self.round_number,
                    )
                    self._save_persistent_session_usage()
                    candidates = self.available_profiles(kind)
                if not candidates:
                    quota_blocked = [
                        p["name"] for p in self.profiles
                        if self.can_do(p, kind) and self.states[p["name"]].status == "QUOTA_LIMIT"
                    ]
                    if quota_blocked:
                        raise FlowQuotaError(
                            f"All eligible Flow profiles currently have an Agent quota banner ({', '.join(quota_blocked)})."
                        )
                    raise RuntimeError(
                        f"Нет доступных Flow-профилей для {kind}: проверь лимиты/паузы. "
                        f"Круг={self.round_number}; exhausted=" + ",".join(
                            name for name, usage in self.round_usage.items() if usage.get("exhausted")
                        )
                    )

            chosen = None
            if self.startup_profile_pending:
                # The GUI-selected profile may perform the first task, but it must NOT
                # reorder FLOW_PROFILES. After it, continue at its actual successor in
                # the configured list (e.g. pro_19 -> pro_20 -> pro_21).
                startup = next(
                    (p for p in self.profiles if str(p["name"]).strip() == self.startup_profile),
                    None,
                )
                self.startup_profile_pending = False
                if startup is not None:
                    startup_index = self.profiles.index(startup)
                    self.index = (startup_index + 1) % len(self.profiles)
                    if startup in candidates:
                        chosen = startup
                        log.info(
                            "[Flow] GUI startup profile=%s selected first; configured order preserved; "
                            "next round-robin profile will be %s.",
                            startup["name"], self.profiles[self.index]["name"],
                        )
                    else:
                        state = self.states.get(self.startup_profile)
                        if state is None:
                            reason = "profile is absent from the unique enabled list (disabled or duplicate user_data_dir/port)"
                        elif state.status not in {"READY", "WORKING"}:
                            reason = f"status={state.status}"
                        elif state.paused_until > now:
                            reason = f"cooldown for {max(0, int(state.paused_until - now))}s"
                        elif not self.can_do(startup, kind):
                            reason = f"supports_video={startup.get('supports_video')} does not allow {kind}"
                        elif self.media_quota_reached(self.startup_profile, kind):
                            reason = f"profile has reached its {kind} quota ({self.media_quota_used(self.startup_profile, kind)}/{self.media_quota_limit(kind)})"
                        else:
                            reason = "profile is not currently eligible; see its quota/cooldown diagnostics"
                        log.warning(
                            "[Flow] GUI-selected startup profile %s cannot be used first for %s (%s); "
                            "continuing from its configured successor %s.",
                            self.startup_profile, kind, reason, self.profiles[self.index]["name"],
                        )
                else:
                    log.warning(
                        "[Flow] GUI-selected startup profile %s is not in the enabled configured order; "
                        "starting at %s.",
                        self.startup_profile, self.profiles[self.index]["name"],
                    )

            # Per-scene saved account labels are legacy hints only. They must not
            # override circular order after a profile's round quota is consumed.
            if chosen is None:
                n = len(self.profiles)
                for step in range(n):
                    pos = (self.index + step) % n
                    candidate = self.profiles[pos]
                    if candidate in candidates:
                        chosen = candidate
                        self.index = (pos + 1) % n
                        break
                if chosen is None:
                    chosen = candidates[0]
                    self.index = (self.profiles.index(chosen) + 1) % len(self.profiles)
                if preferred and preferred != chosen["name"]:
                    log.info(
                        "[Flow] Ignoring saved account hint %s for this target; round-robin selects %s.",
                        preferred, chosen["name"],
                    )

            self.active_profile_name = chosen["name"]
            self.states[chosen["name"]].status = "WORKING"
            return chosen

    def mark_done(self, profile_name: str, kind: str) -> bool:
        state=self.states[profile_name]
        state.status="READY"
        state.failures=0
        state.last_error=""
        state.jobs_done += 1
        usage = self.round_usage.setdefault(profile_name, {"images": 0, "videos": 0, "exhausted": False})
        if kind == "image":
            state.images_done += 1
            state.session_images += 1
            usage["images"] = int(usage.get("images", 0)) + 1
        else:
            state.videos_done += 1
            state.session_videos += 1
            usage["videos"] = int(usage.get("videos", 0)) + 1
        usage["exhausted"] = (
            int(usage.get("images", 0)) >= FLOW_SESSION_IMAGE_LIMIT
            and int(usage.get("videos", 0)) >= FLOW_SESSION_VIDEO_LIMIT
        )
        used_for_kind = self.media_quota_used(profile_name, kind)
        limit_for_kind = self.media_quota_limit(kind)
        quota_reached = used_for_kind >= limit_for_kind
        if quota_reached:
            log.info(
                "[Flow Quota] Профиль %s исчерпал квоту '%s': %s/%s; "
                "квота другого типа медиа остаётся доступной.",
                profile_name, kind, used_for_kind, limit_for_kind,
            )
        self._save_persistent_session_usage()
        return quota_reached

    def mark_technical_error(self, profile_name: str, error: str) -> None:
        state=self.states[profile_name]
        state.status="READY"
        state.failures += 1
        state.last_error=error
        log.warning(
            "[Flow Account] %s technical failure #%s: %s",
            profile_name, state.failures, error[:180],
        )
        if state.failures >= 3:
            state.paused_until=time.time()+180
            state.status="PAUSED"

    def mark_policy_block(self, profile_name: str, reason: str) -> None:
        state=self.states[profile_name]
        state.status="CHALLENGE"
        state.last_error=reason
        state.paused_until=time.time()+3600
        state.quota_detected_at = 0.0
        state.quota_next_probe_at = 0.0
        self._save_persistent_session_usage()
        log.warning("[Flow Account] %s на паузе из-за проверки: %s", profile_name, reason)

    def mark_suspicious_activity(self, profile_name: str, reason: str) -> None:
        """Exclude a suspicious-activity profile for this Python run only.

        _save_persistent_session_usage intentionally serializes this status as READY;
        this quarantine is in-memory only and resets on the next script launch.
        """
        state = self.states[profile_name]
        state.status = "SUSPICIOUS_ACTIVITY"
        state.last_error = str(reason)[:500]
        state.paused_until = 0.0
        state.quota_detected_at = 0.0
        state.quota_next_probe_at = 0.0
        self._save_persistent_session_usage()
        log.warning(
            "[Flow Account] %s отключён до следующего запуска скрипта из-за сообщения о подозрительной активности. "
            "Профиль не будет повторно выбран в этом запуске; продолжаю текущий проект на других профилях. Причина: %s",
            profile_name, str(reason)[:220],
        )

    def mark_quota(self, profile_name: str, reason: str) -> None:
        state=self.states[profile_name]
        if state.status != "QUOTA_LIMIT" or not state.quota_detected_at:
            state.quota_detected_at = time.time()
        state.status="QUOTA_LIMIT"
        state.last_error=reason
        state.paused_until=0.0  # quota expiry is detected by checking Flow's own banner, not a guessed cooldown
        state.quota_next_probe_at=time.time()+FLOW_QUOTA_RECHECK_INTERVAL_SECONDS
        self._save_persistent_session_usage()
        log.warning(
            "[Flow Account] %s исключён из генерации из-за лимита Flow Agent; следующая проверка сброса лимита примерно через %s мин.",
            profile_name, FLOW_QUOTA_RECHECK_INTERVAL_SECONDS // 60,
        )

    async def recheck_quota_profiles(self, playwright, force: bool = False) -> list[str]:
        """Read-only probe quota-limited browser profiles; never submit a prompt."""
        now = time.time()
        due = [
            p for p in self.profiles
            if self.states[p["name"]].status == "QUOTA_LIMIT"
            and (force or self.states[p["name"]].quota_next_probe_at <= now)
        ]
        recovered: list[str] = []
        for profile in due:
            name = profile["name"]
            state = self.states[name]
            controller = FlowController(playwright, self)
            log.info("[Flow Quota Check] Проверяю, снял ли Flow лимит с %s (без отправки промпта).", name)
            try:
                await controller.connect(profile)
                # A freshly opened Flow page can briefly be blank or only partially hydrated.
                # Do not declare a quota cleared merely because its banner was absent on the
                # first DOM snapshot. Recheck the banner repeatedly and require the Agent editor
                # to stay visible for three consecutive polls before releasing this profile.
                ready_samples = 0
                ready_deadline = time.monotonic() + 12.0
                while time.monotonic() < ready_deadline:
                    await controller.check_for_block_or_quota()
                    editor_ready = await controller._agent_prompt_editor_visible()
                    if editor_ready:
                        ready_samples += 1
                        if ready_samples >= 3:
                            break
                    else:
                        ready_samples = 0
                    await asyncio.sleep(1.0)
                if ready_samples < 3:
                    raise FlowTechnicalError(
                        "Flow не подтвердил готовность поля Agent за 12 секунд; отсутствие баннера "
                        "на неполностью загруженной странице не считается снятием квоты."
                    )
                log.info(
                    "[Flow Quota Check] %s: страница Flow загружена, поле Agent стабильно видно (3 проверки); квота отсутствует.",
                    name,
                )
                self.refresh_browser_session_identity(profile)
            except FlowQuotaError as exc:
                self.mark_quota(name, str(exc))
                log.info("[Flow Quota Check] %s: лимит всё ещё действует; профиль остаётся выключенным.", name)
            except FlowSuspiciousActivityError as exc:
                self.mark_suspicious_activity(name, str(exc))
            except FlowPolicyBlockError as exc:
                self.mark_policy_block(name, str(exc))
                log.warning("[Flow Quota Check] %s требует проверки/подтверждения; не использую его для генерации.", name)
            except Exception as exc:
                # A network/CDP failure is not evidence that the quota cleared.
                state.status = "QUOTA_LIMIT"
                state.quota_next_probe_at = time.time() + FLOW_QUOTA_RECHECK_ERROR_SECONDS
                state.last_error = f"Не удалось перепроверить квоту: {exc}"[:500]
                self._save_persistent_session_usage()
                log.warning(
                    "[Flow Quota Check] Не удалось проверить %s (%s); оставляю заблокированным и повторю позже.",
                    name, str(exc)[:180],
                )
            else:
                state.status = "READY"
                state.paused_until = 0.0
                state.quota_detected_at = 0.0
                state.quota_next_probe_at = 0.0
                state.last_error = ""
                self._save_persistent_session_usage()
                recovered.append(name)
                log.info("✅ [Flow Quota Check] Flow больше не показывает лимит для %s; профиль снова доступен.", name)
            finally:
                try:
                    await controller.disconnect(close_chrome=True)
                except Exception:
                    pass
                # connect() can fail before assigning controller.profile even if it
                # launched Chrome; ensure the quarantined browser is closed either way.
                try:
                    await asyncio.to_thread(close_chrome_profile_process, profile)
                except Exception:
                    pass
        return recovered

    async def wait_for_quota_reset(self, playwright, kind: str) -> bool:
        """Keep the same project active until at least one required profile recovers."""
        last_wait_log = 0.0
        log.warning(
            "⏸️ [Flow Quota Wait] Все профили для текущего задания исчерпали лимит или недоступны. "
            "Не переключаю проект; периодически проверяю баннеры Flow, пока хотя бы один профиль не восстановится."
        )
        while True:
            due_or_released = await self.recheck_quota_profiles(playwright)
            if any(self.can_do(self.profile_by_name(name), kind) for name in due_or_released):
                return True

            quota_profiles = [
                p for p in self.profiles
                if self.can_do(p, kind) and self.states[p["name"]].status == "QUOTA_LIMIT"
            ]
            now = time.time()
            if not quota_profiles:
                # A quota may have cleared but the account can still need a normal
                # session-round rollover. Let choose_profile handle that reset.
                eligible = [
                    p for p in self.profiles
                    if self.can_do(p, kind)
                    and self.states[p["name"]].status in {"READY", "WORKING"}
                    and self.states[p["name"]].paused_until <= now
                ]
                if eligible:
                    return True
                # Do not silently advance the project queue after a quota stop.
                # If all browsers now need manual intervention, keep the same project
                # parked here; Ctrl+C remains the explicit way to stop the run.
                next_probe = now + 60.0
            else:
                next_probe = min(
                    (self.states[p["name"]].quota_next_probe_at or now for p in quota_profiles),
                    default=now,
                )
            if now - last_wait_log >= FLOW_QUOTA_WAIT_LOG_INTERVAL_SECONDS:
                minutes = max(0, int((next_probe - now) / 60))
                log.warning(
                    "[Flow Quota Wait] Проект остаётся в работе и не переключается на другую папку. "
                    "Следующая проверка/ожидание примерно %s мин.; Ctrl+C остановит запуск, checkpoint сохранится.",
                    minutes,
                )
                last_wait_log = now
            await asyncio.sleep(max(1.0, min(60.0, next_probe - now if next_probe > now else 1.0)))

    def snapshot(self) -> dict[str, Any]:
        return {name: asdict(state) for name,state in self.states.items()}


# ============================================================================
# WINDOWS / CHROME CDP
# ============================================================================


def cdp_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version",
            timeout=2,
        ) as response:
            return response.status == 200
    except Exception:
        return False


def find_chrome_process_for_port(port: int) -> list[int]:
    pids = []
    marker = f"--remote-debugging-port={port}"
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if any(marker in str(part) for part in cmdline):
                pids.append(int(proc.info["pid"]))
        except Exception:
            pass
    return pids


def find_chrome_process_for_profile(user_data_dir: str | Path) -> list[int]:
    target = str(Path(user_data_dir)).replace("/", "\\").rstrip("\\").lower()
    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            joined = " ".join(str(part) for part in cmdline).replace("/", "\\").lower()
            if target and target in joined:
                pids.append(int(proc.info["pid"]))
        except Exception:
            pass
    return pids


def close_chrome_profile_process(profile: dict[str, Any], timeout: float = 18.0) -> None:
    """Close this dedicated Chrome profile gracefully to avoid crash-restore prompts."""
    port = int(profile["port"])
    root_pids = set(find_chrome_process_for_port(port))
    if not root_pids:
        root_pids.update(find_chrome_process_for_profile(profile["user_data_dir"]))
    if not root_pids:
        return

    # First ask visible Chrome windows to close normally (WM_CLOSE). Terminating
    # Chrome's process tree directly marks the session as crashed on next start.
    if os.name == "nt":
        try:
            user32 = ctypes.windll.user32
            WM_CLOSE = 0x0010
            tree_pids = set(root_pids)
            for pid in list(root_pids):
                try:
                    tree_pids.update(child.pid for child in psutil.Process(pid).children(recursive=True))
                except Exception:
                    pass

            handles = []
            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            def callback(hwnd, lparam):
                try:
                    pid = ctypes.c_ulong()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if int(pid.value) in tree_pids and user32.IsWindowVisible(hwnd):
                        handles.append(hwnd)
                except Exception:
                    pass
                return True

            user32.EnumWindows(EnumWindowsProc(callback), 0)
            for hwnd in handles:
                try:
                    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                except Exception:
                    pass
        except Exception as e:
            log.warning("[Chrome] Не удалось отправить WM_CLOSE для %s: %s", profile["name"], e)

    deadline = time.time() + timeout
    while time.time() < deadline:
        roots_alive = [pid for pid in root_pids if psutil.pid_exists(pid)]
        if not roots_alive and not cdp_alive(port):
            log.info("🧹 [Chrome] Профиль %s штатно закрыт", profile["name"])
            return
        time.sleep(0.25)

    # Fallback only if a graceful window close did not work.
    log.warning(
        "[Chrome] Профиль %s не закрылся штатно за %.0fs; применяем аварийное завершение",
        profile["name"], timeout,
    )
    processes = []
    for pid in root_pids:
        try:
            proc = psutil.Process(pid)
            processes.append(proc)
            processes.extend(proc.children(recursive=True))
        except Exception:
            pass
    unique = {proc.pid: proc for proc in processes}
    for proc in unique.values():
        try:
            proc.terminate()
        except Exception:
            pass
    _, alive = psutil.wait_procs(list(unique.values()), timeout=3.0)
    for proc in alive:
        try:
            proc.kill()
        except Exception:
            pass
    log.info("🧹 [Chrome] Профиль %s остановлен", profile["name"])

def _calculate_flow_window_position(work_area: tuple[int, int, int, int]) -> tuple[int, int]:
    """Calculate the Chrome position while leaving its draggable top strip visible."""
    left, top, right, bottom = (int(value) for value in work_area)
    visible_width = max(80, min(int(FLOW_WINDOW_VISIBLE_GRAB_WIDTH), FLOW_WINDOW_WIDTH))
    visible_height = max(36, min(int(FLOW_WINDOW_VISIBLE_GRAB_HEIGHT), FLOW_WINDOW_HEIGHT))
    return right - visible_width, bottom - visible_height


def get_flow_window_position() -> tuple[int, int]:
    """Return configured Chrome coordinates; on Windows use work area without taskbar."""
    if str(FLOW_WINDOW_OFFSCREEN_MODE).strip().casefold() != "bottom_right" or os.name != "nt":
        return int(FLOW_WINDOW_X), int(FLOW_WINDOW_Y)

    try:
        class _WinRect(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        rect = _WinRect()
        # SPI_GETWORKAREA = 0x0030; excludes the taskbar so the exposed strip
        # remains clickable instead of being covered by the taskbar.
        ok = ctypes.windll.user32.SystemParametersInfoW(
            0x0030, 0, ctypes.byref(rect), 0
        )
        if ok:
            work_area = (rect.left, rect.top, rect.right, rect.bottom)
        else:
            user32 = ctypes.windll.user32
            work_area = (0, 0, int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))
        return _calculate_flow_window_position(work_area)
    except Exception as exc:
        log.warning("[Chrome] Не удалось вычислить скрытую позицию окна; использую обычную: %s", exc)
        return int(FLOW_WINDOW_X), int(FLOW_WINDOW_Y)


def configure_chrome_suppress_prompts_policy() -> bool:
    """Disable Chrome default-browser checks and first-run promotional screens for this Windows user.

    Chrome's enterprise policies are more reliable than command-line switches for
    the welcome/promotional surface that can ask users to make Chrome the default.
    This writes HKCU only (no administrator rights required) and intentionally
    persists so every Flow profile launched by this pipeline inherits the policy.
    """
    if os.name != "nt":
        return False
    try:
        import winreg

        policy_path = r"Software\Policies\Google\Chrome"
        access = winreg.KEY_SET_VALUE
        if hasattr(winreg, "KEY_WOW64_64KEY"):
            access |= winreg.KEY_WOW64_64KEY
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, policy_path, 0, access) as key:
            # false: Chrome must not check/register itself as the default browser.
            winreg.SetValueEx(key, "DefaultBrowserSettingEnabled", 0, winreg.REG_DWORD, 0)
            # false: hide promotional first-run/welcome surfaces, including default-browser prompts.
            winreg.SetValueEx(key, "PromotionsEnabled", 0, winreg.REG_DWORD, 0)
        log.info(
            "[Chrome Policy] HKCU\\%s: DefaultBrowserSettingEnabled=0, PromotionsEnabled=0.",
            policy_path,
        )
        return True
    except Exception as exc:
        log.warning(
            "[Chrome Policy] Не удалось записать политики отключения окна браузера по умолчанию: %s. "
            "Оставляю командный параметр --no-default-browser-check.",
            str(exc)[:220],
        )
        return False


def launch_chrome_profile(profile: dict[str, Any]) -> None:
    user_data_dir = Path(profile["user_data_dir"])
    user_data_dir.mkdir(parents=True, exist_ok=True)

    if not CHROME_PATH.exists():
        raise RuntimeError(f"Chrome не найден: {CHROME_PATH}")

    # Apply current-user Chrome policies before the browser process is created.
    configure_chrome_suppress_prompts_policy()

    port = int(profile["port"])
    proxy_port = get_flow_proxy_port(profile)
    window_x, window_y = get_flow_window_position()
    cmd = [
        str(CHROME_PATH),
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={port}",
        f"--window-size={FLOW_WINDOW_WIDTH},{FLOW_WINDOW_HEIGHT}",
        f"--window-position={window_x},{window_y}",
        # Always start into a fresh window; the controller navigates that
        # existing tab to Flow and selects a current project dynamically.
        "--new-window",
        "--no-first-run",
        "--no-default-browser-check",  # suppress the "Make Chrome your default browser" startup prompt
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
    ]
    if proxy_port:
        cmd.extend([
            f"--proxy-server=http://127.0.0.1:{proxy_port}",
            "--proxy-bypass-list=localhost;127.0.0.1",
        ])

    log.info(
        "🌐 [Chrome] Запускаем профиль %s на CDP %s (proxy=127.0.0.1:%s) в новом окне без восстановления старых вкладок; position=%s,%s size=%sx%s mode=%s",
        profile["name"],
        port,
        proxy_port or "system",
        window_x,
        window_y,
        FLOW_WINDOW_WIDTH,
        FLOW_WINDOW_HEIGHT,
        FLOW_WINDOW_OFFSCREEN_MODE,
    )

    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        ),
    )


def wait_for_cdp(port: int, timeout: int = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cdp_alive(port):
            return True
        time.sleep(1)
    return False


def move_profile_window_to_target(port: int) -> None:
    if os.name != "nt" or not MOVE_WINDOW_WHEN_SWITCHING:
        return

    try:
        user32 = ctypes.windll.user32
        target_pids = set(find_chrome_process_for_port(port))
        if not target_pids:
            return

        windows = []
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd, lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) in target_pids:
                windows.append(hwnd)
            return True

        user32.EnumWindows(EnumWindowsProc(callback), 0)

        target_x, target_y = get_flow_window_position()
        for hwnd in windows:
            user32.MoveWindow(
                hwnd,
                target_x,
                target_y,
                FLOW_WINDOW_WIDTH,
                FLOW_WINDOW_HEIGHT,
                True,
            )
            try:
                user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
            break
    except Exception as e:
        log.debug("[Chrome] move window failed: %s", e)


# ============================================================================
# FLOW UI AUTOMATION (FLOW CONTROLLER)
# ============================================================================


def detect_flow_agent_quota_banner(raw_text: str) -> str | None:
    """Recognize Flow Agent quota banners in common UI languages."""
    text = re.sub(r"\s+", " ", str(raw_text or "")).casefold().replace("’", "'").strip()
    if not text:
        return None

    # English variants used by the Flow Agent quota card.
    english_markers = (
        "you've reached your flow agent limit",
        "you have reached your flow agent limit",
        "you've reached your flow agent quota",
        "you have reached your flow agent quota",
        "you reached your flow agent limit",
        "flow agent quota limit",
        "switch to manual prompting",
    )
    for marker in english_markers:
        if marker in text:
            return marker
    if "come back tomorrow" in text and ("flow agent" in text or "manual prompting" in text):
        return "Flow Agent limit; come back tomorrow"

    # Russian UI: "Вы достигли лимита ... Перейдите к ручному вводу запросов ..."
    russian_markers = (
        "достигли лимита на использование агента flow",
        "достигли лимита использования агента flow",
        "лимита на использование агента flow",
        "перейдите к ручному вводу запросов",
        "перейти к ручному вводу запросов",
    )
    for marker in russian_markers:
        if marker in text:
            return marker
    if "вернитесь завтра" in text and ("доступные тарифы" in text or "лимит" in text or "агент flow" in text):
        return "лимит Flow Agent; вернитесь завтра"

    # Polish and other translated UI strings.
    translated_markers = (
        "osiągnięto limit agenta flow",
        "osiagnieto limit agenta flow",
        "przekroczono limit agenta flow",
        "limit agenta flow",
        "wróć jutro",
        "wroc jutro",
        "ręczne wpisywanie promptów",
        "reczne wpisywanie promptow",
        "has alcanzado el límite de flow agent",
        "vous avez atteint votre limite flow agent",
        "hai raggiunto il limite di flow agent",
    )
    for marker in translated_markers:
        if marker in text:
            return marker

    # Keep support for English/localized cards that expose both technical terms.
    if "quota" in text and "limit" in text:
        return "quota + limit"
    return None


class FlowController:
    def __init__(self, playwright, account_manager: FlowAccountManager):
        self.playwright = playwright
        self.account_manager = account_manager
        self.browser = None
        self.page = None
        self.profile = None
        self._last_prompt_attempts = 0
        # At most one Flow-only cookie cleanup/reload per media target (generic error OR rate limit).
        # If the same card returns, let the normal retry/profile failover handle it.
        self._flow_generic_error_recovery_used = False
        # Used only if the Windows Chrome PID/create-time identity cannot be read.
        self._browser_connection_token = f"controller:{os.getpid()}:{time.time_ns()}"

    async def _wait_for_flow_document_ready(self, timeout: float) -> bool:
        """Wait for the Flow document to become interactive instead of trusting DOMContentLoaded."""
        deadline = time.monotonic() + max(1.0, float(timeout))
        while time.monotonic() < deadline:
            if self.page is None or self.page.is_closed():
                return False
            try:
                current_url = str(self.page.url or "").casefold()
                if "flow.google.com" in current_url:
                    ready_state = await self.page.evaluate("document.readyState")
                    body_text = await self.page.locator("body").inner_text(timeout=2_000)
                    # A non-empty Flow document is enough here; project/editor readiness
                    # is checked later with dedicated, longer waits.
                    if ready_state in {"interactive", "complete"} and len((body_text or "").strip()) >= 20:
                        return True
            except Exception:
                pass
            await asyncio.sleep(FLOW_PAGE_READY_POLL_SECONDS)
        return False

    async def _open_flow_home_slow_network(self, profile_name: str) -> None:
        """Open Flow on a slow VPN, allowing long commits and a separate app-shell wait."""
        last_error: Exception | None = None
        for attempt in range(1, FLOW_PAGE_NAVIGATION_ATTEMPTS + 1):
            if self.page is None or self.page.is_closed():
                raise FlowTechnicalError(f"Вкладка Chrome закрылась при открытии Flow для {profile_name}.")

            log.info(
                "[Flow Connect] Открываю %s для %s: попытка %s/%s, navigation timeout=%ss, "
                "дополнительное ожидание интерфейса=%ss.",
                FLOW_HOME_URL, profile_name, attempt, FLOW_PAGE_NAVIGATION_ATTEMPTS,
                FLOW_PAGE_NAVIGATION_TIMEOUT_MS // 1000, int(FLOW_PAGE_READY_TIMEOUT_SECONDS),
            )
            try:
                # 'commit' waits only for the main document response to start loading;
                # it avoids spending the entire navigation timeout on slow subresources.
                await self.page.goto(
                    FLOW_HOME_URL,
                    wait_until="commit",
                    timeout=FLOW_PAGE_NAVIGATION_TIMEOUT_MS,
                )
            except Exception as exc:
                last_error = exc
                log.warning(
                    "[Flow Connect] Навигация Flow ещё не завершилась (%s/%s): %s. "
                    "Проверяю, не успела ли страница всё-таки открыться.",
                    attempt, FLOW_PAGE_NAVIGATION_ATTEMPTS, type(exc).__name__, str(exc)[:180],
                )

            if await self._wait_for_flow_document_ready(FLOW_PAGE_READY_TIMEOUT_SECONDS):
                log.info(
                    "[Flow Connect] ✅ Документ Flow доступен для %s; продолжаю ждать сам редактор/проект отдельно.",
                    profile_name,
                )
                return

            if attempt < FLOW_PAGE_NAVIGATION_ATTEMPTS:
                log.warning(
                    "[Flow Connect] Flow не показал рабочий документ за %ss; повторяю открытие через 8 секунд.",
                    int(FLOW_PAGE_READY_TIMEOUT_SECONDS),
                )
                await asyncio.sleep(8.0)

        current_url = str(getattr(self.page, "url", "") or "") if self.page else "<closed>"
        detail = f"; последняя ошибка навигации: {last_error}" if last_error else ""
        raise FlowTechnicalError(
            f"Не удалось дождаться интерфейса Flow для {profile_name} после "
            f"{FLOW_PAGE_NAVIGATION_ATTEMPTS} попыток; current_url={current_url}{detail}"
        )

    async def connect(self, profile: dict[str, Any]) -> None:
        port = int(profile["port"])
        profile_name = profile["name"]
        user_data_dir = profile["user_data_dir"]

        if not cdp_alive(port):
            running_pids = find_chrome_process_for_profile(user_data_dir)
            if running_pids:
                raise FlowTechnicalError(
                    f"Профиль {profile_name} уже открыт (PID {running_pids[0]}), "
                    f"но CDP {port} недоступен. Запускай с --remote-debugging-port {port}."
                )

            if not AUTO_LAUNCH_CHROME:
                raise FlowTechnicalError(f"CDP {port} недоступен и AUTO_LAUNCH_CHROME=False")

            launch_chrome_profile(profile)
            if not wait_for_cdp(port, 35):
                raise FlowTechnicalError(f"Chrome {profile_name} не поднял CDP {port}")

        try:
            self.browser = await self.playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}"
            )
        except Exception as e:
            raise FlowTechnicalError(f"CDP connect {profile_name}: {e}") from e

        self.profile = profile
        context = self.browser.contexts[0]

        flow_pages = [p for p in context.pages if "flow.google.com" in p.url.lower()]

        # Chrome may restore a blank/new-tab page before Flow loads. Reuse that
        # existing tab; do not create a new tab and do not open a hard-coded
        # project.
        if not flow_pages:
            usable_pages = [p for p in context.pages if not p.is_closed()]
            if usable_pages:
                self.page = usable_pages[0]
            else:
                raise FlowTechnicalError(
                    f"У профиля {profile_name} нет доступной вкладки Chrome."
                )

            try:
                await self._open_flow_home_slow_network(profile_name)
            except Exception as e:
                if isinstance(e, FlowTechnicalError):
                    raise
                raise FlowTechnicalError(f"Не удалось открыть Flow: {e}") from e

            flow_pages = [p for p in context.pages if "flow.google.com" in p.url.lower()]

        if flow_pages:
            project_pages = [p for p in flow_pages if "/project/" in p.url.lower()]
            self.page = project_pages[-1] if project_pages else flow_pages[-1]

        # If the restored session landed on Flow home instead of a project, try
        # the currently visible project list. This is dynamic and never pins a
        # project ID.
        if "/project/" not in self.page.url.lower():
            log.info(
                "[Flow Connect] Жду список проектов для %s до %ss (медленный VPN).",
                profile_name, int(FLOW_PROJECT_LIST_WAIT_SECONDS),
            )
            deadline = time.time() + FLOW_PROJECT_LIST_WAIT_SECONDS
            opened = False
            while time.time() < deadline:
                try:
                    links = self.page.locator('a[href*="/project/"]')
                    for i in range(await links.count()):
                        link = links.nth(i)
                        if not await link.is_visible():
                            continue
                        href = await link.get_attribute("href")
                        if href and "/project/" in href:
                            await link.click(timeout=5_000)
                            opened = True
                            break
                    if opened:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)

            if not opened:
                log.warning(
                    "[Flow] Не найден открытый проект в текущей сессии %s; "
                    "остаёмся на Flow home.",
                    profile_name,
                )

        await asyncio.sleep(2)
        await self.check_for_block_or_quota()
        move_profile_window_to_target(port)

    async def disconnect(self, close_chrome: bool = False) -> None:
        profile = self.profile
        browser = self.browser

        self.browser = None
        self.page = None
        self.profile = None

        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass

        if close_chrome and profile is not None:
            await asyncio.to_thread(close_chrome_profile_process, profile)

    async def body_text(self) -> str:
        try:
            return await self.page.locator("body").inner_text(timeout=5_000)
        except Exception:
            return ""

    async def _dismiss_flow_cookie_consent(self, stage: str = "Flow page") -> bool:
        """Dismiss a post-cookie-reset consent dialog without accepting optional tracking.

        Prefer Reject all / Only necessary buttons. Never click Accept all automatically.
        The handler is deliberately text/role based so it survives Google/Flow DOM changes.
        """
        if self.page is None or self.page.is_closed():
            return False

        consent_markers = (
            "before you continue to google", "we use cookies", "this site uses cookies",
            "uses cookies to", "cookie consent", "cookie settings", "cookie preferences",
            "cookies and data",
            "используем файлы cookie", "использует файлы cookie", "файлы cookie",
            "настройки файлов cookie", "согласие на использование cookie",
            "utilizamos cookies", "este sitio utiliza cookies", "cookies y datos",
            "uzywamy plikow cookie", "ta strona uzywa plikow cookie",
            "wir verwenden cookies", "diese website verwendet cookies",
        )
        reject_labels = re.compile(
            r"^(?:reject all(?: cookies)?|reject optional(?: cookies)?|decline all(?: cookies)?|"
            r"only necessary(?: cookies)?|necessary only|essential only|"
            r"отклонить все(?: файлы cookie| cookies)?|отклонить необязательные(?: файлы cookie| cookies)?|"
            r"только необходимые(?: файлы cookie| cookies)?|только обязательные(?: файлы cookie| cookies)?|"
            r"оставить только необходимые|"
            r"rechazar tod(?:o|a)s?(?: (?:los|las) cookies)?|rechazar opcionales|solo las necesarias|"
            r"solo necesarias|"
            r"odrzuc wszystkie(?: pliki cookie)?|odrzuc opcjonalne|tylko niezbedne|"
            r"alle ablehnen|nur notwendige(?: cookies)?|nur essenzielle(?: cookies)?)$",
            re.IGNORECASE,
        )

        try:
            dialogs = self.page.locator(
                '[role="dialog"], [role="alertdialog"], [aria-modal="true"], '
                'mat-dialog-container, [data-testid*="cookie" i], [id*="cookie" i]'
            )
            scopes = []
            for index in range(min(await dialogs.count(), 12)):
                candidate = dialogs.nth(index)
                try:
                    if not await candidate.is_visible():
                        continue
                    dialog_text = _normalize_flow_policy_text(
                        await candidate.inner_text(timeout=1_000)
                    )
                    dialog_cookie_markers = consent_markers + ("cookies", "cookie", "куки")
                    if any(_normalize_flow_policy_text(marker) in dialog_text for marker in dialog_cookie_markers):
                        scopes.append(candidate)
                except Exception:
                    continue

            # Some Google consent screens render as a full page rather than a role=dialog.
            # Use the page as scope only when a distinctive consent phrase is present.
            if not scopes:
                page_text = _normalize_flow_policy_text(await self.body_text())
                if any(_normalize_flow_policy_text(marker) in page_text for marker in consent_markers):
                    scopes.append(self.page)

            for scope in scopes:
                try:
                    buttons = scope.get_by_role("button")
                    for index in range(min(await buttons.count(), 40)):
                        button = buttons.nth(index)
                        try:
                            if not await button.is_visible() or not await button.is_enabled():
                                continue
                            label = re.sub(
                                r"\s+", " ",
                                (await button.inner_text(timeout=700) or await button.get_attribute("aria-label") or "").strip(),
                            )
                            # Normalize accents/case before matching labels such as Polish
                            # "Odrzuć wszystkie" and Spanish "Rechazar todas".
                            label_normalized = _normalize_flow_policy_text(label)
                            if not reject_labels.fullmatch(label_normalized):
                                continue
                            log.info(
                                "[Flow Consent] Найден диалог cookies на этапе '%s'; выбираю безопасный вариант '%s' "
                                "(отказ от необязательных cookies).",
                                stage, label,
                            )
                            await button.click(timeout=5_000)
                            await asyncio.sleep(1.0)
                            log.info("[Flow Consent] ✅ Диалог cookies закрыт без принятия необязательных cookies.")
                            return True
                        except Exception:
                            continue
                except Exception:
                    continue

            if scopes:
                log.warning(
                    "[Flow Consent] Найден диалог cookies на этапе '%s', но безопасная кнопка отказа/только необходимых "
                    "не найдена. Не нажимаю Accept all автоматически.",
                    stage,
                )
            return False
        except Exception as exc:
            log.debug("[Flow Consent] Проверка cookie-диалога (%s) завершилась без действия: %s", stage, str(exc)[:160])
            return False

    async def _clickable_ancestor(self, locator):
        """Find the nearest real clickable control around a Flow icon/text element."""
        ancestor_selectors = (
            "xpath=ancestor::button[1]",
            'xpath=ancestor::*[@role="button"][1]',
            'xpath=ancestor::*[@mat-icon-button][1]',
            'xpath=ancestor::*[@mat-button][1]',
            'xpath=ancestor::*[@tabindex="0"][1]',
        )
        for selector in ancestor_selectors:
            try:
                candidate = locator.locator(selector)
                if await candidate.count() and await candidate.is_visible() and await candidate.is_enabled():
                    return candidate
            except Exception:
                continue
        return None

    async def _find_flow_history_control(self):
        """Find the current Flow History header action identified by its forum icon."""
        try:
            icons = self.page.locator(
                'mat-icon.header-action-icon.google-symbols, '
                'mat-icon.google-symbols.header-action-icon, '
                'mat-icon.header-action-icon, mat-icon.google-symbols'
            )
            for index in range(await icons.count() - 1, -1, -1):
                icon = icons.nth(index)
                try:
                    if not await icon.is_visible():
                        continue
                    icon_text = re.sub(r"\s+", " ", (await icon.inner_text(timeout=700)).strip()).casefold()
                    if icon_text != "forum":
                        continue
                    control = await self._clickable_ancestor(icon)
                    if control is not None:
                        return control
                    # A few Angular builds put the click handler on a wrapper without
                    # role=button. Accept it only when it exposes an explicit click cue.
                    parent = icon.locator("xpath=..").first
                    try:
                        if await parent.is_visible() and await parent.is_enabled():
                            role = (await parent.get_attribute("role") or "").casefold()
                            tabindex = await parent.get_attribute("tabindex")
                            classes = (await parent.get_attribute("class") or "").casefold()
                            if role == "button" or tabindex is not None or "button" in classes or "click" in classes:
                                return parent
                    except Exception:
                        pass
                except Exception:
                    continue
        except Exception:
            pass

        # Accessible-name fallback for localized builds where the icon is hidden
        # or the DOM no longer exposes its ligature text.
        history_name = re.compile(
            r"^(?:history|session history|история|история сессий|история сеансов|"
            r"historia|historial|historial de sesiones)$",
            re.IGNORECASE,
        )
        for role in ("button", "tab"):
            try:
                matches = self.page.get_by_role(role, name=history_name)
                for index in range(await matches.count()):
                    item = matches.nth(index)
                    if await item.is_visible() and await item.is_enabled():
                        return item
            except Exception:
                continue
        return None

    async def _find_create_new_session_control(self):
        """Find Create new session in the History panel (English and localized labels)."""
        accepted = re.compile(
            r"^(?:create new session|создать новую сессию|создать новый сеанс|"
            r"создать сессию|новая сессия|новый сеанс|создать новый чат|новый чат|"
            r"utwórz nową sesję|utworz nowa sesje|nowa sesja|crear nueva sesión|"
            r"crear nueva sesion|nueva sesión|nueva sesion)$",
            re.IGNORECASE,
        )

        # This is the specific stable text class reported from the current Flow UI.
        try:
            labels = self.page.locator("span.create-session-button-text")
            for index in range(await labels.count()):
                label = labels.nth(index)
                try:
                    if not await label.is_visible():
                        continue
                    label_text = re.sub(r"\s+", " ", (await label.inner_text(timeout=700)).strip())
                    if not accepted.fullmatch(label_text):
                        continue
                    control = await self._clickable_ancestor(label)
                    if control is not None:
                        return control
                    # In the known Flow component, the button wrapper carries this class.
                    for selector in (
                        'xpath=ancestor::*[contains(@class,"create-session-button")][1]',
                        "xpath=..",
                    ):
                        try:
                            candidate = label.locator(selector)
                            if await candidate.count() and await candidate.is_visible() and await candidate.is_enabled():
                                return candidate
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            pass

        # Role/name fallback for Russian/English interface variants.
        for role in ("button", "menuitem"):
            try:
                matches = self.page.get_by_role(role, name=accepted)
                for index in range(await matches.count()):
                    item = matches.nth(index)
                    if await item.is_visible() and await item.is_enabled():
                        return item
            except Exception:
                continue
        return None

    async def start_new_agent_session(self) -> bool:
        """Open History and create a fresh Agent session using Flow's current two-step UI.

        Failure is deliberately non-fatal: a missing/changed History control must never
        quarantine a browser profile or prevent prompt submission. Clicks are never blindly
        repeated because a timeout may happen after the browser dispatched the click.
        """
        if not ENABLE_FLOW_NEW_SESSION_RESET:
            return False

        profile_name = str((self.profile or {}).get("name", "unknown"))
        try:
            await self._dismiss_flow_cookie_consent("before History → Create new session")
            create_control = await self._find_create_new_session_control()
            if create_control is None:
                history_control = await self._find_flow_history_control()
                if history_control is None:
                    log.warning(
                        "[Flow Session] Не найдена кнопка History/forum для %s; продолжаю без сброса Agent-сессии.",
                        profile_name,
                    )
                    return False
                log.info("[Flow Session] Нажимаю History/forum для %s.", profile_name)
                try:
                    await history_control.click(timeout=5_000)
                except Exception as exc:
                    log.warning(
                        "[Flow Session] Клик History для %s мог быть отправлен (%s): %s; второй клик не повторяю.",
                        profile_name, type(exc).__name__, str(exc)[:160],
                    )
                    return False

                deadline = time.monotonic() + FLOW_NEW_SESSION_HISTORY_WAIT_SECONDS
                while time.monotonic() < deadline:
                    create_control = await self._find_create_new_session_control()
                    if create_control is not None:
                        break
                    await asyncio.sleep(FLOW_NEW_SESSION_PANEL_POLL_SECONDS)

            if create_control is None:
                log.warning(
                    "[Flow Session] History открылся, но Create new session не найден за %.1fс для %s; продолжаю без сброса.",
                    FLOW_NEW_SESSION_HISTORY_WAIT_SECONDS, profile_name,
                )
                return False

            log.info("[Flow Session] Нажимаю Create new session для %s.", profile_name)
            try:
                await create_control.click(timeout=5_000)
            except Exception as exc:
                log.warning(
                    "[Flow Session] Клик Create new session для %s мог быть отправлен (%s): %s; повторного клика не будет.",
                    profile_name, type(exc).__name__, str(exc)[:160],
                )
                return False

            await asyncio.sleep(FLOW_NEW_SESSION_SETTLE_SECONDS)
            log.info("[Flow Session] ✅ Двухшаговое создание новой сессии Flow выполнено для %s.", profile_name)
            return True
        except Exception as exc:
            log.warning(
                "[Flow Session] Ошибка при создании новой сессии для %s; продолжаю без блокировки профиля: %s",
                profile_name, str(exc)[:200],
            )
            return False

    def _agent_browser_instance_identity(self) -> str:
        """Return a stable identity for the running Chrome process."""
        profile_name = str((self.profile or {}).get("name", "unknown"))
        known_identity = self.account_manager.browser_instance_ids.get(profile_name)
        return str(known_identity or self._browser_connection_token)

    async def ensure_new_agent_session_for_project(self, project_state) -> None:
        """Create one History-based Agent session per profile/Chrome instance/project."""
        if not ENABLE_FLOW_NEW_SESSION_RESET or project_state is None:
            return
        profile_name = str((self.profile or {}).get("name", "unknown"))
        browser_identity = self._agent_browser_instance_identity()
        data = project_state.data
        registry = data.get("flow_agent_session_browser_instances")
        if not isinstance(registry, dict):
            registry = {}
            data["flow_agent_session_browser_instances"] = registry

        existing = registry.get(profile_name)
        if (
            isinstance(existing, dict)
            and str(existing.get("ui_version", "")) == FLOW_NEW_SESSION_UI_VERSION
            and (
                not FLOW_NEW_SESSION_PER_BROWSER_OPEN
                or str(existing.get("browser_instance_id", "")) == browser_identity
            )
        ):
            log.info(
                "[Flow Session] %s: History/Create new session уже обработан для этого Chrome (%s); пропускаю повторный клик.",
                profile_name, browser_identity[:80],
            )
            return

        # Persist intent before clicking. If Python stops mid-click, the same Chrome
        # instance will not receive a second potentially destructive click on resume.
        registry[profile_name] = {
            "browser_instance_id": browser_identity,
            "ui_version": FLOW_NEW_SESSION_UI_VERSION,
            "status": "attempting",
            "attempted_at": now_iso(),
        }
        data["flow_agent_session_browser_instances"] = registry
        project_state.save()

        created = await self.start_new_agent_session()
        registry = data.get("flow_agent_session_browser_instances", {})
        if not isinstance(registry, dict):
            registry = {}
        registry[profile_name] = {
            "browser_instance_id": browser_identity,
            "ui_version": FLOW_NEW_SESSION_UI_VERSION,
            "status": "created" if created else "not_available_or_failed",
            "attempted_at": now_iso(),
        }
        data["flow_agent_session_browser_instances"] = registry
        project_state.save()

    async def _clear_flow_scoped_cookies(self) -> tuple[int, int]:
        """Clear cookies owned by flow.google.com only; preserve Google sign-in cookies.

        Cookies scoped to .google.com or accounts.google.com are deliberately retained,
        because deleting those would sign the user out of Google across the profile.
        """
        if self.browser is None or not getattr(self.browser, "contexts", None):
            return 0, 0

        context = self.browser.contexts[0]
        cookies = await context.cookies()
        flow_cookies = []
        for cookie in cookies:
            domain = str(cookie.get("domain", "")).lstrip(".").casefold()
            if domain == "flow.google.com" or domain.endswith(".flow.google.com"):
                flow_cookies.append(cookie)

        cleared = 0
        for cookie in flow_cookies:
            try:
                await context.clear_cookies(
                    name=str(cookie.get("name", "")),
                    domain=str(cookie.get("domain", "flow.google.com")),
                    path=str(cookie.get("path", "/")),
                )
                cleared += 1
            except Exception as exc:
                log.warning(
                    "[Flow Recovery] Не удалось удалить cookie Flow %r (%s): %s",
                    cookie.get("name", "?"), cookie.get("domain", "?"), str(exc)[:160],
                )

        preserved = len(cookies) - len(flow_cookies)
        log.info(
            "[Flow Recovery] Cookie cleanup: удалено Flow-only=%s, сохранено прочих/Google=%s.",
            cleared, preserved,
        )
        return cleared, preserved

    async def _recover_from_generic_flow_error(
        self, kind: str, marker: str, cooldown_seconds: float = 0.0,
    ) -> None:
        """Recover a Flow transient/throttling error without wiping Google sign-in."""
        is_rate_limit = "rate limit" in str(marker).casefold() or cooldown_seconds > 0

        if self._flow_generic_error_recovery_used:
            if is_rate_limit:
                await asyncio.to_thread(rotate_adguard_windows_vpn, f"repeat {marker}")
            raise FlowGenerationFailedError(
                f"Flow снова показал '{marker}', хотя автоматическое восстановление уже выполнялось для этого задания."
            )
        self._flow_generic_error_recovery_used = True

        profile_name = str((self.profile or {}).get("name", "unknown"))
        log.warning(
            "[Flow Recovery] Обнаружена новая ошибка '%s' для %s. Очищаю только cookies flow.google.com "
            "и перезагружаю вкладку; Google login не трогаю.",
            marker, profile_name,
        )

        try:
            await self._clear_flow_scoped_cookies()
        except Exception as exc:
            log.warning(
                "[Flow Recovery] Ошибка очистки Flow-only cookies (всё равно пробую reload): %s",
                str(exc)[:200],
            )

        if is_rate_limit:
            await asyncio.to_thread(rotate_adguard_windows_vpn, f"{profile_name}: {marker}")

        try:
            await self.page.reload(
                wait_until="commit",
                timeout=FLOW_PAGE_NAVIGATION_TIMEOUT_MS,
            )
        except Exception as exc:
            log.warning(
                "[Flow Recovery] page.reload вернул %s: %s; проверяю уже открытую вкладку.",
                type(exc).__name__, str(exc)[:180],
            )
            if self.page is None or self.page.is_closed():
                raise FlowTechnicalError("Flow recovery: вкладка закрылась во время перезагрузки.") from exc

        await asyncio.sleep(1.5)
        consent_dismissed = await self._dismiss_flow_cookie_consent("immediately after Flow cookie cleanup/reload")
        if consent_dismissed:
            # A full Google consent page can redirect back to Flow after a choice.
            await self._wait_for_flow_document_ready(FLOW_PAGE_READY_TIMEOUT_SECONDS)

        await self.ensure_mode("Video" if kind == "video" else "Image")
        await self._dismiss_flow_cookie_consent("after Flow editor became ready")
        await self.check_for_block_or_quota()

        # Start a fresh Agent chat after the Flow page has recovered. This uses the
        # current History -> Create new session UI and never blocks the profile if UI changed.
        await self.start_new_agent_session()

        if cooldown_seconds > 0:
            log.info(
                "[Flow Rate Limit] После очистки cookies и создания новой Agent-сессии выдерживаю паузу %.0fs перед повтором.",
                cooldown_seconds,
            )
            await asyncio.sleep(cooldown_seconds)

        log.info(
            "[Flow Recovery] ✅ Вкладка Flow восстановлена для %s. Текущий prompt будет повторён штатным retry-механизмом.",
            profile_name,
        )

    async def check_for_generation_rejection(
        self,
        baseline_body: str,
        kind: str,
        allow_audio_failure_if_media_found: bool = False,
        media_found: bool = False,
    ) -> None:
        """Detect new Flow errors; optionally keep trying download for a known video result.

        Includes Flow's third-party content-provider/refund refusal, which enters the safe
        prompt-rewrite path rather than the generic technical-timeout path.
        Flow sometimes shows "could not generate audio" even when a playable silent video
        node already exists. In that case, keep attempting the normal download menu first;
        policy refusals and all non-audio errors remain fatal to the current attempt.
        """
        current_raw = await self.body_text()
        current = re.sub(r"\s+", " ", (current_raw or "").lower())
        baseline = re.sub(r"\s+", " ", (baseline_body or "").lower())

        rate_limit_marker = detect_new_flow_rate_limit(current_raw, baseline_body)
        if rate_limit_marker:
            if media_found:
                log.warning(
                    "[Flow Rate Limit] Обнаружено ограничение частоты запросов (%s), но новый результат уже найден; "
                    "не очищаю cookies, продолжаю проверять скачивание.",
                    rate_limit_marker,
                )
            else:
                await self._recover_from_generic_flow_error(
                    kind,
                    f"rate limit: {rate_limit_marker}",
                    cooldown_seconds=FLOW_RATE_LIMIT_RECOVERY_PAUSE_SECONDS,
                )
                raise FlowTechnicalError(
                    "Flow показал ограничение частоты запросов. Cookies flow.google.com очищены, "
                    "страница перезагружена, History → Create new session выполнено; "
                    f"выдержана пауза {FLOW_RATE_LIMIT_RECOVERY_PAUSE_SECONDS:.0f}s. Повторяю текущий prompt."
                )

        policy_marker = detect_new_flow_policy_refusal(current_raw, baseline_body)
        if policy_marker:
            raise FlowGenerationRejectedError(
                f"Flow явно указал ограничение контента для {kind}: {policy_marker}"
            )

        # Flow also shows a generic Agent-side error card in localized UIs, e.g.
        # Russian: "Что-то пошло не так. Повторите попытку. Повторить".
        # Normalize punctuation/ё/diacritics so the hyphen and full stop do not hide
        # the card. Compare against the pre-submit body to avoid reacting to stale
        # errors from an earlier task. If a new media tile already exists, preserve it.
        generic_current = _normalize_flow_policy_text(current_raw)
        generic_baseline = _normalize_flow_policy_text(baseline_body)
        generic_error_markers = (
            ("something went wrong", "something went wrong please try again"),
            ("что то пошло не так", "что то пошло не так повторите попытку"),
            ("algo salio mal", "algo salio mal vuelve a intentarlo"),
            ("cos poszło nie tak", "cos poszło nie tak sprobuj ponownie"),
        )
        generic_marker = None
        for marker_group in generic_error_markers:
            for marker in marker_group:
                if generic_current.count(marker) > generic_baseline.count(marker):
                    generic_marker = marker
                    break
            if generic_marker:
                break

        # The heading itself is specific enough when its occurrence count increases
        # relative to the pre-submit baseline; this avoids stale-card false positives.
        generic_error_is_new = bool(generic_marker)
        if generic_error_is_new:
            if media_found:
                log.warning(
                    "[Flow] Показана новая общая ошибка '%s', но новый медиа-результат уже найден; "
                    "не очищаю cookies и продолжаю скачивание.",
                    generic_marker,
                )
            else:
                await self._recover_from_generic_flow_error(kind, generic_marker)
                raise FlowTechnicalError(
                    f"Flow показал новую общую ошибку '{generic_marker}'. Cookies flow.google.com очищены, "
                    "вкладка перезагружена, History → Create new session выполнено; "
                    "повторяю тот же prompt без ожидания полного таймаута генерации."
                )

        failure_markers = (
            (
                "unable to generate image",
                "could not generate image",
                "couldn't generate image",
                "image generation failed",
                "failed to generate image",
                "sorry, this image failed to generate",
                "this image failed to generate",
                "image failed to generate",
                "we couldn't generate this image",
                "there was an issue generating your image",
                "nieudane wygenerowanie obrazu",
                "nie udało się wygenerować obrazu",
                "не удалось сгенерировать изображение",
                "не удалось создать изображение",
            )
            if kind == "image"
            else (
                "unable to generate video",
                "could not generate video",
                "couldn't generate video",
                "video generation failed",
                "failed to generate video",
                "failed to generate a video",
                "video could not be generated",
                "sorry, this video failed to generate",
                "this video failed to generate",
                "video failed to generate",
                "failed to generate this video",
                "we couldn't generate this video",
                "we could not generate this video",
                "there was an issue generating your video",
                "video failed to be generated",
                "nieudane wygenerowanie filmu",
                "nie udało się wygenerować filmu",
                "nieudane wygenerowanie wideo",
                "не удалось сгенерировать видео",
                "не удалось создать видео",
                # Flow error cards commonly use this exact heading. It was not
                # covered by the earlier "unable/could not generate audio" checks.
                "audio generation failed",
                "failed to generate audio",
                "audio could not be generated",
                "audio generation has failed",
                "nieudane wygenerowanie audio",
                "nieudane wygenerowanie dźwięku",
                "nie udało się wygenerować audio",
                "nie udało się wygenerować dźwięku",
                "unable to generate audio",
                "could not generate audio",
                "couldn't generate audio",
                "не удалось сгенерировать аудио",
                "не удалось создать аудио",
                "не удалось сгенерировать звук",
                "не удалось создать звук",
            )
        )
        audio_failure_markers = {
            "unable to generate audio",
            "could not generate audio",
            "couldn't generate audio",
            "audio generation failed",
            "sorry, audio generation failed",
            "failed to generate audio",
            "audio could not be generated",
            "audio generation has failed",
            "nieudane wygenerowanie audio",
            "nieudane wygenerowanie dźwięku",
            "nie udało się wygenerować audio",
            "nie udało się wygenerować dźwięku",
            "не удалось сгенерировать аудио",
            "не удалось создать аудио",
            "не удалось сгенерировать звук",
            "не удалось создать звук",
        }
        for marker in failure_markers:
            if current.count(marker) > baseline.count(marker):
                if kind == "video" and allow_audio_failure_if_media_found and marker in audio_failure_markers:
                    log.debug(
                        "[Flow] Flow показал '%s', но новый video-элемент уже найден; пробую скачать видео без звука вместо немедленного повтора.",
                        marker,
                    )
                    continue
                # Audio backend failures need a different recovery path: repeating the
                # same sound-bearing prompt wastes a full generation attempt. If no
                # usable video tile exists, stop immediately and request one visual-only
                # silent rewrite at the target level.
                if kind == "video" and marker in audio_failure_markers:
                    raise FlowAudioGenerationError(
                        f"Flow audio generation failed before a usable video result: {marker}"
                    )
                # Other generic error cards remain transient technical failures.
                raise FlowGenerationFailedError(
                    f"Flow показал общую ошибку генерации {kind}: {marker}"
                )

    async def check_for_block_or_quota(self) -> None:
        raw_text = await self.body_text()
        text = re.sub(r"\s+", " ", (raw_text or "")).casefold()
        # Flow's interstitial warning can be localized. Treat it as a profile-level
        # quarantine, not a story/content block: rotate to another browser immediately.
        suspicious_markers = (
            "unusual activity",
            "some unusual activity",
            "suspicious activity",
            "we noticed suspicious activity",
            "we've noticed suspicious activity",
            "extensions that change the page",
            "browser extensions that change",
            "мы заметили подозрительную активность",
            "обнаружили подозрительную активность",
            "подозрительную активность",
            "расширения браузера, которые меняют страницу",
            "rozszerzenia przeglądarki, które zmieniają stronę",
            "podejrzaną aktywność",
            "podejrzana aktywność",
            "actividad sospechosa",
            "activité suspecte",
            "attività sospetta",
        )
        suspicious_marker = next((marker for marker in suspicious_markers if marker in text), None)
        if suspicious_marker:
            raise FlowSuspiciousActivityError(
                f"Flow suspicious-activity warning detected: {suspicious_marker}"
            )
        if "captcha" in text and "verify" in text:
            raise FlowPolicyBlockError("Flow requires verification/CAPTCHA")
        quota_marker = detect_flow_agent_quota_banner(raw_text)
        if quota_marker:
            raise FlowQuotaError(f"Flow Agent quota/limit detected: {quota_marker}")

    async def ensure_project_feed(self) -> None:
        """Закрывает редактор видео и возвращает браузер в ленту проекта."""
        not_found_btn = self.page.locator(
            'button:has-text("Назад к списку проектов"), a:has-text("Назад к списку проектов")'
        ).first
        try:
            if await not_found_btn.is_visible():
                log.info("[Flow] Возврат в проект через историю браузера...")
                await self.page.go_back()
                await asyncio.sleep(2)
        except Exception:
            pass

        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass

        back_btn = self.page.locator(
            'button:has(span:has-text("arrow_back")), '
            'button:has(mat-icon:has-text("arrow_back")), '
            'button[aria-label*="Назад" i], '
            'button[aria-label*="Back" i], '
            'span:has-text("arrow_back")'
        ).first

        try:
            if await back_btn.is_visible():
                log.info("[Flow] Закрываем редактор клипа стрелкой 'Назад'")
                await back_btn.click(timeout=3_000)
                await asyncio.sleep(1.5)
        except Exception:
            pass

        try:
            current_url = self.page.url
            if "/project/" in current_url and len(current_url.split("/project/")[1].split("/")) > 1:
                log.info("[Flow] Возврат в корень проекта через history.back()")
                await self.page.evaluate("window.history.back()")
                await asyncio.sleep(1.5)
        except Exception:
            pass

    async def ensure_mode(self, wanted: str) -> None:
        """Гарантирует нахождение в ленте проекта и готовность поля ввода ProseMirror."""
        consent_dismissed = await self._dismiss_flow_cookie_consent("before project/editor navigation")
        if consent_dismissed:
            await self._wait_for_flow_document_ready(FLOW_PAGE_READY_TIMEOUT_SECONDS)
        await self.ensure_project_feed()
        consent_dismissed = await self._dismiss_flow_cookie_consent("after project feed navigation") or consent_dismissed
        if consent_dismissed:
            await self._wait_for_flow_document_ready(FLOW_PAGE_READY_TIMEOUT_SECONDS)

        deadline = time.time() + FLOW_PROMPT_EDITOR_WAIT_SECONDS
        next_consent_check = time.monotonic() + 3.0
        while time.time() < deadline:
            if time.monotonic() >= next_consent_check:
                if await self._dismiss_flow_cookie_consent("while waiting for prompt editor"):
                    await self._wait_for_flow_document_ready(FLOW_PAGE_READY_TIMEOUT_SECONDS)
                next_consent_check = time.monotonic() + 3.0
            try:
                exact = self.page.locator(
                    'flow-rich-text-editor.prompt-input '
                    'div.prosemirror-editor '
                    'div.ProseMirror[contenteditable="true"]'
                )
                for i in range(await exact.count()):
                    if await exact.nth(i).is_visible():
                        log.info("[Flow] Редактор промпта готов для режима %s", wanted)
                        return
            except Exception:
                pass
            await asyncio.sleep(0.15)

        raise FlowTechnicalError(f"Не появился редактор промпта для режима {wanted} за {int(FLOW_PROMPT_EDITOR_WAIT_SECONDS)} секунд")

    async def prompt_box(self):
        deadline = time.time() + FLOW_PROMPT_BOX_WAIT_SECONDS
        selectors = [
            (
                'flow-rich-text-editor.prompt-input '
                'div.prosemirror-editor '
                'div.ProseMirror[contenteditable="true"]'
            ),
            (
                'flow-rich-text-editor.prompt-input '
                'div[contenteditable="true"]'
            ),
            'div.ProseMirror[contenteditable="true"]',
            'div[contenteditable="true"]',
        ]

        while time.time() < deadline:
            for selector in selectors:
                try:
                    loc = self.page.locator(selector)
                    for i in range(await loc.count()):
                        item = loc.nth(i)
                        if await item.is_visible():
                            return item
                except Exception:
                    pass
            await asyncio.sleep(0.15)

        raise FlowTechnicalError(f"Prompt editor не найден за {int(FLOW_PROMPT_BOX_WAIT_SECONDS)} секунд")

    async def _visible_local_image_preview_count(self) -> int:
        """Count visible images on the Flow page to detect a newly rendered attachment preview."""
        try:
            previews = self.page.locator("img")
            count = await previews.count()
            visible = 0
            for index in range(count):
                try:
                    if await previews.nth(index).is_visible():
                        visible += 1
                except Exception:
                    continue
            return visible
        except Exception:
            return 0

    async def _find_image_upload_input(self):
        """Prefer an image-capable file input; avoid unrelated video/project import inputs."""
        try:
            inputs = self.page.locator('input[type="file"]')
            count = await inputs.count()
            candidates: list[tuple[int, Any]] = []
            for index in range(count):
                item = inputs.nth(index)
                try:
                    accept = (await item.get_attribute("accept") or "").casefold()
                    if accept and not any(token in accept for token in ("image", ".jpg", ".jpeg", ".png", ".webp", "*/*")):
                        continue
                    multiple = await item.get_attribute("multiple") is not None
                    score = 2 if "image" in accept or "*/*" in accept else 1
                    if multiple:
                        score += 1
                    candidates.append((score, item))
                except Exception:
                    continue
            if candidates:
                candidates.sort(key=lambda pair: pair[0], reverse=True)
                # If there are only generic file inputs, do not guess among several.
                if candidates[0][0] == 1 and len(candidates) > 1:
                    return None
                return candidates[0][1]
        except Exception:
            pass
        return None

    async def _click_add_menu_control(self) -> None:
        """Open Flow's '+' insert menu using the add-menu-icon supplied by the UI."""
        # The current Flow UI uses a material icon whose text is exactly "add".
        # Click its containing button instead of guessing by localized button labels.
        selectors = (
            'mat-icon.add-menu-icon',
            'button:has(mat-icon.add-menu-icon)',
        )
        for selector in selectors:
            try:
                loc = self.page.locator(selector)
                for index in range(await loc.count()):
                    item = loc.nth(index)
                    if not await item.is_visible():
                        continue
                    button = item.locator("xpath=ancestor::button[1]")
                    target = button if await button.count() else item
                    if await target.is_visible() and await target.is_enabled():
                        await target.click(timeout=1_800)
                        await asyncio.sleep(0.15)
                        log.debug("[Flow Reference] Открыто меню '+'")
                        return
            except Exception:
                continue
        raise FlowTechnicalError(
            "Не найдена кнопка '+' (mat-icon.add-menu-icon) для открытия меню вставок Flow."
        )

    def _close_native_file_dialog_if_open(self) -> bool:
        """Close a foreground Windows Open dialog only when it is the active native dialog."""
        if os.name != "nt":
            return False
        try:
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar), ctypes.c_int]
            user32.GetClassNameW.restype = ctypes.c_int
            user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
            user32.PostMessageW.restype = ctypes.c_int
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False
            class_name = ctypes.create_unicode_buffer(256)
            if not user32.GetClassNameW(hwnd, class_name, len(class_name)):
                return False
            # Standard Windows file picker dialog class. Do not close Chrome or arbitrary windows.
            if class_name.value != "#32770":
                return False
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            time.sleep(0.2)
            log.warning("[Flow Reference] Закрыл оставшееся системное окно выбора файла Windows.")
            return True
        except Exception:
            return False


    async def _click_upload_action(self, file_path: Path) -> None:
        """Choose a local file through Playwright's file-chooser event, closing Windows' picker automatically."""
        file_path = Path(file_path).expanduser().resolve()
        if not file_path.is_file():
            raise FlowTechnicalError(f"Файл референса не найден на диске: {file_path}")
        # If a previous version of the pipeline left a native Open dialog behind, clear it first.
        self._close_native_file_dialog_if_open()

        targets: list[Any] = []
        # Grounded in the observed Flow DOM: span.upload-container > span.upload-text.
        for selector in ("span.upload-container", "span.upload-text"):
            try:
                loc = self.page.locator(selector)
                for index in range(await loc.count()):
                    item = loc.nth(index)
                    if not await item.is_visible():
                        continue
                    button = item.locator("xpath=ancestor::button[1]")
                    role_button = item.locator("xpath=ancestor::*[@role='button'][1]")
                    if await button.count() and await button.is_visible():
                        target = button
                    elif await role_button.count() and await role_button.is_visible():
                        target = role_button
                    else:
                        target = item if selector.endswith("upload-container") else item.locator("xpath=..")
                    targets.append(target)
            except Exception:
                continue

        # Accessible-name fallback for UI wording variants.
        for role in ("button", "menuitem"):
            try:
                loc = self.page.get_by_role(role, name=re.compile(r"(?i)^\s*(upload|загрузить)\s*$"))
                for index in range(await loc.count()):
                    item = loc.nth(index)
                    if await item.is_visible() and await item.is_enabled():
                        targets.append(item)
            except Exception:
                continue

        seen: set[str] = set()
        for target in targets:
            try:
                signature = await target.evaluate("el => el.outerHTML.slice(0, 300)")
            except Exception:
                signature = str(id(target))
            if signature in seen:
                continue
            seen.add(signature)
            try:
                # Important: do not click Upload and later call input.set_input_files().
                # The click opens a native Windows file dialog, which stays visible if not
                # handled through Playwright's file-chooser event.
                async with self.page.expect_file_chooser(timeout=8_000) as chooser_info:
                    await target.click(timeout=5_000)
            except PlaywrightTimeoutError as exc:
                # Recovery for a UI build where the dialog opens but Playwright misses its event:
                # close only a foreground standard Windows file picker, then set the HTML input directly.
                closed_dialog = self._close_native_file_dialog_if_open()
                upload_input = await self._find_image_upload_input()
                if closed_dialog and upload_input is not None:
                    try:
                        await upload_input.set_input_files(str(file_path), timeout=10_000)
                        log.warning(
                            "[Flow Reference] Системное окно закрыто; файл передан через скрытый input: %s",
                            file_path.name,
                        )
                        return
                    except Exception as fallback_exc:
                        raise FlowTechnicalError(
                            f"Закрыл системный диалог, но не удалось передать {file_path.name} через input: {fallback_exc}"
                        ) from fallback_exc
                raise FlowTechnicalError(
                    "Кнопка 'Загрузить' нажата, но Playwright не перехватил системное окно выбора файла. "
                    "Автоматический резервный способ передачи файла тоже недоступен."
                ) from exc
            except Exception:
                continue

            try:
                chooser = await chooser_info.value
                await chooser.set_files(str(file_path), timeout=10_000)
                await asyncio.sleep(0.45)
                log.info(
                    "[Flow Reference] Файл передан через автоматизированный выбор и системное окно закрыто: %s",
                    file_path.name,
                )
                return
            except Exception as exc:
                raise FlowTechnicalError(
                    f"Не удалось выбрать файл {file_path.name} через системный диалог Flow: {exc}"
                ) from exc

        raise FlowTechnicalError(
            "Меню '+' открылось, но пункт 'Загрузить' не найден в интерфейсе Flow."
        )

    async def _asset_picker_is_open(self) -> bool:
        """Return True when Flow's insert/assets popup is already open."""
        for selector in ("span.upload-container", ".asset-text .asset-title"):
            try:
                loc = self.page.locator(selector)
                for index in range(await loc.count()):
                    if await loc.nth(index).is_visible():
                        return True
            except Exception:
                continue
        return False

    async def _find_add_to_prompt_control(self):
        """Find the actual Flow Add-to-prompt button (not its decorative span)."""
        # Current Flow DOM: <button class="... detail-add-to-prompt-btn ...">
        # Prefer this stable, specific selector before broader text/role searches.
        try:
            direct = self.page.locator("button.detail-add-to-prompt-btn")
            for index in range(await direct.count()):
                item = direct.nth(index)
                if await item.is_visible() and await item.is_enabled():
                    return item
        except Exception:
            pass

        label_pattern = re.compile(r"(?i)^\s*(добавить в запрос|add to prompt|add to request)\s*$")
        # Accessible name fallback.
        for role in ("button", "menuitem"):
            try:
                loc = self.page.get_by_role(role, name=label_pattern)
                for index in range(await loc.count()):
                    item = loc.nth(index)
                    if await item.is_visible() and await item.is_enabled():
                        return item
            except Exception:
                pass

        # Text may resolve to the inner mdc-button__label span; always climb to its real button.
        try:
            texts = self.page.get_by_text(label_pattern)
            for index in range(await texts.count()):
                text_item = texts.nth(index)
                if not await text_item.is_visible():
                    continue
                for selector in (
                    "xpath=ancestor::button[1]",
                    "xpath=ancestor::*[@role='button'][1]",
                    "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' mat-mdc-button-base ')][1]",
                ):
                    try:
                        parent = text_item.locator(selector)
                        if await parent.count() and await parent.is_visible() and await parent.is_enabled():
                            return parent
                    except Exception:
                        continue
        except Exception:
            pass
        return None

    async def _click_add_to_prompt_if_present(
        self, timeout: float | None = 12.0, max_clicks: int | None = 2
    ) -> bool:
        """Click Add-to-prompt with bounded retries and verify the picker closes.

        Never loop forever or repeatedly click a button while Flow is processing it.
        """
        timeout_s = 12.0 if timeout is None else max(0.0, float(timeout))
        click_limit = 2 if max_clicks is None else max(1, int(max_clicks))
        deadline = time.monotonic() + timeout_s
        clicks = 0
        last_error = None

        while time.monotonic() < deadline and clicks < click_limit:
            control = await self._find_add_to_prompt_control()
            if control is None:
                if not await self._asset_picker_is_open():
                    return clicks > 0
                await asyncio.sleep(0.25)
                continue

            try:
                # The button itself is the click target; never target mat-focus-indicator.
                await control.click(timeout=2_000)
                clicks += 1
                log.info(
                    "[Flow Reference] Клик по 'Добавить в запрос': попытка %s/%s",
                    clicks, click_limit,
                )
            except Exception as exc:
                last_error = exc
                # A forced click is a single fallback, not an endless retry loop.
                try:
                    await control.click(timeout=1_000, force=True)
                    clicks += 1
                    log.info(
                        "[Flow Reference] Кнопка нажата принудительно: попытка %s/%s",
                        clicks, click_limit,
                    )
                except Exception as force_exc:
                    last_error = force_exc
                    log.warning(
                        "[Flow Reference] Не удалось нажать кнопку добавления: %s",
                        str(force_exc)[:180],
                    )
                    await asyncio.sleep(0.4)
                    continue

            # Give Angular time to close the picker, without clicking again immediately.
            settle_deadline = min(deadline, time.monotonic() + 3.0)
            while time.monotonic() < settle_deadline:
                still_visible = await self._find_add_to_prompt_control()
                picker_open = await self._asset_picker_is_open()
                if not still_visible and not picker_open:
                    log.info("[Flow Reference] Кнопка сработала; окно Insert закрылось")
                    return True
                await asyncio.sleep(0.25)

        if last_error:
            log.warning("[Flow Reference] Подтверждение Add to prompt не завершено: %s", str(last_error)[:180])
        log.warning(
            "[Flow Reference] Не удалось подтвердить добавление за %.1fс (%s кликов); прекращаю повторы.",
            timeout_s, clicks,
        )
        return False

    async def _find_asset_title(self, filename: str):
        """Find an exact asset title in Flow's insert picker, independent of asset order."""
        try:
            titles = self.page.locator(".asset-text .asset-title")
            for index in range(await titles.count()):
                title = titles.nth(index)
                if not await title.is_visible():
                    continue
                text = (await title.inner_text()).strip()
                if text.casefold() == filename.casefold():
                    return title
        except Exception:
            pass
        return None

    async def _click_asset_title(self, title, filename: str) -> None:
        """Select the named asset from the picker to insert it into the Agent composer."""
        # The provided DOM shows .asset-title inside .asset-text. Clicking the text
        # bubbles to the card in most Flow builds; then try the row and clickable parent.
        row = title.locator("xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' asset-text ')][1]")
        candidates = [title, row]
        for ancestor_selector in (
            "xpath=ancestor::button[1]",
            "xpath=ancestor::*[@role='button'][1]",
        ):
            try:
                ancestor = title.locator(ancestor_selector)
                if await ancestor.count():
                    candidates.insert(0, ancestor)
            except Exception:
                pass
        last_error = None
        for target in candidates:
            try:
                if await target.count() and await target.is_visible() and await target.is_enabled():
                    await target.click(timeout=1_400)
                    await asyncio.sleep(0.15)
                    log.debug("[Flow Reference] Выбран asset из списка: %s", filename)
                    return
            except Exception as exc:
                last_error = exc
        raise FlowTechnicalError(
            f"Файл {filename} появился в списке Flow, но не удалось выбрать его для чата Agent: {last_error}"
        )

    async def _verify_reference_selected(
        self, filename: str, baseline_images: int, timeout: float = 5.0
    ) -> bool:
        """Verify that selecting the asset left the picker and/or added it to the composer."""
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            # Never confirm while the Insert picker is still open. The asset preview
            # inside the picker may increase image counts but is not a composer attachment.
            if await self._asset_picker_is_open():
                await asyncio.sleep(0.25)
                continue
            # Strong signal: the asset's exact filename is rendered outside the picker,
            # e.g. as the selected attachment chip/preview in the Agent composer.
            try:
                exact_texts = self.page.get_by_text(filename, exact=True)
                for index in range(await exact_texts.count()):
                    item = exact_texts.nth(index)
                    if not await item.is_visible():
                        continue
                    inside_picker = await item.evaluate(
                        "el => !!el.closest('.asset-text')"
                    )
                    if not inside_picker:
                        return True
            except Exception:
                pass

            # The picker normally closes after choosing an asset. Consider both its
            # Upload control and asset-title rows when detecting an open picker; image
            # counts are compared against the count taken *after* the asset tile was
            # already rendered, so the tile itself cannot masquerade as a chat preview.
            try:
                picker_open = False
                for selector in ("span.upload-container", ".asset-text .asset-title"):
                    loc = self.page.locator(selector)
                    for index in range(await loc.count()):
                        if await loc.nth(index).is_visible():
                            picker_open = True
                            break
                    if picker_open:
                        break
                current_images = await self._visible_local_image_preview_count()
                if not picker_open:
                    return True
                # A changed thumbnail count is not confirmation: the Insert gallery itself
                # can render the image and previously caused false-positive attachment checks.
            except Exception:
                pass
            await asyncio.sleep(0.25)
        return False

    async def attach_reference_images(self, image_paths: list[Path]) -> None:
        """Attach local identity references through Flow's + > Upload > asset selection flow."""
        paths: list[Path] = []
        seen: set[str] = set()
        for raw_path in image_paths or []:
            path = Path(raw_path).expanduser().resolve()
            identity = str(path).casefold()
            if identity in seen:
                continue
            seen.add(identity)
            if not is_valid_image(path):
                raise FlowTechnicalError(f"Файл референса отсутствует или не является валидным изображением: {path}")
            paths.append(path)
        if not paths:
            return

        log.info(
            "[Flow Reference] Прикрепляю %s эталон(ов): %s",
            len(paths), ", ".join(path.name for path in paths),
        )

        for path in paths:
            # A retry may leave the picker open. Do not click '+' again in that case:
            # doing so can toggle the popup closed and make the next click miss the asset.
            picker_was_open = await self._asset_picker_is_open()
            if not picker_was_open:
                await self._click_add_menu_control()
                await asyncio.sleep(0.15)

            # Reuse this exact local filename when it already exists in the current
            # profile's Flow asset picker; otherwise upload the exact absolute path.
            title = await self._find_asset_title(path.name)
            if title is None:
                log.info("[Flow Reference] Передаю файл по полному пути: %s", path)
                # Use the real file chooser event so the same absolute path is selected
                # and the Windows dialog is closed by Playwright instead of remaining open.
                await self._click_upload_action(path)

                # Flow uploads asynchronously. The asset card may take several seconds.
                title = None
                deadline = time.monotonic() + 15.0
                while time.monotonic() < deadline:
                    title = await self._find_asset_title(path.name)
                    if title is not None:
                        break
                    await asyncio.sleep(0.25)
                if title is None:
                    raise FlowTechnicalError(
                        f"Файл {path.name} передан по полному пути, но карточка .asset-title не появилась в Flow за 15 секунд."
                    )
                log.info("[Flow Reference] Загрузка появилась в списке assets: %s", path.name)
            else:
                log.info("[Flow Reference] Использую уже загруженный asset: %s", path.name)

            # Select the exact row. If Flow then exposes an explicit 'Добавить в запрос'
            # button, click it too; some layouts require this final confirmation.
            baseline_images_for_selection = await self._visible_local_image_preview_count()
            # First left-click selects the asset. In some Flow builds a separate
            # 'Добавить в запрос' button appears only after selection and a short delay.
            await self._click_asset_title(title, path.name)
            await asyncio.sleep(0.2)
            add_clicked = await self._click_add_to_prompt_if_present(timeout=None, max_clicks=None)
            if await self._verify_reference_selected(
                path.name, baseline_images_for_selection, timeout=2.5
            ):
                log.info("[Flow Reference] ✅ Эталон выбран и подтверждён в чате Agent: %s", path.name)
                continue

            # If the Add button is still visible, do not click the asset card again:
            # this could toggle the selection. We already gave the actual button three
            # verified chances. Abort this submission instead of continuing with popup open.
            visible_add_button = await self._find_add_to_prompt_control()
            if visible_add_button is not None:
                log.error(
                    "[Flow Reference] 'Добавить в запрос' остаётся видимой после повторных кликов; "
                    "не отправляю prompt и очищаю интерфейс для retry."
                )
                try:
                    await self.page.keyboard.press("Escape")
                    await asyncio.sleep(0.4)
                except Exception:
                    pass
                raise FlowTechnicalError(
                    f"Flow не закрыл окно Insert после попыток нажать 'Добавить в запрос' для {path.name}; "
                    "генерация не отправлена."
                )

            # Fallback only when no Add button could be found at all: some UI builds
            # attach on the second card click. Never use blind double-clicks.
            log.warning(
                "[Flow Reference] Подтверждение не появилось (button_clicked=%s); "
                "повторно выбираю карточку один раз.", add_clicked,
            )
            title = await self._find_asset_title(path.name)
            if title is None:
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
                raise FlowTechnicalError(
                    f"Файл {path.name} исчез из списка assets до подтверждения выбора."
                )
            await self._click_asset_title(title, path.name)
            await asyncio.sleep(0.2)
            await self._click_add_to_prompt_if_present(timeout=None, max_clicks=None)
            if not await self._verify_reference_selected(
                path.name, baseline_images_for_selection, timeout=4.0
            ):
                try:
                    await self.page.keyboard.press("Escape")
                    await asyncio.sleep(0.4)
                except Exception:
                    pass
                raise FlowTechnicalError(
                    f"Файл {path.name} есть в assets, но Flow не подтвердил его добавление в чат Agent. "
                    "Окно Insert закрыто через Escape; prompt не отправлен, текущая генерация будет повторена."
                )
            log.info("[Flow Reference] ✅ Эталон выбран и подтверждён после повторного действия: %s", path.name)

    async def set_prompt(self, prompt: str) -> None:
        box = await self.prompt_box()
        try:
            await box.click(timeout=5_000)
        except Exception:
            await box.click(force=True)

        try:
            await box.fill("")
            await box.fill(prompt)
            log.info("[Flow] Prompt inserted")
            return
        except Exception:
            pass

        try:
            await box.click(force=True)
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Backspace")
            await self.page.keyboard.insert_text(prompt)
            log.info("[Flow] Prompt inserted via keyboard fallback")
        except Exception as e:
            raise FlowTechnicalError(f"Не удалось вставить prompt: {e}") from e

    async def generate_start(self) -> None:
        try:
            box = await self.prompt_box()
            await box.click(force=True)
            await asyncio.sleep(0.2)
            await box.press("Enter")
            log.info("[Flow] Generate: Enter отправлен из ProseMirror")
            # The chat feed reflows after submit. Park the pointer at the far
            # lower-right edge so it cannot accidentally hover a response's
            # "Flag output" feedback control as messages/results move.
            await asyncio.sleep(0.15)
            try:
                await self.page.mouse.move(12, 12)
            except Exception:
                pass
        except Exception as e:
            raise FlowTechnicalError(f"Ошибка отправки Enter: {e}") from e

    async def snapshot_video_tiles(self) -> set[str]:
        """Snapshot both result thumbnails and actual Flow video-player URLs.

        Flow can attach the generated clip to a <video> element before its
        thumbnail/menu UI finishes re-rendering. Tracking both URLs lets the
        next poll distinguish a genuinely new result from clips already in chat.
        """
        result: set[str] = set()
        selectors = [
            'video[aria-label="Сгенерированное видео"]',
            'video[aria-label*="Generated video" i]',
            'video[src*="/asb/"]',
            'img.thumbnail[alt="Значок сгенерированного видео"]',
            'img.thumbnail',
        ]
        for sel in selectors:
            loc = self.page.locator(sel)
            try:
                count = await loc.count()
            except Exception:
                continue
            for i in range(count):
                item = loc.nth(i)
                try:
                    src = await item.get_attribute("src")
                    if src and src.strip():
                        result.add(src.strip())
                except Exception:
                    pass
        return result

    async def find_new_video_tile(
        self,
        old_srcs: set[str],
        timeout: int = FLOW_VIDEO_GENERATION_TIMEOUT,
        baseline_body: str = "",
        old_image_srcs: set[str] | None = None,
    ):
        """Find a new generated video using its actual <video src> first.

        Thumbnail coordinates are only a fallback: the Flow feed can virtualize
        or move thumbnails while the actual player already has a new source URL.
        """
        started = time.time()
        last_report = 0
        wrong_image_candidate_sightings: dict[str, int] = {}
        # Preserve the gallery/chat scroll positions. Global scroll resets can expose old
        # cards after the baseline snapshot and cause both wrong-media detections and
        # download clicks to target the wrong row.

        video_selectors = [
            'video[aria-label="Сгенерированное видео"]',
            'video[aria-label*="Generated video" i]',
            'video[src*="/asb/"]',
        ]
        thumbnail_selectors = [
            'img.thumbnail[alt="Значок сгенерированного видео"]',
            'img.thumbnail',
        ]

        while time.time() - started < timeout:
            # Quota banners are terminal for this profile. Check before the slower DOM scan
            # so a translated Agent-limit card cannot burn the full generation timeout.
            await self.check_for_block_or_quota()
            # Flow may show an audio-generation error card while a valid silent video result
            # is already present. Search for a new playable media result first; if none exists,
            # the error is checked below and the normal profile retry path is used.

            # First choice: the real video element and its changing source URL.
            for sel in video_selectors:
                elements = self.page.locator(sel)
                try:
                    count = await elements.count()
                except Exception:
                    continue
                for i in range(count):
                    item = elements.nth(i)
                    try:
                        if not await item.is_visible():
                            continue
                        src = (await item.get_attribute("src") or "").strip()
                        if not src or src in old_srcs:
                            continue
                        box = await item.bounding_box()
                        if not box or box["width"] < 200 or box["height"] < 110:
                            continue
                        try:
                            await item.scroll_into_view_if_needed(timeout=3_000)
                            await asyncio.sleep(0.25)
                        except Exception as scroll_exc:
                            log.debug("[Flow] Не удалось прокрутить video-элемент: %s", scroll_exc)
                        # Wait for the player to have metadata/dimensions. A src by itself
                        # can briefly appear while Angular is still wiring up the player.
                        try:
                            state = await item.evaluate("""el => ({
                                readyState: Number(el.readyState || 0),
                                width: Number(el.videoWidth || 0),
                                height: Number(el.videoHeight || 0),
                                currentSrc: String(el.currentSrc || el.src || '')
                            })""")
                        except Exception:
                            state = {"readyState": 0, "width": 0, "height": 0, "currentSrc": src}
                        if not state.get("currentSrc") or (
                            int(state.get("readyState", 0)) < 1
                            and (int(state.get("width", 0)) <= 0 or int(state.get("height", 0)) <= 0)
                        ):
                            log.debug("[Flow] Новый video src обнаружен, жду метаданные плеера: %s", src[:90])
                            continue
                        box = await item.bounding_box() or box
                        log.info(
                            "[Flow] ✅ Новое видео DOM обнаружено: aria-label=%r src=%s… | x=%.0f y=%.0f w=%.0f h=%.0f | readyState=%s",
                            await item.get_attribute("aria-label"), src[:100],
                            box["x"], box["y"], box["width"], box["height"], state.get("readyState"),
                        )
                        return item
                    except Exception as exc:
                        log.debug("[Flow] Ошибка проверки DOM video-кандидата: %s", exc)

            # Newer Flow builds can show the third-party content-provider refusal inside a
            # failed gallery card while the Agent panel still says "Generation has started".
            # Detect that card before image heuristics or the 360s watchdog can misclassify it.
            current_body = await self.body_text()
            third_party_marker = detect_new_flow_third_party_refusal(current_body, baseline_body)
            if third_party_marker:
                raise FlowGenerationRejectedError(
                    "Flow отклонил video по ограничению стороннего контента/правообладателей: "
                    + third_party_marker
                )

            # If Flow silently switched media type and returned a raster image, stop now
            # instead of consuming the full 360-second video timer. This scans only large
            # images in the left All media gallery; Agent-side reference chips are excluded
            # by the scanner's geometry bounds, and known video thumbnails are filtered out.
            baseline_images = old_image_srcs or set()
            try:
                image_candidates = await self._scan_visible_image_candidates()
            except Exception:
                image_candidates = []
            for image in image_candidates:
                if image.get("is_video_thumbnail"):
                    continue
                src = str(image.get("src", "")).strip()
                if not src or not image.get("loaded"):
                    continue
                is_new_src = src not in baseline_images
                sig = str(image.get("sig", "")).strip()
                is_new_dom_signature = bool(image.get("observed")) and bool(sig) and sig not in baseline_images
                if not (is_new_src or is_new_dom_signature):
                    continue
                sightings = wrong_image_candidate_sightings.get(src, 0) + 1
                wrong_image_candidate_sightings[src] = sightings
                # Give a legitimate video thumbnail/player one short extra poll to appear.
                # If the actual video exists, the video-element scan above wins next pass.
                if sightings < 2:
                    log.info(
                        "[Flow Media Type] Подозрительная новая image-карточка (%s/%s подтверждений); "
                        "проверяю, не появляется ли video-плеер: %s…",
                        sightings, 2, src[:80],
                    )
                    continue
                log.error(
                    "[Flow Media Type] Flow вернул НОВОЕ ИЗОБРАЖЕНИЕ вместо видео: "
                    "x=%.0f y=%.0f w=%.0f h=%.0f observed=%s src=%s…",
                    image.get("x", 0), image.get("y", 0), image.get("width", 0), image.get("height", 0),
                    bool(image.get("observed")), src[:100],
                )
                raise FlowWrongMediaKindError(
                    "Flow вернул новое изображение вместо запрошенного видео "
                    f"(gallery image {int(image.get('width', 0))}x{int(image.get('height', 0))}); "
                    "прерываю ожидание и повторяю запрос с усиленным требованием видео."
                )

            # Fallback: thumbnail URL. Scroll it into view before passing it to
            # the output-ready/menu stage; negative y coordinates are never used.
            for sel in thumbnail_selectors:
                elements = self.page.locator(sel)
                try:
                    count = await elements.count()
                except Exception:
                    continue
                for i in range(count):
                    item = elements.nth(i)
                    try:
                        if not await item.is_visible():
                            continue
                        src = (await item.get_attribute("src") or "").strip()
                        if not src or src in old_srcs:
                            continue
                        box = await item.bounding_box()
                        if not box or box["x"] > 900 or box["width"] < 250 or box["height"] < 130:
                            continue
                        try:
                            await item.scroll_into_view_if_needed(timeout=3_000)
                            await asyncio.sleep(0.25)
                            box = await item.bounding_box()
                        except Exception as scroll_exc:
                            log.debug("[Flow] Не удалось прокрутить новую миниатюру: %s", scroll_exc)
                        if not box:
                            continue
                        viewport_height = int(await self.page.evaluate("() => window.innerHeight"))
                        if box["y"] + box["height"] <= 0 or box["y"] >= viewport_height:
                            log.debug("[Flow] Миниатюра вне экрана после прокрутки: y=%.0f h=%.0f", box["y"], box["height"])
                            continue
                        log.info(
                            "[Flow] Новая video thumbnail в области видимости: x=%.0f y=%.0f w=%.0f h=%.0f",
                            box["x"], box["y"], box["width"], box["height"],
                        )
                        return item
                    except Exception as exc:
                        log.debug("[Flow] Ошибка проверки thumbnail-кандидата: %s", exc)

            # No new playable video or thumbnail was found in this poll; only now treat a
            # newly displayed generic generation/audio error as a failed attempt.
            await self.check_for_generation_rejection(baseline_body, "video")

            now = int(time.time())
            if now - last_report >= 10:
                last_report = now
                body = re.sub(r"\s+", " ", await self.body_text())
                log.info("[Flow] ждём новое видео %ss: %s", int(time.time() - started), body[-220:])
            await self.check_for_block_or_quota()
            await asyncio.sleep(2)

        raise FlowTechnicalError(f"Новое видео не появилось за {timeout}s")

    async def normalize_flow_gallery_scroll(self) -> None:
        """Preserve Flow's current scroll positions before taking the baseline snapshot.

        IMPORTANT: Do not scroll every nested container to its bottom. Flow's All media
        gallery and Agent conversation are virtualized independently; forcing all
        scrollables to scrollHeight can move new result tiles outside the viewport and
        make the download handler target an off-screen/stale card. The baseline snapshot
        works without changing scroll position, so this function is intentionally
        non-invasive.
        """
        try:
            state = await self.page.evaluate("""() => ({
                windowY: Number(window.scrollY || 0),
                scrollables: Array.from(document.querySelectorAll('*')).filter(el => {
                    const st = getComputedStyle(el);
                    return (/(auto|scroll)/.test(st.overflowY) || el.scrollHeight > el.clientHeight + 80)
                        && el.clientHeight > 120 && el.scrollHeight > el.clientHeight + 80;
                }).slice(0, 20).map(el => ({
                    tag: el.tagName,
                    id: el.id || '',
                    role: el.getAttribute('role') || '',
                    label: el.getAttribute('aria-label') || '',
                    top: Number(el.scrollTop || 0),
                    max: Math.max(0, Number(el.scrollHeight || 0) - Number(el.clientHeight || 0))
                }))
            })""")
            log.debug(
                "[Flow Gallery] Сохраняю текущую прокрутку без принудительного скролла: windowY=%s, nested=%s",
                (state or {}).get("windowY", "?"), len((state or {}).get("scrollables", [])),
            )
            # A brief settle delay is enough for pending gallery mutations; leave all
            # scrollTop values untouched before the pre-submit image snapshot.
            await asyncio.sleep(0.2)
        except Exception as exc:
            log.debug("[Flow Gallery] Не удалось прочитать состояние прокрутки (без скролла): %s", exc)
            await asyncio.sleep(0.2)

    async def arm_image_generation_observer(self) -> None:
        """Observe newly inserted or updated image nodes before pressing Generate.

        Flow sometimes reuses a DOM container and updates the image in place, which
        can make a URL-only snapshot miss a result. Element identity + src changes
        are recorded so the poller can still recognize that result.
        """
        try:
            await self.page.evaluate("""() => {
                try { window.__flowImageGenObserver?.disconnect(); } catch (_) {}
                const state = { nodes: new WeakSet(), changed: new WeakSet(), startedAt: performance.now(), events: 0 };
                window.__flowImageGenState = state;
                const register = node => {
                    if (!node || node.nodeType !== 1) return;
                    if (node.tagName === 'IMG') { state.nodes.add(node); state.events++; }
                    if (node.querySelectorAll) {
                        node.querySelectorAll('img').forEach(img => { state.nodes.add(img); state.events++; });
                    }
                };
                const observer = new MutationObserver(records => {
                    for (const record of records) {
                        if (record.type === 'childList') {
                            record.addedNodes.forEach(register);
                        } else if (record.type === 'attributes' && record.target && record.target.tagName === 'IMG') {
                            state.changed.add(record.target);
                            state.events++;
                        }
                    }
                });
                observer.observe(document.body || document.documentElement, {
                    subtree: true, childList: true, attributes: true,
                    attributeFilter: ['src', 'srcset', 'data-src', 'alt']
                });
                window.__flowImageGenObserver = observer;
            }""")
        except Exception as exc:
            log.debug("[Flow Image Observer] Не удалось включить наблюдатель DOM: %s", exc)

    async def image_was_added_after_submit(self, img) -> bool:
        try:
            return bool(await img.evaluate("""el => {
                const s = window.__flowImageGenState;
                return !!(s && ((s.nodes && s.nodes.has(el)) || (s.changed && s.changed.has(el))));
            }"""))
        except Exception:
            return False

    async def _scan_visible_image_candidates(self) -> list[dict[str, Any]]:
        """Collect large visible image cards in one browser evaluation (fast, DOM-wide)."""
        try:
            value = await self.page.evaluate("""() => {
                const all = Array.from(document.querySelectorAll('img'));
                const vw = window.innerWidth || document.documentElement.clientWidth || 1024;
                const state = window.__flowImageGenState;
                const out = [];
                for (let index = 0; index < all.length; index++) {
                    const el = all[index];
                    const style = getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
                    const rect = el.getBoundingClientRect();
                    // Keep only actual result tiles in Flow's left media gallery.
                    // The Agent sidebar also displays a small copy/preview of the same
                    // generated image; accepting it makes the right-click hit the browser's
                    // native image menu instead of Flow's download menu. The previous
                    // working detector used x <= 650 and at least 200x100 CSS pixels.
                    const galleryRightEdge = Math.min(650, vw * 0.64);
                    if (rect.width < 200 || rect.height < 100 || rect.x < -10 || rect.x > galleryRightEdge || rect.width > vw * 0.92) continue;
                    const src = String(el.currentSrc || el.getAttribute('src') || el.getAttribute('data-src') || '').trim()
                        || String(el.getAttribute('srcset') || '').trim().split(',')[0].trim().split(/\\s+/)[0] || '';
                    if (!src) continue;
                    let card = el;
                    for (let i = 0; i < 5 && card.parentElement; i++) {
                        const parent = card.parentElement;
                        const clue = (parent.getAttribute('data-testid') || '') + ' ' +
                            (parent.getAttribute('data-id') || '') + ' ' + (parent.getAttribute('role') || '') + ' ' +
                            (parent.getAttribute('aria-label') || '') + ' ' + (parent.className || '');
                        card = parent;
                        if (/card|tile|media|generation|result|asset/i.test(String(clue)) || parent.tagName === 'ARTICLE') break;
                    }
                    const text = String(card.innerText || card.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 180);
                    const alt = String(el.getAttribute('alt') || '');
                    const className = typeof el.className === 'string' ? el.className : '';
                    // Flow's video results may have a raster thumbnail before the actual
                    // <video> player appears. Never mistake that thumbnail for an image-only result.
                    const isVideoThumbnail = el.matches('img.thumbnail') ||
                        /generated video|video thumbnail|значок сгенерированного видео|миниатюр.*видео/i.test(alt + ' ' + className);
                    const attrs = ['data-testid','data-id','data-node-id','aria-label','role','id']
                        .map(k => k + '=' + (card.getAttribute(k) || '')).join('|');
                    const sig = 'sig:' + [attrs, text, alt, Math.round(rect.width), Math.round(rect.height)].join('|');
                    const observed = !!(state && ((state.nodes && state.nodes.has(el)) || (state.changed && state.changed.has(el))));
                    out.push({ index, src, sig, alt, className, card_text: text, is_video_thumbnail: isVideoThumbnail,
                        observed, loaded: !!(el.complete && el.naturalWidth > 0),
                        x: rect.x, y: rect.y, width: rect.width, height: rect.height });
                }
                return out;
            }""")
            return value if isinstance(value, list) else []
        except Exception as exc:
            log.debug("[Flow Image DOM] Ошибка чтения набора image-кандидатов: %s", exc)
            return []

    async def _resolve_gallery_image_candidate(self, item: dict[str, Any]):
        """Resolve the exact scanned gallery image instead of trusting a stale DOM index.

        The Flow Agent sidebar can insert a second IMG between scanning and clicking.
        Matching the URL and approximate geometry avoids returning that sidebar copy.
        """
        expected_src = str(item.get("src", "")).strip()
        try:
            expected_x = float(item.get("x", 0))
            expected_y = float(item.get("y", 0))
        except Exception:
            expected_x = expected_y = 0.0
        if not expected_src:
            return None

        images = self.page.locator("img")
        try:
            count = await images.count()
        except Exception:
            return None

        matches = []
        for i in range(count):
            img = images.nth(i)
            try:
                if not await img.is_visible():
                    continue
                state = await img.evaluate("""el => ({
                    src: String(el.currentSrc || el.src || el.getAttribute('src') || el.getAttribute('data-src') || '').trim(),
                    complete: !!el.complete,
                    naturalWidth: Number(el.naturalWidth || 0)
                })""")
                if not state.get("src") or state.get("src") != expected_src:
                    continue
                if not state.get("complete") or int(state.get("naturalWidth", 0)) <= 0:
                    continue
                box = await img.bounding_box()
                if not box:
                    continue
                # Same gallery-only limits used by the scanner. In particular, never
                # return the small image preview embedded in the right-side Agent chat.
                if (float(box["x"]) > min(650, float(await self.page.evaluate("() => window.innerWidth || 1020")) * 0.64)
                        or float(box["width"]) < 200 or float(box["height"]) < 100):
                    continue
                distance = abs(float(box["x"]) - expected_x) + abs(float(box["y"]) - expected_y)
                matches.append((distance, img, box))
            except Exception:
                continue

        if not matches:
            return None
        matches.sort(key=lambda entry: entry[0])
        return matches[0][1]


    async def snapshot_image_tiles(self) -> set[str]:
        """Snapshot image URLs/signatures from Flow's left media gallery only."""
        result: set[str] = set()
        for item in await self._scan_visible_image_candidates():
            src = str(item.get("src", "")).strip()
            sig = str(item.get("sig", "")).strip()
            if src:
                result.add(src)
            if sig:
                result.add(sig)
        return result

    async def find_new_image_tile(
        self,
        old_srcs: set[str],
        timeout: int = FLOW_IMAGE_GENERATION_TIMEOUT,
        baseline_body: str = "",
        reference_images: list[Path] | None = None,
    ):
        started = time.time()
        last_report = 0
        # The feed's scroll position is intentionally preserved before submission.
        # Do NOT scroll containers globally here or after Generate: Flow virtualizes
        # older cards and this can move the fresh tile away from its download controls.

        while time.time() - started < timeout:
            # Explicit errors/quota are terminal for this attempt; inspect them each poll.
            await self.check_for_block_or_quota()
            await self.check_for_generation_rejection(baseline_body, "image")
            candidates = []
            dom_items = await self._scan_visible_image_candidates()
            for item in dom_items:
                src = str(item.get("src", "")).strip()
                sig = str(item.get("sig", "")).strip()
                if not src:
                    continue
                observed = bool(item.get("observed"))
                new_src = src not in old_srcs
                new_sig = bool(sig and sig not in old_srcs)
                # A new URL is strongest evidence. Also accept a newly inserted/updated
                # large image node even when Flow reuses its URL and keeps the surrounding
                # card signature unchanged; the observer is armed only after reference
                # attachment and the baseline snapshot, immediately before Generate.
                if not (new_src or observed or (new_sig and observed)):
                    continue
                if not item.get("loaded"):
                    log.debug("[Flow Image Observer] Новый image DOM найден, жду загрузку изображения: %s", src[:90])
                    continue
                score = (4 if observed else 0) + (2 if new_src else 0) + (1 if new_sig else 0)
                candidates.append((score, float(item.get("y", 0)), int(item.get("index", 0)), item))
            if candidates:
                # Prefer an observed new DOM node, then the lowest/newest card in the feed.
                candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
                score, _y, image_index, item = candidates[0]
                log.info(
                    "✅ [Flow] Новое изображение распознано: x=%.0f y=%.0f w=%.0f h=%.0f | observed=%s new_src=%s | %s",
                    item.get("x", 0), item.get("y", 0), item.get("width", 0), item.get("height", 0),
                    item.get("observed", False), str(item.get("src", "")) not in old_srcs, str(item.get("src", ""))[:100],
                )
                # Resolve the exact image by currentSrc + geometry. Do not use nth(index):
                # Flow can insert the Agent-side preview between the scan and this click,
                # shifting DOM indices and causing the download action to target the wrong UI.
                tile = await self._resolve_gallery_image_candidate(item)
                if tile is not None:
                    try:
                        await self.page.evaluate("window.__flowImageGenObserver?.disconnect()")
                    except Exception:
                        pass
                    return tile
                log.debug(
                    "[Flow Image DOM] Кандидат исчез/переместился до разрешения точного locator; повторяю скан.")

            elapsed = int(time.time() - started)
            if elapsed - last_report >= 12:
                last_report = elapsed
                body = re.sub(r"\s+", " ", await self.body_text())
                log.info(
                    "[Flow] ждём новое изображение %ss / %ss; видимых DOM-кандидатов: %s, совпадений с новыми результатами: %s; текст: %s",
                    elapsed, timeout, len(dom_items), len(candidates), body[-180:],
                )
            await asyncio.sleep(1.5)

        raise FlowTechnicalError(f"Новое изображение не появилось за {timeout}s")

    async def find_download_label(self):
        labels = self.page.locator("span.label")
        for i in range(await labels.count()):
            item = labels.nth(i)
            try:
                if not await item.is_visible():
                    continue
                value = (await item.inner_text()).strip().lower()
                if value in {"скачать", "download"}:
                    return item
            except Exception:
                pass
        return None

    async def _right_click_exact_tile(self, tile) -> Any | None:
        """Open Flow's download menu on the result or its nearby card wrapper.

        A <video> element can be present and playable while right-clicking the
        child itself opens the browser/player menu rather than Flow's menu. Try
        the exact result first, then only a few visible, sensibly-sized ancestors.
        """
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass

        try:
            tile_tag = await tile.evaluate("el => String(el.tagName || '').toLowerCase()")
        except Exception:
            tile_tag = ""

        candidates = [tile]
        parent = tile
        for _ in range(4):
            try:
                parent = parent.locator("xpath=..")
                candidates.append(parent)
            except Exception:
                break

        seen_boxes: set[tuple[int, int, int, int]] = set()
        for index, candidate in enumerate(candidates):
            try:
                if not await candidate.is_visible():
                    continue
                box = await candidate.bounding_box()
                if not box:
                    continue
                width, height = float(box["width"]), float(box["height"])
                # Images must belong to the left All media gallery. A small copy in the
                # Agent sidebar opens Chrome's native "open/save image" menu instead.
                if tile_tag == "img" and (
                    float(box["x"]) > min(650, float(await self.page.evaluate("() => window.innerWidth || 1020")) * 0.64)
                    or width < 200 or height < 100
                ):
                    log.debug(
                        "[Flow Download] Пропускаю IMG вне галереи/слишком маленький: x=%.0f y=%.0f w=%.0f h=%.0f",
                        box["x"], box["y"], width, height,
                    )
                    continue
                # Skip tiny controls and whole-page wrappers; only right-click the
                # media itself or a local result-card wrapper.
                if width < 180 or height < 90 or width > 1000 or height > 750:
                    continue
                key = (round(box["x"]), round(box["y"]), round(width), round(height))
                if key in seen_boxes:
                    continue
                seen_boxes.add(key)
                try:
                    await candidate.scroll_into_view_if_needed(timeout=3_000)
                except Exception:
                    pass
                await candidate.click(button="right", timeout=4_000, force=True)
                await asyncio.sleep(0.45)
                download = await self.find_download_label()
                if download is not None:
                    if index:
                        log.info("[Flow Download] Меню «Скачать» открыто через контейнер результата (ancestor=%s).", index)
                    return download
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
            except Exception as exc:
                log.debug("[Flow] Right-click кандидата %s не открыл меню: %s", index, exc)
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
        return None

    async def wait_output_ready(
        self, tile, timeout: int = FLOW_GENERATION_TIMEOUT,
        baseline_body: str = "", kind: str = "video",
    ):
        started = time.time()
        last_report = 0
        attempt = 0
        await asyncio.sleep(1.0)

        while time.time() - started < timeout:
            # Flow sometimes adds an error card after a thumbnail appears. Keep
            # monitoring during this phase too, otherwise we wait for a download
            # menu forever and never enter the Gemini policy-rewrite path.
            await self.check_for_generation_rejection(
                baseline_body,
                kind,
                allow_audio_failure_if_media_found=(kind == "video"),
                media_found=True,
            )
            await self.check_for_block_or_quota()
            attempt += 1

            download = await self._right_click_exact_tile(tile)
            if download is not None:
                log.info("[Flow] Output полностью готов: найдено меню «Скачать»")
                return download

            elapsed = int(time.time() - started)
            if elapsed - last_report >= 10:
                last_report = elapsed
                log.info("[Flow] Медиа ещё готовится: %ss (проверка %s)", elapsed, attempt)

            await asyncio.sleep(3)

        raise FlowTechnicalError(f"Вывод не стал доступен для скачивания за {timeout}s")

    async def open_download_menu(
        self, tile, timeout: int = FLOW_GENERATION_TIMEOUT,
        baseline_body: str = "", kind: str = "video",
    ):
        return await self.wait_output_ready(
            tile, timeout=timeout, baseline_body=baseline_body, kind=kind,
        )

    async def find_submenu_item(self, label: str, caption: str | None = None):
        items = self.page.locator("span.item-text")
        caption_aliases = {
            "upscaled": {"upscaled", "повышенное разрешение"},
            "повышенное разрешение": {"upscaled", "повышенное разрешение"},
        }

        wanted_caption = None
        if caption:
            wanted_caption = caption_aliases.get(
                caption.strip().lower(),
                {caption.strip().lower()},
            )

        for i in range(await items.count()):
            item = items.nth(i)
            try:
                if not await item.is_visible():
                    continue

                label_node = item.locator("span.label").first
                caption_node = item.locator("span.caption").first

                if not await label_node.is_visible():
                    continue

                item_label = (await label_node.inner_text()).strip().lower()
                if item_label != label.strip().lower():
                    continue

                if wanted_caption is not None:
                    item_caption = (
                        (await caption_node.inner_text()).strip().lower()
                        if await caption_node.is_visible()
                        else ""
                    )
                    if not any(alias in item_caption for alias in wanted_caption):
                        continue

                return item
            except Exception:
                pass

        return None

    async def wait_download_from_click(self, action, timeout: int = FLOW_DOWNLOAD_TIMEOUT):
        """Ожидает загрузку файла Playwright при клике на элемент."""
        async with self.page.expect_download(timeout=timeout * 1000) as download_info:
            if callable(action):
                res = action()
                if asyncio.iscoroutine(res):
                    await res
            elif asyncio.iscoroutine(action):
                await action
        return await download_info.value

    async def _generate_ready_tile_with_retry(
        self, kind: str, prompt: str, attempt_limit: int | None = None,
        reference_images: list[Path] | None = None,
    ):
        """Retry technical stalls and rewrite refused prompts into safer alternatives."""
        if kind == "video":
            max_generate_wait = FLOW_VIDEO_GENERATION_TIMEOUT
            max_ready_wait = FLOW_VIDEO_OUTPUT_READY_TIMEOUT
            finder = self.find_new_video_tile
            snapshot = self.snapshot_video_tiles
            mode = "Video"
        elif kind == "image":
            max_generate_wait = FLOW_IMAGE_GENERATION_TIMEOUT
            max_ready_wait = FLOW_IMAGE_OUTPUT_READY_TIMEOUT
            finder = self.find_new_image_tile
            snapshot = self.snapshot_image_tiles
            mode = "Image"
        else:
            raise ValueError(kind)

        # Remember whether this is an intro prompt. Gemini/Flow may rewrite it after
        # a rejection, but the silent-mouth visual restriction must survive every retry.
        intro_silent_visual = (
            kind == "video"
            and FLOW_INTRO_SILENT_VISUAL_POLICY_MARKER.casefold() in str(prompt or "").casefold()
        )
        configured_attempts = FLOW_GENERATION_RETRIES_PER_PROFILE + 1
        standard_attempt_limit = max(1, min(configured_attempts, int(attempt_limit or configured_attempts)))
        prompt_submission_limit = standard_attempt_limit
        last_error = None
        self._last_prompt_attempts = 0
        self._last_effective_prompt = prompt
        attempt = 0
        attempts_for_current_prompt = 0
        policy_rewrites_used = 0
        video_wrong_media_retry_used = False
        # One cookie-clear/reload/History-session recovery is allowed for this media target.
        # It covers both generic Flow errors and the new localized request-rate warning.
        self._flow_generic_error_recovery_used = False

        while attempt < prompt_submission_limit:
            # Final submission boundary: enforce the selected style even when an old
            # checkpoint or Gemini policy rewrite contains conflicting style wording.
            prompt = enforce_selected_visual_style(prompt, kind)
            if kind == "video":
                # Make the requested media type explicit at the final submission boundary,
                # including cached/prompts restored from old checkpoints.
                prompt = enforce_flow_video_output(prompt)
                # All video prompts receive the short ambience-only directive too.
                prompt = enforce_flow_ambience_only_audio(prompt)
                if intro_silent_visual:
                    # Keep this as the last intro-specific rule at the actual submit boundary.
                    prompt = enforce_flow_intro_silent_visual_policy(prompt)
            self._last_effective_prompt = prompt
            attempt += 1
            attempts_for_current_prompt += 1
            candidate_tile = None
            try:
                await self.check_for_block_or_quota()
                await self.ensure_mode(mode)
                baseline_body = await self.body_text()
                log.info(
                    "🔁 [Flow] %s: отправка промпта, попытка %s/%s в профиле %s%s",
                    kind.upper(), attempt, prompt_submission_limit,
                    (self.profile or {}).get("name", "unknown"),
                    f" (безопасная переработка {policy_rewrites_used})" if policy_rewrites_used else "",
                )
                # Set text first; some Flow builds keep ingredient chips beside the editor,
                # while others integrate them into the prompt composer. Upload afterwards so
                # clearing/replacing editor text can never erase a newly attached reference.
                await self.set_prompt(prompt)
                # Flow's Agent accepts uploaded image assets as video reference inputs too.
                # Attach only references selected for this specific shot.
                if reference_images:
                    await self.attach_reference_images(reference_images)
                    if await self._asset_picker_is_open():
                        log.error(
                            "[Flow Reference] Insert всё ещё открыт после прикрепления. Генерацию не отправляю; закрываю меню и повторяю тот же промпт."
                        )
                        try:
                            await self.page.keyboard.press("Escape")
                            await asyncio.sleep(0.35)
                        except Exception:
                            pass
                        raise FlowTechnicalError(
                            "Окно Insert осталось открытым после выбора референса; промпт не отправлен, требуется повторная попытка."
                        )
                # Normalize the result feed before snapshot/observer so scrolling lazy-loads
                # do not masquerade as new generation results after Generate is pressed.
                old_srcs: set[str] = set()
                old_image_srcs: set[str] = set()
                if kind == "image":
                    await self.normalize_flow_gallery_scroll()
                    previous_snapshot: set[str] | None = None
                    stable_rounds = 0
                    snapshot_started = time.monotonic()
                    minimum_settle = 2.0 if reference_images else 0.5
                    snapshot_deadline = snapshot_started + (4.0 if reference_images else 1.2)
                    while time.monotonic() < snapshot_deadline:
                        current_snapshot = await snapshot()
                        old_srcs.update(current_snapshot)
                        if previous_snapshot is not None and current_snapshot == previous_snapshot:
                            stable_rounds += 1
                        else:
                            stable_rounds = 0
                        previous_snapshot = current_snapshot
                        settled_long_enough = time.monotonic() - snapshot_started >= minimum_settle
                        if stable_rounds >= 2 and settled_long_enough:
                            break
                        await asyncio.sleep(0.35)
                else:
                    # For video submissions also snapshot image tiles. If Flow returns a
                    # new raster image, we can recognize the wrong media type immediately.
                    old_image_srcs = await self.snapshot_image_tiles()
                    old_srcs = await snapshot()
                log.debug(
                    "[Flow] Snapshot перед генерацией (%s): %s существующих video/image URL/DOM-подписей; reference-tiles исключены",
                    kind, len(old_srcs),
                )
                if kind in {"image", "video"}:
                    await self.arm_image_generation_observer()
                # Start the generation watchdog only after reference attachment and
                # the pre-generation snapshot. Attaching two references can take a
                # long time; it must not consume the time budget for generation or
                # output readiness and trigger a duplicate generation of a result
                # that already appeared.
                attempt_started = time.monotonic()
                log.info("[Flow Timing] Таймер генерации %s запущен после подготовки промпта и эталонов", kind)
                await self.generate_start()
                self._last_prompt_attempts = attempt
                if kind == "image":
                    tile = await finder(
                        old_srcs,
                        timeout=max_generate_wait,
                        baseline_body=baseline_body,
                        reference_images=reference_images,
                    )
                else:
                    tile = await finder(
                        old_srcs,
                        timeout=max_generate_wait,
                        baseline_body=baseline_body,
                        old_image_srcs=old_image_srcs,
                    )
                candidate_tile = tile
                # Generation and output readiness share the original timeout budget.
                remaining_budget = max_generate_wait - (time.monotonic() - attempt_started)
                ready_timeout = min(max_ready_wait, int(remaining_budget))
                if ready_timeout < 1:
                    raise FlowTechnicalError(
                        f"Общий таймер {max_generate_wait}s исчерпан до готовности меню скачивания"
                    )
                download_label = await self.open_download_menu(
                    tile, timeout=ready_timeout, baseline_body=baseline_body, kind=kind,
                )
                elapsed = time.monotonic() - attempt_started
                log.info(
                    "✅ [Flow] %s: результат готов за %.1fs (лимит %ss)",
                    kind.upper(), elapsed, max_generate_wait,
                )
                return tile, download_label

            except (FlowPolicyBlockError, FlowQuotaError):
                raise
            except FlowWrongMediaKindError as e:
                last_error = e
                # Do not wait out the video timeout and do not blindly submit the same
                # still-image-style prompt again. Retry once with a stronger video directive.
                if not video_wrong_media_retry_used:
                    video_wrong_media_retry_used = True
                    prompt = enforce_flow_video_output(prompt, retry_after_image=True)
                    if intro_silent_visual:
                        prompt = enforce_flow_intro_silent_visual_policy(prompt)
                    self._last_effective_prompt = prompt
                    log.warning(
                        "⚠️ [Flow Media Type] Flow returned an image for a video request. "
                        "Generation wait stopped immediately; %s/ %s submission attempts used. "
                        "Retrying once with explicit motion/video requirement.",
                        attempts_for_current_prompt, standard_attempt_limit,
                    )
                    try:
                        await self.page.keyboard.press("Escape")
                    except Exception:
                        pass
                    if attempts_for_current_prompt < standard_attempt_limit:
                        await asyncio.sleep(0.6)
                        continue
                    log.warning(
                        "[Flow Media Type] No same-profile retry budget remains; the stronger video prompt "
                        "will be carried to the next eligible profile."
                    )
                else:
                    log.warning(
                        "[Flow Media Type] Second image-only result in this profile; abandoning this profile "
                        "for this target and carrying the explicit video prompt to another profile."
                    )
                break
            except FlowGenerationRejectedError as e:
                last_error = e
                if attempt >= FLOW_POLICY_REWRITE_MAX_ATTEMPTS:
                    log.error(
                        "❌ [Flow Policy Rewrite] Исчерпан лимит %s отправок промпта для %s; последний отказ: %s",
                        FLOW_POLICY_REWRITE_MAX_ATTEMPTS, kind, str(e)[:220],
                    )
                    raise FlowTechnicalError(
                        f"Flow {kind}: после {attempt} разных/повторных промптов Flow всё ещё отказывает по политике"
                    ) from e

                policy_rewrites_used += 1
                refusal_body = re.sub(r"\s+", " ", await self.body_text()).strip()[-1400:]
                log.warning(
                    "⚠️ [Flow Policy Rewrite] Flow отклонил %s по политике (попытка %s). "
                    "Прошу Gemini безопасно упростить/переформулировать сцену; предел — %s отправок промпта.",
                    kind, attempt, FLOW_POLICY_REWRITE_MAX_ATTEMPTS,
                )
                try:
                    rewritten_prompt = await asyncio.to_thread(
                        rewrite_flow_prompt_after_policy_refusal,
                        prompt, refusal_body, kind, policy_rewrites_used,
                    )
                except Exception as rewrite_error:
                    log.warning(
                        "[Flow Policy Rewrite] Ошибка Gemini при переделке промпта: %s; перехожу к безопасному шаблону.",
                        str(rewrite_error)[:200],
                    )
                    rewritten_prompt = make_policy_safe_fallback_prompt(prompt, kind, policy_rewrites_used)

                if not rewritten_prompt or re.sub(r"\s+", " ", rewritten_prompt).strip() == re.sub(r"\s+", " ", prompt).strip():
                    rewritten_prompt = make_policy_safe_fallback_prompt(prompt, kind, policy_rewrites_used)
                prompt = rewritten_prompt
                if intro_silent_visual:
                    prompt = enforce_flow_intro_silent_visual_policy(prompt)
                self._last_effective_prompt = prompt
                prompt_submission_limit = FLOW_POLICY_REWRITE_MAX_ATTEMPTS
                attempts_for_current_prompt = 0
                video_wrong_media_retry_used = False
                log.info(
                    "[Flow Policy Rewrite] Подготовлен новый безопасный промпт #%s (%s символов). "
                    "Повторяю генерацию в том же профиле.",
                    policy_rewrites_used, len(prompt),
                )
                await asyncio.sleep(0.8)
                continue

            except FlowAudioGenerationError as e:
                # This is not a normal transient failure. Do not resend the same
                # sound-bearing prompt in the remaining technical retry slot; let
                # generate_flow_with_failover run the one bounded silent rewrite.
                last_error = e
                log.warning(
                    "[Flow Audio Failure] Обнаружен сбой аудиогенерации. Останавливаю ожидание и не повторяю тот же prompt; "
                    "передаю задачу в единственный silent-rewrite fallback."
                )
                raise
            except Exception as e:
                last_error = e
                if candidate_tile is not None:
                    log.warning(
                        "[Flow Media Recovery] %s: результат уже найден в DOM, но следующий этап не завершился (%s). Повторно проверяю меню скачивания того же результата без повторной генерации.",
                        kind.upper(), str(e)[:180],
                    )
                    try:
                        recovered_menu = await self.open_download_menu(
                            candidate_tile,
                            timeout=max(30, min(60, max_ready_wait)),
                            baseline_body=baseline_body,
                            kind=kind,
                        )
                        log.info("✅ [Flow Media Recovery] Готовый %s восстановлен без повторной отправки промпта.", kind.upper())
                        return candidate_tile, recovered_menu
                    except Exception as menu_recovery_exc:
                        log.warning(
                            "[Flow Media Recovery] Не удалось восстановить меню того же результата: %s; только после этого рассматриваю повторную генерацию.",
                            str(menu_recovery_exc)[:180],
                        )
                # Before sending the same image prompt again, give Flow a short settling
                # window and re-scan its existing DOM. This avoids duplicate generations
                # when the image tile appeared but the gallery reflow/lazy-load hid it from
                # the first scan. A recovered tile is downloaded without another prompt.
                if kind == "image" and "Новое изображение не появилось" in str(e):
                    log.warning(
                        "[Flow Image Recovery] Таймер истёк без распознанной картинки. Не отправляю повторный промпт сразу: жду %ss и повторно сканирую уже открытую страницу.",
                        FLOW_IMAGE_PRE_RETRY_GRACE_SECONDS,
                    )
                    await asyncio.sleep(FLOW_IMAGE_PRE_RETRY_GRACE_SECONDS)
                    try:
                        recovered_tile = await self.find_new_image_tile(
                            old_srcs,
                            timeout=FLOW_IMAGE_PRE_RETRY_SCAN_SECONDS,
                            baseline_body=baseline_body,
                            reference_images=reference_images,
                        )
                        recovery_menu = await self.open_download_menu(
                            recovered_tile,
                            timeout=max(20, min(45, FLOW_IMAGE_OUTPUT_READY_TIMEOUT)),
                            baseline_body=baseline_body,
                            kind="image",
                        )
                        self._last_prompt_attempts = attempt
                        log.warning(
                            "✅ [Flow Image Recovery] Уже созданное изображение найдено при повторном сканировании. Повторно промпт не отправлялся."
                        )
                        return recovered_tile, recovery_menu
                    except Exception as recovery_exc:
                        log.info(
                            "[Flow Image Recovery] Повторный скан не нашёл готовый результат: %s; теперь разрешено повторить промпт.",
                            str(recovery_exc)[:180],
                        )
                body = re.sub(r"\s+", " ", await self.body_text())[-220:]
                if attempts_for_current_prompt >= standard_attempt_limit:
                    log.error(
                        "❌ [Flow] %s не готов после %s технических попыток для текущего промпта "
                        "(всего отправок/попыток=%s, переработок политики=%s) | error=%s | page=%s",
                        kind.upper(), standard_attempt_limit, attempt, policy_rewrites_used,
                        str(e)[:240], body,
                    )
                    break
                log.warning(
                    "⚠️ [Flow] %s не появился/не стал готов: %s. Повторяю тот же текущий промпт "
                    "(техническая попытка %s/%s; всего %s/%s). page=%s",
                    kind.upper(), str(e)[:220], attempts_for_current_prompt + 1,
                    standard_attempt_limit, attempt + 1, prompt_submission_limit, body,
                )
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
                await asyncio.sleep(2)

        raise FlowTechnicalError(
            f"Flow {kind}: результат не появился после {attempt} попыток в этом профиле "
            f"({policy_rewrites_used} безопасных переработок промпта); последний сбой: {last_error}"
        ) from last_error

    async def generate_video_to_file(
        self, prompt: str, final_path: Path, attempt_limit: int | None = None,
        reference_images: list[Path] | None = None,
    ) -> Path:
        """Generate a Flow video; prefer the upscaled download, then fall back to native.

        The 1080p/upscaled option is tried first and gets the configured 100-second
        window to start a browser download. If Flow does not start it in time, reopen
        the tile menu and fall back to native/original 720p without regenerating the
        video or spending another generation attempt.
        """
        await self.check_for_block_or_quota()
        tile, download = await self._generate_ready_tile_with_retry(
            "video", prompt, attempt_limit=attempt_limit, reference_images=reference_images
        )

        async def reopen_menu():
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            menu_button = await self._right_click_exact_tile(tile)
            if menu_button is None:
                raise FlowTechnicalError("Не удалось повторно открыть меню скачивания готового видео в Flow")
            await menu_button.hover()
            await asyncio.sleep(0.8)
            return menu_button

        await download.hover()
        await asyncio.sleep(0.8)

        # Priority 1: server-side upscale / highest available resolution.
        upscale_options = (
            ("1080p", "Повышенное разрешение"),
            ("1080p", "upscaled"),
            ("2K", "Повышенное разрешение"),
            ("2K", "upscaled"),
        )
        chosen = None
        chosen_label = ""
        for label, caption in upscale_options:
            chosen = await self.find_submenu_item(label, caption)
            if chosen is not None:
                chosen_label = f"{label} (апскейл)"
                break

        # Some Flow layouts label the upscale item differently, so look for the
        # resolution label itself before falling back to native/original quality.
        if chosen is None:
            for label in ("1080p", "2K"):
                chosen = await self.find_submenu_item(label)
                if chosen is not None:
                    chosen_label = f"{label} (высокое разрешение)"
                    break

        high_res_error = None
        if chosen is not None:
            ensure_directory(final_path.parent)
            raw_tmp = final_path.with_suffix(".raw.part.mp4")
            high_res_timeout = FLOW_UPSCALE_DOWNLOAD_TIMEOUT
            log.info(
                "[Flow Download] Сначала пробую скачать с апскейлом: '%s'; ожидание до %ss.",
                chosen_label, high_res_timeout,
            )
            try:
                dl = await self.wait_download_from_click(
                    lambda: chosen.click(timeout=10_000), timeout=high_res_timeout,
                )
                await dl.save_as(str(raw_tmp))
                if not is_valid_video(raw_tmp):
                    raise FlowTechnicalError(f"Скачанный апскейленный MP4 невалиден: {raw_tmp}")
                raw_tmp.replace(final_path)
                log.info(
                    "[Flow Download] ✅ Видео скачано с апскейлом (%s): %s | %.2fs",
                    chosen_label, final_path, get_duration(final_path) or 0.0,
                )
                return final_path
            except PlaywrightTimeoutError as exc:
                high_res_error = exc
                log.warning(
                    "[Flow Download] Апскейл '%s' не вызвал событие загрузки за %ss; "
                    "не генерирую видео заново, открываю меню для запасной загрузки. ",
                    chosen_label, high_res_timeout,
                )
            except FlowTechnicalError:
                raise

        # Priority 2: native/original download if upscale is missing or timed out.
        download = await reopen_menu()
        native_options = (
            ("720p", None),
            ("Оригинальный размер", None),
            ("Original size", None),
            ("Original", None),
            ("Оригинал", None),
        )
        chosen = None
        chosen_label = ""
        for label, caption in native_options:
            chosen = await self.find_submenu_item(label, caption)
            if chosen is not None:
                chosen_label = label
                break

        if chosen is None:
            visible_items = self.page.locator("span.item-text")
            labels = []
            for i in range(min(await visible_items.count(), 30)):
                item = visible_items.nth(i)
                try:
                    if await item.is_visible():
                        labels.append(re.sub(r"\s+", " ", (await item.inner_text()).strip())[:100])
                except Exception:
                    pass
            detail = f"; апскейл ранее не вызвал загрузку: {type(high_res_error).__name__}" if high_res_error else ""
            raise FlowTechnicalError(
                "Flow video готов, но не найден пункт прямой загрузки Original/720p после попытки апскейла"
                f"{detail}. Видимые пункты меню: {labels[:15]}"
            )

        raw_tmp = final_path.with_suffix(".raw.part.mp4")
        log.info("[Flow Download] Запасной вариант после апскейла: '%s' (timeout=%ss).", chosen_label, FLOW_DOWNLOAD_TIMEOUT)
        try:
            dl = await self.wait_download_from_click(
                lambda: chosen.click(timeout=10_000), timeout=FLOW_DOWNLOAD_TIMEOUT,
            )
        except PlaywrightTimeoutError as exc:
            body = re.sub(r"\s+", " ", await self.body_text()).strip()[-700:]
            detail = f"; апскейл ранее не вызвал загрузку за {FLOW_UPSCALE_DOWNLOAD_TIMEOUT}s: {type(high_res_error).__name__}" if high_res_error else ""
            raise FlowTechnicalError(
                f"Flow создал видео, но загрузка '{chosen_label}' не началась за {FLOW_DOWNLOAD_TIMEOUT}s{detail}. "
                f"Видео в Flow не удалялось; текст страницы: {body}"
            ) from exc

        await dl.save_as(str(raw_tmp))
        if not is_valid_video(raw_tmp):
            raise FlowTechnicalError(f"Скачанный Flow MP4 невалиден: {raw_tmp}")
        raw_tmp.replace(final_path)
        log.info(
            "[Flow Download] ✅ Видео скачано запасным вариантом (%s): %s | %.2fs",
            chosen_label, final_path, get_duration(final_path) or 0.0,
        )
        return final_path

    async def generate_image_to_file(
        self, prompt: str, final_path: Path, attempt_limit: int | None = None,
        reference_images: list[Path] | None = None,
    ) -> Path:
        """Download a generated image at high resolution when available.

        Try Flow's 2K/1080p upscale first. Unlike the previous implementation,
        the upscale download is bounded by FLOW_UPSCALE_DOWNLOAD_TIMEOUT. If the
        browser never emits a download event, reopen this same result's menu and
        download its native/original image instead. The image is NOT regenerated
        and no additional Flow generation quota is spent by this fallback.
        """
        await self.check_for_block_or_quota()
        tile, download = await self._generate_ready_tile_with_retry(
            "image", prompt, attempt_limit=attempt_limit, reference_images=reference_images
        )

        async def reopen_menu():
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            menu_button = await self._right_click_exact_tile(tile)
            if menu_button is None:
                raise FlowTechnicalError(
                    "Не удалось повторно открыть меню скачивания уже готового изображения в Flow"
                )
            await menu_button.hover()
            await asyncio.sleep(0.6)
            return menu_button

        async def save_image_download(dl, quality_label: str) -> Path:
            raw_tmp = final_path.with_suffix(".raw.part.jpg")
            try:
                raw_tmp.unlink(missing_ok=True)
            except Exception:
                pass
            await dl.save_as(str(raw_tmp))
            if not is_valid_image(raw_tmp):
                raise FlowTechnicalError(
                    f"Скачанное изображение Flow невалидно ({quality_label}): {raw_tmp}"
                )
            raw_tmp.replace(final_path)
            log.info(
                "[Flow Download] ✅ Изображение скачано (%s): %s",
                quality_label, final_path,
            )
            return final_path

        await download.hover()
        await asyncio.sleep(0.6)

        # Priority 1: Flow server-side upscale. Do not include Original here:
        # it must go through the native fallback path without an upscale timeout.
        upscale_options = (
            ("2K", "Повышенное разрешение"),
            ("2K", "upscaled"),
            ("1080p", "Повышенное разрешение"),
            ("1080p", "upscaled"),
        )
        upscaled = None
        upscale_label = ""
        for label, caption in upscale_options:
            upscaled = await self.find_submenu_item(label, caption)
            if upscaled is not None:
                upscale_label = f"{label} (апскейл)"
                break

        # Some Flow layouts show the resolution label without a caption.
        if upscaled is None:
            for label in ("2K", "1080p"):
                upscaled = await self.find_submenu_item(label)
                if upscaled is not None:
                    upscale_label = f"{label} (высокое разрешение)"
                    break

        high_res_error = None
        ensure_directory(final_path.parent)
        if upscaled is not None:
            log.info(
                "[Flow Download] Сначала пробую скачать изображение с апскейлом: '%s'; ожидание до %ss.",
                upscale_label, FLOW_UPSCALE_DOWNLOAD_TIMEOUT,
            )
            try:
                dl = await self.wait_download_from_click(
                    lambda: upscaled.click(timeout=10_000),
                    timeout=FLOW_UPSCALE_DOWNLOAD_TIMEOUT,
                )
                return await save_image_download(dl, upscale_label)
            except PlaywrightTimeoutError as exc:
                high_res_error = exc
                log.warning(
                    "[Flow Download] Апскейл изображения '%s' не начал загрузку за %ss; "
                    "не генерирую изображение заново, переключаюсь на Original/720p.",
                    upscale_label, FLOW_UPSCALE_DOWNLOAD_TIMEOUT,
                )

        # Priority 2: the same image at native/original resolution.
        download = await reopen_menu()
        native_options = (
            ("720p", None),
            ("Оригинальный размер", None),
            ("Original size", None),
            ("Original", None),
            ("Оригинал", None),
        )
        native = None
        native_label = ""
        for label, caption in native_options:
            native = await self.find_submenu_item(label, caption)
            if native is not None:
                native_label = label
                break

        # In some Flow versions the main Download row itself downloads the
        # original image; in others it opens a resolution submenu. Preserve both
        # behaviours and only wait briefly before scanning the submenu again.
        if native is None:
            log.warning(
                "[Flow Download] Не найден пункт Original/720p; пробую прямую кнопку 'Скачать' для исходного изображения."
            )
            try:
                dl = await self.wait_download_from_click(
                    lambda: download.click(timeout=10_000), timeout=12,
                )
                return await save_image_download(dl, "Original (прямая загрузка)")
            except PlaywrightTimeoutError:
                await asyncio.sleep(0.6)
                native = None
                for label, caption in native_options:
                    native = await self.find_submenu_item(label, caption)
                    if native is not None:
                        native_label = label
                        break

        if native is None:
            visible_items = self.page.locator("span.item-text")
            labels = []
            for i in range(min(await visible_items.count(), 30)):
                item = visible_items.nth(i)
                try:
                    if await item.is_visible():
                        labels.append(re.sub(r"\s+", " ", (await item.inner_text()).strip())[:100])
                except Exception:
                    pass
            detail = (
                f"; апскейл не начал загрузку за {FLOW_UPSCALE_DOWNLOAD_TIMEOUT}s"
                if high_res_error else ""
            )
            raise FlowTechnicalError(
                "Flow создал изображение, но не найден пункт прямой загрузки Original/720p"
                f"{detail}. Видимые пункты меню: {labels[:15]}"
            )

        log.info(
            "[Flow Download] Запасной вариант после апскейла: изображение '%s' (timeout=%ss).",
            native_label, FLOW_DOWNLOAD_TIMEOUT,
        )
        try:
            dl = await self.wait_download_from_click(
                lambda: native.click(timeout=10_000), timeout=FLOW_DOWNLOAD_TIMEOUT,
            )
        except PlaywrightTimeoutError as exc:
            body = re.sub(r"\s+", " ", await self.body_text()).strip()[-700:]
            detail = (
                f"; апскейл ранее не начал загрузку за {FLOW_UPSCALE_DOWNLOAD_TIMEOUT}s"
                if high_res_error else ""
            )
            raise FlowTechnicalError(
                f"Flow создал изображение, но загрузка '{native_label}' не началась за "
                f"{FLOW_DOWNLOAD_TIMEOUT}s{detail}. Изображение в Flow не удалялось; "
                f"текст страницы: {body}"
            ) from exc

        return await save_image_download(dl, f"{native_label} (запасная загрузка)")



# ============================================================================
# FLOW GENERATION WITH CHECKPOINT / ACCOUNT FAILOVER
# ============================================================================


def rewrite_flow_prompt_after_audio_failure(
    original_prompt: str,
    failure_text: str = "",
    rewrite_index: int = 1,
) -> str:
    """Preserve the visible story but remove audio complexity after Flow audio failure.

    This is intentionally a single, bounded fallback. It does not try to evade a
    provider restriction; it asks Flow for a genuinely silent visual-only clip.
    """
    raw = str(original_prompt or "").strip()
    core = _flow_prompt_core(raw).strip() or raw[:5000]
    style_name = visual_style_label(ACTIVE_VISUAL_STYLE_ID)
    style_description = visual_style_description("video", ACTIVE_VISUAL_STYLE_ID)
    failure_excerpt = re.sub(r"\s+", " ", str(failure_text or "audio generation failed")).strip()[-700:]

    request = f"""You are simplifying a Google Flow video prompt after its audio-generation subsystem failed.
This is a technical reliability fallback, NOT a request to bypass safety or copyright rules.
Rewrite ONLY to simplify the sound/audio requirements while preserving the visible story.

Selected visual style: {style_name}.
Selected style description: {style_description}.
Observed error: {failure_excerpt}

Requirements:
- Preserve the same harmless subject, character identity, age, clothing, setting, era, visible action, and narrative meaning wherever possible.
- Preserve the selected visual medium and art style exactly; do not convert illustration/comic art into photorealism.
- Make it one simple, coherent 8-second visual shot with one ordinary visible action and restrained camera movement.
- Remove every request or description of sound: no ambience, environmental audio, sound effects, music, soundtrack, speech, dialogue, voice, narration, singing, humming, or whistling.
- Do not describe what anything sounds like. Do not ask Flow to synthesize, generate, or attach an audio track. The result must be a silent visual-only clip.
- Do not remove visible actions merely because they might normally make a sound. Show them silently.
- Do not add new plot events, new characters, brands, or copyrighted references.
- Keep the prompt concise (about 50-90 words). Return ONLY the rewritten visual prompt, without explanation.

CURRENT VISUAL PROMPT:
{core[:5000]}""".strip()

    rewritten = ""
    try:
        rewritten = call_text_api(request) or ""
    except Exception as exc:
        log.warning("[Flow Audio Rewrite] Текстовый провайдер не ответил: %s; использую локальный silent fallback.", str(exc)[:180])

    if rewritten:
        rewritten = re.sub(
            r"^(?:revised\s+prompt|rewritten\s+prompt|video\s+prompt|prompt)\s*:\s*",
            "", rewritten.strip(), flags=re.IGNORECASE,
        ).strip().strip('`').strip().strip('"').strip()
        old_norm = re.sub(r"\s+", " ", core).casefold()
        new_norm = re.sub(r"\s+", " ", _flow_prompt_core(rewritten)).casefold()
        if len(new_norm) >= 30 and new_norm != old_norm and difflib.SequenceMatcher(None, old_norm, new_norm).ratio() < 0.97:
            finalized = _finish_rewritten_flow_prompt(rewritten, original_prompt, "video")
            finalized = apply_youtube_content_safety_rules(finalized, "video")
            finalized = apply_visible_text_language_rule(finalized, _infer_prompt_text_language(original_prompt))
            finalized = enforce_selected_visual_style(finalized, "video")
            finalized = enforce_flow_video_output(finalized)
            finalized = enforce_flow_silent_video_audio(finalized)
            log.info(
                "[Flow Audio Rewrite] Подготовлен silent visual-only промпт #%s (%s символов); визуальный сюжет сохранён.",
                rewrite_index, len(finalized),
            )
            return finalized

    # Deterministic local fallback if Gemini/Groq is unavailable or returns a near-copy.
    # Remove standalone sound-focused sentences, but keep the visual prompt whenever
    # sentence splitting would remove too much useful narrative context.
    audio_terms = re.compile(
        r"\b(?:sound|audio|ambience|ambient|music|soundtrack|sound effects?|dialogue|speech|voice(?:over)?|"
        r"sing(?:ing)?|humming|whistling|noise|sonido|audio|ambiente|musica|dialogo|voz|canto|ruido|"
        r"звук\w*|аудио|музык\w*|шум\w*|голос\w*|диалог\w*|польск\w*|dzwiek\w*|muzyk\w*|"
        r"dialog\w*|glos\w*|spiew\w*)\b",
        re.IGNORECASE,
    )
    sentences = re.split(r"(?<=[.!?])\s+|\n+", core)
    visual_sentences = [sentence.strip() for sentence in sentences if sentence.strip() and not audio_terms.search(sentence)]
    fallback_core = " ".join(visual_sentences).strip()
    if len(fallback_core) < 40:
        fallback_core = core
    fallback = _finish_rewritten_flow_prompt(fallback_core, original_prompt, "video")
    fallback = apply_youtube_content_safety_rules(fallback, "video")
    fallback = apply_visible_text_language_rule(fallback, _infer_prompt_text_language(original_prompt))
    fallback = enforce_selected_visual_style(fallback, "video")
    fallback = enforce_flow_video_output(fallback)
    fallback = enforce_flow_silent_video_audio(fallback)
    log.warning("[Flow Audio Rewrite] Использую локальный silent fallback; повтор ограничен одним rewrite-раундом.")
    return fallback


def rewrite_flow_prompt_after_technical_failure(
    original_prompt: str,
    failure_text: str = "",
    media_kind: str = "video",
    rewrite_index: int = 1,
) -> str:
    """Ask Gemini for a materially simpler, technically robust prompt after profile failover.

    This is separate from content-policy rewriting: it is used only after the standard
    attempts across available Flow profiles have failed, commonly on Flow's audio backend.
    The last submission boundary will re-apply the selected style and ambience-only policy.
    """
    raw = str(original_prompt or "").strip()
    # Strip only our appended policy/style boilerplate from the text shown to Gemini, so it
    # can simplify the scene instead of recursively copying the same large instructions.
    raw = re.sub(
        rf"\s*{re.escape(FLOW_FINAL_STYLE_LOCK_START)}.*?{re.escape(FLOW_FINAL_STYLE_LOCK_END)}\s*",
        "\n\n", raw, flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    policy_pos = raw.casefold().find(FLOW_VIDEO_AUDIO_POLICY_MARKER.casefold())
    if policy_pos >= 0:
        raw = raw[:policy_pos].rstrip()
    core = _flow_prompt_core(raw).strip() or raw[:5000]
    style_name = visual_style_label(ACTIVE_VISUAL_STYLE_ID)
    style_description = visual_style_description("video", ACTIVE_VISUAL_STYLE_ID)
    request = f"""You are simplifying a Google Flow video prompt after a technical generation failure.
This is NOT a content-policy bypass request. Keep the harmless narrative purpose, era, setting,
visible characters and their identity, and the selected visual style: {style_name}.
Selected style description: {style_description}.
Failure observed: {str(failure_text or 'Flow generation failed')[:700]}.

Make the next prompt technically simple and easy to render:
- one coherent 8-second shot, one simple action, no complex choreography or multiple simultaneous events;
- at most one restrained camera movement and few moving objects;
- preserve the selected art medium throughout every frame; do not switch to photorealism if the selected style is a painting/illustration;
- sound is OPTIONAL. Request only a very soft, ordinary environmental ambience if it naturally fits;
- if audio is unavailable or would be artificial, SILENCE IS ACCEPTABLE; do not make sound essential to the scene;
- absolutely no music, soundtrack, dialogue, narration, singing, humming, whistling, shrill or sudden sounds;
- do not mention the error, policies, or this instruction in the resulting prompt.
Return ONLY a single concise video prompt (about 60-100 words), no preamble.

CURRENT PROMPT:
{core[:5000]}""".strip()
    rewritten = call_text_api(request)
    if rewritten:
        rewritten = re.sub(r"^(?:revised\s+prompt|rewritten\s+prompt|video\s+prompt|prompt)\s*:\s*", "", rewritten.strip(), flags=re.IGNORECASE)
        rewritten = rewritten.strip().strip('`').strip().strip('"').strip()
        old_norm = re.sub(r"\s+", " ", _flow_prompt_core(core)).casefold()
        new_norm = re.sub(r"\s+", " ", _flow_prompt_core(rewritten)).casefold()
        if len(new_norm) >= 30 and new_norm != old_norm and difflib.SequenceMatcher(None, old_norm, new_norm).ratio() < 0.94:
            finalized = _finish_rewritten_flow_prompt(rewritten, original_prompt, media_kind)
            finalized = apply_youtube_content_safety_rules(finalized, media_kind)
            finalized = apply_visible_text_language_rule(finalized, _infer_prompt_text_language(original_prompt))
            finalized = enforce_selected_visual_style(finalized, media_kind)
            if media_kind == "video":
                finalized = enforce_flow_ambience_only_audio(finalized)
            log.info("[Flow Technical Rewrite] Gemini подготовил упрощённый промпт #%s (%s символов).", rewrite_index, len(finalized))
            return finalized

    log.warning("[Flow Technical Rewrite] Gemini не вернул существенно упрощённый промпт; применяю безопасный простой fallback.")
    fallback = make_policy_safe_fallback_prompt(original_prompt, media_kind, rewrite_index)
    fallback = apply_visible_text_language_rule(
        apply_youtube_content_safety_rules(fallback, media_kind),
        _infer_prompt_text_language(original_prompt),
    )
    fallback = enforce_selected_visual_style(fallback, media_kind)
    if media_kind == "video":
        fallback = enforce_flow_ambience_only_audio(fallback)
    return fallback


async def generate_flow_with_failover(
    manager: FlowAccountManager,
    playwright,
    kind: str,
    prompt: str,
    target_file: Path,
    preferred_account: str | None = None,
    reference_images: list[Path] | None = None,
    _rewrite_round: int = 0,
    _attempt_key_suffix: str = "",
    _audio_rewrite_round: int = 0,
) -> tuple[Path, str]:
    """Generate with bounded retries, one general simplification pass, and one audio-silent fallback.

    An explicit audio-backend failure never repeats the same prompt: it triggers one
    Gemini/Groq rewrite to a silent visual-only clip, tried once on at most two profiles.
    """
    if kind not in {"video", "image"}:
        raise ValueError(kind)

    # Intro clips are identifiable by their dedicated output names. Re-append the
    # visual-silence rule on every profile/failover/rewrite round, even if Gemini's
    # simplification removed policy boilerplate from the rewritten prompt.
    is_intro_video_target = kind == "video" and target_file.name.casefold().startswith("intro_raw_")
    if is_intro_video_target:
        prompt = enforce_flow_intro_silent_visual_policy(prompt)

    manager.last_quota_kind = kind
    # Do not open quota-limited browsers before the user's selected startup profile gets
    # its first real job. Previously this preflight ran for every target (including prompt
    # prefetch), which reordered browser startup and could mistake a half-loaded page for
    # a recovered account. Quota checks happen only when the current project has no usable
    # profiles and wait_for_quota_reset is actively waiting.

    target_key = str(target_file.resolve()) + _attempt_key_suffix
    # Videos get two technical submissions per available profile before Gemini is asked
    # to simplify. Images retain the existing smaller four-submission budget.
    max_target_attempts = (
        max(FLOW_MAX_TOTAL_GENERATION_ATTEMPTS, (FLOW_GENERATION_RETRIES_PER_PROFILE + 1) * max(1, len(manager.profiles)))
        if kind == "video" else FLOW_MAX_TOTAL_GENERATION_ATTEMPTS
    )
    profile_attempts = 0
    tried: set[str] = set()
    last_error: Exception | None = None

    # Agent session setup is best-effort and non-fatal. A missing/changed History
    # control must never quarantine a profile or consume a generation attempt.
    # Once the one silent-audio rewrite has been used, test it on at most two
    # accounts and submit it once per account. Never cycle through all 33 profiles
    # with a prompt after a known audio-backend failure.
    max_profiles_to_try = max(1, len(manager.profiles))
    if _audio_rewrite_round > 0:
        max_profiles_to_try = min(2, max_profiles_to_try)
    while (
        profile_attempts < max_profiles_to_try
        and manager.generation_attempts_by_target.get(target_key, 0) < max_target_attempts
    ):
        try:
            profile = await manager.choose_profile(kind, preferred=preferred_account)
        except Exception as selection_error:
            if not isinstance(last_error, FlowQuotaError):
                last_error = selection_error
            log.warning(
                "[Flow Failover] Нет следующего доступного профиля для %s (%s): %s",
                target_file.name, kind, str(selection_error)[:220],
            )
            break
        preferred_account = None

        if profile["name"] in tried:
            alternatives = [
                item for item in manager.available_profiles(kind)
                if item["name"] not in tried
            ]
            if not alternatives:
                break
            profile = alternatives[0]
            manager.active_profile_name = profile["name"]

        tried.add(profile["name"])
        profile_attempts += 1

        controller = manager.active_controller
        if controller is None or controller.profile is None or controller.profile["name"] != profile["name"]:
            if controller is not None:
                try:
                    await controller.disconnect(close_chrome=True)
                except Exception:
                    pass
            controller = FlowController(playwright, manager)
            manager.active_controller = controller
            try:
                await controller.connect(profile)
                manager.refresh_browser_session_identity(profile)
            except FlowSuspiciousActivityError as e:
                manager.mark_suspicious_activity(profile["name"], str(e))
                last_error = e
                try:
                    await controller.disconnect(close_chrome=True)
                except Exception:
                    pass
                manager.active_controller = None
                manager.active_profile_name = None
                manager.index = (manager.profiles.index(profile) + 1) % len(manager.profiles)
                continue
            except FlowPolicyBlockError as e:
                manager.mark_policy_block(profile["name"], str(e))
                try:
                    await controller.disconnect(close_chrome=True)
                except Exception:
                    pass
                manager.active_controller = None
                manager.active_profile_name = None
                raise
            except FlowQuotaError as e:
                manager.mark_quota(profile["name"], str(e))
                last_error = e
                log.warning(
                    "⚠️ [Flow Failover] %s has reached the Flow Agent quota; skipping this profile and trying another account for %s.",
                    profile["name"], target_file.name,
                )
                try:
                    await controller.disconnect(close_chrome=True)
                except Exception:
                    pass
                manager.active_controller = None
                manager.active_profile_name = None
                continue
            except Exception as e:
                last_error = e
                manager.mark_technical_error(profile["name"], f"connect: {e}")
                log.warning("⚠️ [Flow] Не удалось подключить профиль %s: %s", profile["name"], str(e)[:220])
                try:
                    await controller.disconnect(close_chrome=True)
                except Exception:
                    pass
                manager.active_controller = None
                manager.active_profile_name = None
                continue
        else:
            try:
                await controller.check_for_block_or_quota()
            except FlowSuspiciousActivityError as e:
                manager.mark_suspicious_activity(profile["name"], str(e))
                last_error = e
                try:
                    await controller.disconnect(close_chrome=True)
                except Exception:
                    pass
                manager.active_controller = None
                manager.active_profile_name = None
                manager.index = (manager.profiles.index(profile) + 1) % len(manager.profiles)
                continue
            except FlowQuotaError as e:
                manager.mark_quota(profile["name"], str(e))
                last_error = e
                log.warning(
                    "⚠️ [Flow Failover] %s already has a Flow Agent quota banner; trying another account for %s.",
                    profile["name"], target_file.name,
                )
                try:
                    await controller.disconnect(close_chrome=True)
                except Exception:
                    pass
                manager.active_controller = None
                manager.active_profile_name = None
                continue

        attempts_used = manager.generation_attempts_by_target.get(target_key, 0)
        remaining_budget = max_target_attempts - attempts_used
        per_profile_limit = (
            1 if _audio_rewrite_round > 0
            else min(FLOW_GENERATION_RETRIES_PER_PROFILE + 1, remaining_budget)
        )

        try:
            # Open the requested media mode first so the Agent header is mounted.
            await controller.ensure_mode("Video" if kind == "video" else "Image")
            current_project_state = getattr(manager, "current_project_state", None)
            if current_project_state is not None:
                await controller.ensure_new_agent_session_for_project(current_project_state)

            log.info(
                "🌐 [Flow] %s -> %s | images=%s/%s videos=%s/%s | attempt budget=%s/%s",
                profile["name"], kind,
                manager.round_usage.get(profile["name"], {}).get("images", 0),
                FLOW_SESSION_IMAGE_LIMIT,
                manager.round_usage.get(profile["name"], {}).get("videos", 0),
                FLOW_SESSION_VIDEO_LIMIT,
                attempts_used, max_target_attempts,
            )

            if kind == "video":
                await controller.generate_video_to_file(
                    prompt, target_file, attempt_limit=per_profile_limit, reference_images=reference_images
                )
            else:
                await controller.generate_image_to_file(
                    prompt, target_file, attempt_limit=per_profile_limit, reference_images=reference_images
                )

            submitted = max(1, int(controller._last_prompt_attempts or 1))
            attempts_used += submitted
            manager.generation_attempts_by_target[target_key] = attempts_used

            if kind == "image":
                is_character_reference = target_file.parent.name.casefold() == CHARACTER_REFERENCE_DIR_NAME.casefold()
                purpose_label = "эталон персонажа" if is_character_reference else "сцена/изображение проекта"
                log.info(
                    "[Flow Usage] Успешная генерация '%s' (%s); она учитывается в лимите изображений профиля.",
                    target_file.name, purpose_label,
                )
            quota_rotate = manager.mark_done(profile["name"], kind)
            log.info(
                "✅ [Flow] %s завершил %s: quota images=%s/%s videos=%s/%s | attempts=%s/%s",
                profile["name"], kind,
                manager.round_usage.get(profile["name"], {}).get("images", 0),
                FLOW_SESSION_IMAGE_LIMIT,
                manager.round_usage.get(profile["name"], {}).get("videos", 0),
                FLOW_SESSION_VIDEO_LIMIT,
                attempts_used, max_target_attempts,
            )

            if quota_rotate or FLOW_ROTATE_AFTER_EACH_SUCCESS:
                usage = manager.round_usage.get(profile["name"], {})
                if quota_rotate:
                    log.info(
                        "🔄 [Flow Rotation] %s достиг лимита %s (%s/%s); закрываю браузер и перехожу к следующему профилю. "
                        "Текущие независимые счётчики: images=%s/%s videos=%s/%s.",
                        profile["name"], kind, manager.media_quota_used(profile["name"], kind),
                        manager.media_quota_limit(kind),
                        usage.get("images", 0), FLOW_SESSION_IMAGE_LIMIT,
                        usage.get("videos", 0), FLOW_SESSION_VIDEO_LIMIT,
                    )
                else:
                    log.warning(
                        "[Flow Rotation] Принудительная ротация после успешного медиа включена вручную; "
                        "профиль %s будет закрыт до достижения квоты.", profile["name"],
                    )
                await controller.disconnect(close_chrome=True)
                manager.active_controller = None
                manager.active_profile_name = None
            else:
                log.info(
                    "[Flow Rotation] Остаюсь на %s: %s %s/%s, images=%s/%s, videos=%s/%s. "
                    "Следующая задача продолжит работу в этом же профиле.",
                    profile["name"], kind, manager.media_quota_used(profile["name"], kind),
                    manager.media_quota_limit(kind),
                    manager.round_usage.get(profile["name"], {}).get("images", 0), FLOW_SESSION_IMAGE_LIMIT,
                    manager.round_usage.get(profile["name"], {}).get("videos", 0), FLOW_SESSION_VIDEO_LIMIT,
                )
            return target_file, profile["name"]

        except FlowGenerationRejectedError as e:
            submitted = int(controller._last_prompt_attempts or 0)
            manager.generation_attempts_by_target[target_key] = min(
                max_target_attempts, attempts_used + submitted
            )
            last_error = e
            log.warning(
                "⚠️ [Flow] Flow явно указал ограничение контента для %s в %s. "
                "Не повторяю отказ на другом профиле; слот будет закрыт резервным кадром: %s",
                kind, profile["name"], str(e)[:220],
            )
            try:
                await controller.disconnect(close_chrome=True)
            except Exception:
                pass
            manager.active_controller = None
            manager.active_profile_name = None
            raise

        except FlowSuspiciousActivityError as e:
            submitted = int(controller._last_prompt_attempts or 0)
            attempts_used += submitted
            manager.generation_attempts_by_target[target_key] = min(
                max_target_attempts, attempts_used
            )
            last_error = e
            manager.mark_suspicious_activity(profile["name"], str(e))
            try:
                await controller.disconnect(close_chrome=True)
            except Exception:
                pass
            manager.active_controller = None
            manager.active_profile_name = None
            manager.index = (manager.profiles.index(profile) + 1) % len(manager.profiles)
            log.warning(
                "⚠️ [Flow Failover] Профиль %s отложен до следующего запуска скрипта; "
                "повторяю тот же материал на другом браузере (%s/%s).",
                profile["name"], profile_attempts, max_profiles_to_try,
            )
            continue

        except FlowPolicyBlockError:
            raise

        except FlowQuotaError as e:
            # A Flow Agent quota belongs to the active account, not to this scene.
            # Mark the profile and try another available account before pausing the project.
            manager.mark_quota(profile["name"], str(e))
            last_error = e
            log.warning(
                "⚠️ [Flow Failover] %s reached the Flow Agent quota during %s; trying another account for %s.",
                profile["name"], kind, target_file.name,
            )
            try:
                await controller.disconnect(close_chrome=True)
            except Exception:
                pass
            manager.active_controller = None
            manager.active_profile_name = None
            continue

        except FlowAudioGenerationError as e:
            # Audio errors are not retried with the same prompt. Convert the visual
            # request once to a silent clip, then test on at most two accounts.
            effective_prompt = getattr(controller, "_last_effective_prompt", None)
            if isinstance(effective_prompt, str) and effective_prompt.strip():
                prompt = effective_prompt
            submitted = int(controller._last_prompt_attempts or 0)
            attempts_used += submitted
            manager.generation_attempts_by_target[target_key] = min(
                max_target_attempts, attempts_used
            )
            last_error = e
            manager.mark_technical_error(profile["name"], str(e))
            log.warning(
                "⚠️ [Flow Audio Failure] %s: %s. Same prompt will NOT be resent. audio rewrite round=%s.",
                profile["name"], str(e)[:200], _audio_rewrite_round,
            )
            try:
                await controller.disconnect(close_chrome=True)
            except Exception:
                pass
            manager.active_controller = None
            manager.active_profile_name = None

            if kind == "video" and _audio_rewrite_round == 0:
                log.warning(
                    "[Flow Audio Rewrite] Один раз прошу Gemini/Groq убрать звуковые требования, "
                    "сохранить видимую сцену и сделать полностью silent visual-only video."
                )
                try:
                    silent_prompt = await asyncio.to_thread(
                        rewrite_flow_prompt_after_audio_failure,
                        prompt,
                        str(e),
                        1,
                    )
                except Exception as rewrite_error:
                    log.warning(
                        "[Flow Audio Rewrite] Rewrite call failed: %s; applying local silent fallback.",
                        str(rewrite_error)[:180],
                    )
                    silent_prompt = enforce_flow_silent_video_audio(prompt)
                if is_intro_video_target:
                    silent_prompt = enforce_flow_intro_silent_visual_policy(silent_prompt)
                return await generate_flow_with_failover(
                    manager,
                    playwright,
                    kind,
                    silent_prompt,
                    target_file,
                    preferred_account=None,
                    reference_images=reference_images,
                    _rewrite_round=_rewrite_round,
                    _attempt_key_suffix=f"{_attempt_key_suffix}|audio-silent-fallback",
                    _audio_rewrite_round=1,
                )

            # The silent prompt already failed on this profile. Allow only the
            # remaining bounded profile check; no further audio rewrite is allowed.
            log.warning(
                "[Flow Audio Failure] Silent fallback still failed. Will not rewrite or repeat again; "
                "at most two profiles are checked in this fallback round."
            )

        except FlowTechnicalError as e:
            # If Flow rejected the original prompt and Gemini already made a safer
            # replacement, carry that replacement to the next profile after a later
            # technical failure. Never replay a known-refused original prompt.
            effective_prompt = getattr(controller, "_last_effective_prompt", None)
            if isinstance(effective_prompt, str) and effective_prompt.strip():
                prompt = effective_prompt
            submitted = int(controller._last_prompt_attempts or 0)
            attempts_used += submitted
            manager.generation_attempts_by_target[target_key] = min(
                max_target_attempts, attempts_used
            )
            last_error = e
            manager.mark_technical_error(profile["name"], str(e))
            log.warning(
                "⚠️ [Flow] Технический сбой %s: %s | использовано попыток %s/%s",
                profile["name"], str(e)[:220],
                manager.generation_attempts_by_target[target_key], max_target_attempts,
            )
            try:
                await controller.disconnect(close_chrome=True)
            except Exception:
                pass
            manager.active_controller = None
            manager.active_profile_name = None

        except Exception as e:
            submitted = int(controller._last_prompt_attempts or 0)
            attempts_used += submitted
            manager.generation_attempts_by_target[target_key] = min(
                max_target_attempts, attempts_used
            )
            last_error = e
            manager.mark_technical_error(profile["name"], str(e))
            log.warning(
                "⚠️ [Flow] Непредвиденный сбой %s: %s | использовано попыток %s/%s",
                profile["name"], str(e)[:220],
                manager.generation_attempts_by_target[target_key], max_target_attempts,
            )
            try:
                await controller.disconnect(close_chrome=True)
            except Exception:
                pass
            manager.active_controller = None
            manager.active_profile_name = None

        await asyncio.sleep(1)

    if isinstance(last_error, FlowQuotaError):
        raise FlowQuotaError(
            f"Flow Agent quota reached; no remaining available account for {kind} target {target_file.name}. "
            f"Last profile error: {last_error}"
        ) from last_error

    used = manager.generation_attempts_by_target.get(target_key, 0)
    if used >= max_target_attempts:
        message = (
            f"Flow {kind}: действительно исчерпан лимит {max_target_attempts} "
            f"отправок промпта на файл {target_file.name}; последний сбой: {last_error}"
        )
    else:
        tried_label = ", ".join(sorted(tried)) or "ни один профиль"
        message = (
            f"Flow {kind}: не удалось подготовить рабочий профиль для {target_file.name}. "
            f"Проверены: {tried_label}; фактических отправок промпта {used}/"
            f"{max_target_attempts}; последний сбой: {last_error}"
        )
    if kind == "video" and _rewrite_round < 1 and _audio_rewrite_round == 0 and not isinstance(last_error, FlowAudioGenerationError):
        log.warning(
            "[Flow Failover] Не удалось получить видео после попыток на доступных профилях (%s). "
            "Передаю промпт Gemini для одного упрощения и повторю полный цикл профилей.",
            target_file.name,
        )
        try:
            simplified_prompt = await asyncio.to_thread(
                rewrite_flow_prompt_after_technical_failure,
                prompt,
                str(last_error or message),
                kind,
                _rewrite_round + 1,
            )
        except Exception as rewrite_error:
            log.exception("[Flow Technical Rewrite] Не удалось вызвать Gemini: %s", rewrite_error)
            simplified_prompt = make_policy_safe_fallback_prompt(prompt, kind, _rewrite_round + 1)
            simplified_prompt = enforce_selected_visual_style(simplified_prompt, kind)
            simplified_prompt = enforce_flow_ambience_only_audio(simplified_prompt)

        if simplified_prompt and re.sub(r"\s+", " ", simplified_prompt).strip() != re.sub(r"\s+", " ", prompt).strip():
            return await generate_flow_with_failover(
                manager,
                playwright,
                kind,
                simplified_prompt,
                target_file,
                preferred_account=None,
                reference_images=reference_images,
                _rewrite_round=_rewrite_round + 1,
                _attempt_key_suffix=f"|gemini-rewrite-{_rewrite_round + 1}",
            )
        log.warning("[Flow Technical Rewrite] Упрощённый промпт не отличается; продолжаю с резервом на уровне проекта.")

    raise FlowTechnicalError(message) from last_error




async def ensure_character_reference_images(
    state: "ProjectState",
    analysis: dict[str, Any],
    story_text: str,
    manager: FlowAccountManager,
    playwright,
) -> list[dict[str, Any]]:
    """Generate/reuse local identity portraits for primary characters only."""
    bible = analysis.get("character_bible", {}) if isinstance(analysis, dict) else {}
    candidates = select_primary_reference_characters(bible, story_text)
    if not candidates:
        log.info("[Character Reference] Нет выделенных основных персонажей; генерация эталонов пропущена.")
        return []

    references_dir = ensure_directory(state.project_folder / CHARACTER_REFERENCE_DIR_NAME)
    setting_context = analysis.get("context", {}) if isinstance(analysis.get("context", {}), dict) else {}
    created_or_reused: list[dict[str, Any]] = []
    changed = False
    # Persist the selected cast and demote stale primary labels from previous runs.
    # Otherwise an old reference that is no longer in the chosen top cast could still
    # be attached to prompts merely because its priority/path remained in the bible.
    def _stable_character_key(item: dict[str, Any]) -> str:
        return re.sub(r"\W+", "", str(item.get("id") or item.get("name", "")).casefold())

    selected_cast_keys = {_stable_character_key(item) for item in candidates}
    for item in bible.get("characters", []) if isinstance(bible.get("characters", []), list) else []:
        if not isinstance(item, dict):
            continue
        key = _stable_character_key(item)
        desired_priority = "primary" if key in selected_cast_keys else (
            "supporting" if str(item.get("visual_reference_priority", "")).casefold()
            in {"primary", "main", "protagonist", "главный", "главная", "главные"}
            else item.get("visual_reference_priority", "supporting")
        )
        if item.get("visual_reference_priority") != desired_priority:
            item["visual_reference_priority"] = desired_priority
            changed = True

    for entry in candidates:
        if entry.get("channel_reference_locked"):
            external_path = Path(str(entry.get("reference_image_path", "")))
            if not is_valid_image(external_path):
                raise FlowTechnicalError(f"Каналный эталон главного героя не найден/невалиден: {external_path}")
            log.info(
                "[Hero Etalon] Не генерирую второй эталон для %s — использую загруженную картинку %s.",
                entry.get("name", "The Narrator"), external_path.name,
            )
            created_or_reused.append(entry)
            continue
        stable_id = re.sub(
            r"[^A-Z0-9]+", "_", str(entry.get("id") or entry.get("name", "CHARACTER")).upper()
        ).strip("_") or "CHARACTER"
        identity_payload = {
            key: entry.get(key, "")
            for key in (
                "name", "kind", "age_or_stage", "face_or_head", "skin_tone_or_coat",
                "hair_or_mane", "eyes", "build_or_body", "distinctive_features", "continuity_lock",
            )
        }
        # Preserve legacy realistic reference filenames; only painterly references need a style-specific variant.
        if ACTIVE_VISUAL_STYLE_ID != "realistic":
            identity_payload["visual_style_id"] = ACTIVE_VISUAL_STYLE_ID
        identity_hash = hashlib.sha256(
            json.dumps(identity_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        target = references_dir / f"{safe_name(stable_id, 'CHARACTER')}_{identity_hash[:8]}.jpg"

        if not is_valid_image(target):
            log.info(
                "[Character Reference] Создаю эталон для основного персонажа %s (%s/%s)",
                entry.get("name", stable_id), len(created_or_reused) + 1, len(candidates),
            )
            prompt = make_full_color_edge_to_edge_prompt(
                apply_youtube_content_safety_rules(character_reference_prompt(entry, setting_context), "image")
            )
            try:
                await generate_flow_with_failover(
                    manager, playwright, "image", prompt, target,
                )
            except Exception:
                target.unlink(missing_ok=True)
                log.exception(
                    "[Character Reference] Не удалось создать эталон для %s; дальнейшую генерацию без эталона не подменяю молча.",
                    entry.get("name", stable_id),
                )
                raise
            if not is_valid_image(target):
                raise FlowTechnicalError(
                    f"Flow не создал валидный эталон персонажа {entry.get('name', stable_id)}: {target}"
                )
            log.info("[Character Reference] Эталон сохранён: %s", target)
        else:
            log.info("[Character Reference] Использую сохранённый эталон: %s", target)

        new_path = str(target.resolve())
        if entry.get("reference_image_path") != new_path or entry.get("reference_identity_sha256") != identity_hash:
            entry["reference_image_path"] = new_path
            entry["reference_identity_sha256"] = identity_hash
            entry["reference_image_updated_at"] = now_iso()
            changed = True
        created_or_reused.append(entry)

    # Preserve the exact paths in the persistent bible so resumed runs reuse them.
    if changed:
        state.data.setdefault("analysis", {})["character_bible"] = bible
        state.data["analysis"] = analysis
        state.save()
        bible_path = state.work_dir / "character_bible.json"
        try:
            bible_path.write_text(json.dumps(bible, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("[Character Reference] Не удалось обновить character_bible.json: %s", exc)
    return created_or_reused


# ============================================================================
# CHECKPOINT / RESUME
# ============================================================================


class ProjectState:
    def __init__(self, project_folder: Path):
        self.project_folder = project_folder
        self.state_dir = ensure_directory(project_folder / STATE_DIR_NAME)
        self.media_dir = ensure_directory(project_folder / MEDIA_DIR_NAME)
        self.work_dir = ensure_directory(project_folder / WORK_DIR_NAME)
        self.path = self.state_dir / "state.json"
        self.data = self._load()

    def _default(self) -> dict[str, Any]:
        return {
            "schema": 2,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "project": self.project_folder.name,
            "status": "NEW",
            "stage": "init",
            "audio": {
                "path": str(self.state_dir / "audio.wav"),
                "done": False,
            },
            "whisper": {
                "subtitle_path": str(self.state_dir / "subtitles.ass"),
                "segments_path": str(self.state_dir / "segments.json"),
                "done": False,
            },
            "analysis": {
                "characters": {},
                "context": {},
                "done": False,
            },
            "intro": {
                "enabled": ENABLE_FLOW_INTRO_DEFAULT,
                "target_duration": INTRO_TARGET_SECONDS,
                "storyboard_version": INTRO_STORYBOARD_VERSION,
                "prompts": [],
                "clips": [],
                "output": str(self.media_dir / "intro_64s.mp4"),
                "done": False,
            },
            "main_clips": [],
            "scene_plan": [],
            "timing": {
                "intro_duration": 0.0,
                "audio_duration": 0.0,
                "main_timeline_start": 0.0,
                "timeline_signature": "",
            },
            "montage": {
                "no_audio": str(self.work_dir / "video_no_audio.mp4"),
                "with_audio": str(self.work_dir / "video_with_audio.mp4"),
                "done": False,
            },
            "final": {
                "title": "",
                "video": "",
                "subtitles": "",
                "subtitles_burned": False,
                "done": False,
            },
            "thumbnails": [],
            "thumbnail_delivery_version": 0,
            "source_preview_delivery": "",
            # Final output must include the narration/story source before source-folder cleanup.
            "story_text_delivery": "",
            "provenance_inspection": {"version": 1, "status": "not_checked", "markers": [], "format_tags": {}},
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            data = self._default()
            self.save(data)
            return data
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            base = self._default()
            for key, value in base.items():
                if key not in data:
                    data[key] = value
            # Additive migration: preserve every existing checkpoint while advancing the schema.
            previous_schema = int(data.get("schema", 1) or 1)
            if previous_schema < 2:
                data["schema"] = 2
                log.info("[State] Миграция схемы %s -> 2 без сброса существующих checkpoint.", previous_schema)
            for clip in data.get("main_clips", []) if isinstance(data.get("main_clips", []), list) else []:
                if isinstance(clip, dict):
                    clip.setdefault("scene_break", False)
                    clip.setdefault("media_type", "image")
                    clip.setdefault("transition_out", "cut")
                    clip.setdefault("transition_dur", 0.0)
            return data
        except Exception as e:
            broken = self.path.with_name(f"state_broken_{int(time.time())}.json")
            shutil.copy2(self.path, broken)
            log.warning(
                "[State] state.json повреждён, создаём новый. Backup: %s (%s)",
                broken,
                e,
            )
            data = self._default()
            self.save(data)
            return data

    def save(self, data: dict[str, Any] | None = None) -> None:
        if data is not None:
            self.data = data
        self.data["updated_at"] = now_iso()
        atomic_write_json(self.path, self.data)

    def set_stage(self, stage: str, status: str = "WORKING") -> None:
        self.data["stage"] = stage
        self.data["status"] = status
        self.save()

    def reconcile(self) -> None:
        # Files on disk are the source of truth for resume. This is deliberately
        # broader than state.json so an interrupted run can recover after a
        # crash between download and state.save().
        audio_path=Path(self.data["audio"]["path"])
        if is_valid_audio(audio_path):
            self.data["audio"]["done"]=True

        whisper=self.data["whisper"]
        if Path(whisper["subtitle_path"]).exists() and Path(whisper["segments_path"]).exists():
            whisper["done"]=True

        analysis=self.data["analysis"]
        analysis["done"]=bool(analysis.get("characters") is not None and analysis.get("context") is not None and (analysis.get("characters") or analysis.get("context")))

        intro=self.data["intro"]
        if intro.get("enabled"):
            # Recover individual intro clip list from disk even when a crash
            # happened before the previous state.save().
            recovered=[]
            for p in sorted(self.media_dir.glob("intro_clip_*.mp4")):
                if is_valid_video(p):
                    recovered.append(str(p))
            if recovered:
                intro["clips"] = recovered
                intro["clip_durations"] = [
                    get_duration(Path(p)) or 0.0
                    for p in recovered
                ]
                intro["actual_duration"] = sum(intro["clip_durations"])
            else:
                intro.setdefault("clip_durations", [])
                intro.setdefault("actual_duration", 0.0)

            intro["done"] = is_valid_video(Path(intro["output"]))
            if intro["done"]:
                intro["actual_duration"] = get_duration(
                    Path(intro["output"])
                ) or intro.get("actual_duration", 0.0)

        # Recover scene records by index and by actual files.
        records={}
        for item in self.data.get("main_clips",[]):
            try:
                records[int(item["index"])]=dict(item)
            except Exception:
                pass

        for p in self.media_dir.glob("scene_*.mp4"):
            m=re.search(r"scene_(\d+)\.mp4$",p.name,re.I)
            if not m: continue
            idx=int(m.group(1))-1
            rec=records.setdefault(idx,{"index":idx})
            rec["output_path"]=str(p)
            if is_valid_video(p): rec["done"]=True
        for p in self.media_dir.glob("scene_*.jpg"):
            m=re.search(r"scene_(\d+)\.jpg$",p.name,re.I)
            if not m: continue
            idx=int(m.group(1))-1
            rec=records.setdefault(idx,{"index":idx})
            rec["image_path"]=str(p)

        if records:
            self.data["main_clips"]=[records[k] for k in sorted(records)]

        thumbs = self.data.get("thumbnails", [])
        for thumb in thumbs:
            path=Path(thumb.get("path",""))
            thumb["done"]=is_valid_image(path)

        # If thumbnails were downloaded before their state was written, recover
        # them from the output folder.
        if not thumbs:
            recovered_thumbs=[]
            for p in sorted(self.project_folder.rglob("Thumb*_Flow_2K.jpg")):
                recovered_thumbs.append({
                    "index": len(recovered_thumbs),
                    "prompt": "",
                    "path": str(p),
                    "done": is_valid_image(p),
                })
            if recovered_thumbs:
                self.data["thumbnails"] = recovered_thumbs

        final_video=str(self.data["final"].get("video",""))
        self.data["final"]["done"]=bool(final_video and is_valid_video(Path(final_video)))

        # Intermediate montage outputs are also resumable.
        for key in ("no_audio","with_audio"):
            value=str(self.data["montage"].get(key,""))
            if value and is_valid_video(Path(value)):
                self.data["montage"]["done"]=True

        self.save()

    def print_resume_summary(self) -> None:
        print("\n" + "=" * 72)
        print("ВОЗОБНОВЛЕНИЕ ПРОЕКТА")
        print("=" * 72)
        print("Project:", self.project_folder.name)
        print("Stage  :", self.data.get("stage"))
        print("Audio  :", "✅" if self.data["audio"]["done"] else "❌")
        print("Whisper:", "✅" if self.data["whisper"]["done"] else "❌")
        print("Analysis:", "✅" if self.data["analysis"]["done"] else "❌")
        print("Intro  :", "✅" if self.data["intro"]["done"] else "❌")

        clips = self.data.get("main_clips", [])
        done = sum(1 for c in clips if c.get("done"))
        print(f"Flow clips: {done}/{len(clips)}")
        print("Final  :", "✅" if self.data["final"]["done"] else "❌")
        print("State  :", self.path)
        print("Media  :", self.media_dir)
        print("=" * 72 + "\n")


# ============================================================================
# VIDEO MONTAGE & CONVERSION
# ============================================================================


def extract_whisper_sentences(segments, start_time: float, audio_duration: float) -> list[dict[str, Any]]:
    """Build sentence units from Whisper word timestamps without changing their clock."""
    if isinstance(segments, dict):
        raw_words = segments.get("words", [])
    else:
        raw_words = []
        for segment in segments or []:
            if isinstance(segment, dict):
                words = segment.get("words") or []
                if words:
                    raw_words.extend(words)
                elif str(segment.get("text", "")).strip():
                    raw_words.append({"start": segment.get("start", 0.0), "end": segment.get("end", 0.0), "word": segment.get("text", "")})
    words = []
    for item in raw_words or []:
        try:
            a, b = float(item.get("start", 0.0)), float(item.get("end", 0.0))
            token = str(item.get("word", item.get("text", ""))).strip()
            if token and b > a and b > start_time and a < audio_duration:
                words.append({"start": max(start_time, a), "end": min(audio_duration, b), "word": token})
        except (TypeError, ValueError, AttributeError):
            continue
    words.sort(key=lambda x: (x["start"], x["end"]))
    sentences: list[dict[str, Any]] = []
    current = []
    for i, word in enumerate(words):
        current.append(word)
        next_start = words[i + 1]["start"] if i + 1 < len(words) else audio_duration
        pause = max(0.0, next_start - word["end"])
        terminal = bool(re.search(r"[.!?…][\"'”’»)]*$", word["word"]))
        if terminal or pause >= 0.35 or i == len(words) - 1:
            text = " ".join(x["word"] for x in current).strip()
            if text:
                sentences.append({
                    "id": len(sentences) + 1,
                    "start": float(current[0]["start"]),
                    "end": float(current[-1]["end"]),
                    "text": text,
                    "pause_after": float(pause),
                })
            current = []
    return sentences


def _semantic_boundary_time(left: dict, right: dict) -> tuple[float, float]:
    gap = max(0.0, float(right["start"]) - float(left["end"]))
    # The cut stays in the silent gap and no more than 150 ms after the last word.
    cut = min(float(right["start"]), float(left["end"]) + min(gap * 0.5, 0.15))
    return cut, gap


def calculate_pause_aligned_intervals(
    segments, audio_duration: float, start_time: float, min_dur: float = 8.0,
    target_dur: float = 15.0, max_dur: float = 22.0,
) -> list[dict[str, Any]]:
    """Deterministic local fallback: cut only at sentence/pause boundaries."""
    sentences = extract_whisper_sentences(segments, start_time, audio_duration)
    if not sentences:
        return [{"start": a, "end": b, "scene_break": False, "pause_before": 0.0}
                for a, b in calculate_dynamic_slide_intervals(audio_duration, start_time, min_dur, max_dur)]
    groups: list[list[dict]] = []
    cursor = 0
    while cursor < len(sentences):
        best = cursor + 1
        best_score = float("inf")
        for end_idx in range(cursor + 1, len(sentences) + 1):
            end_time = float(sentences[end_idx - 1]["end"])
            dur = end_time - float(sentences[cursor]["start"])
            if dur > max_dur and end_idx > cursor + 1:
                break
            if dur >= min_dur:
                score = abs(dur - target_dur)
                if score < best_score:
                    best, best_score = end_idx, score
                if dur >= target_dur:
                    break
        if best == cursor + 1 and cursor + 1 < len(sentences):
            while best < len(sentences) and float(sentences[best - 1]["end"]) - float(sentences[cursor]["start"]) < min_dur:
                best += 1
        groups.append(sentences[cursor:best])
        cursor = best
    # Merge a very short tail with its predecessor.
    if len(groups) > 1 and float(groups[-1][-1]["end"]) - float(groups[-1][0]["start"]) < 6.5:
        groups[-2].extend(groups[-1])
        groups.pop()
    result = []
    for i, group in enumerate(groups):
        start = float(start_time) if i == 0 else result[-1]["end"]
        if i < len(groups) - 1:
            boundary, pause = _semantic_boundary_time(group[-1], groups[i + 1][0])
        else:
            boundary, pause = float(audio_duration), 0.0
        if boundary <= start:
            boundary = min(float(audio_duration), max(start + 0.05, float(group[-1]["end"])))
        result.append({"start": start, "end": boundary, "scene_break": False,
                       "pause_before": pause if i else 0.0,
                       "_sentence_from": group[0]["id"], "_sentence_to": group[-1]["id"]})
    if result:
        result[-1]["end"] = float(audio_duration)
    # Final hard cap: split any residual >22s interval at the nearest Whisper word boundary.
    capped = []
    for interval in result:
        pending = dict(interval)
        while float(pending["end"]) - float(pending["start"]) > 22.0:
            left = float(pending["start"]); right = float(pending["end"])
            target = (left + right) / 2.0
            cut = _nearest_whisper_cut(segments, target, left + 5.5, right - 5.5)
            if cut <= left + 0.05 or cut >= right - 0.05:
                cut = target
            second = dict(pending)
            pending["end"] = round(cut, 3)
            pending["duration"] = round(cut - left, 3)
            second["start"] = round(cut, 3)
            second["scene_break"] = False
            second["pause_before"] = 0.0
            pending["_split_continuation"] = True
            capped.append(pending)
            pending = second
        capped.append(pending)
    return [x for x in capped if x["end"] > x["start"]]


def fit_semantic_scene_count(
    scenes: list[dict[str, Any]], segments, target_count: int,
    start_time: float, audio_duration: float,
) -> list[dict[str, Any]]:
    """Adjust semantic grouping to an existing project's scene count without extra Flow work."""
    result = [dict(x) for x in scenes]
    if not result or target_count <= 0:
        return result
    sentences = extract_whisper_sentences(segments, start_time, audio_duration)
    by_id = {int(x["id"]): x for x in sentences}
    while len(result) > target_count:
        candidates = []
        for i in range(len(result) - 1):
            combined = float(result[i + 1]["end"]) - float(result[i]["start"])
            pause = float(result[i + 1].get("pause_before", 0.0) or 0.0)
            candidates.append((pause, abs(combined - 15.0), i))
        _, _, index = min(candidates)
        result[index]["end"] = result[index + 1]["end"]
        result[index]["_sentence_to"] = result[index + 1].get("_sentence_to")
        result.pop(index + 1)
    while len(result) < target_count:
        candidates = []
        for i, scene in enumerate(result):
            a, b = scene.get("_sentence_from"), scene.get("_sentence_to")
            if a is None or b is None or int(b) <= int(a):
                continue
            candidates.append((float(scene["end"]) - float(scene["start"]), i))
        if not candidates:
            break
        _, index = max(candidates)
        scene = result[index]
        a, b = int(scene["_sentence_from"]), int(scene["_sentence_to"])
        possible = [k for k in range(a, b) if k in by_id and (k + 1) in by_id]
        if not possible:
            break
        midpoint = (float(scene["start"]) + float(scene["end"])) * 0.5
        split_id = min(possible, key=lambda k: abs((float(by_id[k]["end"]) + float(by_id[k + 1]["start"])) * 0.5 - midpoint))
        cut, pause = _semantic_boundary_time(by_id[split_id], by_id[split_id + 1])
        if cut - float(scene["start"]) < 6.5 or float(scene["end"]) - cut < 6.5:
            break
        second = dict(scene)
        second["start"] = cut
        second["scene_break"] = False
        second["pause_before"] = pause
        second["_sentence_from"] = split_id + 1
        scene["end"] = cut
        scene["_sentence_to"] = split_id
        result.insert(index + 1, second)
    if result:
        result[0]["start"] = float(start_time)
        result[-1]["end"] = float(audio_duration)
    return result


def plan_semantic_scene_intervals(
    segments, audio_duration: float, start_time: float, min_dur: float = 8.0,
    target_dur: float = 15.0, max_dur: float = 22.0,
) -> list[dict[str, Any]]:
    """Ask the text model for semantic sentence groups; locally validate and align every cut."""
    sentences = extract_whisper_sentences(segments, start_time, audio_duration)
    if not sentences:
        return calculate_pause_aligned_intervals(segments, audio_duration, start_time, min_dur, target_dur, max_dur)

    blocks: list[list[dict]] = []
    block: list[dict] = []
    block_start = float(sentences[0]["start"])
    for sentence in sentences:
        block.append(sentence)
        span = float(sentence["end"]) - block_start
        if len(block) >= 50 or (len(block) >= 30 and span >= 240.0):
            blocks.append(block)
            block = []
            if len(blocks) < len(sentences):
                block_start = float(sentence["end"])
    if block:
        blocks.append(block)

    all_groups: list[dict[str, Any]] = []
    for block_index, units in enumerate(blocks):
        block_duration = float(units[-1]["end"]) - float(units[0]["start"])
        target_count = max(1, round(block_duration / target_dur))
        numbered = "\n".join(f'{x["id"]}: {x["text"]}' for x in units)
        prompt = (
            "Group these numbered Whisper transcript sentences into visual scenes for a documentary. "
            f"Each scene should last {min_dur:.0f}-{max_dur:.0f} seconds; target about {target_count} scenes "
            "(acceptable count ±20%). Start a new scene when action, location, character, or time changes. "
            "Set scene_break=true only for a genuine location, time, or story-act change; aim for roughly 25-35% of scenes, not every sentence boundary. "
            "Return ONLY strict JSON array: [{\"from\":1,\"to\":3,\"scene_break\":false}, ...]. "
            "Cover every sentence exactly once, in order, with contiguous ranges. Do not invent IDs.\n"
            + numbered
        )
        parsed = None
        try:
            response = call_text_api(prompt)
            match = re.search(r"\[[\s\S]*\]", str(response or ""))
            candidate = json.loads(match.group(0)) if match else None
            if isinstance(candidate, list) and candidate:
                expected = [int(x["id"]) for x in units]
                covered = []
                checked = []
                for item in candidate:
                    a, b = int(item["from"]), int(item["to"])
                    if a > b or a not in expected or b not in expected:
                        raise ValueError("range outside block")
                    checked.append({"from": a, "to": b, "scene_break": bool(item.get("scene_break", False))})
                    covered.extend(range(a, b + 1))
                if covered != expected:
                    raise ValueError("sentence coverage has gaps, overlaps, or wrong order")
                if not (target_count * 0.8 <= len(checked) <= target_count * 1.2 or len(checked) == 1):
                    raise ValueError("scene count outside ±20% target")
                parsed = checked
        except Exception as exc:
            log.warning("[Semantic Planning] Блок %s: LLM JSON/coverage invalid (%s); использую локальные паузы.", block_index + 1, exc)

        if parsed is None:
            # Reuse the local pause aligner for this block only; do not make another ASR call.
            block_start = float(units[0]["start"])
            block_end = float(units[-1]["end"])
            if isinstance(segments, dict):
                source_words = segments.get("words", []) or []
            else:
                source_words = []
                for source_segment in segments or []:
                    if isinstance(source_segment, dict):
                        source_words.extend(source_segment.get("words") or [])
            block_words = []
            for word in source_words:
                try:
                    if float(word.get("end", 0.0)) > block_start and float(word.get("start", 0.0)) < block_end:
                        block_words.append(word)
                except (TypeError, ValueError, AttributeError):
                    continue
            fallback_intervals = calculate_pause_aligned_intervals(
                {"words": block_words}, block_end, block_start, min_dur, target_dur, max_dur
            )
            local_groups: list[dict] = []
            for interval in fallback_intervals:
                matched = [unit for unit in units
                           if float(unit["end"]) > float(interval["start"])
                           and float(unit["start"]) < float(interval["end"])]
                if matched:
                    local_groups.append({
                        "from": matched[0]["id"], "to": matched[-1]["id"], "scene_break": False
                    })
            parsed = local_groups or [{"from": units[0]["id"], "to": units[-1]["id"], "scene_break": False}]

        # Repair overlong scenes by splitting at the nearest sentence boundary.
        repaired = []
        for group in parsed:
            a, b = int(group["from"]), int(group["to"])
            chunk = [x for x in units if a <= int(x["id"]) <= b]
            pieces: list[list[dict]] = []
            piece: list[dict] = []
            for sentence in chunk:
                if piece and float(sentence["end"]) - float(piece[0]["start"]) > 23.0:
                    pieces.append(piece)
                    piece = []
                piece.append(sentence)
            if piece:
                pieces.append(piece)
            for pidx, piece in enumerate(pieces):
                repaired.append({"from": piece[0]["id"], "to": piece[-1]["id"],
                                 "scene_break": bool(group.get("scene_break", False)) if pidx == 0 else False})
        # Merge tiny groups with the preceding group to avoid flash-scene cuts.
        merged = []
        pending_short = None
        for group in repaired:
            chunk = [x for x in units if group["from"] <= x["id"] <= group["to"]]
            dur = float(chunk[-1]["end"]) - float(chunk[0]["start"])
            if dur < 6.5:
                if merged:
                    merged[-1]["to"] = group["to"]
                elif pending_short is None:
                    pending_short = dict(group)
                else:
                    pending_short["to"] = group["to"]
            else:
                if pending_short is not None:
                    group = dict(group)
                    group["from"] = pending_short["from"]
                    group["scene_break"] = pending_short.get("scene_break", False)
                    pending_short = None
                merged.append(dict(group))
        if pending_short is not None:
            if merged:
                merged[-1]["to"] = pending_short["to"]
            else:
                merged.append(pending_short)
        all_groups.extend(merged)

    # Convert sentence ranges to exact pause-centred scene boundaries.
    sentence_by_id = {int(x["id"]): x for x in sentences}
    scene_specs = []
    for group in all_groups:
        first = sentence_by_id[int(group["from"])]
        last = sentence_by_id[int(group["to"])]
        scene_specs.append({"first": first, "last": last, "scene_break": bool(group.get("scene_break", False))})
    result = []
    for i, spec in enumerate(scene_specs):
        if i == 0:
            start = float(start_time)
            pause = 0.0
        else:
            start, pause = _semantic_boundary_time(scene_specs[i - 1]["last"], spec["first"])
        if i + 1 < len(scene_specs):
            end, _ = _semantic_boundary_time(spec["last"], scene_specs[i + 1]["first"])
        else:
            end = float(audio_duration)
        if end <= start:
            end = min(float(audio_duration), max(start + 0.05, float(spec["last"]["end"])))
        result.append({"start": start, "end": end, "scene_break": spec["scene_break"],
                       "pause_before": pause, "_sentence_from": spec["first"]["id"],
                       "_sentence_to": spec["last"]["id"]})
    if result:
        result[0]["start"] = float(start_time)
        result[-1]["end"] = float(audio_duration)
    return [x for x in result if x["end"] > x["start"]]


def calculate_dynamic_slide_intervals(
    audio_duration: float,
    start_time: float = 0.0,
    min_dur: float = SLIDE_MIN_DUR,
    max_dur: float = SLIDE_MAX_DUR,
) -> list[tuple[float, float]]:
    intervals=[]
    current_time=start_time
    while current_time < audio_duration - 0.01:
        remaining=audio_duration-current_time
        if remaining <= max_dur:
            intervals.append((current_time,audio_duration))
            break
        target=round(random.uniform(min_dur,max_dur),2)
        if remaining-target < min_dur*0.65:
            target=round(remaining/2.0,2)
        end_time=min(current_time+target,audio_duration)
        intervals.append((current_time,end_time))
        current_time=end_time
    return intervals


def generate_vpy_script(
    image_path: Path,
    script_path: Path,
    frames: int,
    zoom_dir: str,
    pan_dir: str,
    speed_factor: float,
) -> None:
    safe_path = str(image_path).replace("\\", "/")
    start_scale, end_scale = (1.0, 1.20) if zoom_dir == "in" else (1.20, 1.0)
    pan_x_start = pan_x_end = pan_y_start = pan_y_end = 0.0
    amp = 0.045
    if pan_dir == "left":
        pan_x_start, pan_x_end = -amp, amp
    elif pan_dir == "right":
        pan_x_start, pan_x_end = amp, -amp
    elif pan_dir == "up":
        pan_y_start, pan_y_end = -amp, amp
    elif pan_dir == "down":
        pan_y_start, pan_y_end = amp, -amp

    cp = random.uniform(*CROP_PERCENT_RANGE)
    log.info(
        "📐 [Монтаж] Случайная обрезка: %.2f%% | Ken Burns speed: %.3f",
        cp * 100,
        speed_factor,
    )

    frame_count = max(1, int(frames))
    script = f"""import vapoursynth as vs
core = vs.core
core.num_threads = 4
clip = core.imwri.Read(r'{safe_path}', alpha=False)[0]
clip = core.resize.Spline36(clip, format=vs.RGB24)
clip = core.std.CropRel(clip, left=int(clip.width*{cp:.4f}), right=int(clip.width*{cp:.4f}), top=int(clip.height*{cp:.4f}), bottom=int(clip.height*{cp:.4f}))
clip = core.resize.Spline36(clip, width=clip.width, height=clip.height)
clip = core.std.Loop(clip, times={frame_count})
clip = core.std.Trim(clip, first=0, length={frame_count})
def zoom_and_pan(n, f):
    t = n / ({frame_count} - 1) if {frame_count} > 1 else 0
    t = t ** (1 / {speed_factor:.3f}) if {speed_factor:.3f} != 1 else t
    t = t * t * (3 - 2 * t)
    s = {start_scale} + ({end_scale} - {start_scale}) * t
    s = max(1.0001, s)
    px = {pan_x_start} + ({pan_x_end} - {pan_x_start}) * t
    py = {pan_y_start} + ({pan_y_end} - {pan_y_start}) * t
    sw, sh = f.width / s, f.height / s
    sl = max(0.0, min((f.width - sw) / 2.0 + px * f.width, f.width - sw))
    st = max(0.0, min((f.height - sh) / 2.0 + py * f.height, f.height - sh))
    return core.resize.Spline36(f, width=f.width, height=f.height, src_left=sl, src_top=st, src_width=min(sw, f.width-sl), src_height=min(sh, f.height-st))
final_clip = core.resize.Spline36(core.std.FrameEval(clip, lambda n: zoom_and_pan(n, clip)), format=vs.YUV420P8, width=1920, height=1080, matrix_s="709")
final_clip.set_output()"""
    script_path.write_text(script, encoding='utf-8')


def render_slide(
    vpy_path: Path,
    output_path: Path,
    duration: float,
    fps: int = FPS,
    fade_out_dur: float = 0.0,
    max_retries: int = 2,
) -> bool:
    for attempt in range(max_retries+1):
        try:
            if not vpy_path.exists():
                return False
            temp_out=output_path.with_suffix('.temp.mp4')
            grain=1 if ACTIVE_VISUAL_STYLE_ID in {"comic_explainer", "cartoon_2d"} else random.randint(*FILM_GRAIN_STRENGTH)
            contrast=round(random.uniform(*COLOR_CONTRAST_JITTER),3)
            brightness=round(random.uniform(*COLOR_BRIGHTNESS_JITTER),3)
            saturation=round(random.uniform(*COLOR_SATURATION_JITTER),3)
            bitrate=f"{random.randint(*VIDEO_BITRATE_RANGE)}k"
            log.info(
                "🎛️ [Монтаж] Grain=%s Contrast=%.3f Brightness=%.3f Saturation=%.3f Bitrate=%s",
                grain,contrast,brightness,saturation,bitrate,
            )
            vf=f"noise=alls={grain}:allf=t+u,eq=contrast={contrast}:brightness={brightness}:saturation={saturation}"
            encoder=HW_ENCODER if attempt==0 else 'libx264'
            preset=ENCODER_PRESET if attempt==0 else 'fast'
            rc=['-rc','vbr','-b:v',bitrate,'-maxrate','12000k'] if attempt==0 else ['-crf','18','-b:v',bitrate]

            if shutil.which('vspipe'):
                p1=subprocess.Popen(['vspipe',str(vpy_path),'-c','y4m','-'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
                cmd=['ffmpeg','-y','-i','pipe:','-vf',vf,'-c:v',encoder,'-preset',preset,*rc,'-pix_fmt','yuv420p','-r',str(fps),'-t',str(duration),*FFMPEG_CLEAN_META_VIDEO,str(temp_out)]
                p2=subprocess.Popen(cmd,stdin=p1.stdout,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                p1.stdout.close()
                try:
                    p2.wait(timeout=900); p1.wait(timeout=900)
                except subprocess.TimeoutExpired:
                    p1.kill(); p2.kill(); raise TimeoutError('Таймаут рендера слайда')
                if p2.returncode!=0:
                    raise RuntimeError(f'FFmpeg code {p2.returncode}')
            else:
                raise RuntimeError('vspipe не найден')

            if fade_out_dur>0:
                fade=min(float(fade_out_dur), max(0.05,float(duration)-0.05))
                fade_tmp=output_path.with_suffix('.fade_temp.mp4')
                subprocess.run(
                    ['ffmpeg','-y','-i',str(temp_out),'-vf',f'fade=t=out:st={max(0.0,duration-fade):.3f}:d={fade:.3f}','-c:v',encoder,'-preset',preset,*rc,'-pix_fmt','yuv420p','-r',str(fps),*FFMPEG_CLEAN_META_VIDEO,str(fade_tmp)],
                    check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                )
                fade_tmp.replace(output_path)
                temp_out.unlink(missing_ok=True)
            else:
                temp_out.replace(output_path)
            return is_valid_video(output_path)
        except Exception as e:
            log.warning('⚠️ [Монтаж] Ошибка рендера слайда: %s',e)
            if attempt<max_retries:
                time.sleep(2)

    # Last-resort standard FFmpeg still clip; project does not fail just because
    # VapourSynth is unavailable on the machine.
    try:
        return image_to_clip(vpy_path, output_path, duration) if False else False
    except Exception:
        return False


def image_to_clip(image_path: Path, output_path: Path, duration: float) -> bool:
    """Fallback still renderer with the same visual finish as the primary scene renderer."""
    angle = math.radians(random.choice((-1, 1)) * random.uniform(0.25, 0.45))
    contrast, brightness, saturation = random.uniform(*COLOR_CONTRAST_JITTER), random.uniform(*COLOR_BRIGHTNESS_JITTER), random.uniform(*COLOR_SATURATION_JITTER)
    grain = 1 if ACTIVE_VISUAL_STYLE_ID in {"comic_explainer", "cartoon_2d"} else random.randint(*FILM_GRAIN_STRENGTH)
    vf = f"rotate={angle:.9f}:ow=iw:oh=ih:bilinear=1,crop=iw*0.965:ih*0.965,scale=1920:1080:flags=lanczos,eq=contrast={contrast:.4f}:brightness={brightness:.4f}:saturation={saturation:.4f},noise=alls={grain}:allf=t+u,setsar=1"
    bitrate = f"{random.randint(*VIDEO_BITRATE_RANGE)}k"
    base = [FFMPEG_BIN, "-y", "-loop", "1", "-i", str(image_path), "-t", f"{duration:.3f}", "-vf", vf, "-r", str(FPS), "-pix_fmt", "yuv420p", "-an"]
    attempts = [base + ["-c:v", HW_ENCODER, "-preset", ENCODER_PRESET, "-b:v", bitrate, "-maxrate", "12000k", *FFMPEG_CLEAN_META_VIDEO, str(output_path)], base + ["-c:v", "libx264", "-preset", "fast", "-crf", "18", *FFMPEG_CLEAN_META_VIDEO, str(output_path)]]
    for cmd in attempts:
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
            if is_valid_video(output_path):
                return True
        except Exception:
            output_path.unlink(missing_ok=True)
    return False




def _load_depth_estimator() -> tuple[Any, Any, Any]:
    """Load MiDaS DPT_Hybrid once. The first run may download model files via Torch Hub."""
    global _DEPTH_MODEL, _DEPTH_TRANSFORM, _DEPTH_DEVICE, _DEPTH_MODEL_ERROR

    if _DEPTH_MODEL is not None and _DEPTH_TRANSFORM is not None:
        import cv2
        import numpy as np
        import torch
        return torch, cv2, np
    if _DEPTH_MODEL_ERROR:
        raise RuntimeError(_DEPTH_MODEL_ERROR)

    try:
        import cv2
        import numpy as np
        import torch

        _DEPTH_DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        log.info(
            "🧭 [Parallax] Загружаем оценку глубины MiDaS DPT_Hybrid на %s. "
            "При первом запуске Torch Hub может скачать веса модели.",
            _DEPTH_DEVICE,
        )
        # Requires internet only on the first run if model code/weights are not cached.
        try:
            model = torch.hub.load(
                "intel-isl/MiDaS", "DPT_Hybrid", pretrained=True, trust_repo=True
            )
        except TypeError:
            model = torch.hub.load("intel-isl/MiDaS", "DPT_Hybrid", pretrained=True)
        transforms = torch.hub.load("intel-isl/MiDaS", "transforms")
        _DEPTH_TRANSFORM = transforms.dpt_transform
        _DEPTH_MODEL = model.to(_DEPTH_DEVICE).eval()
        log.info("✅ [Parallax] MiDaS DPT_Hybrid готов; глубина будет кэшироваться на диске.")
        return torch, cv2, np
    except Exception as exc:
        _DEPTH_MODEL_ERROR = (
            f"Не удалось загрузить backend 2.5D parallax: {type(exc).__name__}: {exc}. "
            "Проверь наличие opencv-python и доступность первого скачивания MiDaS через Torch Hub."
        )
        raise RuntimeError(_DEPTH_MODEL_ERROR) from exc


def initialize_parallax_backend() -> bool:
    """Warm up dependencies before scene rendering, returning whether parallax can run."""
    if not ENABLE_2_5D_PARALLAX:
        return False
    try:
        _load_depth_estimator()
        return True
    except Exception as exc:
        log.warning("⚠️ [Parallax] 2.5D недоступен; используем прежний Ken Burns: %s", exc)
        return False


def _depth_cache_is_current(image_path: Path, depth_path: Path, meta_path: Path) -> bool:
    try:
        if not depth_path.exists() or not meta_path.exists():
            return False
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        stat = image_path.stat()
        return (
            int(meta.get("source_size", -1)) == stat.st_size
            and int(meta.get("source_mtime_ns", -1)) == stat.st_mtime_ns
            and int(meta.get("version", 0)) == 1
        )
    except Exception:
        return False


def estimate_depth_map(image_path: Path, depth_path: Path) -> Path:
    """Create or reuse a MiDaS-relative-depth PNG for one still image.

    MiDaS predicts a relative depth field per image; it is percentile-normalized
    and smoothed before caching. Render-time contrast is applied separately.
    """
    torch, cv2, np = _load_depth_estimator()
    meta_path = depth_path.with_suffix(".json")
    if _depth_cache_is_current(image_path, depth_path, meta_path):
        return depth_path

    with Image.open(image_path) as source:
        image_rgb = source.convert("RGB")
        # Keep depth inference practical for large generated images; DPT transform
        # performs its own input sizing, and the predicted map is restored below.
        if max(image_rgb.size) > PARALLAX_DEPTH_MODEL_MAX_SIDE:
            ratio = PARALLAX_DEPTH_MODEL_MAX_SIDE / max(image_rgb.size)
            small_size = (
                max(1, int(image_rgb.width * ratio)),
                max(1, int(image_rgb.height * ratio)),
            )
            resampling = getattr(Image, "Resampling", Image).BILINEAR
            image_rgb = image_rgb.resize(small_size, resampling)
        rgb_array = np.asarray(image_rgb, dtype=np.uint8)

    input_batch = _DEPTH_TRANSFORM(rgb_array).to(_DEPTH_DEVICE)
    with torch.inference_mode():
        prediction = _DEPTH_MODEL(input_batch)
        prediction = torch.nn.functional.interpolate(
            prediction.unsqueeze(1),
            size=(rgb_array.shape[0], rgb_array.shape[1]),
            mode="bicubic",
            align_corners=False,
        ).squeeze()
    depth = prediction.detach().float().cpu().numpy()
    depth = np.nan_to_num(depth, nan=0.0, posinf=0.0, neginf=0.0)
    low, high = np.percentile(depth, (2.0, 98.0))
    if high - low < 1e-6:
        raise RuntimeError("Модель глубины вернула почти постоянную карту")
    depth = np.clip((depth - low) / (high - low), 0.0, 1.0).astype(np.float32)
    depth = cv2.GaussianBlur(depth, (0, 0), sigmaX=PARALLAX_DEPTH_SMOOTH_SIGMA)
    depth_u8 = np.clip(depth * 255.0, 0, 255).astype(np.uint8)

    ensure_directory(depth_path.parent)
    if not _cv2_write_unicode(depth_path, depth_u8, cv2, np):
        raise RuntimeError(f"Не удалось сохранить карту глубины: {depth_path}")
    stat = image_path.stat()
    meta_path.write_text(
        json.dumps({
            "version": 1,
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "width": int(depth_u8.shape[1]),
            "height": int(depth_u8.shape[0]),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return depth_path


def _cv2_read_unicode(path: Path, flags: int, cv2, np):
    """OpenCV file reader that also works with Cyrillic Windows paths."""
    try:
        encoded = np.fromfile(str(path), dtype=np.uint8)
        if encoded.size == 0:
            return None
        return cv2.imdecode(encoded, flags)
    except Exception:
        return None


def _cv2_write_unicode(path: Path, image, cv2, np) -> bool:
    """OpenCV file writer that also works with Cyrillic Windows paths."""
    try:
        extension = path.suffix.lower() or ".png"
        ok, encoded = cv2.imencode(extension, image)
        if not ok:
            return False
        encoded.tofile(str(path))
        return True
    except Exception:
        return False


def _resize_cover_cv2(image, width: int, height: int, cv2):
    """Resize and center-crop to fill a 16:9 canvas without letterbox bars."""
    h, w = image.shape[:2]
    # Avoid a redundant interpolation when the source is already at the chosen canvas size.
    if w == width and h == height:
        return image.copy()
    scale = max(width / max(1, w), height / max(1, h))
    resized_w = max(width, int(round(w * scale)))
    resized_h = max(height, int(round(h * scale)))
    interpolation = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
    resized = cv2.resize(image, (resized_w, resized_h), interpolation=interpolation)
    x0 = max(0, (resized_w - width) // 2)
    y0 = max(0, (resized_h - height) // 2)
    return resized[y0:y0 + height, x0:x0 + width].copy()


# Subject adaptation uses prompt semantics only. No image face/person detectors are used.
# Visual prompt/image cues adapt the strength of 2.5D depth warping. Living subjects
# retain the effect at reduced strength rather than being routed entirely to Ken Burns.
_LIVING_SUBJECT_PATTERN = re.compile(
    r"\b(?:"
    r"person|people|human|man|woman|men|women|boy|girl|child|children|baby|face|portrait|"
    r"warrior|soldier|knight|rider|riders|king|queen|prince|princess|father|mother|family|"
    r"peasant|farmer|villager|villagers|monk|guard|guards|hunter|fisherman|crowd|army|"
    r"horse|horses|donkey|donkeys|mule|mules|wolf|wolves|dog|dogs|cat|cats|bird|birds|"
    r"eagle|deer|goat|sheep|cow|cattle|bull|pig|fox|bear|lion|tiger|camel|ox|animal|animals|"
    r"mężczyzna|mężczyźni|kobieta|kobiety|człowiek|ludzie|twarz|portret|chłopiec|dziewczyna|"
    r"dziecko|wojownik|żołnierz|rycerz|jeździec|koń|osioł|wilk|pies|kot|ptak|jeleń|koza|owca|"
    r"krowa|byk|lis|niedźwiedź|lew|tygrys|wielbłąd|mnich|chłop|król|królowa|książę|księżniczka|"
    r"ojciec|matka|rodzina|tłum|strażnik|łowca|zwierzę|zwierzęta|"
    r"человек|люди|мужчина|женщина|мужчины|женщины|лицо|портрет|мальчик|девочка|ребёнок|ребенок|"
    r"воин|солдат|рыцарь|всадник|лошадь|осёл|осел|волк|собака|кошка|птица|олень|коза|овца|"
    r"корова|бык|лиса|медведь|лев|тигр|верблюд|монах|крестьянин|король|королева|принц|принцесса|"
    r"отец|мать|семья|толпа|стражник|охотник|животное|животные"
    r")\b",
    re.IGNORECASE,
)


_LIVING_NEGATION_PATTERN = re.compile(
    r"\b(?:no|without|exclude|excluding|avoid|absent|absence\s+of|free\s+of|none|nobody|not\s+visible|not\s+present|not\s+shown|no\s+visible)\b",
    re.IGNORECASE,
)


def _find_positive_living_match(text: str):
    """Find a positive person/animal cue while ignoring common negative prompt text."""
    for match in _LIVING_SUBJECT_PATTERN.finditer(text or ""):
        token = match.group(0).lower()
        # Avoid matching "man" inside the style phrase "man-made structure".
        if token == "man" and re.match(r"[\s-]*made\b", (text or "")[match.end():], re.IGNORECASE):
            continue
        # Only consider negation since the last clause boundary; a later clause
        # may positively describe a subject after saying "no people in the distance".
        before = (text or "")[:match.start()]
        clause_start = max(before.rfind(ch) for ch in (",", ";", ".", "\n", "!", "?")) + 1
        clause_prefix = before[clause_start:]
        neg = _LIVING_NEGATION_PATTERN.search(clause_prefix)
        if neg and not re.search(r"\b(?:but|however|yet|although|instead)\b", clause_prefix[neg.end():], re.IGNORECASE):
            continue
        return match
    return None


def scene_has_living_subject(
    record: dict,
    scene_plan: Any = None,
    characters: Any = None,
    image_path: Path | None = None,
) -> tuple[bool, str]:
    """Classify living subjects from the VISUAL PROMPT only (no face detector).

    We intentionally avoid computer-vision face/person detection here because it
    produced false positives on backgrounds and non-human shapes. Narration text is
    not used: only what the image prompt or, when absent, scene plan says is visible.
    """
    visual_prompt_parts = [
        str(record.get("prompt", "")),
        str(record.get("visual_prompt", "")),
        str(record.get("scene_description", "")),
    ]
    visual_prompt = " ".join(p for p in visual_prompt_parts if p).strip()
    plan_text = str(scene_plan or record.get("plan", ""))

    if visual_prompt:
        match = _find_positive_living_match(visual_prompt)
        if match:
            return True, f"visual prompt marker '{match.group(0)}'"
    else:
        match = _find_positive_living_match(plan_text)
        if match:
            return True, f"scene plan marker '{match.group(0)}'"

    # Known character names are only a text cue when explicitly present in the
    # visual prompt; no image face/person detectors are called.
    if visual_prompt and isinstance(characters, dict):
        names: list[str] = []
        for key, value in characters.items():
            if isinstance(key, str) and len(key.strip()) >= 3 and key.lower() not in {
                "main", "primary", "secondary", "description", "appearance", "role", "context"
            }:
                names.append(key.strip())
            if isinstance(value, dict):
                for field in ("name", "character_name", "character"):
                    val = value.get(field)
                    if isinstance(val, str) and len(val.strip()) >= 3:
                        names.append(val.strip())
        for name in sorted(set(names), key=len, reverse=True):
            if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", visual_prompt, re.IGNORECASE):
                return True, f"visual prompt character '{name}'"

    return False, "no living-subject cue in visual prompt"


def choose_parallax_motion_profile(
    record: dict,
    scene_plan: Any,
    characters: Any,
    image_path: Path,
    scene_index: int | None = None,
    prev_zooms: list[str] | None = None,
    forced_zoom_dir: str | None = None,
) -> dict[str, Any]:
    """Choose deterministic 2.5D movement; living subjects retain centered calibrated depth."""
    living, reason = scene_has_living_subject(record, scene_plan=scene_plan, characters=characters, image_path=None)
    raw_visual_prompt = " ".join([str(record.get("prompt", "")), str(record.get("visual_prompt", "")),
                                  str(record.get("scene_description", ""))])
    raw_visual_prompt = re.split(r"(?im)^\s*(?:STYLE LOCK|NEGATIVE(?: PROMPT)?|DO NOT|AVOID|INSTRUCTIONS?)\s*[:\-]", raw_visual_prompt)[0]
    raw_visual_prompt = re.sub(r"(?i)\b(?:no person|without people|no humans|first-person|third-person|no talking[- ]head)\b", " ", raw_visual_prompt)
    prompt = raw_visual_prompt.lower()
    landscape_cues = re.compile(
        r"\b(?:landscape|wide shot|panorama|desert|mountain|mountains|valley|plain|road|trail|forest|woods|coast|ocean|sea|river|lake|field|horizon|skyline|cityscape|village|architecture|building|buildings|ruins|temple|castle|fortress|street|courtyard|interior|hall|room|corridor|canyon|cliff|waterfall|bridge|object|objects|still life|пейзаж|горы|пустыня|долина|дорога|лес|река|озеро|море|океан|поле|горизонт|здание|архитектура|руины|замок|улица|двор|интерьер|зал|комната|коридор|предмет)\b",
        re.IGNORECASE,
    )
    environment_scene = bool(landscape_cues.search(prompt)) and not living
    seed_source = f"{image_path.name}|{record.get('prompt', '')}|{scene_plan or ''}|{scene_index}"
    seed = int(hashlib.md5(seed_source.encode("utf-8", errors="ignore")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    if living:
        strength = 0.34
        zoom_amount = rng.choice([0.20, 0.21, 0.22, 0.23])
        pan_dir, pan_multiplier = "center", 0.0
    elif environment_scene:
        strength = 1.00
        zoom_amount = rng.choice([0.23, 0.24, 0.25, 0.26])
        pan_dir = rng.choice(["left", "right", "diagonal_left", "diagonal_right", "center"])
        pan_multiplier = rng.uniform(0.45, 0.55)
    else:
        strength = 0.76
        zoom_amount = rng.choice([0.21, 0.22, 0.23, 0.24])
        pan_dir = rng.choice(["left", "right", "diagonal_left", "diagonal_right", "center"])
        pan_multiplier = rng.uniform(0.45, 0.55)
    zoom_dir = str(forced_zoom_dir) if forced_zoom_dir in {"in", "out"} else rng.choice(["in", "out"])
    prior = list(prev_zooms or [])
    if forced_zoom_dir not in {"in", "out"} and len(prior) >= 2 and prior[-1] == prior[-2] == zoom_dir:
        zoom_dir = "out" if zoom_dir == "in" else "in"
    profile = "pull_out" if zoom_dir == "out" else "push_in"
    turn_strength = rng.choice([0.62, 0.66, 0.70])
    turn_direction = rng.choice([-1.0, 1.0])
    return {
        "living_subject": living,
        "reason": reason if living else ("landscape/environment prompt" if environment_scene else "general visual prompt"),
        "depth_strength": strength, "zoom_amount": zoom_amount, "profile": profile,
        "pan_dir": pan_dir, "zoom_dir": zoom_dir, "pan_multiplier": pan_multiplier,
        "turn_strength": turn_strength, "turn_direction": turn_direction,
    }


def render_parallax_slide(
    image_path: Path,
    depth_map_path: Path,
    output_path: Path,
    duration: float,
    fps: int = FPS,
    fade_out_dur: float = 0.0,
    zoom_dir: str = "in",
    pan_dir: str = "right",
    depth_strength: float = 1.0,
    zoom_amount: float | None = None,
    pan_multiplier: float = 1.0,
    motion_profile: str = "push_in",
    turn_strength: float = 0.45,
    turn_direction: float = 1.0,
) -> bool:
    """Render a still with a centered one-way zoom and the existing MiDaS 2.5D depth effect.

    The caller selects only push-in or pull-out and keeps the camera center fixed.
    Foreground/background depth-layer displacement remains enabled; this is a 2.5D
    approximation, not a true 3D reconstruction.
    """
    torch, cv2, np = _load_depth_estimator()
    estimate_depth_map(image_path, depth_map_path)

    image = _cv2_read_unicode(image_path, cv2.IMREAD_COLOR, cv2, np)
    depth_u8 = _cv2_read_unicode(depth_map_path, cv2.IMREAD_GRAYSCALE, cv2, np)
    if image is None or depth_u8 is None:
        raise RuntimeError("Не удалось прочитать исходное изображение или карту глубины")

    canvas_w = PARALLAX_CANVAS_WIDTH
    canvas_h = PARALLAX_CANVAS_HEIGHT
    image_canvas = _resize_cover_cv2(image, canvas_w, canvas_h, cv2)
    depth_canvas = _resize_cover_cv2(depth_u8, canvas_w, canvas_h, cv2).astype(np.float32) / 255.0
    # No face/person detector is used. A broad depth blur softens small depth
    # fluctuations without making false face detections decide the camera effect.
    depth_canvas = cv2.GaussianBlur(depth_canvas, (0, 0), sigmaX=PARALLAX_DEPTH_RENDER_SMOOTH_SIGMA)
    # MiDaS creates relative, not metric, depth. Expand the mid-range contrast
    # slightly to make near/far layers read more clearly without increasing camera
    # turn displacement. This is post-processing on each loaded map, so cache files
    # remain valid and do not need to be re-inferred by MiDaS.
    depth_canvas = np.clip(
        (depth_canvas - 0.5) * PARALLAX_DEPTH_CONTRAST + 0.5,
        0.0, 1.0,
    ).astype(np.float32)
    preserve_headroom = False

    # Depth-map warping can bend people/animals because hidden geometry is not
    # reconstructed. Still render every slide with 2.5D, but attenuate differential
    # motion for likely living subjects. Landscape/architecture gets full strength.
    depth_strength = max(0.22, min(1.25, float(depth_strength)))
    depth_dx = (depth_canvas - 0.5) * float(PARALLAX_DISPLACEMENT_X) * depth_strength
    depth_dy = (depth_canvas - 0.5) * float(PARALLAX_DISPLACEMENT_Y) * depth_strength
    # Universal subtle yaw-like movement: every still receives a randomly directed
    # horizontal/vertical shift per depth layer, regardless of the scene category.
    # The low fixed amplitude and contrast-shaped depth map are meant to show
    # separation without relying on strong lateral camera panning.
    turn_strength = max(0.0, min(1.0, float(turn_strength)))
    turn_dx = (depth_canvas - 0.5) * PARALLAX_CAMERA_TURN_X * turn_strength * (1.0 if turn_direction >= 0 else -1.0)
    turn_dy = (depth_canvas - 0.5) * PARALLAX_CAMERA_TURN_Y * turn_strength * (1.0 if turn_direction >= 0 else -1.0)

    # All movement vectors are monotonic for the whole slide. "orbit_*" is a
    # shallow diagonal side pass with depth-separated layers, not a reversing arc.
    if pan_dir == "center":
        # Zoom-first move: keep the camera center fixed while depth layers shift
        # gently relative to one another. This preserves depth without a lateral orbit.
        move_x, move_y = 0.34, 0.08
        camera_move_x, camera_move_y = 0.0, 0.0
    elif pan_dir in {"left", "side_left", "diagonal_left", "orbit_left"}:
        move_x = -1.0
        move_y = {"left": 0.12, "side_left": 0.0, "diagonal_left": 0.32, "orbit_left": -0.20}[pan_dir]
        camera_move_x, camera_move_y = move_x, move_y
    elif pan_dir in {"right", "side_right", "diagonal_right", "orbit_right"}:
        move_x = 1.0
        move_y = {"right": -0.12, "side_right": 0.0, "diagonal_right": -0.32, "orbit_right": 0.20}[pan_dir]
        camera_move_x, camera_move_y = move_x, move_y
    elif pan_dir == "up":
        move_x, move_y = 0.12, -1.0
        camera_move_x, camera_move_y = move_x, move_y
    else:
        move_x, move_y = -0.12, 1.0
        camera_move_x, camera_move_y = move_x, move_y

    # Precompute the depth displacement field once at source-canvas resolution.
    # Each output frame samples only FHD-sized coordinate maps, avoiding full 2752x1536
    # map arithmetic on every frame while retaining the native-resolution color source.
    offset_x = (-depth_dx * move_x + turn_dx).astype(np.float32)
    offset_y = (-depth_dy * move_y + turn_dy).astype(np.float32)

    total_frames = max(1, int(round(float(duration) * fps)))
    log.info(
        "🎥 [Parallax Motion] profile=%s zoom=%s %.0f%% pan=%s depth=%.2f turn=%.2f dir=%+.0f",
        motion_profile, zoom_dir,
        (PARALLAX_ZOOM_AMOUNT if zoom_amount is None else float(zoom_amount)) * 100,
        pan_dir, depth_strength, turn_strength, turn_direction,
    )
    output_w = PARALLAX_RENDER_WIDTH
    output_h = PARALLAX_RENDER_HEIGHT
    # Reuse these output-coordinate grids for every frame to avoid repeated
    # multi-megabyte allocations during a long slide render.
    out_y_grid, out_x_grid = np.mgrid[0:output_h, 0:output_w].astype(np.float32)
    out_x_centered = out_x_grid - output_w * 0.5
    out_y_centered = out_y_grid - output_h * 0.5
    rotation_angle = math.radians(random.choice((-1, 1)) * random.uniform(0.25, 0.45))
    rotation_cos, rotation_sin = math.cos(rotation_angle), math.sin(rotation_angle)
    rot_x_centered = out_x_centered * rotation_cos - out_y_centered * rotation_sin
    rot_y_centered = out_x_centered * rotation_sin + out_y_centered * rotation_cos
    temp_path = output_path.with_suffix(".parallax-temp.mp4")
    ensure_directory(output_path.parent)

    # Keep the existing visual character controls, but combine them with the
    # relative displacement of foreground and background from the depth map.
    grain = 1 if ACTIVE_VISUAL_STYLE_ID in {"comic_explainer", "cartoon_2d"} else random.randint(*FILM_GRAIN_STRENGTH)
    contrast = round(random.uniform(*COLOR_CONTRAST_JITTER), 3)
    brightness = round(random.uniform(*COLOR_BRIGHTNESS_JITTER), 3)
    saturation = round(random.uniform(*COLOR_SATURATION_JITTER), 3)
    bitrate = f"{random.randint(*VIDEO_BITRATE_RANGE)}k"
    vf_parts = [
        f"eq=contrast={contrast}:brightness={brightness}:saturation={saturation}",
    ]
    if ENABLE_PARALLAX_FILM_GRAIN and grain > 0:
        vf_parts.append(f"noise=alls={grain}:allf=t+u")
    vf_parts.append("setsar=1,format=yuv420p")
    if fade_out_dur > 0:
        fade = min(float(fade_out_dur), max(0.05, float(duration) - 0.05))
        vf_parts.append(f"fade=t=out:st={max(0.0, duration - fade):.3f}:d={fade:.3f}")
    video_filter = ",".join(vf_parts)

    def frame_iter():
        for frame_idx in range(total_frames):
            t = frame_idx / max(1, total_frames - 1)
            # Quintic smootherstep: zero velocity and acceleration at both ends.
            # motion progresses only 0 -> 1 (never reverses across the midpoint),
            # which avoids the apparent mid-shot snap in the previous -1 -> +1 path.
            eased = t * t * (3.0 - 2.0 * t)
            # Cubic smoothstep gives a natural S-curve with zero endpoint velocity.
            motion = eased
            # Single-direction depth motion only. Each depth layer moves gradually
            # in the same direction for the entire clip; there is no arc that
            # reverses around mid-shot. More conservative offsets limit rubbery
            # distortion at object boundaries.
            scene_zoom_amount = PARALLAX_ZOOM_AMOUNT if zoom_amount is None else max(0.06, min(0.26, float(zoom_amount)))
            if zoom_dir == "out":
                scale = 1.0 + scene_zoom_amount * (1.0 - eased)
            else:
                scale = 1.0 + scene_zoom_amount * eased

            # IMPORTANT: use floating-point source coordinates for every output
            # pixel. The previous implementation rounded crop width/height and
            # camera position to integers, so zoom changed in visible 1-pixel
            # steps every several frames ("staircase" motion). Remap directly
            # to the 1920x1080 output grid with sub-pixel coordinates instead.
            # Camera center drifts in one direction only; no sine/orbit term.
            cam_cx = canvas_w * 0.5 + camera_move_x * eased * PARALLAX_PAN_AMOUNT_X * pan_multiplier
            cam_cy = canvas_h * 0.5 + camera_move_y * eased * PARALLAX_PAN_AMOUNT_Y * pan_multiplier

            # Preserve headroom smoothly rather than shifting the crop by
            # rounded integers. When a face/head sits near the top, bias the
            # source window upward in proportion to the zoomed crop's height.
            # Convert output FHD pixels to source-canvas pixels with the same scale on
            # both axes. Using the smaller ratio fills the FHD frame while preserving
            # aspect ratio (a few source pixels may be cropped horizontally).
            output_pixel_scale = min(canvas_w / output_w, canvas_h / output_h) * 0.97
            if preserve_headroom:
                visible_h = output_h * output_pixel_scale / scale
                vertical_room = max(0.0, canvas_h - visible_h)
                cam_cy -= 0.34 * vertical_room

            visible_w = output_w * output_pixel_scale / scale
            visible_h = output_h * output_pixel_scale / scale
            horizontal_room = max(0.0, canvas_w - visible_w)
            vertical_room = max(0.0, canvas_h - visible_h)
            cam_cx += camera_move_x * float(pan_multiplier) * horizontal_room * 0.12 * motion
            cam_cy += camera_move_y * float(pan_multiplier) * vertical_room * 0.12 * motion
            map_x_out = cam_cx + rot_x_centered * output_pixel_scale / scale
            map_y_out = cam_cy + rot_y_centered * output_pixel_scale / scale
            map_x_out = map_x_out.astype(np.float32)
            map_y_out = map_y_out.astype(np.float32)

            # Sample the precomputed offset fields on the FHD camera view, then
            # sample the native-resolution color image once. The final encoded frame
            # is FHD, but no reduced-resolution color canvas is used before this step.
            offset_x_view = cv2.remap(
                offset_x, map_x_out, map_y_out,
                interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE,
            )
            offset_y_view = cv2.remap(
                offset_y, map_x_out, map_y_out,
                interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE,
            )
            source_map_x = (map_x_out + offset_x_view * motion).astype(np.float32)
            source_map_y = (map_y_out + offset_y_view * motion).astype(np.float32)
            frame = cv2.remap(
                image_canvas, source_map_x, source_map_y,
                # Cubic sampling is sharper than bilinear when mapping native pixels
                # down to FHD, without the much higher cost of an 8x8 Lanczos kernel.
                interpolation=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT101,
            )
            yield np.ascontiguousarray(frame)

    last_error = None
    for encoder in (HW_ENCODER, "libx264"):
        temp_path.unlink(missing_ok=True)
        if encoder == "libx264":
            encoder_args = ["-preset", "fast", "-crf", "18"]
        else:
            encoder_args = ["-preset", ENCODER_PRESET, "-rc", "vbr", "-b:v", bitrate, "-maxrate", "12000k"]
        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{output_w}x{output_h}", "-r", str(fps), "-i", "pipe:0",
            "-vf", video_filter,
            "-c:v", encoder, *encoder_args,
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-video_track_timescale", "15360", "-an", "-t", f"{duration:.3f}",
            *FFMPEG_CLEAN_META_VIDEO, str(temp_path),
        ]
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            assert proc.stdin is not None
            for frame in frame_iter():
                proc.stdin.write(frame.tobytes())
            proc.stdin.close()
            proc.stdin = None
            return_code = proc.wait(timeout=max(900, int(duration * 4)))
            if return_code == 0 and is_valid_video(temp_path):
                temp_path.replace(output_path)
                log.info(
                    "🎥 [Parallax] 2.5D-слайд готов: %s | %.1fs | %s",
                    output_path.name, duration, encoder,
                )
                return True
            last_error = RuntimeError(f"FFmpeg encoder {encoder} returned {return_code}")
        except Exception as exc:
            last_error = exc
            if proc is not None:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
        finally:
            if proc is not None and proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        log.warning("⚠️ [Parallax] Кодировщик %s не сработал; пробуем следующий: %s", encoder, last_error)

    temp_path.unlink(missing_ok=True)
    raise RuntimeError(f"Не удалось закодировать 2.5D-слайд: {last_error}")


def audit_mp4_container(path: Path) -> list[str]:
    """Inspect top-level MP4 boxes; skip mdat payloads and inspect moov metadata."""
    issues: list[str] = []
    if not path.is_file():
        return ["file_missing"]
    size = path.stat().st_size
    markers = (b"lavf", b"lavc", b"google", b"encoder", b"writing application", b"creation_time", b"com.apple.quicktime", b"handler_name")
    try:
        with path.open("rb") as media:
            while media.tell() + 8 <= size:
                start = media.tell()
                head = media.read(8)
                if len(head) != 8:
                    break
                box_size, box_type = struct.unpack(">I4s", head)
                header_size = 8
                if box_size == 1:
                    ext = media.read(8)
                    if len(ext) != 8:
                        issues.append("truncated_extended_box")
                        break
                    box_size = struct.unpack(">Q", ext)[0]
                    header_size = 16
                elif box_size == 0:
                    box_size = size - start
                if box_size < header_size or start + box_size > size:
                    issues.append(f"invalid_box_size:{box_type.decode('latin1', errors='replace')}")
                    break
                box_name = box_type.decode("latin1", errors="replace")
                if box_type == b"uuid":
                    issues.append(f"top_level_uuid_box@{start}")
                payload_size = box_size - header_size
                if box_type == b"mdat":
                    media.seek(payload_size, os.SEEK_CUR)
                elif box_type in {b"moov", b"udta", b"meta"}:
                    payload = media.read(min(payload_size, 64 * 1024 * 1024))
                    lower = payload.lower()
                    for marker in markers:
                        if marker in lower:
                            issues.append(f"service_tag:{marker.decode('ascii', errors='replace')}@{box_name}")
                    if len(payload) < payload_size:
                        media.seek(payload_size - len(payload), os.SEEK_CUR)
                else:
                    media.seek(payload_size, os.SEEK_CUR)
                if media.tell() <= start:
                    issues.append("box_reader_stalled")
                    break
    except Exception as exc:
        issues.append(f"audit_error:{type(exc).__name__}:{exc}")
    return sorted(set(issues))


def inspect_mp4_provenance(path: Path) -> dict[str, Any]:
    """Read-only indicator scan for embedded provenance/C2PA/JUMBF markers.

    Never remuxes, rewrites, strips, or hides metadata. This is not a full
    C2PA signature validator; no detected marker does not prove absence.
    """
    report: dict[str, Any] = {
        "version": 1, "status": "not_checked", "markers": [],
        "format_tags": {}, "stream_tags": [], "checked_at": now_iso(),
    }
    if not path.is_file():
        report["status"] = "file_missing"
        return report

    marker_names = (b"c2pa", b"jumb", b"jumbf", b"content credentials")
    found: set[str] = set()
    try:
        file_size = path.stat().st_size
        with path.open("rb") as media:
            windows = [media.read(min(file_size, 16 * 1024 * 1024))]
            if file_size > 16 * 1024 * 1024:
                media.seek(max(0, file_size - 16 * 1024 * 1024))
                windows.append(media.read(16 * 1024 * 1024))
        for window in windows:
            lowered = window.lower()
            for marker in marker_names:
                if marker in lowered:
                    found.add(marker.decode("ascii"))
    except OSError as exc:
        report["status"] = "read_error"
        report["error"] = str(exc)
        return report

    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            parsed = json.loads(probe.stdout)
            fmt = parsed.get("format", {}) if isinstance(parsed, dict) else {}
            report["format_tags"] = fmt.get("tags", {}) if isinstance(fmt, dict) else {}
            report["stream_tags"] = [
                stream.get("tags", {}) for stream in (parsed.get("streams", []) if isinstance(parsed, dict) else [])
                if isinstance(stream, dict) and stream.get("tags")
            ]
        else:
            report["ffprobe_error"] = (probe.stderr or "ffprobe returned no data")[-1000:]
    except Exception as exc:
        report["ffprobe_error"] = str(exc)

    report["markers"] = sorted(found)
    tags_text = json.dumps(
        {"format": report["format_tags"], "streams": report["stream_tags"]},
        ensure_ascii=False, default=str,
    ).casefold()
    indicators = [name for name in ("c2pa", "jumbf", "content credentials", "provenance") if name in tags_text]
    report["tag_indicators"] = indicators
    report["status"] = "markers_detected" if found or indicators else "no_known_markers_detected"
    log.info(
        "[Provenance] Read-only MP4 inspection: %s | status=%s | byte_markers=%s | tag_indicators=%s",
        path.name, report["status"], report["markers"], indicators,
    )
    return report


def has_audio_stream(path: Path) -> bool:
    """Return True when the media file contains an audio stream."""
    if not path.exists():
        return False
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=index",
                "-of", "csv=p=0",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def remove_video_audio_track(path: Path) -> bool:
    """Remove every audio stream from a video file without re-encoding its video."""
    if not is_valid_video(path):
        return False
    if not has_audio_stream(path):
        return True
    temp_path = path.with_name(f"{path.stem}.silent_tmp{path.suffix}")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(path),
                "-map", "0:v:0", "-c:v", "copy", "-an",
                "-movflags", "+faststart", str(temp_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
        )
        if not is_valid_video(temp_path) or has_audio_stream(temp_path):
            temp_path.unlink(missing_ok=True)
            return False
        temp_path.replace(path)
        log.info("🔇 [Intro Audio] Удалена аудиодорожка из %s (речь/музыка Flow не попадут в ролик).", path.name)
        return True
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        log.error("[Intro Audio] Не удалось удалить аудио из %s: %s", path.name, exc)
        return False


def _is_native_fhd_h264(path: Path, target_duration: float | None = None) -> bool:
    """Check if a clip already matches the montage video stream so it need not be re-encoded."""
    if not is_valid_video(path):
        return False
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,profile,width,height,pix_fmt,avg_frame_rate,sample_aspect_ratio,time_base",
                "-of", "json", str(path),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", timeout=30, check=True,
        )
        streams = json.loads(result.stdout).get("streams", [])
        if not streams:
            return False
        stream = streams[0]
        rate = str(stream.get("avg_frame_rate", "0/1"))
        try:
            a, b = rate.split("/", 1)
            fps_value = float(a) / max(1.0, float(b))
        except Exception:
            return False
        if not (
            stream.get("codec_name") == "h264"
            and int(stream.get("width", 0)) == 1920
            and int(stream.get("height", 0)) == 1080
            and stream.get("pix_fmt") == "yuv420p"
            and stream.get("profile") in {"High", "Main"}
            and stream.get("sample_aspect_ratio") == "1:1"
            and stream.get("time_base") == "1/15360"
            and abs(fps_value - FPS) < 0.02
        ):
            return False
        if target_duration is not None:
            actual = get_duration(path)
            if actual is None or abs(actual - float(target_duration)) > 0.08:
                return False
        return True
    except Exception:
        return False


def normalize_clip(
    input_path: Path,
    output_path: Path,
    target_duration: float | None = None,
    preserve_audio: bool = False,
) -> None:
    """Normalize clips but stream-copy an already-compliant FHD video stream.

    This avoids an unnecessary lossy video encode after the native-resolution MiDaS
    render. Only audio may be re-encoded when the montage requires a uniform audio track.
    """
    duration_arg = ["-t", f"{target_duration:.3f}"] if target_duration is not None else []

    if _is_native_fhd_h264(input_path, target_duration):
        duration = target_duration if target_duration is not None else (get_duration(input_path) or 0.1)
        if preserve_audio and not has_audio_stream(input_path):
            fast_cmd = [
                "ffmpeg", "-y", "-i", str(input_path),
                "-f", "lavfi", "-t", f"{duration:.3f}", "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                "-t", f"{duration:.3f}", str(output_path),
            ]
        elif preserve_audio:
            fast_cmd = [
                "ffmpeg", "-y", "-i", str(input_path),
                "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                "-af", "aresample=async=1:first_pts=0",
                "-t", f"{duration:.3f}", str(output_path),
            ]
        else:
            fast_cmd = [
                "ffmpeg", "-y", "-i", str(input_path),
                "-map", "0:v:0", "-c:v", "copy", "-an",
                "-t", f"{duration:.3f}", str(output_path),
            ]
        try:
            subprocess.run(fast_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            log.warning("[Montage] Не удалось быстро скопировать совместимый FHD-поток; выполняю полную нормализацию: %s", exc)

    vf = (
        "scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )

    if preserve_audio:
        duration = target_duration if target_duration is not None else get_duration(input_path)
        if duration is None:
            duration = 8.0

        if has_audio_stream(input_path):
            cmd = [
                "ffmpeg", "-y",
                "-i", str(input_path),
                "-vf", vf,
                "-r", str(FPS),
                "-pix_fmt", "yuv420p", "-profile:v", "high", "-video_track_timescale", "15360",
                "-c:v", HW_ENCODER,
                "-preset", ENCODER_PRESET,
                "-b:v", "8000k",
                "-map", "0:v:0",
                "-map", "0:a:0",
                "-c:a", "aac",
                "-ar", "44100",
                "-ac", "2",
                "-af", "aresample=async=1:first_pts=0",
                *duration_arg,
                str(output_path),
            ]
        else:
            # Add silence to clips without audio so all concat inputs have
            # identical video+audio stream layouts.
            cmd = [
                "ffmpeg", "-y",
                "-i", str(input_path),
                "-f", "lavfi",
                "-t", f"{duration:.3f}",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-filter_complex", f"[0:v]{vf}[v]",
                "-map", "[v]",
                "-map", "1:a:0",
                "-c:v", HW_ENCODER,
                "-preset", ENCODER_PRESET,
                "-b:v", "8000k",
                "-c:a", "aac",
                "-ar", "44100",
                "-ac", "2",
                "-t", f"{duration:.3f}",
                str(output_path),
            ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-vf", vf,
            "-r", str(FPS),
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-video_track_timescale", "15360",
            "-c:v", HW_ENCODER,
            "-preset", ENCODER_PRESET,
            "-b:v", "8000k",
            "-an",
            *duration_arg,
            str(output_path),
        ]

    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    except subprocess.CalledProcessError:
        fallback = cmd.copy()
        if HW_ENCODER in fallback:
            i = fallback.index(HW_ENCODER)
            fallback[i] = "libx264"
            if i + 1 < len(fallback) and fallback[i + 1] == "-preset":
                # Keep preset position; replace encoder only.
                pass
            elif i + 1 < len(fallback):
                fallback[i + 1] = "fast"
        # The explicit FFmpeg command above already has a suitable AAC path.
        # For CPU fallback, force the video preset correctly.
        try:
            if "libx264" in fallback:
                ei = fallback.index("libx264")
                if ei + 2 < len(fallback) and fallback[ei + 1] == "-preset":
                    fallback[ei + 2] = "fast"
            subprocess.run(
                fallback,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            # Never silently remove audio when preserve_audio=True.
            # A real failure must remain visible instead of producing a
            # successful-looking but silent intro.
            raise


def normalize_clip_for_concat(
    input_path: Path,
    output_path: Path,
    target_duration: float | None = None,
    preserve_audio: bool = False,
) -> None:
    normalize_clip(
        input_path,
        output_path,
        target_duration=target_duration,
        preserve_audio=preserve_audio,
    )

def trim_video(input_path: Path, output_path: Path, duration: float) -> None:
    """Trim a media file to an explicitly requested duration."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-t", f"{duration:.3f}",
            "-c:v", "copy",
            "-an",
            str(output_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _ffconcat_file_line(path: Path) -> str:
    """Serialize a path for FFmpeg's concat demuxer (not shell quoting).

    FFmpeg concat lists use their own single-quote parser. A literal apostrophe
    must be represented as the sequence '\''; otherwise project folders such as
    "The General's Mindset" are parsed as truncated paths and FFmpeg exits with
    AVERROR(ENOENT) (-2 / 4294967294 on Windows).
    """
    value = path.resolve().as_posix()
    value = value.replace("\\", "\\\\").replace("'", "'\\''")
    return f"file '{value}'\n"


def _write_ffconcat_listing(paths: list[Path], listing: Path) -> None:
    listing.parent.mkdir(parents=True, exist_ok=True)
    with listing.open("w", encoding="utf-8", newline="\n") as handle:
        for path in paths:
            handle.write(_ffconcat_file_line(path))


def _run_concat_demuxer_with_fallback(
    listing: Path,
    temp_output: Path,
    output_path: Path,
    *,
    preserve_audio: bool,
    context: str,
    duration_hint: float | None = None,
) -> bool:
    """Try packet-copy first, then re-encode if streams aren't concat-compatible."""
    timeout = max(300, int(duration_hint * 10)) if duration_hint else 900
    base = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(listing),
    ]
    strategies: list[tuple[str, list[str]]] = [
        ("stream-copy", ["-c", "copy", *(FFMPEG_CLEAN_META_AV if preserve_audio else FFMPEG_CLEAN_META_VIDEO)]),
    ]
    audio_args = (
        ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"]
        if preserve_audio
        else ["-an"]
    )
    strategies.extend([
        ("hardware-reencode", [
            "-map", "0:v:0", *audio_args,
            "-c:v", HW_ENCODER, "-preset", ENCODER_PRESET,
            "-b:v", "8000k", "-maxrate", "10000k",
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-movflags", "+faststart",
            *(FFMPEG_CLEAN_META_AV if preserve_audio else FFMPEG_CLEAN_META_VIDEO),
        ]),
        ("software-reencode", [
            "-map", "0:v:0", *audio_args,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-r", str(FPS), "-movflags", "+faststart",
            *(FFMPEG_CLEAN_META_AV if preserve_audio else FFMPEG_CLEAN_META_VIDEO),
        ]),
    ])
    failures: list[str] = []
    for strategy_name, output_args in strategies:
        temp_output.unlink(missing_ok=True)
        try:
            result = subprocess.run(
                base + output_args + [str(temp_output)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                errors="replace",
                timeout=timeout,
            )
            if is_valid_video(temp_output):
                temp_output.replace(output_path)
                log.info("✅ [FFmpeg concat] %s: сборка успешна через %s", context, strategy_name)
                return True
            detail = (result.stderr or "выходной файл не прошёл проверку видео").strip()
            failures.append(f"{strategy_name}: {detail[-1000:]}")
        except Exception as exc:
            detail = getattr(exc, "stderr", None) or str(exc)
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            failures.append(f"{strategy_name}: {str(detail)[-1000:]}")
        finally:
            if not is_valid_video(temp_output):
                temp_output.unlink(missing_ok=True)
    log.error("❌ [FFmpeg concat] Не удалось собрать %s. Попытки: %s", context, " | ".join(failures))
    return False


def _concat_normalized_parts(parts: list[Path], output_path: Path, temp_dir: Path, preserve_audio: bool = True) -> bool:
    """Hard-cut fallback for already normalized parts."""
    if not parts:
        return False
    listing = temp_dir / f"{output_path.stem}_fallback_concat.txt"
    _write_ffconcat_listing(parts, listing)
    temp = output_path.with_suffix(".fallback-temp.mp4")
    total_duration = sum(get_duration(path) or 0.0 for path in parts)
    return _run_concat_demuxer_with_fallback(
        listing, temp, output_path, preserve_audio=preserve_audio,
        context=output_path.name, duration_hint=total_duration or None,
    )


def _render_crossfade_group(
    input_paths: list[Path],
    durations: list[float],
    output_path: Path,
    temp_dir: Path,
    preserve_audio: bool,
    transitions: list[dict[str, Any]] | None = None,
) -> bool:
    """Render only requested dissolves/fade-to-black seams; hard-cut runs are pre-concatenated."""
    if len(input_paths) < 2 or len(input_paths) != len(durations):
        return False
    transitions = transitions or [{"type": "dissolve", "duration": SLIDE_CROSSFADE_SECONDS} for _ in range(len(input_paths) - 1)]
    if len(transitions) != len(input_paths) - 1:
        return False
    total_duration = sum(max(0.1, float(d)) for d in durations)
    filters = [f"[0:v]fps={FPS},settb=AVTB,format=yuv420p[v0]"]
    current_label = "v0"
    accumulated_duration = float(durations[0])
    for i in range(1, len(input_paths)):
        spec = transitions[i - 1] or {}
        transition = max(0.0, float(spec.get("duration", 0.0) or 0.0))
        if transition <= 0.0:
            return False
        # Freeze the incoming first frame for T seconds. The scene's actual motion
        # therefore starts on its exact scheduled boundary after the overlap.
        filters.append(f"[{i}:v]tpad=start_mode=clone:start_duration={transition:.3f},fps={FPS},settb=AVTB,format=yuv420p[p{i}]")
        next_label = f"xf{i}"
        kind = "fadeblack" if str(spec.get("type", "dissolve")) == "fadeblack" else "fade"
        offset = max(0.0, accumulated_duration - transition)
        filters.append(f"[{current_label}][p{i}]xfade=transition={kind}:duration={transition:.3f}:offset={offset:.3f}[{next_label}]")
        current_label = next_label
        accumulated_duration += float(durations[i])
    temp_output = output_path.with_suffix(".xfade-temp.mp4")
    temp_output.unlink(missing_ok=True)
    base_cmd = [FFMPEG_BIN, "-y", "-filter_complex_threads", "2"]
    for path in input_paths:
        base_cmd.extend(["-i", str(path)])
    if preserve_audio:
        base_cmd.extend(["-f", "lavfi", "-t", f"{total_duration:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])
    base_cmd.extend(["-filter_complex", ";".join(filters), "-map", f"[{current_label}]"])
    if preserve_audio:
        base_cmd.extend(["-map", f"{len(input_paths)}:a:0", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2"])
    else:
        base_cmd.append("-an")
    encoders = [(HW_ENCODER, ["-preset", ENCODER_PRESET, "-rc", "vbr", "-b:v", "8000k", "-maxrate", "10000k"]),
                ("libx264", ["-preset", "fast", "-crf", "20"])]
    for encoder, encoder_args in encoders:
        temp_output.unlink(missing_ok=True)
        cmd = base_cmd + ["-c:v", encoder, *encoder_args, "-pix_fmt", "yuv420p", "-r", str(FPS),
                          "-t", f"{total_duration:.3f}", *(FFMPEG_CLEAN_META_AV if preserve_audio else FFMPEG_CLEAN_META_VIDEO), str(temp_output)]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=max(900, int(total_duration * 5)))
            actual = get_duration(temp_output)
            if is_valid_video(temp_output) and actual is not None and abs(actual - total_duration) < 0.25:
                temp_output.replace(output_path)
                log.info("[Transitions] %s segments; duration %.3fs; transition specs=%s", len(input_paths), actual, transitions)
                return True
        except Exception as exc:
            log.warning("[Transitions] xfade group failed on %s: %s", encoder, exc)
    temp_output.unlink(missing_ok=True)
    return False





def concatenate_videos(
    video_parts: list[tuple[Path, float | None]],
    output_path: Path,
    temp_dir: Path,
    preserve_audio: bool = False,
    crossfade_duration: float = 0.0,
    transition_start_index: int = 0,
    transitions: list[dict[str, Any]] | None = None,
) -> bool:
    """Normalize once, stream-copy hard-cut runs, and xfade only flagged boundaries."""
    indexed = [(idx, p, d) for idx, (p, d) in enumerate(video_parts) if p.exists() and is_valid_video(p)]
    if not indexed:
        return False
    created: list[Path] = []
    normalized = []
    try:
        for seq_idx, (source_idx, path, duration) in enumerate(indexed):
            norm = temp_dir / f"concat_norm_{output_path.stem}_{seq_idx:04d}.mp4"
            normalize_clip_for_concat(path, norm, target_duration=duration, preserve_audio=preserve_audio)
            if not is_valid_video(norm):
                raise RuntimeError(f"normalization failed: {path.name}")
            actual = float(duration) if duration is not None else (get_duration(norm) or 0.0)
            normalized.append((source_idx, norm, actual))
            created.append(norm)

        # Transition metadata is indexed by the original video_parts list; missing
        # legacy fields are interpreted as hard cuts for backward-compatible state.json.
        def spec_after(source_index: int) -> dict[str, Any]:
            if transitions and source_index < len(transitions):
                item = transitions[source_index] or {}
                return {"type": str(item.get("type", "cut")), "duration": max(0.0, float(item.get("duration", 0.0) or 0.0))}
            if crossfade_duration > 0 and source_index >= transition_start_index:
                return {"type": "dissolve", "duration": float(crossfade_duration)}
            return {"type": "cut", "duration": 0.0}

        prefix = [x for x in normalized if x[0] < transition_start_index]
        slides = [x for x in normalized if x[0] >= transition_start_index]
        last_source_index = max((x[0] for x in slides), default=-1)
        # Merge contiguous hard-cut scenes into one stream-copy file each.
        runs: list[tuple[Path, float]] = []
        run_transitions: list[dict[str, Any]] = []
        current: list[tuple[int, Path, float]] = []
        for item in slides:
            current.append(item)
            boundary = spec_after(item[0]) if item[0] < last_source_index else {"type": "cut", "duration": 0.0}
            if boundary["type"] in {"dissolve", "fadeblack"} and boundary["duration"] > 0:
                run_path = current[0][1] if len(current) == 1 else temp_dir / f"{output_path.stem}_cutrun_{len(runs):04d}.mp4"
                if len(current) > 1:
                    if not _concat_normalized_parts([x[1] for x in current], run_path, temp_dir, preserve_audio=preserve_audio):
                        raise RuntimeError("stream-copy concat of cut run failed")
                    created.append(run_path)
                runs.append((run_path, sum(x[2] for x in current)))
                run_transitions.append({"type": boundary["type"], "duration": boundary["duration"]})
                current = []
        if current:
            run_path = current[0][1] if len(current) == 1 else temp_dir / f"{output_path.stem}_cutrun_{len(runs):04d}.mp4"
            if len(current) > 1:
                if not _concat_normalized_parts([x[1] for x in current], run_path, temp_dir, preserve_audio=preserve_audio):
                    raise RuntimeError("stream-copy concat of final cut run failed")
                created.append(run_path)
            runs.append((run_path, sum(x[2] for x in current)))

        slide_output = None
        if len(runs) == 1:
            slide_output = runs[0][0]
        elif len(runs) > 1:
            slide_output = temp_dir / f"{output_path.stem}_mixed_transitions.mp4"
            if not _render_crossfade_group([x[0] for x in runs], [x[1] for x in runs],
                                           slide_output, temp_dir, preserve_audio, run_transitions):
                # If transition render fails, retain every scene and preserve exact clock via hard cuts.
                log.warning("[Transitions] Mixed xfade fallback to stream-copy hard cuts; all scenes retained.")
                if not _concat_normalized_parts([x[0] for x in runs], slide_output, temp_dir, preserve_audio=preserve_audio):
                    raise RuntimeError("fallback stream-copy concat failed")
            created.append(slide_output)

        final_parts = [x[1] for x in prefix]
        if slide_output is not None:
            final_parts.append(slide_output)
        if not final_parts:
            return False
        listing = temp_dir / f"{output_path.stem}_concat_inputs.txt"
        _write_ffconcat_listing(final_parts, listing)
        temp = output_path.with_suffix(".temp.mp4")
        total_duration = sum(get_duration(p) or 0.0 for p in final_parts)
        if not _run_concat_demuxer_with_fallback(listing, temp, output_path, preserve_audio=preserve_audio,
                                                  context=output_path.name, duration_hint=total_duration or None):
            raise RuntimeError(f"Не удалось собрать {output_path.name}")
        actual_total = get_duration(output_path)
        expected_total = sum(x[2] for x in normalized)
        if actual_total is not None and abs(actual_total - expected_total) >= 0.25:
            log.error("[A/V Timing] concat duration mismatch: expected=%.3fs actual=%.3fs delta=%.3fs",
                      expected_total, actual_total, actual_total - expected_total)
        log.info("✅ [Montage] %s | scenes=%s | cut-runs=%s | transition boundaries=%s",
                 output_path.name, len(normalized), len(runs), len(run_transitions))
        return is_valid_video(output_path)
    finally:
        for p in set(created):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass





def ensure_resolution(video_path: Path, output_path: Path):
    try:
        subprocess.run(['ffmpeg','-y','-i',str(video_path),'-vf','scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1','-r',str(FPS),'-pix_fmt','yuv420p','-c:v',HW_ENCODER,'-preset',ENCODER_PRESET,'-b:v','8000k','-an',str(output_path)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    except Exception:
        subprocess.run(['ffmpeg','-y','-i',str(video_path),'-vf','scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1','-r',str(FPS),'-pix_fmt','yuv420p','-c:v','libx264','-preset','fast','-crf','18','-an',str(output_path)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def concatenate_slides(slide_paths: list[Path], output_video_path: Path) -> bool:
    existing=[p for p in slide_paths if p.exists() and is_valid_video(p)]
    if not existing: return False
    temp_dir=existing[0].parent
    norm_slides=[]
    for i,p in enumerate(existing):
        norm=temp_dir/f'norm_{i:04d}.mp4'; ensure_resolution(p,norm); norm_slides.append(norm)
    txt=temp_dir/'inputs.txt'
    _write_ffconcat_listing(norm_slides, txt)
    temp=output_video_path.with_suffix('.temp.mp4')
    ok = _run_concat_demuxer_with_fallback(
        txt, temp, output_video_path, preserve_audio=False,
        context=output_video_path.name,
        duration_hint=sum(get_duration(p) or 0.0 for p in norm_slides) or None,
    )
    for p in norm_slides: p.unlink(missing_ok=True)
    return ok


def prepare_background_music(
    files: list[Path],
    target_duration: float,
    temp_dir: Path,
) -> Path | None:
    valid = [f for f in files if is_valid_audio(f)]
    if not valid:
        return None

    selected: list[Path] = []
    current = 0.0
    available = list(valid)

    while current < target_duration and available:
        path = random.choice(available)
        available.remove(path)
        duration = get_duration(path)
        if duration is None:
            continue
        selected.append(path)
        current += duration
        if not available and current < target_duration:
            available = list(valid)

    listing = temp_dir / "music_list.txt"
    _write_ffconcat_listing(selected, listing)

    output = temp_dir / "background_music.mp3"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(listing),
            "-c:a", "mp3",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2",
            "-t", str(target_duration + 5),
            str(output),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return output


def add_audio_to_video(
    video_path: Path,
    voice_path: Path,
    music_path: Path | None,
    output_path: Path,
) -> bool:
    """Mix narration with optional intro ambience/music, using finite audio streams.

    Do not use apad here. In real projects the source video can contain an AAC track
    with awkward EOF/timestamps; padded inputs combined with amix=duration=longest
    caused FFmpeg to write every video frame, then wait indefinitely for audio EOF.
    The narration is the first amix input and determines the mixed-audio duration.
    """
    video_duration = get_duration(video_path)
    if video_duration is None:
        log.error("[Audio Mix] Не удалось определить длительность видео: %s", video_path)
        return False

    voice_gain = float(VOICE_VOLUME_GAIN)
    duration = f"{video_duration:.3f}"
    use_music = bool(music_path and music_path.exists() and ENABLE_BACKGROUND_MUSIC)
    has_source_audio = has_audio_stream(video_path)
    log.info(
        "🔊 [Audio Mix] Intro/environment audio=%s at gain=%.3f (peak limit=%.2f) | narration gain=%.3f | background music=%s",
        "ON" if has_source_audio else "OFF", FLOW_INTRO_AUDIO_GAIN, FLOW_INTRO_AUDIO_PEAK_LIMIT,
        voice_gain, "ON" if use_music else "OFF",
    )
    temp = output_path.with_suffix(".audio_temp.mp4")

    # Every input is trimmed and has its timestamps rebased. No apad is used.
    # amix duration=first makes the narration stream (listed first) the finite EOF.
    voice_chain = f"[1:a]volume={voice_gain},atrim=duration={duration},asetpts=PTS-STARTPTS[voice]"
    if has_source_audio and use_music:
        music_gain = round(random.uniform(*MUSIC_VOLUME_RANGE), 3)
        filter_complex = (
            f"[0:a]highpass=f=100,lowpass=f=4200,volume={FLOW_INTRO_AUDIO_GAIN},acompressor=threshold=0.12:ratio=5:attack=10:release=220,alimiter=limit={FLOW_INTRO_AUDIO_PEAK_LIMIT}:level=false,atrim=duration={duration},asetpts=PTS-STARTPTS[amb];"
            f"{voice_chain};"
            f"[2:a]volume={music_gain},atrim=duration={duration},asetpts=PTS-STARTPTS[music];"
            "[voice][amb][music]amix=inputs=3:duration=first:normalize=0:dropout_transition=0[a]"
        )
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path), "-i", str(voice_path),
            "-i", str(music_path), "-filter_complex", filter_complex,
            "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
            "-ar", "44100", "-b:a", "192k", "-ac", "2", "-t", duration, "-shortest",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof", *FFMPEG_CLEAN_META_AV, str(temp),
        ]
    elif has_source_audio:
        filter_complex = (
            f"[0:a]highpass=f=100,lowpass=f=4200,volume={FLOW_INTRO_AUDIO_GAIN},acompressor=threshold=0.12:ratio=5:attack=10:release=220,alimiter=limit={FLOW_INTRO_AUDIO_PEAK_LIMIT}:level=false,atrim=duration={duration},asetpts=PTS-STARTPTS[amb];"
            f"{voice_chain};"
            "[voice][amb]amix=inputs=2:duration=first:normalize=0:dropout_transition=0[a]"
        )
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path), "-i", str(voice_path),
            "-filter_complex", filter_complex,
            "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
            "-ar", "44100", "-b:a", "192k", "-ac", "2", "-t", duration, "-shortest",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof", *FFMPEG_CLEAN_META_AV, str(temp),
        ]
    elif use_music:
        music_gain = round(random.uniform(*MUSIC_VOLUME_RANGE), 3)
        filter_complex = (
            f"{voice_chain};"
            f"[2:a]volume={music_gain},atrim=duration={duration},asetpts=PTS-STARTPTS[music];"
            "[voice][music]amix=inputs=2:duration=first:normalize=0:dropout_transition=0[a]"
        )
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path), "-i", str(voice_path),
            "-i", str(music_path), "-filter_complex", filter_complex,
            "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
            "-ar", "44100", "-b:a", "192k", "-ac", "2", "-t", duration, "-shortest",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof", *FFMPEG_CLEAN_META_AV, str(temp),
        ]
    else:
        filter_complex = voice_chain
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path), "-i", str(voice_path),
            "-filter_complex", filter_complex,
            "-map", "0:v:0", "-map", "[voice]", "-c:v", "copy", "-c:a", "aac",
            "-ar", "44100", "-b:a", "192k", "-ac", "2", "-t", duration, "-shortest",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof", *FFMPEG_CLEAN_META_AV, str(temp),
        ]

    ffmpeg_log = output_path.with_suffix(".audio_mix_ffmpeg.log")
    started_at = time.monotonic()
    timeout_seconds = 900
    log.info(
        "[Audio Mix] Запускаю FFmpeg: video=%s, voice=%s, duration=%.1f s, timeout=%s s; finite amix/no apad",
        video_path.name, voice_path.name, video_duration, timeout_seconds,
    )
    try:
        with ffmpeg_log.open("w", encoding="utf-8", errors="replace") as err_file:
            subprocess.run(
                cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=err_file,
                timeout=timeout_seconds,
            )
        if not temp.exists() or temp.stat().st_size < 100_000:
            log.error("[Audio Mix] FFmpeg завершился, но временный MP4 отсутствует/слишком мал: %s", temp)
            return False
        temp.replace(output_path)
        elapsed = time.monotonic() - started_at
        log.info("[Audio Mix] FFmpeg завершён за %.1f s; проверяю результат %s", elapsed, output_path.name)
        valid = is_valid_video(output_path)
        if not valid:
            log.error("[Audio Mix] Итоговый файл не прошёл проверку: %s", output_path)
        return valid
    except subprocess.TimeoutExpired:
        temp_size = 0
        try:
            temp_size = temp.stat().st_size if temp.exists() else 0
        except OSError:
            pass
        log.exception(
            "[Audio Mix] FFmpeg не завершился за %s s; temp_size=%.2f GiB; подробный лог: %s. "
            "Временный файл оставлен для диагностики.",
            timeout_seconds, temp_size / (1024 ** 3), ffmpeg_log,
        )
        return False
    except Exception:
        log.exception("[Audio Mix] Ошибка FFmpeg. Подробности в %s", ffmpeg_log)
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def ensure_final_full_hd(video_path: Path) -> bool:
    """Ensure final MP4 is 1920x1080 while preserving aspect ratio and audio."""
    if not is_valid_video(video_path):
        return False
    width, height = get_video_resolution(video_path)
    if width == 1920 and height == 1080:
        log.info("[Final QA] Итоговое видео подтверждено: 1920x1080 Full HD.")
        return True

    temp_path = video_path.with_name(video_path.stem + ".fhd_reencode.tmp.mp4")
    vf = "scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
    attempts = [
        ["ffmpeg", "-y", "-i", str(video_path), "-map", "0:v:0", "-map", "0:a?", "-vf", vf,
         "-c:v", "h264_nvenc", "-preset", ENCODER_PRESET, "-rc", "vbr", "-cq", "18", "-b:v", "10M",
         "-maxrate", "14M", "-bufsize", "20M", "-c:a", "copy", "-movflags", "+faststart", *FFMPEG_CLEAN_META_AV, str(temp_path)],
        ["ffmpeg", "-y", "-i", str(video_path), "-map", "0:v:0", "-map", "0:a?", "-vf", vf,
         "-c:v", "libx264", "-preset", "slow", "-crf", "17", "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-movflags", "+faststart", *FFMPEG_CLEAN_META_AV, str(temp_path)],
    ]
    for args in attempts:
        try:
            subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600)
            out_w, out_h = get_video_resolution(temp_path)
            if out_w == 1920 and out_h == 1080 and is_valid_video(temp_path):
                temp_path.replace(video_path)
                log.warning("[Final QA] Было %sx%s; итоговое видео приведено к 1920x1080 Full HD.", width, height)
                return True
        except Exception as exc:
            log.warning("[Final QA] Full HD re-encode attempt failed (%s); trying fallback if available.", type(exc).__name__)
        temp_path.unlink(missing_ok=True)
    log.error("[Final QA] Не удалось подтвердить/получить 1920x1080; исходное видео сохранено без изменений (%sx%s).", width, height)
    return False


def _ffmpeg_filter_quote(value: str) -> str:
    """Quote a value for an FFmpeg filter argument; source path is staged without apostrophes."""
    value = str(value).replace("\\", "/")
    escaped = []
    for char in value:
        if char in {"\\", "'", ":", ",", "[", "]", ";"}:
            escaped.append("\\")
        escaped.append(char)
    return "'" + "".join(escaped) + "'"


def _stage_subtitle_file_for_ffmpeg(subtitle_file: Path) -> Path:
    """Copy ASS captions to a temporary path without apostrophes, which break FFmpeg filter parsing."""
    roots: list[Path] = []
    try:
        roots.append(Path(tempfile.gettempdir()))
    except Exception:
        pass
    windows_root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Temp"
    if windows_root not in roots:
        roots.append(windows_root)
    last_error: Exception | None = None
    for root in roots:
        try:
            if "'" in str(root):
                continue
            root.mkdir(parents=True, exist_ok=True)
            staged = root / f"flow_subtitles_{os.getpid()}_{int(time.time() * 1000)}.ass"
            shutil.copy2(subtitle_file, staged)
            if staged.is_file() and staged.stat().st_size >= 20:
                return staged
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Не удалось создать временную копию ASS без апострофов: {last_error}")


def add_subtitles_to_video(
    video_path: Path,
    subtitle_file: Path,
    output_dir: Path,
    video_name: str,
) -> Path | None:
    """Burn captions into the video. A silent copy is never treated as a subtitle success."""
    ensure_directory(output_dir)
    output = output_dir / f"{video_name}.mp4"
    if not is_valid_video(video_path):
        log.error("[Subtitles] Исходное видео не прошло проверку: %s", video_path)
        return None
    if not subtitle_file.is_file() or subtitle_file.stat().st_size < 20:
        log.error("[Subtitles] Файл субтитров отсутствует/пустой: %s", subtitle_file)
        return None

    temp_output = output_dir / f"{video_name}.subtitles_burn.tmp.mp4"
    ffmpeg_log = output_dir / f".{video_name}.subtitle_burn.log"
    try:
        temp_output.unlink(missing_ok=True)
    except OSError:
        pass

    staged_subtitle: Path | None = None
    failures: list[str] = []
    try:
        staged_subtitle = _stage_subtitle_file_for_ffmpeg(subtitle_file)
        subtitle_ref = _ffmpeg_filter_quote(str(staged_subtitle.resolve()))
        margin = SUBTITLE_MARGIN_V
        font_size = SUBTITLE_FONT_SIZE
        style = (
            "FontName=Roboto Condensed Black,"
            f"FontSize={font_size},MaxLines=2,WrapStyle=0,MarginV={margin},"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&HCC101010,BackColour=&H50000000,"
            "BorderStyle=1,Outline=2,Shadow=1,Bold=0"
        )
        vf = f"subtitles=filename={subtitle_ref}:force_style='{style}'"

        strategies = [
            (
                "GPU",
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
                 "-map", "0:v:0", "-map", "0:a?", "-vf", vf,
                 "-c:v", HW_ENCODER, "-preset", ENCODER_PRESET, "-rc", "vbr", "-b:v", "8000k",
                 "-c:a", "copy", "-movflags", "+faststart", *FFMPEG_CLEAN_META_AV, str(temp_output)],
            ),
            (
                "CPU",
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
                 "-map", "0:v:0", "-map", "0:a?", "-vf", vf,
                 "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                 "-c:a", "copy", "-movflags", "+faststart", *FFMPEG_CLEAN_META_AV, str(temp_output)],
            ),
        ]

        for strategy_name, cmd in strategies:
            try:
                result = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                    encoding="utf-8", errors="replace", timeout=1800,
                )
                if result.returncode == 0 and is_valid_video(temp_output):
                    os.replace(temp_output, output)
                    try:
                        ffmpeg_log.unlink(missing_ok=True)
                    except OSError:
                        pass
                    log.info("✅ [Subtitles] Субтитры записаны в видео (%s): %s", strategy_name, output.name)
                    return output
                detail = (result.stderr or result.stdout or f"returncode={result.returncode}")[-3500:]
                failures.append(f"{strategy_name}: {detail}")
                log.warning("[Subtitles] Стратегия %s не сработала: %s", strategy_name, detail[-900:])
            except subprocess.TimeoutExpired:
                failures.append(f"{strategy_name}: FFmpeg timeout after 1800 seconds")
            except Exception as exc:
                failures.append(f"{strategy_name}: {exc}")
            try:
                temp_output.unlink(missing_ok=True)
            except OSError:
                pass

    except Exception as exc:
        failures.append(f"subtitle staging/filter setup: {exc}")
    finally:
        if staged_subtitle is not None:
            try:
                staged_subtitle.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            temp_output.unlink(missing_ok=True)
        except OSError:
            pass

    try:
        ffmpeg_log.write_text("\n\n".join(failures), encoding="utf-8")
    except OSError:
        pass
    log.error("❌ [Subtitles] Не удалось встроить субтитры; финал НЕ будет отмечен готовым. Подробности: %s", ffmpeg_log)
    return None

def check_disk_space(disk_path: Path, required_space_mb: int = 2048) -> bool:
    try:
        return psutil.disk_usage(str(disk_path)).free >= required_space_mb * 1024 * 1024
    except Exception:
        return True


def get_unique_temp_dir() -> Path:
    base=Path(tempfile.gettempdir()) / "flow_pipeline"
    base.mkdir(parents=True,exist_ok=True)
    path=base/f"run_{int(time.time())}_{random.randint(1000,9999)}"
    path.mkdir(parents=True,exist_ok=False)
    return path


def clear_temp(temp_to_clear: Path | None) -> None:
    if temp_to_clear and temp_to_clear.exists():
        shutil.rmtree(temp_to_clear,ignore_errors=True)

# ============================================================================
# TITLES / PREVIEWS
# ============================================================================


TITLE_VARIANTS_START_MARKER = "[[YOUTUBE_TITLE_VARIANTS_START]]"
TITLE_VARIANTS_END_MARKER = "[[YOUTUBE_TITLE_VARIANTS_END]]"


def capitalize_words(text: str) -> str:
    """Capitalize the first character of each whitespace-separated word."""
    return " ".join(word[:1].upper() + word[1:] for word in text.split())


def clean_title_candidate(text: str, max_chars: int = 100) -> str:
    """Normalize a title to title case and enforce a hard character limit."""
    value = (text or "").strip().strip('"\'“”„‟[]')
    value = re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s*", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = capitalize_words(value)
    if len(value) > max_chars:
        shortened = value[:max_chars]
        if " " in shortened:
            shortened = shortened.rsplit(" ", 1)[0]
        value = shortened.rstrip(" .,:;—-")
    return value[:max_chars].strip()


def _looks_like_narration_body(value: str, *, min_chars: int = 250, min_words: int = 35) -> bool:
    """Conservative check used only when recovering text from unusual marker layouts."""
    candidate = str(value or "").strip()
    if len(candidate) < min_chars:
        return False
    words = re.findall(r"[\w’'-]+", candidate, flags=re.UNICODE)
    return len(words) >= min_words


def extract_script_body(text: str) -> str:
    """Remove the generated title appendix and recover a body if an old file placed it after the appendix.

    The normal format is: narration first, then TITLE_VARIANTS_START_MARKER and title options.
    Some interrupted/legacy writes can leave the body after TITLE_VARIANTS_END_MARKER instead.
    Never treat the title list itself as narration, and never fabricate missing source text.
    """
    raw = str(text or "").lstrip("\ufeff")
    marker_at = raw.find(TITLE_VARIANTS_START_MARKER)
    if marker_at >= 0:
        prefix = raw[:marker_at].rstrip()
        if prefix:
            return prefix
        end_at = raw.find(TITLE_VARIANTS_END_MARKER, marker_at + len(TITLE_VARIANTS_START_MARKER))
        if end_at >= 0:
            suffix = raw[end_at + len(TITLE_VARIANTS_END_MARKER):].strip()
            if _looks_like_narration_body(suffix):
                log.warning(
                    "[Script] Обнаружен нестандартный формат TXT: тело сценария находится после блока вариантов заголовка; восстановил его из суффикса."
                )
                return suffix
        return ""
    return raw.rstrip()


def append_title_variants_to_source_file(
    script_path: Path,
    variants_path: Path,
    fallback_title: str = "",
    target_lang: str = "ru",
) -> list[str]:
    """Append/replace title options in the source .txt without duplicating them on reruns."""
    raw_variants: list[str] = []
    if variants_path.exists():
        for raw_line in variants_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            # Variant file lines use labels (e.g. "Вариант 2: ...").
            candidate = line.split(":", 1)[1].strip() if ":" in line else line
            # Title_Variants.txt may append a Russian gloss in parentheses; the source-script
            # appendix should retain only the publishable title, not its owner-facing gloss.
            candidate = re.sub(r"\s+\([^()]*[А-Яа-яЁё][^()]*\)\s*$", "", candidate).strip()
            candidate = clean_title_candidate(candidate, max_chars=100)
            if candidate and candidate.casefold() not in {x.casefold() for x in raw_variants}:
                raw_variants.append(candidate)
            if len(raw_variants) >= 5:
                break

    fallback = clean_title_candidate(fallback_title, max_chars=100)
    if not raw_variants and fallback:
        raw_variants = [fallback]
    if not raw_variants:
        log.warning("[Titles] Не удалось получить варианты названия для %s", script_path)
        return []

    original = script_path.read_text(encoding="utf-8", errors="replace") if script_path.exists() else ""
    body = extract_script_body(original)
    # Critical data-loss guard: never rewrite a source file into a title-only file.
    # If its narration body is already missing, leave the original file untouched.
    if script_path.exists() and not body.strip():
        log.error(
            "[Titles] Не добавляю варианты заголовка: у %s пустое тело сценария до маркера. "
            "Исходный TXT оставлен без изменений; сначала восстанови текст сценария.",
            script_path,
        )
        return []
    if target_lang == "es":
        heading, variant_label = "OPCIONES DE TÍTULO PARA PUBLICAR (NO FORMAN PARTE DEL GUIÓN):", "Opción"
    elif target_lang == "pl":
        heading, variant_label = "WARIANTY TYTUŁU DO PUBLIKACJI (NIE SĄ CZĘŚCIĄ SCENARIUSZA):", "Wariant"
    else:
        heading, variant_label = "ВАРИАНТЫ НАЗВАНИЙ ДЛЯ ПУБЛИКАЦИИ (НЕ ЧАСТЬ СЦЕНАРИЯ):", "Вариант"
    appendix = [TITLE_VARIANTS_START_MARKER, heading]
    appendix.extend(f"{variant_label} {index}: {title}" for index, title in enumerate(raw_variants, start=1))
    appendix.append(TITLE_VARIANTS_END_MARKER)
    script_path.write_text(body + "\n\n" + "\n".join(appendix) + "\n", encoding="utf-8")
    log.info("[Titles] В исходный текст добавлено вариантов названия: %s", len(raw_variants))
    return raw_variants



def generate_story_title_seed(story_text: str, target_lang: str = "ru", fallback_name: str = "Documentary") -> str:
    """Derive a fresh title from the actual current story, never from stale state/filename alone."""
    lang_names = {"ru": "Russian", "es": "Spanish", "pl": "Polish"}
    language = lang_names.get(target_lang, "Russian")
    text = (story_text or "").strip()
    if not text:
        return clean_title_candidate(fallback_name, 100)
    excerpt = text[:9000]
    if len(text) > 11500:
        excerpt += "\n\n[Later-story context]\n" + text[-2500:]
    prompt = f"""
You are a careful documentary-channel editor. Read the actual narration below and create ONE accurate, compelling title in {language}.
Base it on the protagonist, central discovery/conflict and distinctive facts present in this story. Do not use any old filename or previous title. Do not invent civilizations, events, characters, places or outcomes not in the narration. Avoid misleading sensationalism. The title must be at most 100 characters and every word should begin with a capital letter. Return only the title, with no labels or quotation marks.

NARRATION:
{excerpt}
""".strip()
    response = call_text_api(prompt)
    if response:
        candidate = response.strip().splitlines()[0].strip().strip('"\'“”')
        candidate = re.sub(
            r"^(?:title|название|tytuł|título|основной перевод|główne tłumaczenie|título principal)\s*:\s*",
            "", candidate, flags=re.I,
        )
        candidate = clean_title_candidate(candidate, max_chars=100)
        if candidate:
            return candidate
    # Useful deterministic fallback rather than reusing a possibly stale unrelated title.
    first_sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    first_sentence = re.sub(r"\s+", " ", first_sentence).strip()
    if len(first_sentence) > 90:
        first_sentence = first_sentence[:87].rsplit(" ", 1)[0] + "..."
    fallback = clean_title_candidate(first_sentence, max_chars=100)
    return fallback or clean_title_candidate(fallback_name, 100) or "Documentary"


def _translate_title_variants_to_russian(candidates: list[str]) -> list[str]:
    """Translate a batch of non-Russian title variants into Russian, preserving order."""
    if not candidates:
        return []

    numbered_titles = "\n".join(f"{index}. {candidate}" for index, candidate in enumerate(candidates, start=1))
    prompt = f"""
Translate each provided video title into concise, natural Russian so a Russian-speaking channel owner can understand it.
Preserve the original meaning and do not add facts, drama, explanations, or commentary.
Return exactly one Russian translation per input title, in the same order, using only this format:
1: [Russian translation]
2: [Russian translation]
Do not repeat the original-language title. Do not omit or combine items. Do not add an introduction or notes.

TITLES:
{numbered_titles}
""".strip()

    response = call_text_api(prompt)
    translations: dict[int, str] = {}
    if response:
        for line in response.splitlines():
            match = re.match(r"^\s*(\d+)\s*[:.)-]\s*(.*?)\s*$", line)
            if not match:
                continue
            index = int(match.group(1))
            value = match.group(2).strip().strip("\"'“”„‟[]")
            value = re.sub(r"^(?:русский перевод|перевод на русский|перевод)\s*:\s*", "", value, flags=re.I)
            value = re.sub(r"\s+", " ", value).strip()
            value = value.strip("\"'“”„‟")
            if 1 <= index <= len(candidates) and value:
                translations[index] = value

    result: list[str] = []
    missing: list[int] = []
    for index, candidate in enumerate(candidates, start=1):
        translation = translations.get(index, "")
        if translation:
            result.append(f"{candidate} ({translation})")
        else:
            result.append(candidate)
            missing.append(index)

    if missing:
        log.warning(
            "[Titles] Russian translations unavailable for variants %s; keeping original titles instead of guessing.",
            ", ".join(map(str, missing)),
        )
    return result


def title_variants_need_russian_translation(path: Path, target_lang: str) -> bool:
    """Return True when an existing Spanish/Polish title file needs Russian parentheticals."""
    normalized = str(target_lang or "ru").strip().lower().split("-")[0]
    if normalized not in {"es", "pl"}:
        return False
    if not path.exists():
        return True

    title_values: list[str] = []
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or ":" not in line:
                continue
            value = line.split(":", 1)[1].strip()
            if value:
                title_values.append(value)
    except OSError:
        return True

    if not title_values:
        return True
    # Cyrillic in a final parenthetical means a Russian translation has been appended.
    return any(not re.search(r"\([^()]*[А-Яа-яЁё][^()]*\)\s*$", value) for value in title_values)


def translate_and_paraphrase_title(
    title: str,
    output_dir: Path,
    target_lang: str = "ru",
) -> str:
    if target_lang == "es":
        prompt = f"""
Eres editor de un canal documental en español. Traduce y adapta el título a español latinoamericano natural.
Crea exactamente 5 opciones serias y periodísticas, cada una de 100 caracteres o menos.
Cada palabra debe comenzar con mayúscula. No inventes hechos ni sensacionalices sufrimiento o violencia.
Devuelve solo estas líneas:
Título Principal: [Opción 1]
Opción 2: [Opción 2]
Opción 3: [Opción 3]
Opción 4: [Opción 4]
Opción 5: [Opción 5]

Título Original: {title}
""".strip()
        primary_prefix = "Título Principal"
    elif target_lang == "pl":
        prompt = f"""
Przetłumacz i sparafrazuj tytuł filmu dokumentalnego na język polski.
Utwórz dokładnie 5 poważnych, publicystycznych wariantów. Każdy tytuł ma maksymalnie 100 znaków.
Każde słowo zaczyna się wielką literą. Nie sensacjonalizuj cierpienia ani przemocy, nie wymyślaj faktów.
Zwróć wyłącznie:
Główne Tłumaczenie: [Wariant 1]
Wariant 2: [Wariant 2]
Wariant 3: [Wariant 3]
Wariant 4: [Wariant 4]
Wariant 5: [Wariant 5]

Oryginał: {title}
""".strip()
        primary_prefix = "Główne Tłumaczenie"
    else:
        prompt = f"""
You are a senior editor of an educational documentary channel.
Translate/paraphrase the documentary title into Russian and create exactly 5 serious journalistic variants.
Each TITLE itself must be 100 characters or fewer. Every word must start with a capital letter (Title Case).
Avoid sensationalist gore/violence wording, misleading claims, and invented facts.
Return only these lines:
Основной Перевод: [Вариант 1]
Вариант 2: [Вариант 2]
Вариант 3: [Вариант 3]
Вариант 4: [Вариант 4]
Вариант 5: [Вариант 5]

Original: {title}
""".strip()
        primary_prefix = "Основной Перевод"

    result = call_text_api(prompt)
    ensure_directory(output_dir)
    formatted_lines: list[str] = []
    main_title = ""
    if result:
        for line in result.splitlines():
            line = line.strip()
            if not line:
                continue
            if ":" in line:
                prefix, title_part = line.split(":", 1)
                candidate = clean_title_candidate(title_part, max_chars=100)
                if not candidate:
                    continue
                formatted_lines.append(f"{prefix.strip()}: {candidate}")
                if prefix.strip().casefold() == primary_prefix.casefold() and not main_title:
                    main_title = candidate
            else:
                candidate = clean_title_candidate(line, max_chars=100)
                if candidate:
                    formatted_lines.append(candidate)
                    if not main_title:
                        main_title = candidate

    # Normalize, de-duplicate, and keep at most five unique candidates.
    candidates: list[str] = []
    for line in formatted_lines:
        value = line.split(":", 1)[1].strip() if ":" in line else line
        value = clean_title_candidate(value, max_chars=100)
        if value and value.casefold() not in {x.casefold() for x in candidates}:
            candidates.append(value)
        if len(candidates) >= 5:
            break

    fallback = clean_title_candidate(title, max_chars=100)
    if not candidates and fallback:
        candidates.append(fallback)
    if main_title:
        main_clean = clean_title_candidate(main_title, max_chars=100)
        if main_clean:
            candidates = [main_clean] + [x for x in candidates if x.casefold() != main_clean.casefold()]

    # If the model returned fewer than five distinct options, use a few neutral
    # editorial framings rather than repeating identical titles. They add no new factual claims.
    if target_lang == "es":
        safe_suffixes = (" — Historia Y Contexto", " — Relato Documental", " — Datos Y Antecedentes", " — Análisis De Los Hechos")
    elif target_lang == "pl":
        safe_suffixes = (" — Historia I Kontekst", " — Opowieść Dokumentalna", " — Fakty I Tło", " — Przegląd Wydarzeń")
    else:
        safe_suffixes = (" — История И Контекст", " — Документальный Рассказ", " — Факты И Предыстория", " — Обзор Событий")
    base = candidates[0] if candidates else fallback
    for suffix in safe_suffixes:
        if len(candidates) >= 5 or not base:
            break
        prefix_limit = max(1, 100 - len(suffix))
        prefix = base[:prefix_limit]
        if " " in prefix:
            prefix = prefix.rsplit(" ", 1)[0]
        candidate = clean_title_candidate(prefix.rstrip(" —-,:;") + suffix, max_chars=100)
        if candidate and candidate.casefold() not in {x.casefold() for x in candidates}:
            candidates.append(candidate)

    normalized_target_lang = str(target_lang or "ru").strip().lower().split("-")[0]
    # Non-Russian title variants get a Russian gloss in parentheses for the channel owner.
    # Russian-language projects must remain unchanged and should not repeat a Russian translation.
    file_candidates = (
        _translate_title_variants_to_russian(candidates)
        if normalized_target_lang in {"es", "pl"}
        else list(candidates)
    )

    if target_lang == "es":
        label_main, variant_label = "Título Principal", "Opción"
    elif target_lang == "pl":
        label_main, variant_label = "Główne Tłumaczenie", "Wariant"
    else:
        label_main, variant_label = "Основной Перевод", "Вариант"
    labels = [label_main] + [f"{variant_label} {index}" for index in range(2, len(file_candidates) + 1)]
    formatted_lines = [f"{labels[index]}: {value}" for index, value in enumerate(file_candidates)]
    (output_dir / "Title_Variants.txt").write_text("\n".join(formatted_lines), encoding="utf-8")
    return clean_title_candidate((candidates[0] if candidates else main_title) or title, max_chars=100)


def make_clip_intervals(
    start_time: float,
    end_time: float,
    chunk: float = FLOW_CLIP_SECONDS,
) -> list[tuple[float, float]]:
    result = []
    current = start_time
    while current < end_time - 0.01:
        stop = min(current + chunk, end_time)
        result.append((current, stop))
        current = stop
    return result


# ============================================================================
# INTRO (FLOW VIDEO)
# ============================================================================



def create_silent_intro_fallback(
    state: ProjectState,
    shot_index: int,
    output_path: Path,
    matching_image: Path | None = None,
) -> bool:
    """Create a silent 8-second clip only from an image generated for this exact shot.

    Never reuse a previous intro clip, an unrelated character reference, a main-story
    still, or a neutral dark frame: those would make the visual contradict narration.
    """
    if matching_image is None or not is_valid_image(Path(matching_image)):
        log.error(
            "[Intro Fallback] Нет изображения, соответствующего именно shot %s; "
            "отказываюсь подставлять предыдущую/случайную сцену.",
            shot_index,
        )
        return False

    ensure_directory(output_path.parent)
    temp_output = output_path.with_name(output_path.stem + ".matched_still_fallback.tmp.mp4")
    temp_output.unlink(missing_ok=True)
    try:
        # A gentle 5% Ken Burns push keeps the matched still from looking like a frozen
        # poster, while preserving the exact subject/action chosen for this narration slot.
        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(matching_image),
            "-t", f"{FLOW_CLIP_SECONDS:.3f}",
            "-vf",
            "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
            "zoompan=z='min(zoom+0.000208333,1.05)':x='iw/2-(iw/zoom/2)':"
            "y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30,noise=alls=2:allf=t+u,setsar=1",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temp_output),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        if is_valid_video(temp_output):
            temp_output.replace(output_path)
            log.warning(
                "[Intro Fallback] Flow video shot %s недоступен; использую 8s-клип "
                "с лёгким zoom из отдельного изображения той же сцены: %s.",
                shot_index, Path(matching_image).name,
            )
            return True
    except Exception as exc:
        log.warning(
            "[Intro Fallback] Не удалось анимировать согласованное изображение для shot %s (%s).",
            shot_index, type(exc).__name__,
        )
    finally:
        temp_output.unlink(missing_ok=True)
    return False


def normalize_intro_clip_duration(path: Path, target_duration: float = FLOW_CLIP_SECONDS) -> bool:
    """Silently master an intro clip and lock it to its narration window."""
    if not is_valid_video(path):
        return False
    current = get_duration(path)
    if current is None or current <= 0.0:
        return False
    temp = path.with_name(path.stem + ".time_aligned.tmp.mp4")
    temp.unlink(missing_ok=True)
    speed_pts = random.uniform(0.985, 1.015)
    pad_seconds = max(0.0, target_duration - current * speed_pts + 0.25)
    angle = math.radians(random.choice((-1, 1)) * random.uniform(0.30, 0.45))
    crop = random.uniform(0.950, 0.962)
    contrast, brightness, saturation = random.uniform(*COLOR_CONTRAST_JITTER), random.uniform(*COLOR_BRIGHTNESS_JITTER), random.uniform(*COLOR_SATURATION_JITTER)
    grain = 1 if ACTIVE_VISUAL_STYLE_ID in {"comic_explainer", "cartoon_2d"} else random.randint(2, 3)
    vf = f"setpts=PTS/{speed_pts:.6f},rotate={angle:.9f}:ow=iw:oh=ih:bilinear=1,crop=iw*{crop:.6f}:ih*{crop:.6f},scale=1920:1080:flags=lanczos,eq=contrast={contrast:.4f}:brightness={brightness:.4f}:saturation={saturation:.4f},noise=alls={grain}:allf=t+u,fps={FPS},tpad=stop_mode=clone:stop_duration={pad_seconds:.3f},setsar=1"
    bitrate = f"{random.randint(*VIDEO_BITRATE_RANGE)}k"
    attempts = [
        [FFMPEG_BIN, "-y", "-i", str(path), "-map", "0:v:0", "-an", "-vf", vf, "-t", f"{target_duration:.3f}", "-r", str(FPS), "-c:v", HW_ENCODER, "-preset", ENCODER_PRESET, "-rc", "vbr", "-b:v", bitrate, "-maxrate", "12000k", "-pix_fmt", "yuv420p", "-profile:v", "high", "-video_track_timescale", "15360", "-movflags", "+faststart", *FFMPEG_CLEAN_META_VIDEO, str(temp)],
        [FFMPEG_BIN, "-y", "-i", str(path), "-map", "0:v:0", "-an", "-vf", vf, "-t", f"{target_duration:.3f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", *FFMPEG_CLEAN_META_VIDEO, str(temp)],
    ]
    try:
        last_error = None
        for cmd in attempts:
            temp.unlink(missing_ok=True)
            try:
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)
                actual = get_duration(temp)
                if is_valid_video(temp) and actual is not None and abs(actual - target_duration) <= 0.10 and not has_audio_stream(temp):
                    temp.replace(path)
                    log.info("[Intro Master] %s: %.3fs -> %.3fs; silent output", path.name, current, target_duration)
                    return True
            except Exception as exc:
                last_error = exc
        raise RuntimeError(str(last_error or "duration/video validation failed"))
    except Exception as exc:
        log.error("[Intro Alignment] Не удалось обработать %s: %s", path.name, exc)
        return False
    finally:
        temp.unlink(missing_ok=True)



async def build_intro(
    state: ProjectState,
    text_content: str,
    manager: FlowAccountManager,
    playwright,
    target_lang: str = "ru",
    narration_segments: list[dict[str, Any]] | None = None,
) -> float:
    intro = state.data["intro"]
    narration_duration = max((float(x.get("end", 0) or 0) for x in (narration_segments or []) if isinstance(x, dict)), default=0.0)
    if narration_duration <= 0.0:
        narration_duration = float(state.data.get("timing", {}).get("audio_duration", 0.0) or 0.0)
    saved_plan = intro.get("plan") if isinstance(intro.get("plan"), dict) else None
    intro_plan = saved_plan if saved_plan and isinstance(saved_plan.get("clip_durations"), list) else plan_floating_intro(narration_segments or [], narration_duration)
    intro["plan"] = intro_plan
    intro["clip_durations"] = list(intro_plan["clip_durations"])
    intro["intro_end_time"] = float(intro_plan["intro_end_time"])
    intro_count = len(intro_plan["clip_durations"])
    intro_durations = [float(x) for x in intro_plan["clip_durations"]]
    INTRO_MASTER_VERSION = "intro-silent-fhd-floating-v2"
    state.save()
    if intro.get("master_version") != INTRO_MASTER_VERSION:
        intro["master_version"] = INTRO_MASTER_VERSION
        intro["mastered_clips"] = []
    if not intro.get("enabled", True):
        intro["actual_duration"] = 0.0
        intro["clip_durations"] = []
        intro["clips"] = []
        intro["done"] = False
        state.save()
        return 0.0

    prompts = intro.get("prompts", [])
    storyboard_needs_refresh = (
        not isinstance(prompts, list)
        or len(prompts) != intro_count
        or intro.get("storyboard_version") != INTRO_STORYBOARD_VERSION
    )
    if storyboard_needs_refresh:
        old_output = Path(str(intro.get("output", ""))) if intro.get("output") else None
        montage_state_at_refresh = state.data.get("montage", {}) if isinstance(state.data.get("montage"), dict) else {}
        final_state_at_refresh = state.data.get("final", {}) if isinstance(state.data.get("final"), dict) else {}
        had_legacy_intro = (
            bool(prompts)
            or bool(intro.get("done"))
            or bool(old_output and old_output.exists())
            or any(
                (state.media_dir / f"{kind}_{shot_index:02d}.mp4").is_file()
                for shot_index in range(1, intro_count + 1)
                for kind in ("intro_raw", "intro_clip")
            )
            or any(
                Path(str(montage_state_at_refresh.get(key, "") or "")).is_file()
                for key in ("no_audio", "with_audio")
                if str(montage_state_at_refresh.get(key, "") or "").strip()
            )
            or bool(str(final_state_at_refresh.get("video", "") or "").strip() and Path(str(final_state_at_refresh.get("video"))).is_file())
        )
        chars = state.data.get("analysis", {}).get("characters", {})
        ctx = state.data.get("analysis", {}).get("context", {})
        timed_windows = build_intro_narration_windows(narration_segments, shot_count=intro_count, clip_durations=intro_durations)
        timed_opening_text = " ".join(
            str(item.get("text", "") or "").strip() for item in timed_windows
            if str(item.get("text", "") or "").strip()
        ).strip()
        if not timed_opening_text:
            # Last-resort text partition if Whisper produced no usable timestamps at all.
            # This is less precise than word timing, but still prevents every shot from
            # receiving the entire opening script or an empty narration assignment.
            narration_words = str(text_content or "").split()
            if narration_words:
                for window_index, window in enumerate(timed_windows):
                    left = round(window_index * len(narration_words) / intro_count)
                    right = round((window_index + 1) * len(narration_words) / intro_count)
                    window["text"] = " ".join(narration_words[left:right]).strip()
                timed_opening_text = " ".join(
                    str(item.get("text", "") or "") for item in timed_windows
                ).strip()
            log.warning(
                "[Intro Alignment] Whisper timestamps unavailable; using ordered text chunks as a degraded fallback. "
                "Exact Whisper-word synchronization cannot be guaranteed in this fallback mode."
            )
        else:
            log.info(
                "[Intro Alignment] Создаю %s shot-промптов по реальным Whisper-таймкодам; "
                "каждый клип привязан к своему сохранённому временному окну озвучки.", intro_count
            )
        new_prompts = generate_intro_storyboard_gemini(
            timed_opening_text, chars, ctx, target_lang=target_lang, timed_windows=timed_windows
        )
        if len(new_prompts) != intro_count:
            raise FlowTechnicalError(
                f"Раскадровка интро содержит {len(new_prompts)} кадров вместо {intro_count}"
            )

        # A legacy short intro must not be mistaken for a completed eight-shot intro.
        # Archive legacy assets (do not destroy them) so all eight clips use matching prompts.
        if had_legacy_intro:
            legacy_backup_dir = ensure_directory(state.work_dir / "intro_legacy_backup")
            legacy_assets = [
                state.media_dir / pattern
                for shot_index in range(1, intro_count + 1)
                for pattern in (f"intro_raw_{shot_index:02d}.mp4", f"intro_clip_{shot_index:02d}.mp4")
            ]
            if old_output and old_output.exists():
                legacy_assets.append(old_output)
            for candidate in legacy_assets:
                if not candidate.exists():
                    continue
                destination = legacy_backup_dir / candidate.name
                if destination.exists():
                    destination = legacy_backup_dir / f"{candidate.stem}_{int(time.time())}_{random.randint(1000, 9999)}{candidate.suffix}"
                try:
                    shutil.move(str(candidate), str(destination))
                    log.info("[Intro] Старый клип сохранён в архив: %s", destination)
                except Exception as exc:
                    raise FlowTechnicalError(
                        f"Не удалось архивировать старый intro-файл {candidate}: {exc}"
                    ) from exc
            intro["clips"] = []
            intro["clip_durations"] = []
            intro["actual_duration"] = 0.0
            intro["done"] = False
            intro["skipped_shots"] = []
            intro["content_safety_reviews"] = {}

            # Intro audio/prompt policy changed. Archive dependent renders by renaming
            # in place (fast, no large cross-drive copy), then force their reconstruction.
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            montage_state = state.data.setdefault("montage", {})
            for key in ("no_audio", "with_audio"):
                old_value = str(montage_state.get(key, "") or "").strip()
                candidate = Path(old_value) if old_value else None
                if candidate and candidate.is_file():
                    archived = candidate.with_name(f"{candidate.stem}.pre_intro_refresh_{stamp}{candidate.suffix}")
                    try:
                        candidate.replace(archived)
                        log.info("[Intro] Старый зависимый монтаж сохранён: %s", archived)
                    except OSError as exc:
                        raise FlowTechnicalError(
                            f"Не удалось сохранить старый монтаж перед обновлением интро {candidate}: {exc}"
                        ) from exc
            montage_state["no_audio"] = str(state.work_dir / "video_no_audio.mp4")
            montage_state["with_audio"] = str(state.work_dir / "video_with_audio.mp4")
            montage_state["done"] = False

            final_state = state.data.setdefault("final", {})
            old_final_value = str(final_state.get("video", "") or "").strip()
            old_final_path = Path(old_final_value) if old_final_value else None
            if old_final_path and old_final_path.is_file():
                archived_final = old_final_path.with_name(
                    f"{old_final_path.stem}.pre_intro_refresh_{stamp}{old_final_path.suffix}"
                )
                try:
                    old_final_path.replace(archived_final)
                    log.info("[Intro] Старое итоговое видео сохранено: %s", archived_final)
                except OSError as exc:
                    raise FlowTechnicalError(
                        f"Не удалось сохранить старое итоговое видео перед обновлением интро {old_final_path}: {exc}"
                    ) from exc
            final_state["video"] = ""
            final_state["done"] = False
            final_state["subtitles_burned"] = False

        intro["output"] = str(state.media_dir / "intro_floating.mp4")
        intro["prompts"] = new_prompts
        intro["storyboard_version"] = INTRO_STORYBOARD_VERSION
        intro["target_duration"] = float(intro_plan["intro_end_time"])
        state.save()

    # Older checkpoints may contain prompts made before safety/audio constraints existed.
    # The storyboard version is bumped so old Flow intro clips are archived and regenerated;
    # this also ensures the newly enforced prompt matches the actual audio policy.
    for saved_shot in intro.get("prompts", []):
        if isinstance(saved_shot, dict):
            saved_shot["prompt"] = enforce_flow_intro_silent_visual_policy(
                enforce_flow_ambience_only_audio(
                    apply_visible_text_language_rule(
                        apply_youtube_content_safety_rules(str(saved_shot.get("prompt", "")), "video"),
                        target_lang,
                    )
                )
            )
    state.save()
    log.info("[Intro Audio] Используются беззвучно мастеренные клипы; аудиодорожка Flow не включается.")

    clip_paths: list[Path] = []
    clip_durations: list[float] = []
    intro_analysis = state.data.get("analysis", {}) if isinstance(state.data.get("analysis"), dict) else {}
    intro_hero_path, intro_hero_entry = get_project_hero_etalon(intro_analysis)
    intro_character_bible = intro_analysis.get("character_bible", {}) if isinstance(intro_analysis.get("character_bible"), dict) else {}
    if intro_hero_path:
        log.info("[Intro Reference] Найден эталон канала для интро: %s; он прикрепляется только к кадрам CAST=PROTAGONIST/BOTH.", intro_hero_path.name)

    for idx, shot in enumerate(intro["prompts"], start=1):
        raw_path = state.media_dir / f"intro_raw_{idx:02d}.mp4"
        clip_path = state.media_dir / f"intro_clip_{idx:02d}.mp4"
        shot_target_duration = intro_durations[idx - 1]
        shot_start = float(shot.get("narration_start", sum(intro_durations[:idx - 1])) or 0.0)
        shot_end = float(shot.get("narration_end", shot_start + shot_target_duration) or shot_start + shot_target_duration)
        shot_excerpt = re.sub(r"\s+", " ", str(shot.get("narration_excerpt", "") or "").strip())
        log.info(
            "[Intro Alignment] Shot %s/%s -> %s-%s | narration=%s",
            idx, intro_count, _format_intro_timecode(shot_start), _format_intro_timecode(shot_end),
            shot_excerpt[:260] if shot_excerpt else "[no speech in this window]",
        )

        # Each clip is aligned to its own fixed Whisper time window, not merely kept at
        # whatever duration Flow happens to return.
        mastered_clips = set(intro.get("mastered_clips", []))
        if is_valid_video(raw_path) and (clip_path.name not in mastered_clips or not is_valid_video(clip_path)):
            shutil.copy2(raw_path, clip_path)
            log.info(
                "⏭️ [Intro] Полный raw clip %s: %.2fs",
                raw_path.name,
                get_duration(raw_path) or 0.0,
            )
        elif not is_valid_video(clip_path):
            # The storyboard explicitly decides who is visible in each intro shot.
            # A channel etalon is attached only when the protagonist is in the cast.
            cast_mode = str(shot.get("cast", "NONE") or "NONE").strip().upper()
            if cast_mode not in {"NONE", "PROTAGONIST", "SUPPORTING", "BOTH"}:
                cast_mode = "NONE"
            shot_references: list[Path] = []
            if cast_mode != "NONE":
                shot_references = references_for_scene(
                    intro_character_bible, f"CAST={cast_mode}", str(shot.get("prompt", ""))
                )
            if intro_hero_path and cast_mode in {"PROTAGONIST", "BOTH"}:
                hero_key = str(intro_hero_path.resolve()).casefold()
                if not any(str(path.resolve()).casefold() == hero_key for path in shot_references):
                    shot_references.insert(0, intro_hero_path)
            if intro_hero_path and cast_mode in {"PROTAGONIST", "BOTH"}:
                log.info("[Intro Reference] Shot %s/%s CAST=%s; прикрепляю эталон главного героя %s.", idx, intro_count, cast_mode, intro_hero_path.name)
            elif cast_mode == "NONE":
                log.info("[Intro Reference] Shot %s/%s CAST=NONE; эталон героя не прикрепляется.", idx, intro_count)
            else:
                log.info("[Intro Reference] Shot %s/%s CAST=%s; secondary references=%s; герой-эталон не прикрепляется.", idx, intro_count, cast_mode, len(shot_references))

            generation_prompt = str(shot.get("prompt", "")).strip()
            hero_key = str(intro_hero_path.resolve()).casefold() if intro_hero_path else ""
            hero_attached = bool(hero_key and any(str(path.resolve()).casefold() == hero_key for path in shot_references))
            if hero_attached:
                generation_prompt += (
                    "\n\nINTRO PROTAGONIST ETALON: The attached channel image is the identity reference for the central narrator/main protagonist only. "
                    "Preserve their recognizable face/head shape, hair, eyes, age and body proportions. Match the etalon's deliberately near-white stylized skin colour, not a realistic peach/beige complexion. "
                    "It is not a composition reference: follow this shot's action, framing, setting and camera direction. Wardrobe may change only when the story explicitly requires it; "
                    "otherwise use the selected reference outfit as the default. Do not insert the protagonist into a shot whose cast is marked NONE."
                )
            generation_prompt = apply_visible_text_language_rule(
                apply_youtube_content_safety_rules(generation_prompt, "video"), target_lang
            )
            generation_prompt = enforce_selected_visual_style(generation_prompt, "video")
            generation_prompt = enforce_flow_ambience_only_audio(generation_prompt)
            # Put the Mateo-specific skin colour block AFTER style, language and audio rules,
            # so it is the last authoritative instruction sent to Flow.
            if hero_attached:
                generation_prompt = append_channel_hero_skin_lock(generation_prompt, intro_hero_entry, "video")
            # Enforce silent facial performance last so later prompt additions cannot
            # accidentally weaken the closed-mouth / no-lip-motion rule.
            generation_prompt = enforce_flow_intro_silent_visual_policy(generation_prompt)
            # Always retry any missing shot: the final intro must contain all eight clips.
            try:
                await generate_flow_with_failover(
                    manager,
                    playwright,
                    "video",
                    generation_prompt,
                    raw_path,
                    reference_images=shot_references,
                )
                if not is_valid_video(raw_path):
                    raise FlowTechnicalError(
                        f"Intro {idx}: Flow вернул невалидный видеофайл"
                    )
                shutil.copy2(raw_path, clip_path)
            except Exception as exc:
                log.error(
                    "❌ [Intro] Flow не создал клип %s/%s после повторов на профилях и упрощения Gemini: %s",
                    idx, intro_count, str(exc)[:300],
                )
                # Preserve semantic synchronization: if Flow cannot render this shot as
                # video, request a still IMAGE from the exact same shot prompt and animate
                # that image for 8 seconds. Never reuse the previous shot or an unrelated
                # character/main-story image.
                fallback_image_path = state.media_dir / f"intro_matched_still_{idx:02d}.jpg"
                fallback_image_path.unlink(missing_ok=True)
                shot_core = _flow_prompt_core(generation_prompt).strip()
                still_prompt = (
                    "Create ONE single full-bleed 16:9 still illustration/frame for this exact intro shot. "
                    "Preserve the same subject, character identity, visible action, object, location, era, "
                    "composition and selected visual style from the prompt below. This still will be shown "
                    "during the exact narration interval assigned to this shot, so do not substitute a generic "
                    "portrait, unrelated location, stock image, or different story event. No collage, no text, "
                    "no captions, no logo, no watermark.\n\n" + shot_core
                )
                still_prompt = apply_visible_text_language_rule(
                    apply_youtube_content_safety_rules(still_prompt, "image"), target_lang
                )
                still_prompt = enforce_selected_visual_style(still_prompt, "image")
                try:
                    log.warning(
                        "[Intro Matched Still] Flow video shot %s failed; generating a matching still from the same scene prompt.",
                        idx,
                    )
                    await generate_flow_with_failover(
                        manager, playwright, "image", still_prompt, fallback_image_path,
                        reference_images=shot_references,
                        _attempt_key_suffix=f"|intro-matched-still-{idx}",
                    )
                except Exception as image_exc:
                    log.error(
                        "[Intro Matched Still] Не удалось создать изображение именно для shot %s: %s",
                        idx, str(image_exc)[:300],
                    )
                fallback_ok = create_silent_intro_fallback(
                    state, idx, raw_path, matching_image=fallback_image_path,
                )
                if not fallback_ok or not is_valid_video(raw_path):
                    raise FlowTechnicalError(
                        f"Не удалось создать семантически соответствующий клип интро {idx}/{intro_count}. "
                        "Не подставляю чужой кадр; checkpoint сохранён для повторного запуска."
                    ) from exc
                shutil.copy2(raw_path, clip_path)
                skipped = intro.setdefault("skipped_shots", [])
                if not isinstance(skipped, list):
                    skipped = []
                    intro["skipped_shots"] = skipped
                skipped.append({
                    "shot": idx,
                    "reason": str(exc)[:700],
                    "fallback": "same_shot_generated_still",
                    "fallback_image": str(fallback_image_path),
                    "narration_start": shot_start,
                    "narration_end": shot_end,
                    "narration_excerpt": shot_excerpt,
                    "created_at": now_iso(),
                })
                state.save()

        # Keep the original Flow audio stream so natural environmental ambience can
        # survive. The prompt forbids dialogue/music; final mix keeps this track quiet.

        if clip_path.name not in mastered_clips or not is_valid_video(clip_path):
            if not normalize_intro_clip_duration(clip_path, shot_target_duration):
                raise FlowTechnicalError(
                    f"Intro {idx}: не удалось выровнять клип по окну озвучки {shot_start:.3f}-{shot_end:.3f}s"
                )
            mastered_clips.add(clip_path.name)
            intro["mastered_clips"] = sorted(mastered_clips)
            state.save()
        duration = get_duration(clip_path)
        if duration is None or duration <= 0.05 or abs(duration - shot_target_duration) > 0.10:
            raise FlowTechnicalError(
                f"Intro {idx}: длительность {duration!r}s не совпадает с окном {shot_target_duration:.3f}s"
            )

        if ENABLE_YOUTUBE_CONTENT_SAFETY_REVIEW:
            review = review_video_for_youtube(
                clip_path, context=f"Intro shot {idx}: {text_content[:800]}", temp_dir=state.work_dir
            )
            review["sha256"] = sha256_file(clip_path) if clip_path.exists() else ""
            intro.setdefault("content_safety_reviews", {})[str(idx)] = review
            state.save()
            review_decision = str(review.get("decision", "REVIEW")).upper().strip()
            if review_decision == "REJECT":
                raise FlowContentSafetyReviewError(
                    f"Intro shot {idx} was explicitly rejected by content review: "
                    f"{review.get('reason', 'no reason')}"
                )
            elif review_decision != "PASS":
                # An unavailable reviewer must not abort a technically valid intro.
                # Keep the decision/reason in the checkpoint so the user can inspect it.
                log.warning(
                    "[Content Safety] Intro shot %s review=%s; continuing without a confirmed pass "
                    "because this is not an explicit REJECT. Reason: %s",
                    idx, review_decision, review.get("reason", "no reason"),
                )

        clip_paths.append(clip_path)
        clip_durations.append(duration)

        intro["clips"] = [str(p) for p in clip_paths]
        intro["clip_durations"] = clip_durations
        intro["actual_duration"] = sum(clip_durations)
        state.save()

    if len(clip_paths) != intro_count:
        raise FlowTechnicalError(
            f"Интро неполное: подготовлено {len(clip_paths)} из {intro_count} обязательных клипов."
        )

    temp = state.work_dir / "intro_concat_full.mp4"
    parts = [(p, get_duration(p)) for p in clip_paths]
    concat_ok = False
    concat_error: Exception | None = None
    try:
        concat_ok = concatenate_videos(
            parts,
            temp,
            state.work_dir,
            preserve_audio=False,
        )
    except Exception as exc:
        concat_error = exc
        log.exception("[Intro] Ошибка полной склейки; включаю резервную сборку вместо остановки проекта.")

    # Last-resort progress guarantee: if even FFmpeg's packet-copy and re-encode
    # paths fail, use an already validated downloaded shot as a short intro rather
    # than losing the entire project after all clips were generated.
    if not concat_ok or not is_valid_video(temp):
        fallback_source = next((p for p in clip_paths if is_valid_video(p)), None)
        if fallback_source is not None:
            try:
                temp.unlink(missing_ok=True)
                shutil.copy2(fallback_source, temp)
                concat_ok = is_valid_video(temp)
                if concat_ok:
                    intro["assembly_fallback"] = f"single_clip:{fallback_source.name}"
                    state.save()
                    log.error(
                        "⚠️ [Intro Fallback] Не удалось собрать %s клипов в один файл. "
                        "Проект не остановлен: использую проверенный клип %s как короткое интро; остальные клипы сохранены.",
                        len(clip_paths), fallback_source.name,
                    )
            except Exception as fallback_exc:
                log.exception("[Intro Fallback] Не удалось включить резервный клип: %s", fallback_exc)
                concat_ok = False
        if not concat_ok or not is_valid_video(temp):
            reason = f"; исходная ошибка: {concat_error}" if concat_error else ""
            raise RuntimeError(f"Не удалось собрать intro и не найден рабочий резервный клип{reason}")
    else:
        intro["assembly_fallback"] = ""

    actual = get_duration(temp) or 0.0
    if actual <= 0.05:
        raise RuntimeError("Получилось пустое intro")

    # Every intro clip is locked to its 8-second Whisper narration window; total intro duration is 64 seconds.
    shutil.copy2(temp, Path(intro["output"]))
    log.info(
        "🔉 [Intro] Сохраняю исходную аудиодорожку Flow с gain=%.3f (около 15%% исходной амплитуды); пики ограничены под диктором. "
        "Модель может иногда нарушать ограничения, поэтому громкость снижена и ограничены резкие высокие частоты.",
        FLOW_INTRO_AUDIO_GAIN,
    )
    intro["actual_duration"] = get_duration(Path(intro["output"])) or actual
    intro["target_duration"] = float(intro_plan["intro_end_time"])
    intro["done"] = is_valid_video(Path(intro["output"]))
    state.set_stage("intro_done" if intro["done"] else "intro")
    state.save()

    log.info(
        "🎬 [Intro] Полная фактическая длительность: %.2fs; план: %s клипов; беззвучный мастеринг и Whisper-синхронизация завершены.",
        intro["actual_duration"], intro_count,
    )
    return intro["actual_duration"]


# ============================================================================
# FAILED SCENE FALLBACK
# ============================================================================


def find_fallback_scene_image(state: ProjectState, current_index: int, target_path: Path) -> tuple[Path, str]:
    """Return a previously generated clean image so a failed scene does not stop the project."""
    records = state.data.get("main_clips", [])

    # Prefer the nearest earlier scene so the current scene can hold its visual
    # for the failed slot; this preserves the original audio timeline exactly.
    for record in reversed(records[:current_index]):
        candidate_text = str(record.get("image_path", "")).strip()
        if not candidate_text:
            continue
        candidate = Path(candidate_text)
        try:
            if candidate.resolve() == target_path.resolve():
                continue
        except Exception:
            pass
        review = record.get("content_safety_review", {})
        try:
            if ENABLE_YOUTUBE_CONTENT_SAFETY_REVIEW:
                verified_safe = (
                    isinstance(review, dict)
                    and review.get("decision") == "PASS"
                    and review.get("sha256") == sha256_file(candidate)
                )
            else:
                # With remote vision review disabled, use technical QA and safe prompts.
                verified_safe = True
        except Exception:
            verified_safe = False
        if is_valid_image(candidate) and not image_quality_issues(candidate) and verified_safe:
            return candidate, f"previous scene {record.get('index', '?') + 1 if isinstance(record.get('index'), int) else record.get('index', '?')}"

    # On resume, a clean scene may already exist later in the timeline.
    nearby = []
    for candidate in state.media_dir.glob("scene_*.jpg"):
        try:
            if candidate.resolve() == target_path.resolve() or not is_valid_image(candidate):
                continue
            match = re.search(r"scene_(\d+)\.jpg$", candidate.name, re.I)
            number = int(match.group(1)) - 1 if match else current_index
            issues = image_quality_issues(candidate)
            matching = next((r for r in records if str(r.get("image_path", "")) == str(candidate)), {})
            review = matching.get("content_safety_review", {}) if isinstance(matching, dict) else {}
            if ENABLE_YOUTUBE_CONTENT_SAFETY_REVIEW:
                safe_hash_matches = (
                    isinstance(review, dict)
                    and review.get("decision") == "PASS"
                    and review.get("sha256") == sha256_file(candidate)
                )
            else:
                safe_hash_matches = True
            if not issues and safe_hash_matches:
                nearby.append((abs(number - current_index), candidate))
        except Exception:
            continue
    if nearby:
        nearby.sort(key=lambda x: x[0])
        return nearby[0][1], "nearest existing clean scene"

    # If the first-ever scene fails, a frame from the intro is a better fallback
    # than ending the entire pipeline or leaving a timing gap.
    intro_output = Path(str(state.data.get("intro", {}).get("output", "")))
    if intro_output.exists() and is_valid_video(intro_output):
        extracted = state.work_dir / "fallback_intro_frame.jpg"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "1.0", "-i", str(intro_output), "-frames:v", "1", "-q:v", "2", str(extracted)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            )
            if is_valid_image(extracted) and not image_quality_issues(extracted):
                if not ENABLE_YOUTUBE_CONTENT_SAFETY_REVIEW:
                    return extracted, "intro fallback frame (technical QA passed; safe-prompt workflow)"
                intro_review = review_images_for_youtube([extracted], context="Fallback frame from intro video", media_kind="image")
                if intro_review.get("decision") == "PASS":
                    return extracted, "intro fallback frame (content review passed)"
        except Exception as exc:
            log.warning("[Flow Fallback] Не удалось извлечь кадр из intro: %s", exc)

    # Last-resort visual placeholder: clean 16:9 muted-color gradient, no text.
    placeholder = state.work_dir / "fallback_neutral_scene.jpg"
    try:
        import numpy as np
        from PIL import Image, ImageFilter
        if not is_valid_image(placeholder):
            width, height = 1920, 1080
            arr = np.zeros((height, width, 3), dtype=np.uint8)
            top = np.array([73, 87, 96], dtype=float)
            bottom = np.array([26, 37, 45], dtype=float)
            for y in range(height):
                t = y / max(1, height - 1)
                arr[y, :, :] = (top * (1 - t) + bottom * t).astype(np.uint8)
            image = Image.fromarray(arr, "RGB").filter(ImageFilter.GaussianBlur(0.4))
            image.save(placeholder, quality=92)
        return placeholder, "neutral fallback (no earlier generated image available)"
    except Exception as exc:
        raise FlowTechnicalError(f"Не удалось создать резервный кадр: {exc}") from exc


# ============================================================================
# PROJECT EXECUTION PIPELINE
# ============================================================================


def archive_and_reset_for_language_change(state: ProjectState, new_lang: str) -> None:
    """Keep checkpoints for the selected narration language; archive incompatible outputs on a language switch."""
    previous_lang = state.data.get("narration_language")
    if previous_lang == new_lang:
        return
    # Existing legacy states did not store a language. Preserve Russian/Polish projects,
    # but force a clean rebuild when Spanish is selected so an old-language WAV is never reused.
    if previous_lang is None and new_lang != "es":
        state.data["narration_language"] = new_lang
        state.save()
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = ensure_directory(state.work_dir / f"language_switch_archive_{previous_lang or 'legacy'}_to_{new_lang}_{timestamp}")
    targets: list[Path] = []
    for key_path in (
        state.data.get("audio", {}).get("path"),
        state.data.get("whisper", {}).get("subtitle_path"),
        state.data.get("whisper", {}).get("segments_path"),
        state.data.get("intro", {}).get("output"),
        state.data.get("montage", {}).get("no_audio"),
        state.data.get("montage", {}).get("with_audio"),
        state.data.get("final", {}).get("video"),
        state.data.get("final", {}).get("subtitles"),
    ):
        if key_path:
            targets.append(Path(str(key_path)))
    for item in state.data.get("intro", {}).get("clips", []):
        if item:
            targets.append(Path(str(item)))
    for record in state.data.get("main_clips", []):
        for key in ("image_path", "output_path"):
            if record.get(key):
                targets.append(Path(str(record[key])))
    for thumb in state.data.get("thumbnails", []):
        if thumb.get("path"):
            targets.append(Path(str(thumb["path"])))
    targets.extend(state.media_dir.glob("intro_clip_*.mp4"))
    targets.extend(state.media_dir.glob("intro_raw_*.mp4"))
    targets.extend(state.media_dir.glob("scene_*.jpg"))
    targets.extend(state.media_dir.glob("scene_*.mp4"))
    targets.extend(state.project_folder.rglob("Thumb*_Flow_2K.jpg"))

    seen: set[str] = set()
    archived = 0
    for item in targets:
        try:
            if not item.is_file():
                continue
            resolved = str(item.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            destination = archive_dir / f"{archived:04d}_{item.name}"
            shutil.move(str(item), str(destination))
            archived += 1
        except Exception as exc:
            log.warning("[Language] Не удалось архивировать %s: %s", item, exc)

    state.data["audio"]["done"] = False
    state.data["whisper"]["done"] = False
    state.data["analysis"] = {"characters": {}, "context": {}, "done": False}
    intro = state.data["intro"]
    intro["enabled"] = bool(intro.get("enabled", ENABLE_FLOW_INTRO_DEFAULT))
    intro["target_duration"] = INTRO_TARGET_SECONDS
    intro["storyboard_version"] = INTRO_STORYBOARD_VERSION
    intro["prompts"] = []
    intro["clips"] = []
    intro["clip_durations"] = []
    intro["actual_duration"] = 0.0
    intro["done"] = False
    intro["output"] = str(state.media_dir / "intro_floating.mp4")
    state.data["main_clips"] = []
    state.data["scene_plan"] = []
    state.data["timing"] = {"intro_duration": 0.0, "audio_duration": 0.0, "main_timeline_start": 0.0, "timeline_signature": ""}
    state.data["montage"]["done"] = False
    state.data["final"]["title"] = ""
    state.data["final"]["video"] = ""
    state.data["final"]["subtitles"] = ""
    state.data["final"]["subtitles_burned"] = False
    state.data["final"]["done"] = False
    state.data["thumbnails"] = []
    state.data["thumbnail_delivery_version"] = 0
    state.data["source_preview_delivery"] = ""
    state.data["narration_language"] = new_lang
    state.save()
    log.warning(
        "[Language] Язык озвучки изменён (%s → %s): старые аудио/таймлайн/рендеры перемещены в %s; проект будет построен заново. Архивировано файлов: %s",
        previous_lang or "legacy/unknown", new_lang, archive_dir, archived,
    )


PROJECT_DONE_MARKER = ".flow_pipeline_done.json"
THUMBNAIL_DELIVERY_VERSION = 6


def find_project_preview(folder: Path) -> Path | None:
    """Find an existing user-supplied preview without mistaking scene images/character refs for it."""
    image_suffixes = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    keywords = ("preview", "thumbnail", "thumb", "cover", "poster", "обложка", "превью", "миниатюр", "okladka")
    excluded_parts = {STATE_DIR_NAME.casefold(), WORK_DIR_NAME.casefold(), CHARACTER_REFERENCE_DIR_NAME.casefold()}

    candidates: list[tuple[int, int, int, Path]] = []
    generic_images: list[tuple[int, int, float, Path]] = []
    try:
        paths = folder.rglob("*")
        for candidate in paths:
            if not candidate.is_file() or candidate.suffix.casefold() not in image_suffixes:
                continue
            try:
                rel = candidate.relative_to(folder)
                parts = [part.casefold() for part in rel.parts]
                if any(part in excluded_parts for part in parts):
                    continue
                stem = candidate.stem.casefold()
                if re.fullmatch(r"scene[_-]?\d+", stem) or stem.startswith("fallback_"):
                    continue
                if stem.startswith("thumb") and "flow_2k" in stem:
                    continue
                size = candidate.stat().st_size
                if size < 10_000:
                    continue
                matched = [word for word in keywords if word in stem or any(word in part for part in parts[:-1])]
                if matched:
                    score = 100 + len(matched) * 15
                    if len(rel.parts) == 1:
                        score += 40
                    if parts and parts[0] == MEDIA_DIR_NAME.casefold():
                        score -= 10
                    candidates.append((score, -len(rel.parts), size, candidate))
                else:
                    # Arbitrarily named source previews are still eligible, but scene renders,
                    # references and work caches are excluded above.
                    try:
                        with Image.open(candidate) as image:
                            width, height = image.size
                        ratio = width / max(1, height)
                        aspect_penalty = abs(ratio - (16 / 9))
                        pixels = width * height
                        score = (100.0 - min(100.0, aspect_penalty * 80.0)) + min(100.0, pixels / 30_000.0)
                        generic_images.append((score, size, float(-len(rel.parts)), candidate))
                    except Exception:
                        continue
            except OSError:
                continue
    except OSError:
        return None

    if candidates:
        candidates.sort(reverse=True, key=lambda item: (item[0], item[1], item[2], item[3].name.casefold()))
        return candidates[0][3]

    # Common upstream setups use a single, generically named image in the project root.
    root_images: list[Path] = []
    try:
        for candidate in folder.iterdir():
            if not candidate.is_file() or candidate.suffix.casefold() not in image_suffixes:
                continue
            stem = candidate.stem.casefold()
            if stem.startswith(("scene_", "thumb", "character", "reference", "ref_", "logo", "avatar")):
                continue
            if candidate.stat().st_size >= 10_000 and is_valid_image(candidate):
                root_images.append(candidate)
    except OSError:
        return None
    if len(root_images) == 1:
        return root_images[0]
    if root_images:
        # In many input folders the preview has a generic filename. Prefer a large landscape image,
        # typical of a YouTube preview, rather than a portrait/avatar.
        ranked: list[tuple[float, int, Path]] = []
        for candidate in root_images:
            try:
                with Image.open(candidate) as image:
                    width, height = image.size
                ratio = width / max(1, height)
                aspect_penalty = abs(ratio - (16 / 9))
                pixels = width * height
                score = (100.0 - min(100.0, aspect_penalty * 80.0)) + min(100.0, pixels / 30_000.0)
                ranked.append((score, candidate.stat().st_size, candidate))
            except Exception:
                continue
        if ranked:
            ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
            log.warning("[Preview] Имя оригинального превью не распознано; выбрано самое подходящее широкоформатное изображение из корня: %s", ranked[0][2].name)
            return ranked[0][2]
    if generic_images:
        generic_images.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        log.warning(
            "[Preview] Не найдено изображение с узнаваемым именем; для сохранения перед удалением проекта выбрано изображение %s",
            generic_images[0][3].relative_to(folder),
        )
        return generic_images[0][3]
    return None


def set_explorer_extra_large_icons(folder: Path) -> bool:
    """Open the final output folder and set Windows Explorer to Extra large icons.

    This is a Windows Explorer preference, not a Google Drive web setting. The folder
    window is intentionally left open so the view-change can be persisted by Explorer.
    Direct COM properties are set first; Ctrl+Shift+1 is used as a UI fallback.
    """
    if os.name != "nt":
        return False
    target = Path(folder)
    if not target.is_dir():
        return False
    target_ps = str(target.resolve()).replace("'", "''")
    ps = r"""
$ErrorActionPreference = 'Stop'
$target = [System.IO.Path]::GetFullPath('__TARGET__').TrimEnd('\')
$shell = New-Object -ComObject Shell.Application
$shell.Explore($target)
$deadline = (Get-Date).AddSeconds(12)
$applied = $false
$ws = New-Object -ComObject WScript.Shell
while ((Get-Date) -lt $deadline -and -not $applied) {
    foreach ($w in @($shell.Windows())) {
        try {
            $rawPath = [string]$w.Document.Folder.Self.Path
            if ([string]::IsNullOrWhiteSpace($rawPath)) { continue }
            $p = [System.IO.Path]::GetFullPath($rawPath).TrimEnd('\')
            if ($p -ine $target) { continue }

            # FVM_ICON=1 is icon view; IconSize=256 requests extra-large icons.
            try { $w.Document.CurrentViewMode = 1 } catch {}
            try { $w.Document.ViewMode = 1 } catch {}
            try { $w.Document.IconSize = 256 } catch {}
            try { $w.Document.Refresh() } catch {}
            Start-Sleep -Milliseconds 400

            # Fallback to Explorer's own shortcut: Ctrl+Shift+1 = Extra large icons.
            $activated = $false
            try {
                if (-not ('FlowExplorerViewNativeV34' -as [type])) {
                    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class FlowExplorerViewNativeV34 {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
"@ -ErrorAction SilentlyContinue
                }
                $activated = [FlowExplorerViewNativeV34]::SetForegroundWindow([IntPtr]([long]$w.HWND))
            } catch {}
            if ($activated) {
                Start-Sleep -Milliseconds 250
                try { $ws.SendKeys('^+1') } catch {}
                Start-Sleep -Milliseconds 1000
            }
            $applied = $true
            # Do not close this window: closing immediately prevented reliable persistence
            # on some virtual/mounted Google Drive folders.
            break
        } catch {}
    }
    if (-not $applied) { Start-Sleep -Milliseconds 250 }
}
if ($applied) { exit 0 } else { exit 2 }
""".replace('__TARGET__', target_ps)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=18,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            log.info("🗂️ [Explorer] Открыл папку финального результата и применил «Огромные значки»: %s", target)
            return True
        log.warning(
            "[Explorer] Не удалось автоматически установить «Огромные значки» для %s: %s",
            target, (result.stderr or result.stdout or f"exit={result.returncode}").strip()[:300],
        )
    except Exception as exc:
        log.warning("[Explorer] Не удалось применить вид «Огромные значки» для %s: %s", target, exc)
    return False


def get_project_hero_etalon(analysis: dict[str, Any] | None) -> tuple[Path | None, dict[str, Any] | None]:
    """Return the exact channel etalon already selected for this project, without re-randomizing it."""
    data = analysis if isinstance(analysis, dict) else {}
    bible = data.get("character_bible", {})
    entries = bible.get("characters", []) if isinstance(bible, dict) else []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("channel_reference_locked"):
                continue
            raw = str(entry.get("reference_image_path", "") or data.get("channel_hero_reference_path", "")).strip()
            if raw:
                candidate = Path(raw)
                if is_valid_image(candidate):
                    return candidate.resolve(), entry
    raw = str(data.get("channel_hero_reference_path", "") or "").strip()
    if raw:
        candidate = Path(raw)
        if is_valid_image(candidate):
            return candidate.resolve(), None
    return None, None


def append_thumbnail_hero_identity_instruction(
    prompt: str, hero_entry: dict[str, Any] | None, reference_layout: str
) -> str:
    """Tell Flow which attached image is the protagonist identity reference for thumbnail generation."""
    if not reference_layout:
        return str(prompt or "").strip()
    instruction = (
        "\n\nTHUMBNAIL CHARACTER ETALON INSTRUCTION: " + reference_layout + ". "
        "The attached channel etalon is a character identity reference, not a scene/composition reference. "
        "When showing the protagonist, keep the same recognizable face/head shape, hair, eyes, skin tone, age and body proportions from the etalon. "
        "The selected etalon outfit is the project's default wardrobe; do not revert to a generic outfit in the script if it conflicts. "
        "Wardrobe may change only when the story context clearly requires a different outfit. Do not add unshown accessories, logos or tools."
    )
    result = str(prompt or "").strip() + instruction
    return append_channel_hero_skin_lock(result, hero_entry, "thumbnail")


def prepare_thumbnail_prompt_for_submission(
    prompt: str,
    reference_images: list[Path],
    analysis: dict[str, Any] | None,
    target_lang: str,
) -> str:
    """Apply standard thumbnail rules, then put the channel hero skin lock last if its etalon is attached."""
    prepared = apply_visible_text_language_rule(
        apply_youtube_content_safety_rules(str(prompt or ""), "thumbnail"),
        target_lang,
    )
    hero_path, hero_entry = get_project_hero_etalon(analysis)
    if hero_path is not None and hero_entry is not None:
        try:
            hero_key = str(hero_path.resolve()).casefold()
            reference_keys = {str(path.resolve()).casefold() for path in reference_images if path.is_file()}
        except Exception:
            hero_key, reference_keys = str(hero_path).casefold(), {str(path).casefold() for path in reference_images}
        if hero_key in reference_keys:
            prepared = append_channel_hero_skin_lock(prepared, hero_entry, "thumbnail")
    return prepared


def copy_original_preview(folder: Path, output_dir: Path) -> Path | None:
    source = find_project_preview(folder)
    if source is None:
        log.warning("[Preview] В исходной папке не найдено оригинальное превью по имени/формату; будут сохранены два новых превью.")
        return None
    destination = output_dir / f"Preview_Original{source.suffix.casefold()}"
    try:
        ensure_directory(output_dir)
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        if not is_valid_image(destination):
            raise RuntimeError("копия оригинального превью не прошла проверку изображения")
        # If the source extension changed, avoid leaving a stale second copy of the original preview.
        for old_copy in output_dir.glob("Preview_Original.*"):
            if old_copy.resolve() != destination.resolve():
                old_copy.unlink(missing_ok=True)
        log.info("✅ [Preview] Оригинальное превью сохранено рядом с финальным видео: %s", destination)
        return destination
    except Exception as exc:
        log.error("[Preview] Не удалось сохранить оригинальное превью в папку результата: %s", exc)
        # Do not let cleanup delete the only copy of the source preview.
        raise FlowTechnicalError(f"Не удалось скопировать оригинальное превью в папку финального результата: {exc}") from exc


def final_delivery_outputs_ready(folder: Path, final_path: Path) -> bool:
    """Check the persisted delivery manifest before allowing input-project deletion."""
    try:
        state_path = folder / STATE_DIR_NAME / "state.json"
        if not state_path.is_file() or not is_valid_video(final_path):
            return False
        # Read-only provenance report; the delivered MP4 is never rewritten.
        data = json.loads(state_path.read_text(encoding="utf-8", errors="replace"))
        provenance_report = inspect_mp4_provenance(final_path)
        if isinstance(data, dict):
            data["provenance_inspection"] = provenance_report
            try:
                atomic_write_json(state_path, data)
            except Exception as state_exc:
                log.warning("[Provenance] Не удалось сохранить отчёт в state.json: %s", state_exc)
        final = data.get("final", {}) if isinstance(data, dict) else {}
        thumbs = data.get("thumbnails", []) if isinstance(data, dict) else []
        if not final.get("done") or not final.get("subtitles_burned"):
            return False
        recorded_final = str(final.get("video", "") or "").strip()
        if not recorded_final:
            return False
        if Path(recorded_final).resolve() != final_path.resolve():
            return False
        if data.get("thumbnail_delivery_version") != THUMBNAIL_DELIVERY_VERSION:
            return False
        if not isinstance(thumbs, list) or len(thumbs) != 2:
            return False
        expected_names = {"Preview_Similar_To_Original.jpg", "Thumbnail_Clickable_Story.jpg"}
        delivered_names: set[str] = set()
        delivered_paths: set[Path] = set()
        for thumb in thumbs:
            p = Path(str(thumb.get("path", "")))
            if (
                not is_valid_image(p)
                or not preview_image_has_delivery_size(p)
                or p.name not in expected_names
            ):
                return False
            delivered_names.add(p.name)
            delivered_paths.add(p.resolve())
        if delivered_names != expected_names or len(delivered_paths) != 2:
            return False
        original_copy = str(data.get("source_preview_delivery", "") or "")
        source_preview_exists = find_project_preview(folder) is not None
        if source_preview_exists and not original_copy:
            return False
        if original_copy:
            original_path = Path(original_copy)
            if not is_valid_image(original_path) or not preview_image_has_delivery_size(original_path):
                return False
        # Validate every preview/thumbnail file left in the final output folder,
        # not just the two manifest entries and the copied original.
        for candidate in final_path.parent.iterdir():
            if is_preview_delivery_asset(candidate) and not preview_image_has_delivery_size(candidate):
                return False
        story_text_value = str(data.get("story_text_delivery", "") or "").strip()
        if not story_text_value:
            return False
        story_text_path = Path(story_text_value)
        if not story_text_path.is_file() or story_text_path.stat().st_size <= 0:
            return False
        # Final video, delivered story text, and images must live outside source folder.
        folder_resolved = folder.resolve()
        required = [final_path.resolve(), story_text_path.resolve()] + [Path(str(t.get("path", ""))).resolve() for t in thumbs]
        if original_copy:
            required.append(Path(original_copy).resolve())
        if any(p == folder_resolved or folder_resolved in p.parents for p in required):
            return False
        return True
    except Exception:
        return False


def project_missing_required_intro_clips(folder: Path) -> bool:
    """Return True when a previously completed project's intro clips were deleted."""
    try:
        state_path = folder / STATE_DIR_NAME / "state.json"
        if not state_path.is_file():
            return False
        data = json.loads(state_path.read_text(encoding="utf-8"))
        intro = data.get("intro", {}) if isinstance(data, dict) else {}
        if not intro.get("enabled", True):
            return False
        media_dir = folder / MEDIA_DIR_NAME
        # Check actual media-folder contents, not absolute clip paths in state.json:
        # finalization may have renamed the project directory with a _DONE suffix.
        missing_intro_clip = any(
            not is_valid_video(media_dir / f"intro_clip_{idx:02d}.mp4")
            for idx in range(1, intro_count + 1)
        )
        intro_output = Path(str(intro.get("output", "") or ""))
        missing_intro_output = bool(intro.get("done")) and (
            not intro_output.name or not is_valid_video(media_dir / intro_output.name)
        )
        final = data.get("final", {}) if isinstance(data, dict) else {}
        final_path_value = str(final.get("video", "") or "")
        missing_final_output = bool(final.get("done")) and bool(final_path_value) and not Path(final_path_value).is_file()
        return missing_intro_clip or missing_intro_output or missing_final_output
    except Exception:
        return False


def restore_completed_folder_if_assets_missing(folder: Path) -> Path:
    """Re-open a _DONE folder only when required generated video assets are missing."""
    if not folder.name.endswith("_DONE") or not project_missing_required_intro_clips(folder):
        return folder
    original = folder.with_name(folder.name[:-5])
    if original.exists():
        log.warning(
            "[Resume Repair] %s has missing required video assets, but cannot restore its name because %s already exists.",
            folder.name, original.name,
        )
        return folder
    try:
        folder.rename(original)
        try:
            (original / PROJECT_DONE_MARKER).unlink(missing_ok=True)
        except Exception:
            pass
        try:
            (original.parent / f".{original.name}.flow_pipeline_done.json").unlink(missing_ok=True)
        except Exception:
            pass
        log.warning(
            "[Resume Repair] Restored completed project %s because required video assets are missing; it will resume from checkpoint.",
            original.name,
        )
        return original
    except OSError as exc:
        log.error("[Resume Repair] Cannot restore %s for repair: %s", folder.name, exc)
        return folder


def project_manual_reopen_requested(folder: Path) -> bool:
    """Detect a completed folder whose _DONE suffix was manually removed.

    Successful finalization leaves an in-folder marker but removes the sidecar
    marker. Renaming Foo_DONE back to Foo is therefore an explicit user request
    to reopen it. If both markers are present, assume the rename had failed and
    keep honoring the completion marker.
    """
    if folder.name.endswith("_DONE"):
        return False
    try:
        inside_marker = (folder / PROJECT_DONE_MARKER).is_file()
        sidecar_marker = (folder.parent / f".{folder.name}.flow_pipeline_done.json").is_file()
        return inside_marker and not sidecar_marker
    except OSError:
        return False


def is_completed_project_folder(folder: Path) -> bool:
    """Skip *_DONE folders; removing that suffix explicitly reopens a project."""
    if folder.name.endswith("_DONE"):
        return True

    # A completed project renamed back manually has the in-folder marker, but
    # its sidecar was deleted during successful finalization. Treat that as an
    # explicit request to resume rather than silently filtering it out.
    if project_manual_reopen_requested(folder):
        return False

    # If finalization couldn't rename the folder, the sidecar marker remains.
    # Keep skipping it unless required outputs have gone missing.
    try:
        sidecar_marked_done = (folder.parent / f".{folder.name}.flow_pipeline_done.json").is_file()
        if sidecar_marked_done and project_missing_required_intro_clips(folder):
            return False
        return sidecar_marked_done
    except OSError:
        return False


def is_reserved_channel_asset_folder(folder: Path) -> bool:
    """Folders used for channel-wide assets are not video projects."""
    reserved_names = {
        CHANNEL_HERO_REFERENCE_DIR_NAME.casefold(),
        CHARACTER_REFERENCE_DIR_NAME.casefold(),
        STATE_DIR_NAME.casefold(),
        MEDIA_DIR_NAME.casefold(),
        WORK_DIR_NAME.casefold(),
        "assets", "references", "reference", "thumbs", "thumbnails",
    }
    return folder.name.casefold() in reserved_names or folder.name.startswith(".")


def _write_project_done_marker(folder: Path, final_path: Path) -> tuple[bool, bool]:
    payload = {
        "status": "DONE",
        "project_folder": str(folder),
        "final_video": str(final_path),
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "note": "Project completed; marker prevents reprocessing if Windows/Drive denies folder rename.",
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    inside_ok = False
    sidecar_ok = False
    try:
        (folder / PROJECT_DONE_MARKER).write_text(text, encoding="utf-8")
        inside_ok = True
    except Exception as exc:
        log.warning("[Project Done] Не удалось записать маркер внутри папки: %s", exc)
    try:
        sidecar = folder.parent / f".{folder.name}.flow_pipeline_done.json"
        sidecar.write_text(text, encoding="utf-8")
        sidecar_ok = True
    except Exception as exc:
        log.warning("[Project Done] Не удалось записать внешний маркер: %s", exc)
    return inside_ok, sidecar_ok


def finalize_project_folder(folder: Path, final_path: Path) -> bool:
    """Delete source/work assets only after final video, story text, subtitles and thumbnails are verified."""
    if not folder.exists():
        return is_valid_video(final_path)
    if not final_delivery_outputs_ready(folder, final_path):
        log.error(
            "[Project Cleanup] Отказываюсь удалять исходную папку: финал, текст истории, встроенные субтитры или превью не прошли проверку. Папка сохранена: %s",
            folder,
        )
        return False

    source = folder.resolve()
    protected = [final_path.resolve()]
    try:
        data = json.loads((folder / STATE_DIR_NAME / "state.json").read_text(encoding="utf-8", errors="replace"))
        for thumb in data.get("thumbnails", []):
            protected.append(Path(str(thumb.get("path", ""))).resolve())
        original_copy = str(data.get("source_preview_delivery", "") or "")
        if original_copy:
            protected.append(Path(original_copy).resolve())
        story_text_delivery = str(data.get("story_text_delivery", "") or "").strip()
        if story_text_delivery:
            protected.append(Path(story_text_delivery).resolve())
    except Exception as exc:
        log.error("[Project Cleanup] Не могу проверить список выходных файлов; папка сохранена: %s", exc)
        return False

    if any(p == source or source in p.parents for p in protected):
        log.error("[Project Cleanup] Защита от потери данных: один из результатов расположен внутри исходной папки. Не удаляю %s", folder)
        return False

    import gc
    gc.collect()
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            shutil.rmtree(folder, onerror=lambda func, name, exc_info: _retry_remove_readonly(func, name, exc_info))
            if not folder.exists():
                sidecar = folder.parent / f".{folder.name}.flow_pipeline_done.json"
                try:
                    sidecar.unlink(missing_ok=True)
                except OSError:
                    pass
                log.info(
                    "🧹 [Project Cleanup] Исходная папка проекта полностью удалена после проверки финального видео и превью: %s",
                    folder,
                )
                return True
        except Exception as exc:
            last_error = exc
            log.warning("[Project Cleanup] Попытка %s/3 не удалась: %s", attempt, exc)
            time.sleep(1.0 * attempt)
    log.error(
        "[Project Cleanup] НЕ удалось полностью удалить исходную папку; выходные файлы сохранены, при следующем запуске очистка будет повторена: %s (%s)",
        folder, last_error,
    )
    return False


def _retry_remove_readonly(func, name, exc_info) -> None:
    """Make a Windows read-only file deletable, then retry the requested removal function."""
    try:
        os.chmod(name, 0o666)
        func(name)
    except Exception:
        raise


async def process_project(
    folder: Path,
    channel_name: str,
    project_lang: str,
    selected_intro: bool,
    manager: FlowAccountManager,
    playwright,
    selected_visual_style: str = "realistic",
) -> bool:
    manager.last_project_stop_reason = None
    log.info("[TTS Voice] Project=%s | channel=%s | voice=%s", folder.name, channel_name, get_channel_tts_voice(channel_name))
    manual_reopen_requested = project_manual_reopen_requested(folder)
    if is_completed_project_folder(folder):
        log.info("⏭️ [Project Done] %s уже завершён (_DONE или sidecar marker); пропускаю повторную обработку.", folder.name)
        return True
    if manual_reopen_requested:
        log.warning(
            "[Resume Repair] Обнаружено ручное удаление суффикса _DONE у %s; продолжаю проверку checkpoint вместо пропуска по marker.",
            folder.name,
        )

    global ACTIVE_VISUAL_STYLE_ID
    state = ProjectState(folder)
    manager.current_project_state = state
    state.data.setdefault("final", {}).setdefault("subtitles_burned", False)
    state.data.setdefault("thumbnail_delivery_version", 0)
    state.data.setdefault("source_preview_delivery", "")
    state.data.setdefault("story_text_delivery", "")
    requested_style = normalize_visual_style(selected_visual_style)
    stored_style = normalize_visual_style(state.data.get("visual_style_id")) if state.data.get("visual_style_id") else None
    has_existing_visual_work = bool(
        state.data.get("main_clips")
        or state.data.get("intro", {}).get("clips")
        or state.data.get("intro", {}).get("done")
        or state.data.get("montage", {}).get("done")
        or state.data.get("final", {}).get("done")
    )
    if stored_style:
        project_style = stored_style
        if requested_style != stored_style:
            log.warning(
                "[Visual Style] Проект уже закреплён за стилем '%s'; выбор меню '%s' не применён, чтобы не смешивать стили в готовых сценах. Для смены стиля начни новый проект или сбрось визуальные результаты этого проекта.",
                visual_style_label(stored_style), visual_style_label(requested_style),
            )
    elif has_existing_visual_work:
        # Existing projects without a style marker are assumed to use the legacy realism mode.
        project_style = "realistic"
        log.warning(
            "[Visual Style] В старом проекте уже есть визуальные результаты без сохранённого стиля; продолжаю в стиле реализма, чтобы не смешивать эстетику."
        )
        state.data["visual_style_id"] = project_style
        state.save()
    else:
        project_style = requested_style
        state.data["visual_style_id"] = project_style
        state.save()
    ACTIVE_VISUAL_STYLE_ID = project_style
    log.info("🎨 [Visual Style] %s (%s)", visual_style_label(project_style), project_style)

    # Remove only the obsolete failure/pending flags from the old edit_square UI.
    # Keep the per-browser registry; History-based entries are tagged with UI_VERSION,
    # while old string entries are automatically retried once with the new two-click flow.
    obsolete_session_keys = (
        "flow_agent_session_failed_profiles",
        "flow_agent_session_profiles",
        "flow_agent_session_setup_pending",
    )
    removed_session_metadata = False
    for obsolete_key in obsolete_session_keys:
        if state.data.pop(obsolete_key, None) is not None:
            removed_session_metadata = True
    if removed_session_metadata:
        log.info("[Flow Session] Удалены старые флаги блокировки edit_square; History-сессии включены без блокировки профиля.")
        state.save()
    # If intro clips were deliberately deleted (e.g. clips containing unwanted
    # dialogue/music), invalidate only downstream renders and rebuild the missing
    # intro shots from the saved storyboard. Keep narration, subtitles and scene work.
    if project_missing_required_intro_clips(folder):
        log.warning(
            "[Resume Repair] Required intro/final video assets are missing. Rebuilding missing intro shots and downstream montage/final video as needed; reusing narration and existing scene assets."
        )
        state.data.setdefault("intro", {})["done"] = False
        state.data.setdefault("montage", {})["done"] = False
        state.data.setdefault("final", {})["done"] = False
        state.data.setdefault("final", {})["subtitles_burned"] = False
        state.data.setdefault("timing", {})["intro_duration"] = 0.0
        state.save()

    # Recover the last tiny crash window: final state was saved as DONE but the
    # process ended before folder rename/marker completion.
    if state.data.get("final", {}).get("done") and not manual_reopen_requested:
        final_candidate = Path(str(state.data.get("final", {}).get("video", "")))
        if final_candidate.is_file() and final_delivery_outputs_ready(folder, final_candidate):
            if finalize_project_folder(folder, final_candidate):
                return True
            log.warning("[Project Cleanup] Готовые результаты уже есть, но исходная папка не удалена. Повторю очистку после дальнейшей проверки.")
            return False
    archive_and_reset_for_language_change(state, project_lang)
    state.data["intro"]["enabled"] = bool(selected_intro)
    state.reconcile()
    state.print_resume_summary()

    text_files = sorted(folder.glob("*.txt"))
    if not text_files:
        log.error("[Script] Проект %s не запущен: в корне папки нет ни одного .txt со сценарием.", folder)
        return False

    # Do not silently select the alphabetically first TXT if it contains only a title
    # appendix/metadata. Search all root-level TXT files and choose the first one whose
    # actual narration body remains non-empty after removing the generated title appendix.
    text_file = None
    text_content = ""
    for candidate in text_files:
        try:
            candidate_body = extract_script_body(candidate.read_text(encoding="utf-8", errors="replace")).strip()
        except Exception as exc:
            log.warning("[Script] Не удалось прочитать %s: %s", candidate.name, exc)
            continue
        if candidate_body:
            text_file = candidate
            text_content = candidate_body
            break
        try:
            raw_candidate = candidate.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
            marker_position = raw_candidate.find(TITLE_VARIANTS_START_MARKER)
            first_nonempty = next((line.strip() for line in raw_candidate.splitlines() if line.strip()), "<пустой файл>")
            marker_info = (
                f"marker_position={marker_position}; first_line={first_nonempty[:100]!r}"
                if marker_position >= 0 else f"first_line={first_nonempty[:100]!r}"
            )
        except Exception:
            marker_info = "не удалось прочитать диагностические сведения"
        log.warning(
            "[Script] Пропускаю %s: тело сценария пустое после удаления блока вариантов заголовка (%s).",
            candidate.name, marker_info,
        )

    if text_file is None:
        log.error(
            "[Script] Проект %s не запущен: найдено TXT-файлов: %d, но ни в одном нет текста сценария до TITLE_VARIANTS_START_MARKER. "
            "Проверь исходный .txt; этапы TTS/Flow не запускались.",
            folder, len(text_files),
        )
        return False

    title_from_filename = " ".join(
        word.capitalize()
        for word in text_file.stem.replace("_", " ").split()
    )
    log.info("[Script] Использую сценарий: %s (%d символов тела).", text_file.name, len(text_content))
    source_language_guess = detect_script_language(text_content)
    if source_language_guess:
        log.info(
            "[Narration Language] Source script language estimate=%s; selected narration/visual-text language=%s.",
            source_language_guess.upper(), str(project_lang).upper(),
        )
        # This pipeline translates input text only when Spanish is selected. For RU/PL,
        # it assumes the source is already in that language; prevent silent Spanish audio
        # paired with Russian/Polish visual-text instructions.
        if project_lang in {"ru", "pl"} and source_language_guess != project_lang:
            mismatch_message = (
                f"Язык исходного сценария определён как {source_language_guess.upper()}, "
                f"но в меню выбран {project_lang.upper()}. Перевод входного текста на RU/PL этим конвейером не выполняется. "
                f"Выбери {source_language_guess.upper()} для соответствия озвучки, субтитров и текста на изображениях "
                f"либо подготовь исходный сценарий на языке выбранной озвучки."
            )
            state.data["language_mismatch"] = {
                "source_guess": source_language_guess,
                "selected_language": project_lang,
                "message": mismatch_message,
                "checked_at": now_iso(),
            }
            state.set_stage("language_mismatch", "NEEDS_LANGUAGE_SELECTION")
            state.save()
            log.error("[Narration Language] %s Проект не запускаю, чтобы не смешивать языки озвучки и визуального текста.", mismatch_message)
            return False
        state.data.pop("language_mismatch", None)
    story_source_sha256 = hashlib.sha256(text_content.encode("utf-8")).hexdigest()
    previous_source_sha256 = str(state.data.get("source_script_sha256", "") or "")
    if previous_source_sha256 and previous_source_sha256 != story_source_sha256:
        log.warning(
            "[Project Source] Script body changed for %s. Archiving existing state/media/work and rebuilding this story to avoid mixing assets.",
            folder.name,
        )
        archive_root = ensure_directory(folder / "_flow_archive" / f"script_revision_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        previous_final = str(state.data.get("final", {}).get("video", "") or "")
        if previous_final:
            previous_final_path = Path(previous_final)
            if previous_final_path.is_file():
                try:
                    # Archive beside the old output on the same drive where possible, avoiding
                    # a cross-drive copy of a large MP4 into the source-folder archive.
                    old_output_archive = ensure_directory(previous_final_path.parent / "_stale_story_archive" / f"{folder.name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
                    archived_old_output = old_output_archive / previous_final_path.name
                    shutil.move(str(previous_final_path), str(archived_old_output))
                    log.warning("[Project Source] Перенесён устаревший MP4 от предыдущей истории: %s", archived_old_output)
                except Exception as exc:
                    log.warning("[Project Source] Не удалось архивировать старый итоговый MP4 %s: %s", previous_final_path, exc)
        for source_dir, label in ((state.media_dir, "media"), (state.state_dir, "state"), (state.work_dir, "work")):
            destination_dir = ensure_directory(archive_root / label)
            for item in list(source_dir.iterdir()):
                if source_dir == state.state_dir and item.resolve() == state.path.resolve():
                    continue
                destination = destination_dir / item.name
                if destination.exists():
                    destination = destination_dir / f"{item.stem}_{int(time.time())}{item.suffix}"
                try:
                    shutil.move(str(item), str(destination))
                except Exception as exc:
                    log.warning("[Project Source] Could not archive %s: %s", item, exc)
        state.data = state._default()
        state.data["project"] = folder.name
        state.data["visual_style_id"] = project_style
        state.data["source_script_sha256"] = story_source_sha256
        state.data["narration_language"] = project_lang
        state.save()
    else:
        state.data["source_script_sha256"] = story_source_sha256
        state.save()

    # Advisory policy/monetization preflight before spending time on translation, TTS, and visuals.
    monetization_report = build_youtube_monetization_preflight(folder, text_file, text_content)
    state.data["youtube_monetization_preflight"] = monetization_report
    report_path = save_youtube_monetization_preflight(folder, monetization_report)
    state.save()
    script_review = monetization_report.get("script_review", {})
    similarity_status = monetization_report.get("channel_script_similarity", {}).get("status", "unknown")
    log.info(
        "[YouTube Preflight] decision=%s | script_similarity=%s | report=%s",
        script_review.get("decision", "REVIEW"), similarity_status, report_path,
    )
    if script_review.get("decision") == "HIGH_RISK":
        state.set_stage("monetization_review", "REVIEW_REQUIRED")
        state.save()
        log.error(
            "[YouTube Preflight] High-confidence policy risk; edit the source text and retry. Report: %s | reason: %s",
            report_path, script_review.get("reason", "See report"),
        )
        return False

    # For Spanish projects, translate the full narration only after preflight passes.
    # The source text remains unchanged for provenance and similarity checks.
    narration_text = text_content
    if project_lang == "es":
        narration_text = translate_script_for_narration(text_content, project_lang, state.work_dir)

    try:
        # 1. Audio
        state.set_stage("audio")
        audio_path = Path(state.data["audio"]["path"])

        if not state.data["audio"]["done"]:
            # Reuse a project-local narration WAV/MP3 when present, including Spanish.
            # Older code explicitly ignored every root-level audio file for ES projects,
            # which caused needless TTS regeneration even when the matching .wav existed.
            audio_candidates = []
            for candidate in sorted(
                list(folder.glob("*.wav")) + list(folder.glob("*.mp3")),
                key=lambda item: item.name.casefold(),
            ):
                try:
                    if candidate.is_file() and is_valid_audio(candidate):
                        audio_candidates.append(candidate)
                except Exception as audio_check_exc:
                    log.debug("[Audio] Не удалось проверить %s: %s", candidate, audio_check_exc)

            def _audio_name_key(value: str) -> str:
                normalized = unicodedata.normalize("NFKD", str(value)).casefold()
                normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
                return "".join(ch for ch in normalized if ch.isalnum())

            project_audio_keys = {
                key for key in (
                    _audio_name_key(folder.name),
                    _audio_name_key(text_file.stem),
                ) if key
            }
            # Prefer an audio file whose basename matches this project or its source script.
            matching_audio = [
                candidate for candidate in audio_candidates
                if _audio_name_key(candidate.stem) in project_audio_keys
            ]
            if matching_audio:
                existing_audio = matching_audio[0]
            elif len(audio_candidates) == 1:
                # A single valid WAV/MP3 in this project folder is unambiguous enough to reuse.
                existing_audio = audio_candidates[0]
            elif project_lang != "es" and audio_candidates:
                # Preserve legacy non-Spanish behavior when several audio files are present.
                existing_audio = audio_candidates[0]
            else:
                existing_audio = None
                if audio_candidates:
                    log.warning(
                        "[Audio] В папке есть %s валидных WAV/MP3, но имя не совпадает с проектом; "
                        "не беру случайный файл для испанской озвучки.",
                        len(audio_candidates),
                    )

            if existing_audio:
                if not master_voice_audio(existing_audio, audio_path):
                    raise RuntimeError(f"Не удалось выполнить мастеринг существующей озвучки до Whisper: {existing_audio}")
                log.info(
                    "🎧 [Audio] Используем готовую озвучку из папки проекта: %s (lang=%s; размер=%.1f MB)",
                    existing_audio.name, project_lang, existing_audio.stat().st_size / (1024 * 1024),
                )
            else:
                log.info("🎙️ [Audio] Готовый WAV/MP3 для этого проекта не найден — генерируем TTS...")
                if not generate_audio_gemini_sync(
                    narration_text,
                    state.work_dir,
                    audio_path,
                ):
                    raise RuntimeError("Gemini TTS failed")

            state.data["audio"]["done"] = is_valid_audio(audio_path)
            state.save()

        audio_duration = get_duration(audio_path)
        if audio_duration is None:
            raise RuntimeError("Не удалось определить длительность audio.wav")

        # 2. Whisper
        subtitle_path = Path(state.data["whisper"]["subtitle_path"])
        segments_path = Path(state.data["whisper"]["segments_path"])
        state.set_stage("whisper")

        if not state.data["whisper"]["done"]:
            segments = transcribe_audio(
                audio_path,
                subtitle_path,
                segments_path,
                project_lang,
            )
            state.data["whisper"]["done"] = bool(
                subtitle_path.exists() and segments_path.exists() and segments
            )
            state.save()
        else:
            segments = load_segments(segments_path)

        full_transcription = transcript_text(segments)
        if not full_transcription:
            raise RuntimeError("Whisper не вернул текст")
        narration_integrity_path = state.work_dir / "narration_integrity.json"
        narration_integrity = audit_narration_coverage(
            narration_text, full_transcription, narration_integrity_path
        )
        state.data["narration_integrity"] = {
            "path": str(narration_integrity_path),
            "expected_word_coverage": narration_integrity.get("expected_word_coverage"),
            "sequence_similarity": narration_integrity.get("sequence_similarity"),
            "status": narration_integrity.get("status"),
        }
        state.save()

        # 3. Analysis + persistent character continuity bible
        state.set_stage("analysis")
        analysis = state.data["analysis"]
        if not analysis.get("context"):
            analysis["context"] = analyze_context(text_content)
            state.save()

        bible_entries = analysis.get("character_bible", {}).get("characters", []) if isinstance(analysis.get("character_bible"), dict) else []
        bible_is_current = (
            isinstance(analysis.get("character_bible"), dict)
            and int(analysis.get("character_bible", {}).get("version", 0) or 0) >= 5
            and analysis.get("character_bible_source_sha256") == story_source_sha256
            and isinstance(bible_entries, list)
            and bool(bible_entries)
            and all(isinstance(item, dict) and bool(item.get("aliases")) and bool(item.get("narrative_role")) for item in bible_entries)
        )
        if not bible_is_current:
            bible = analyze_character_bible(text_content)
            if not bible.get("characters"):
                # Conservative fallback: preserve existing descriptions when the API
                # is temporarily unavailable, then save them as a basic canonical bible.
                fallback = analysis.get("characters", {}) if isinstance(analysis.get("characters", {}), dict) else {}
                entries = []
                for fallback_index, (name, description) in enumerate(list(fallback.items())[:8]):
                    entries.append({
                        "id": re.sub(r"[^A-Z0-9]+", "_", str(name).upper()).strip("_"),
                        "name": str(name), "aliases": [str(name)], "kind": "human",
                        "narrative_role": "protagonist" if fallback_index == 0 else "supporting",
                        "visual_reference_priority": "primary" if fallback_index == 0 else "supporting",
                        "age_or_stage": "follow story; keep unchanged",
                        "face_or_head": "use the same face every time", "skin_tone_or_coat": "keep one consistent tone",
                        "hair_or_mane": "keep the same colour and hairstyle", "eyes": "keep the same eye colour",
                        "build_or_body": "keep the same body type", "wardrobe_or_equipment": "era-appropriate default outfit; adapt only when story time, weather, location, or activity warrants it",
                        "distinctive_features": "do not invent new permanent marks", "continuity_lock": str(description)[:700],
                    })
                bible = {"version": 5, "characters": entries}
            analysis["character_bible"] = bible
            analysis["character_bible_source_sha256"] = story_source_sha256
            log.info("[Character Bible] Сохранено постоянных персонажей: %s", len(bible.get("characters", [])))

        # If the channel has an image in <channel>/etalon/, bind it to the one
        # canonical protagonist. This makes the user's only setup step dropping a picture there.
        hero_reference_path = apply_channel_hero_reference_to_bible(
            analysis["character_bible"],
            text_content,
            folder,
            analysis.get("characters", {}) if isinstance(analysis.get("characters", {}), dict) else {},
        )
        if hero_reference_path:
            analysis["channel_hero_reference_path"] = str(hero_reference_path)
        else:
            analysis.pop("channel_hero_reference_path", None)
        state.data["analysis"] = analysis
        state.save()

        canonical_map = character_bible_prompt_map(
            analysis["character_bible"],
            analysis.get("characters", {}) if isinstance(analysis.get("characters", {}), dict) else {},
        )
        if canonical_map:
            analysis["characters"] = canonical_map
        elif not analysis.get("characters"):
            analysis["characters"] = analyze_characters(text_content)
        analysis["done"] = True
        state.save()
        try:
            (state.work_dir / "character_bible.json").write_text(
                json.dumps(analysis.get("character_bible", {"version": 1, "characters": []}), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("[Character Bible] Не удалось сохранить отдельный JSON: %s", exc)

        # Prepare reusable local identity portraits for primary characters only.
        # Images are created once per character identity and reused across Chrome profiles.
        await ensure_character_reference_images(state, analysis, text_content, manager, playwright)

        # 4. Intro — use actual Flow duration; never clamp to 20 seconds.
        previous_intro_duration = 0.0
        try:
            previous_intro_duration = float(
                state.data.get("timing", {}).get("intro_duration", 0.0)
            )
        except Exception:
            pass

        intro_duration = await build_intro(
            state,
            narration_text,
            manager,
            playwright,
            target_lang=project_lang,
            narration_segments=segments,
        )
        if intro_duration <= 0.05:
            intro_duration = 0.0

        intro_timing_changed = (
            previous_intro_duration > 0.05
            and abs(previous_intro_duration - intro_duration) > 0.10
        )

        state.data.setdefault("timing", {})
        state.data["timing"]["intro_duration"] = intro_duration
        state.data["timing"]["audio_duration"] = audio_duration
        if intro_timing_changed:
            # Old montage/final renders refer to the old visual timeline.
            # Empty saved paths must never become Path(".") and be unlinked.
            for key in ("no_audio", "with_audio"):
                stale_value = str(state.data["montage"].get(key, "") or "").strip()
                if not stale_value:
                    continue
                stale = Path(stale_value)
                if stale.is_file():
                    try:
                        stale.unlink(missing_ok=True)
                    except OSError as exc:
                        log.warning("[Timing] Не удалось удалить устаревший монтаж %s: %s; продолжаю пересборку.", stale, exc)
            state.data["montage"]["done"] = False
            state.data["final"]["done"] = False
            state.data["final"]["subtitles_burned"] = False
            stale_final_value = str(state.data["final"].get("video", "") or "").strip()
            if stale_final_value:
                stale_final = Path(stale_final_value)
                if stale_final.is_file():
                    try:
                        stale_final.unlink(missing_ok=True)
                    except OSError as exc:
                        log.warning("[Timing] Не удалось удалить устаревший финал %s: %s; продолжаю пересборку.", stale_final, exc)
            state.data["final"]["video"] = ""
            log.warning(
                "🔄 [Timing] Intro изменился %.2fs -> %.2fs; пересобираем визуальный timeline.",
                previous_intro_duration,
                intro_duration,
            )

        # 5. Planning
        state.set_stage("planning")
        saved_intervals = []
        for item in state.data.get("main_clips", []):
            try:
                saved_intervals.append((float(item["start"]), float(item["end"])))
            except Exception:
                saved_intervals.append(None)

        saved_timeline_start = None
        saved_timeline_signature = str(
            state.data.get("timing", {}).get("timeline_signature", "")
        ).strip()
        try:
            saved_timeline_start = float(
                state.data.get("timing", {}).get("main_timeline_start")
            )
        except Exception:
            pass

        legacy_timeline = bool(saved_intervals) and not saved_timeline_signature
        timeline_shifted = (
            legacy_timeline
            or (
                saved_timeline_start is not None
                and abs(saved_timeline_start - intro_duration) > 0.10
            )
        )

        semantic_stale = "semantic-v2" not in saved_timeline_signature
        if saved_intervals and all(x is not None for x in saved_intervals) and not timeline_shifted and not semantic_stale:
            intervals = [x for x in saved_intervals if x is not None]
            log.info(
                "⏭️ [Resume] Используем сохранённый таймлайн: %s сцен, старт %.2fs",
                len(intervals),
                intro_duration,
            )
        elif USE_DYNAMIC_SLIDE_INTERVALS:
            semantic_scenes = plan_semantic_scene_intervals(
                segments, audio_duration, intro_duration, min_dur=8.0, target_dur=15.0, max_dur=22.0
            )
            # Existing projects keep their previous scene count so semantic timing
            # never increases the number of Flow image requests.
            prior_count = sum(1 for item in saved_intervals if item is not None)
            if prior_count and len(semantic_scenes) != prior_count:
                semantic_scenes = fit_semantic_scene_count(semantic_scenes, segments, prior_count, intro_duration, audio_duration)
            if prior_count and len(semantic_scenes) != prior_count:
                log.warning("[Semantic Planning] Не удалось безопасно подогнать границы к прежнему числу сцен (%s); сохраняю прежний таймлайн, чтобы не увеличивать квоты Flow.", prior_count)
                old_records_for_count = state.data.get("main_clips", [])
                semantic_scenes = [
                    {"start": float(item[0]), "end": float(item[1]),
                     "scene_break": bool(old_records_for_count[i].get("scene_break", False)) if i < len(old_records_for_count) else False,
                     "pause_before": float(old_records_for_count[i].get("pause_before", 0.0) or 0.0) if i < len(old_records_for_count) else 0.0}
                    for i, item in enumerate(saved_intervals) if item is not None
                ]
            intervals = [(float(x["start"]), float(x["end"])) for x in semantic_scenes]
            if not intervals:
                intervals = calculate_dynamic_slide_intervals(audio_duration, start_time=intro_duration)
            log.info("[Semantic Planning] Построено %s сцен по Whisper-предложениям/паузам.", len(intervals))
            if timeline_shifted:
                log.info(
                    "🔄 [Timing] Сохранённый таймлайн устарел; новые сцены считаются от %.2fs.",
                    intro_duration,
                )
        else:
            intervals = make_clip_intervals(
                intro_duration,
                audio_duration,
                FLOW_CLIP_SECONDS,
            )

        timeline_signature = (
            f"intro={intro_duration:.3f}|audio={audio_duration:.3f}|"
            f"n={len(intervals)}|semantic-v2={SEMANTIC_TIMELINE_VERSION}"
        )
        state.data["timing"]["main_timeline_start"] = intro_duration
        state.data["timing"]["timeline_signature"] = timeline_signature

        existing_records = {
            int(item["index"]): item
            for item in state.data.get("main_clips", [])
            if "index" in item
        }

        main_clips = []
        segment_texts = []
        semantic_by_index = semantic_scenes if "semantic_scenes" in locals() and len(semantic_scenes) == len(intervals) else [
            {"scene_break": bool(existing_records.get(i, {}).get("scene_break", False)),
             "pause_before": float(existing_records.get(i, {}).get("pause_before", 0.0) or 0.0)}
            for i in range(len(intervals))
        ]

        for idx, (start, end) in enumerate(intervals):
            old = existing_records.get(idx, {})
            same_timeline = old.get("timeline_signature") == timeline_signature
            text_slot = get_text_for_timeslot(segments, start, end)
            segment_texts.append(text_slot)

            record = {
                "index": idx,
                "start": start,
                "end": end,
                "duration": max(0.05, end - start),
                "text": text_slot,
                "scene_break": bool(semantic_by_index[idx].get("scene_break", False)),
                "media_type": "image",
                "transition_out": "cut",
                "transition_dur": 0.0,
                "pause_before": float(semantic_by_index[idx].get("pause_before", 0.0) or 0.0),
                "plan": old.get("plan", ""),
                "prompt": old.get("prompt", "") if old.get("prompt") and is_valid_image(Path(str(old.get("image_path", "")))) else "",
                "image_path": old.get(
                    "image_path",
                    str(state.media_dir / f"scene_{idx + 1:04d}.jpg"),
                ),
                "output_path": old.get(
                    "output_path",
                    str(state.media_dir / f"scene_{idx + 1:04d}.mp4"),
                ),
                "done": bool(old.get("done", False)) if same_timeline else False,
                "account": old.get("account", ""),
                "timeline_signature": timeline_signature,
                "render_effect_version": old.get("render_effect_version", ""),
                # A semantic timeline refresh changes timing, not the existing Flow still.
                # Reuse a valid cached image and only regenerate when the still is missing
                # or a previous quality/safety checkpoint explicitly requested it.
                "needs_regeneration": bool(old.get("needs_regeneration", False)) or not is_valid_image(Path(str(old.get("image_path", "")))),
            }

            if same_timeline and is_valid_video(Path(record["output_path"])) and not record["needs_regeneration"]:
                record["done"] = True

            main_clips.append(record)

        scene_break_count = 0
        last_fadeblack_scene = -999
        last_fadeblack_time = -1e9
        for scene_idx, scene_record in enumerate(main_clips):
            if scene_idx + 1 >= len(main_clips):
                scene_record["transition_out"] = "cut"
                scene_record["transition_dur"] = 0.0
                continue
            next_scene = main_clips[scene_idx + 1]
            if not bool(next_scene.get("scene_break", False)):
                scene_record["transition_out"] = "cut"
                scene_record["transition_dur"] = 0.0
                continue
            scene_break_count += 1
            pause = float(next_scene.get("pause_before", 0.0) or 0.0)
            next_start = float(next_scene.get("start", 0.0) or 0.0)
            fadeblack_allowed = scene_idx - last_fadeblack_scene >= 4 and next_start - last_fadeblack_time >= 50.0
            if pause >= 0.8 and fadeblack_allowed:
                scene_record["transition_out"] = "fadeblack"
                scene_record["transition_dur"] = round(random.uniform(0.45, 0.55), 3)
                last_fadeblack_scene = scene_idx
                last_fadeblack_time = next_start
            else:
                scene_record["transition_out"] = "dissolve"
                scene_record["transition_dur"] = round(random.uniform(0.35, 0.45), 3)

        state.data["main_clips"] = main_clips
        state.save()

        if (
            intro_timing_changed
            or timeline_shifted
            or not state.data.get("scene_plan")
            or len(state.data["scene_plan"]) != len(segment_texts)
            or state.data.get("scene_plan_version") != SCENE_PLAN_VERSION
        ):
            old_scene_plan = list(state.data.get("scene_plan", []))
            storyboard_contexts = []
            for scene_record in state.data.get("main_clips", []):
                scene_start = float(scene_record.get("start", 0.0) or 0.0)
                scene_end = float(scene_record.get("end", scene_start) or scene_start)
                storyboard_contexts.append({
                    "before": get_text_for_timeslot(segments, max(0.0, scene_start - 28.0), scene_start),
                    "after": get_text_for_timeslot(segments, scene_end, min(audio_duration, scene_end + 28.0)),
                })
            new_scene_plan = generate_scene_plan(
                segment_texts,
                scene_contexts=storyboard_contexts,
                character_bible=analysis.get("character_bible", {}),
            )
            state.data["scene_plan"] = new_scene_plan
            state.data["scene_plan_version"] = SCENE_PLAN_VERSION

            # If the old plan excluded people but the new contextual plan explicitly
            # adds them (or vice versa), invalidate only that scene's cached assets so
            # the visual matches the narration. Other complete scenes stay untouched.
            for scene_index, scene_record in enumerate(state.data.get("main_clips", [])):
                new_entry = new_scene_plan[scene_index] if scene_index < len(new_scene_plan) else ""
                old_entry = old_scene_plan[scene_index] if scene_index < len(old_scene_plan) else ""
                old_mode = _scene_cast_mode(old_entry)
                new_mode = _scene_cast_mode(new_entry)
                mode_changed = bool(old_entry and old_mode and new_mode and old_mode != new_mode)
                old_plan_lower = str(old_entry or "").casefold()
                old_was_local_fallback = any(marker in old_plan_lower for marker in (
                    "performing the concrete action or interaction narrated in this beat",
                    "faithfully illustrate the specific concrete subject of this narration",
                ))
                fallback_plan_replaced = bool(old_was_local_fallback and new_entry)
                scene_image = Path(str(scene_record.get("image_path", "")))
                scene_video = Path(str(scene_record.get("output_path", "")))
                if mode_changed or fallback_plan_replaced:
                    if mode_changed:
                        log.warning(
                            "[Scene Plan] Scene %s cast changed %s -> %s; regenerating this scene's image/render for narration alignment.",
                            scene_index + 1, old_mode, new_mode,
                        )
                    else:
                        log.warning(
                            "[Scene Plan] Scene %s had a local fallback storyboard; the complete selected-provider plan is now available. "
                            "Regenerating its old image/render so the fallback frame cannot remain in the final video.",
                            scene_index + 1,
                        )
                    scene_image.unlink(missing_ok=True)
                    scene_video.unlink(missing_ok=True)
                    scene_record["done"] = False
                    scene_record["needs_regeneration"] = True
                    scene_record["render_effect_version"] = ""
                    scene_record["prompt"] = ""
                elif scene_record.get("needs_regeneration") or not is_valid_image(scene_image):
                    scene_record["prompt"] = ""
            state.save()

        if not state.data.get("video_inserts_assigned"):
            for plan_idx, plan_record in enumerate(state.data["main_clips"]):
                if not plan_record.get("plan") and plan_idx < len(state.data.get("scene_plan", [])):
                    plan_record["plan"] = state.data["scene_plan"][plan_idx]
            state.data["main_clips"] = assign_main_video_insert_slots(state.data["main_clips"], segments, intro_duration, audio_duration)
            state.data["scene_plan"] = [str(x.get("plan", "")) for x in state.data["main_clips"]]
            state.data["video_inserts_assigned"] = True
            state.save()
        # 6. Flow Images -> Video Clips. Use adaptive 2.5D on every still image:
        # soft depth when visual cues suggest people/animals, full depth on landscapes.
        # Intro/Flow/Veo video files are never passed through this renderer.
        state.set_stage("flow_media")
        chars = analysis.get("characters", {})
        ctx = analysis.get("context", {})
        character_bible_for_refs = analysis.get("character_bible", {})
        known_character_reference_images: list[Path] = []
        reference_entries = character_bible_for_refs.get("characters", []) if isinstance(character_bible_for_refs, dict) else []
        for character_entry in reference_entries:
            if not isinstance(character_entry, dict):
                continue
            raw_reference_path = str(character_entry.get("reference_image_path", "")).strip()
            if not raw_reference_path:
                continue
            candidate_reference_path = Path(raw_reference_path)
            if candidate_reference_path.is_file() and candidate_reference_path not in known_character_reference_images:
                known_character_reference_images.append(candidate_reference_path)
        if known_character_reference_images:
            log.info(
                "[Character Reference] Контроль случайного скачивания включён для %s локальных эталонов.",
                len(known_character_reference_images),
            )
        parallax_available = initialize_parallax_backend()
        active_render_version = (
            PARALLAX_RENDER_VERSION if parallax_available
            else PARALLAX_FALLBACK_RENDER_VERSION
        )
        scene_render_changed = False

        # One-scene look-ahead: generate the next missing scene prompt in a worker
        # thread while Flow is waiting for the current image (and, if needed, while
        # the current still is rendered). This does not generate images in parallel,
        # does not reorder scenes, and does not write the shared project state from
        # the worker. A single look-ahead task avoids flooding Gemini or its key pool.
        prompt_prefetch_tasks: dict[int, asyncio.Task[str]] = {}

        async def _prefetch_scene_prompt(scene_index: int) -> str:
            upcoming = state.data["main_clips"][scene_index]
            upcoming_start = float(upcoming["start"])
            upcoming_end = float(upcoming["end"])
            upcoming_before = get_text_for_timeslot(
                segments, max(0, upcoming_start - 40), upcoming_start
            )
            upcoming_after = get_text_for_timeslot(
                segments, upcoming_end, min(audio_duration, upcoming_end + 40)
            )
            upcoming_plan = (
                state.data.get("scene_plan", [])[scene_index]
                if scene_index < len(state.data.get("scene_plan", [])) else ""
            )
            log.info(
                "⚡ [Prompt Prefetch] Начинаю промпт сцены %s параллельно с обработкой сцены %s.",
                scene_index + 1, scene_index,
            )
            return await asyncio.to_thread(
                generate_image_prompt,
                scene_index,
                str(upcoming.get("text", "")),
                upcoming_before,
                upcoming_after,
                upcoming_plan,
                chars,
                ctx,
                project_lang,
                defer_character_locks=True,
            )

        planned_zoom_dirs: list[str] = []
        zoom_history: list[str] = []
        for zoom_idx, zoom_record in enumerate(state.data["main_clips"]):
            zoom_plan = state.data.get("scene_plan", [])[zoom_idx] if zoom_idx < len(state.data.get("scene_plan", [])) else ""
            zoom_image = Path(str(zoom_record.get("image_path", "")))
            seed_source = f"{zoom_image.name}|{zoom_record.get('prompt', '')}|{zoom_plan or ''}|{zoom_idx}"
            seed = int(hashlib.md5(seed_source.encode("utf-8", errors="ignore")).hexdigest()[:8], 16)
            zoom_dir_candidate = random.Random(seed).choice(["in", "out"])
            if len(zoom_history) >= 2 and zoom_history[-1] == zoom_history[-2] == zoom_dir_candidate:
                zoom_dir_candidate = "out" if zoom_dir_candidate == "in" else "in"
            planned_zoom_dirs.append(zoom_dir_candidate)
            zoom_history.append(zoom_dir_candidate)

        for idx, record in enumerate(state.data["main_clips"]):
            output_path = Path(record["output_path"])
            if record.get("media_type") == "video":
                if is_valid_video(output_path):
                    record["done"] = True
                    record["video_insert_mastered"] = True
                    state.save()
                    continue
                raw_video = state.media_dir / f"scene_{idx + 1:04d}_raw.mp4"
                scene_plan_video = str(record.get("plan") or (state.data.get("scene_plan", [""])[idx] if idx < len(state.data.get("scene_plan", [])) else ""))
                video_prompt = (
                    "Cinematic documentary video insert. No talking head, no lip motion, no dialogue. "
                    "Show physical movement, travel, nature/weather, crowds, or a location/era change. "
                    "Match the established visual style and exact narration beat; no text, logo or watermark.\n\n"
                    + str(record.get("text", "")) + "\n\n" + scene_plan_video
                )
                video_prompt = enforce_selected_visual_style(
                    enforce_flow_intro_silent_visual_policy(
                        enforce_flow_ambience_only_audio(
                            apply_visible_text_language_rule(
                                apply_youtube_content_safety_rules(video_prompt, "video"), project_lang
                            )
                        )
                    ), "video"
                )
                try:
                    video_cast_mode = _scene_cast_mode(scene_plan_video)
                    video_references = references_for_scene(
                        character_bible_for_refs, f"CAST={video_cast_mode}", video_prompt
                    ) if video_cast_mode else []
                    await generate_flow_with_failover(
                        manager, playwright, "video", video_prompt, raw_video,
                        reference_images=video_references,
                    )
                    target_duration = float(record["end"]) - float(record["start"])
                    if not master_main_video_insert(raw_video, output_path, target_duration):
                        raise FlowTechnicalError("Veo insert mastering failed")
                    record["done"] = True
                    record["video_insert_raw_path"] = str(raw_video)
                    record["video_insert_mastered"] = True
                    state.save()
                    continue
                except Exception as video_exc:
                    log.warning("[Veo Inserts] Scene %s failed; falling back to still image: %s", idx + 1, video_exc)
                    record["media_type"] = "image"
                    record["done"] = False
                    record["video_insert_fallback"] = str(video_exc)[:700]
                    raw_video.unlink(missing_ok=True)
                    output_path.unlink(missing_ok=True)
                    state.save()
            image_path = Path(record["image_path"])
            scene_plan_for_effect = (
                state.data.get("scene_plan", [])[idx]
                if idx < len(state.data.get("scene_plan", [])) else ""
            )
            # Every still slide uses 2.5D when the backend is available. Subject
            # strength is classified later, once this slide's actual visual prompt
            # and final JPG are available; narration is never used for this choice.
            use_parallax_for_scene = bool(parallax_available)
            scene_target_render_version = (
                PARALLAX_RENDER_VERSION if use_parallax_for_scene
                else PARALLAX_FALLBACK_RENDER_VERSION
            )
            # Do not delete previously rendered scenes based only on similarity to
            # character references. Character continuity naturally creates similar
            # portraits/colors/poses; valid cached assets should remain resumable.
            living_subject = False
            living_reason = "pending visual-prompt/image classification"
            parallax_strength = 1.0
            quality_retry_count = int(record.get("quality_retry_count", 0) or 0)
            existing_quality_issues = image_quality_issues(image_path) if is_valid_image(image_path) else []

            # Do not reuse a rendered MP4 when its source image is missing: there is
            # no way to verify that cached visual, so regenerate from a reviewable still.
            if ENABLE_YOUTUBE_CONTENT_SAFETY_REVIEW and is_valid_video(output_path) and not is_valid_image(image_path):
                record["needs_regeneration"] = True
                record["done"] = False
                output_path.unlink(missing_ok=True)
                state.save()

            # Content-safety review must happen before reusing an already-rendered scene.
            if ENABLE_YOUTUBE_CONTENT_SAFETY_REVIEW and is_valid_image(image_path):
                safety_review = review_image_with_cache(record, image_path, context=record.get("text", ""))
                review_decision = str(safety_review.get("decision", "REVIEW")).upper().strip()
                if review_decision == "PASS":
                    record["content_safety_retry_count"] = 0
                elif review_decision == "REJECT":
                    log.warning(
                        "[Content Safety] Scene %s explicitly rejected: %s. Replacing it with a neutral, non-graphic prompt.",
                        idx + 1, safety_review.get("reason", "no reason"),
                    )
                    record["content_safety_rejected_sha256"] = safety_review.get("sha256", "")
                    record["needs_regeneration"] = True
                    record["done"] = False
                    record["prompt"] = make_safe_regeneration_prompt(
                        context_text=record.get("text", ""), setting_context=ctx, media_kind="image"
                    )
                    output_path.unlink(missing_ok=True)
                    image_path.unlink(missing_ok=True)
                    state.save()
                else:
                    # REVIEW typically means uncertainty or a transient API/network error.
                    # Keep the generated scene and record a warning rather than restart work.
                    log.warning(
                        "[Content Safety] Scene %s review=%s; keeping the existing image and continuing. Reason: %s",
                        idx + 1, review_decision, safety_review.get("reason", "no reason"),
                    )
                    record["content_safety_review_advisory"] = {
                        "decision": review_decision,
                        "reason": str(safety_review.get("reason", "no reason"))[:1000],
                        "reviewed_at": safety_review.get("reviewed_at", now_iso()),
                    }
                    state.save()

            if existing_quality_issues and quality_retry_count < IMAGE_QUALITY_RETRIES:
                quality_retry_count += 1
                record["quality_retry_count"] = quality_retry_count
                record["quality_issues"] = existing_quality_issues
                record["needs_regeneration"] = True
                record["done"] = False
                output_path.unlink(missing_ok=True)
                log.warning(
                    "🧪 [Image QA] Scene %s/%s: %s. Удаляю старый рендер и перегенерирую с жёсткими требованиями цвета/16:9.",
                    idx + 1, len(state.data["main_clips"]), "; ".join(existing_quality_issues),
                )
                state.save()
            elif is_valid_video(output_path) and (
                record.get("render_effect_version") == scene_target_render_version
                # If the depth backend cannot load, don't needlessly re-render
                # all legacy Ken Burns clips; retry parallax on a later run when
                # the dependencies/model become available.
                or (not parallax_available and not record.get("render_effect_version"))
            ):
                if existing_quality_issues:
                    log.warning(
                        "⚠️ [Image QA] Scene %s всё ещё имеет особенности (%s), но лимит QA-повтора уже использован; оставляю результат, чтобы не зациклить проект.",
                        idx + 1, "; ".join(existing_quality_issues),
                    )
                record["done"] = True
                state.save()
                log.info(
                    "⏭️ [Resume] Scene %s/%s уже готова (render=%s).",
                    idx + 1,
                    len(state.data["main_clips"]),
                    scene_target_render_version,
                )
                continue
            elif is_valid_video(output_path):
                log.info(
                    "🔄 [Parallax] Пересобираю сцену %s из уже готового JPG: старый render=%s, новый=%s. "
                    "Повторно запрашивать изображение у Flow не нужно.",
                    idx + 1,
                    record.get("render_effect_version") or "legacy-KenBurns",
                    scene_target_render_version,
                )

            start = float(record["start"])
            end = float(record["end"])
            before = get_text_for_timeslot(
                segments,
                max(0, start - 40),
                start,
            )
            after = get_text_for_timeslot(
                segments,
                end,
                min(audio_duration, end + 40),
            )

            # Автоматический сброс старых видео-промптов в пользу фотореалистичных
            current_prompt = str(record.get("prompt", "")).lower()
            if any(w in current_prompt for w in ["video shot", "camera movement", "continuous realistic motion", "controlled camera movement"]):
                record["prompt"] = ""
            else:
                # Older checkpoints may contain identity-lock sections copied from neighboring narration.
                cleaned_saved_prompt = strip_character_identity_lock_sections(str(record.get("prompt", "")))
                if cleaned_saved_prompt != str(record.get("prompt", "")):
                    record["prompt"] = cleaned_saved_prompt
                    state.save()

            if not record.get("prompt"):
                prefetch_task = prompt_prefetch_tasks.pop(idx, None)
                if prefetch_task is not None:
                    try:
                        record["prompt"] = await prefetch_task
                        log.info("⚡ [Prompt Prefetch] Промпт сцены %s готов заранее.", idx + 1)
                    except Exception as exc:
                        log.warning(
                            "[Prompt Prefetch] Сцена %s: предварительная генерация не удалась (%s); "
                            "генерирую обычным способом.", idx + 1, exc,
                        )
                        record["prompt"] = generate_image_prompt(
                            idx, record["text"], before, after,
                            state.data["scene_plan"][idx], chars, ctx,
                            target_lang=project_lang,
                        )
                else:
                    record["prompt"] = generate_image_prompt(
                        idx,
                        record["text"],
                        before,
                        after,
                        state.data["scene_plan"][idx],
                        chars,
                        ctx,
                        target_lang=project_lang,
                    )
                state.save()

            # Start at most one prompt ahead. Only prefetch when the upcoming scene
            # actually needs a new still and its saved prompt is absent/obsolete.
            next_idx = idx + 1
            if next_idx < len(state.data["main_clips"]) and next_idx not in prompt_prefetch_tasks:
                next_record = state.data["main_clips"][next_idx]
                next_saved_prompt = str(next_record.get("prompt", "") or "").strip()
                next_lower = next_saved_prompt.casefold()
                stale_prompt = any(
                    marker in next_lower
                    for marker in ("video shot", "camera movement", "continuous realistic motion", "controlled camera movement")
                )
                next_image = Path(str(next_record.get("image_path", "")))
                next_needs_image = bool(next_record.get("needs_regeneration")) or not is_valid_image(next_image)
                if next_needs_image and (not next_saved_prompt or stale_prompt):
                    prompt_prefetch_tasks[next_idx] = asyncio.create_task(_prefetch_scene_prompt(next_idx))
                    # Give the task a scheduling turn before any synchronous 2.5D renderer
                    # can occupy the event loop. The actual Gemini HTTP call runs in a thread.
                    await asyncio.sleep(0)

            log.info(
                "🖼️ [Flow Image] Scene %s/%s | %.2f-%.2fs",
                idx + 1,
                len(state.data["main_clips"]),
                start,
                end,
            )

            if record.get("needs_regeneration") or not is_valid_image(image_path):
                base_prompt = apply_visible_text_language_rule(
                    apply_youtube_content_safety_rules(str(record["prompt"]), "image"),
                    project_lang,
                )
                record["prompt"] = base_prompt
                generation_prompt = (
                    make_full_color_edge_to_edge_prompt(base_prompt, existing_quality_issues)
                    if quality_retry_count > 0 else make_full_color_edge_to_edge_prompt(base_prompt)
                )
                generation_prompt = apply_visible_text_language_rule(generation_prompt, project_lang)
                # Only the visible-shot prompt/shot plan determine who appears in this image.
                # Do not use narration before/after or the already augmented full generation prompt:
                # those may mention people who are not in an object-only close-up.
                scene_reference_images = references_for_scene(
                    analysis.get("character_bible", {}),
                    record.get("prompt", ""),
                    scene_plan_for_effect,
                )
                cast_mode = _scene_cast_mode(scene_plan_for_effect)
                if cast_mode == "NONE":
                    # Do not append the character bible/ref map to an object-only scene.
                    generation_prompt = (
                        generation_prompt
                        + "\n\nMANDATORY CAST OVERRIDE — CAST=NONE: no visible people, no protagonist, no human silhouettes or people in the background. "
                          "Illustrate only the scene's meaningful objects, setting, POV details, or visual metaphor."
                    )
                else:
                    generation_prompt = add_reference_usage_instruction(
                        generation_prompt, analysis.get("character_bible", {}), scene_reference_images
                    )
                if scene_reference_images:
                    log.info(
                        "[Character Reference] Сцена %s | CAST=%s | использует: %s",
                        idx + 1, cast_mode or "legacy", ", ".join(path.name for path in scene_reference_images),
                    )
                elif cast_mode:
                    log.info("[Character Reference] Сцена %s | CAST=%s | эталон героя не прикрепляется.", idx + 1, cast_mode)
                generation_failed = False
                generation_failure_reason = ""
                generated_issues: list[str] = []
                try:
                    await generate_flow_with_failover(
                        manager,
                        playwright,
                        "image",
                        generation_prompt,
                        image_path,
                        preferred_account=record.get("account") or None,
                        reference_images=scene_reference_images,
                    )

                    generated_issues = image_quality_issues(image_path)
                    if generated_issues and quality_retry_count < IMAGE_QUALITY_RETRIES:
                        quality_retry_count += 1
                        record["quality_retry_count"] = quality_retry_count
                        record["quality_issues"] = generated_issues
                        state.save()
                        log.warning(
                            "🧪 [Image QA] Scene %s содержит: %s. Повторная генерация с усиленным промптом (%s/%s).",
                            idx + 1, "; ".join(generated_issues), quality_retry_count, IMAGE_QUALITY_RETRIES,
                        )
                        strict_prompt = apply_visible_text_language_rule(
                            make_full_color_edge_to_edge_prompt(base_prompt, generated_issues),
                            project_lang,
                        )
                        strict_prompt = add_reference_usage_instruction(
                            strict_prompt, analysis.get("character_bible", {}), scene_reference_images
                        )
                        await generate_flow_with_failover(
                            manager,
                            playwright,
                            "image",
                            strict_prompt,
                            image_path,
                            preferred_account=record.get("account") or None,
                            reference_images=scene_reference_images,
                        )
                        generated_issues = image_quality_issues(image_path)
                except FlowGenerationRejectedError as exc:
                    generation_failed = True
                    generation_failure_reason = f"Flow rejection: {exc}"
                except FlowTechnicalError as exc:
                    generation_failed = True
                    generation_failure_reason = str(exc)

                if not generation_failed and ENABLE_YOUTUBE_CONTENT_SAFETY_REVIEW and is_valid_image(image_path):
                    safety_review = review_image_with_cache(record, image_path, context=record.get("text", ""))
                    review_decision = str(safety_review.get("decision", "REVIEW")).upper().strip()
                    if review_decision == "REJECT":
                        rejected_hash = safety_review.get("sha256", sha256_file(image_path))
                        log.warning(
                            "[Content Safety] Newly generated scene %s did not pass (%s): %s",
                            idx + 1, safety_review.get("decision"), safety_review.get("reason", "no reason"),
                        )
                        image_path.unlink(missing_ok=True)
                        output_path.unlink(missing_ok=True)
                        retry_count = int(record.get("content_safety_retry_count", 0) or 0)
                        if retry_count >= YOUTUBE_CONTENT_SAFETY_RETRIES:
                            record["content_safety_rejected_sha256"] = rejected_hash
                            state.save()
                            raise FlowContentSafetyReviewError(
                                f"Scene {idx + 1} was explicitly REJECTED and no more safety replacements remain: "
                                f"{safety_review.get('reason', 'no reason')}"
                            )
                        record["content_safety_retry_count"] = retry_count + 1
                        safe_retry_prompt = make_safe_regeneration_prompt(
                            context_text=record.get("text", ""), setting_context=ctx, media_kind="image"
                        )
                        record["prompt"] = safe_retry_prompt
                        state.save()
                        safe_retry_generation_prompt = apply_visible_text_language_rule(
                            make_full_color_edge_to_edge_prompt(safe_retry_prompt), project_lang
                        )
                        safe_retry_generation_prompt = add_reference_usage_instruction(
                            safe_retry_generation_prompt, analysis.get("character_bible", {}), scene_reference_images
                        )
                        await generate_flow_with_failover(
                            manager, playwright, "image",
                            safe_retry_generation_prompt,
                            image_path, preferred_account=record.get("account") or None,
                            reference_images=scene_reference_images,
                        )
                        if not is_valid_image(image_path):
                            raise FlowContentSafetyReviewError(
                                f"Scene {idx + 1}: safe replacement image was not generated; stopping before publication"
                            )
                        generated_issues = image_quality_issues(image_path)
                        second_review = review_image_with_cache(record, image_path, context="Safe replacement scene; non-graphic documentary framing")
                        second_decision = str(second_review.get("decision", "REVIEW")).upper().strip()
                        if second_decision == "REJECT":
                            second_hash = second_review.get("sha256", sha256_file(image_path))
                            image_path.unlink(missing_ok=True)
                            output_path.unlink(missing_ok=True)
                            record["content_safety_rejected_sha256"] = second_hash
                            state.save()
                            raise FlowContentSafetyReviewError(
                                f"Scene {idx + 1}: neutral replacement was explicitly rejected ({second_review.get('reason', 'no reason')})"
                            )
                        if second_decision != "PASS":
                            log.warning(
                                "[Content Safety] Safe replacement for scene %s returned %s; keeping the neutral prompt result. Reason: %s",
                                idx + 1, second_decision, second_review.get("reason", "no reason"),
                            )
                        record["content_safety_retry_count"] = 0
                        log.info("[Content Safety] Scene %s safe replacement kept (review=%s)", idx + 1, second_decision)
                    elif review_decision != "PASS":
                        log.warning(
                            "[Content Safety] Newly generated scene %s review=%s; continuing without a confirmed pass. Reason: %s",
                            idx + 1, review_decision, safety_review.get("reason", "no reason"),
                        )
                        record["content_safety_review_advisory"] = {
                            "decision": review_decision,
                            "reason": str(safety_review.get("reason", "no reason"))[:1000],
                            "reviewed_at": safety_review.get("reviewed_at", now_iso()),
                        }

                if generation_failed:
                    fallback_image, fallback_source = find_fallback_scene_image(
                        state, idx, image_path
                    )
                    ensure_directory(image_path.parent)
                    shutil.copy2(fallback_image, image_path)
                    record["generation_skipped"] = True
                    record["fallback_image_from"] = fallback_source
                    record["generation_failure_reason"] = generation_failure_reason[:500]
                    record.pop("quality_issues", None)
                    record["needs_regeneration"] = False
                    record["done"] = False
                    log.warning(
                        "⏭️ [Flow Fallback] Scene %s/%s не создана после максимум 4 технических попыток/явного отказа. "
                        "Использую предыдущий/резервный кадр (%s); таймлайн сохранён. Причина: %s",
                        idx + 1, len(state.data["main_clips"]), fallback_source,
                        generation_failure_reason[:200],
                    )
                else:
                    record.pop("generation_skipped", None)
                    record.pop("fallback_image_from", None)
                    record.pop("generation_failure_reason", None)
                    if generated_issues:
                        record["quality_issues"] = generated_issues
                        log.warning(
                            "⚠️ [Image QA] Scene %s после проверки всё ещё: %s. Оставляю результат после ограниченного QA-повтора.",
                            idx + 1, "; ".join(generated_issues),
                        )
                    else:
                        record.pop("quality_issues", None)
                        log.info(
                            "✅ [Image QA] Scene %s: цветное изображение, без обнаруженных широких чёрных боковых полос.",
                            idx + 1,
                        )
                    record["needs_regeneration"] = False
                    record["done"] = False
                state.save()

            # Choose depth strength and camera motion from the actual visual prompt.
            # No face detector is used for routing: it produced false positives.
            # Each scene gets a stable centered zoom direction without another Gemini call.
            motion_choice = choose_parallax_motion_profile(
                record,
                scene_plan=scene_plan_for_effect,
                characters=chars,
                image_path=image_path,
                scene_index=idx,
                forced_zoom_dir=planned_zoom_dirs[idx] if idx < len(planned_zoom_dirs) else None,
            )
            living_subject = bool(motion_choice["living_subject"])
            living_reason = str(motion_choice["reason"])
            parallax_strength = float(motion_choice["depth_strength"])
            zoom_dir = str(motion_choice["zoom_dir"])
            pan_dir = str(motion_choice["pan_dir"])
            zoom_amount = float(motion_choice["zoom_amount"])
            pan_multiplier = float(motion_choice["pan_multiplier"])
            turn_strength = float(motion_choice["turn_strength"])
            turn_direction = float(motion_choice["turn_direction"])
            motion_profile = str(motion_choice["profile"])
            use_parallax_for_scene = bool(parallax_available)
            if parallax_available:
                log.info(
                    "🎚️ [Parallax] Scene %s: %s | profile=%s | zoom=%s %.0f%% | pan=%s | depth=%.2f | turn=%.2f dir=%+.0f",
                    idx + 1, living_reason, motion_profile, zoom_dir, zoom_amount * 100,
                    pan_dir, parallax_strength, turn_strength, turn_direction,
                )
            rendered_ok = False
            render_mode = PARALLAX_FALLBACK_RENDER_VERSION
            if use_parallax_for_scene:
                depth_path = state.work_dir / "depth_maps" / f"scene_{idx + 1:04d}_depth.png"
                # Motion is centered push-in/pull-out only; 2.5D depth layers remain active.
                transition = 0.0
                try:
                    rendered_ok = render_parallax_slide(
                        image_path,
                        depth_path,
                        output_path,
                        float(record["duration"]),
                        fps=FPS,
                        fade_out_dur=transition,
                        zoom_dir=zoom_dir,
                        pan_dir=pan_dir,
                        depth_strength=parallax_strength,
                        zoom_amount=zoom_amount,
                        pan_multiplier=pan_multiplier,
                        motion_profile=motion_profile,
                        turn_strength=turn_strength,
                        turn_direction=turn_direction,
                    )
                    if rendered_ok:
                        render_mode = PARALLAX_RENDER_VERSION
                except Exception as exc:
                    log.warning(
                        "⚠️ [Parallax] Scene %s не удалось собрать в 2.5D: %s. "
                        "Использую Ken Burns для этой сцены.",
                        idx + 1, exc,
                    )
                    rendered_ok = False

            if not rendered_ok:
                if parallax_available:
                    log.info(
                        "🎞️ [Ken Burns] Scene %s: 2.5D не сработал, использую резервный плавный зум. Причина классификации: %s",
                        idx + 1, living_reason,
                    )
                work_vpy = state.work_dir / f"scene_{idx + 1:04d}.vpy"
                # Match the main 2.5D path: zoom only, with no whole-frame side/diagonal pan.
                zoom_dir = str(motion_choice["zoom_dir"])
                pan_dir = "center"
                speed_factor = random.uniform(*ZOOM_SPEED_FACTOR_RANGE)
                frames = max(1, int(round(float(record["duration"]) * FPS)))
                # Concatenation is a hard cut (no overlap), so fading each
                # slide to black creates a visible dark flash at every boundary.
                transition = 0.0

                generate_vpy_script(
                    image_path,
                    work_vpy,
                    frames,
                    zoom_dir,
                    pan_dir,
                    speed_factor,
                )

                rendered_ok = render_slide(
                    work_vpy,
                    output_path,
                    float(record["duration"]),
                    fps=FPS,
                    fade_out_dur=transition,
                )

                if not rendered_ok:
                    log.warning(
                        "⚠️ [Монтаж] Ken Burns renderer недоступен — используем простой FFmpeg fallback для Scene %s",
                        idx + 1,
                    )
                    rendered_ok = image_to_clip(
                        image_path,
                        output_path,
                        float(record["duration"]),
                    )
                render_mode = PARALLAX_FALLBACK_RENDER_VERSION

            if not rendered_ok:
                raise RuntimeError(
                    f"Scene {idx + 1}: не удалось собрать MP4 из Flow JPG"
                )

            record["done"] = True
            record["needs_regeneration"] = False
            record["render_effect_version"] = render_mode
            scene_render_changed = True
            state.save()

        # Scene mp4 files changed (for example, old Ken Burns renders were upgraded
        # to 2.5D). Invalidate all dependent montage/final files so the new motion
        # actually appears in the delivered video. Still images are not regenerated.
        if scene_render_changed:
            for key in ("no_audio", "with_audio"):
                stale_value = str(state.data["montage"].get(key, "") or "").strip()
                if not stale_value:
                    continue
                stale = Path(stale_value)
                if stale.is_file():
                    try:
                        stale.unlink(missing_ok=True)
                    except OSError as exc:
                        log.warning("[Parallax] Не удалось удалить устаревший монтаж %s: %s; продолжаю пересборку.", stale, exc)
            state.data["montage"]["done"] = False
            stale_final_value = str(state.data["final"].get("video", "") or "").strip()
            if stale_final_value:
                stale_final = Path(stale_final_value)
                if stale_final.is_file():
                    try:
                        stale_final.unlink(missing_ok=True)
                    except OSError as exc:
                        log.warning("[Parallax] Не удалось удалить устаревший финал %s: %s; продолжаю пересборку.", stale_final, exc)
            state.data["final"]["done"] = False
            state.data["final"]["subtitles_burned"] = False
            state.data["final"]["video"] = ""
            state.save()
            log.info("🔄 [Parallax] Обновлённые слайды требуют пересборки монтажа и финального видео.")

        # 7. Montage Concat
        # Transition-only change: rebuild the combined montage/final render when
        # the cross-dissolve version changes. Individual scene camera motion remains
        # governed by the attached baseline render version and is not modified here.
        if state.data["montage"].get("transition_version") != MONTAGE_TRANSITION_VERSION:
            for key in ("no_audio", "with_audio"):
                stale = Path(str(state.data["montage"].get(key, "")))
                if stale.is_file():
                    stale.unlink(missing_ok=True)
            state.data["montage"]["done"] = False
            state.data["montage"]["transition_version"] = MONTAGE_TRANSITION_VERSION
            old_final = str(state.data.get("final", {}).get("video", "") or "").strip()
            if old_final:
                old_final_path = Path(old_final)
                if old_final_path.is_file():
                    old_final_path.unlink(missing_ok=True)
            state.data["final"]["done"] = False
            state.data["final"]["subtitles_burned"] = False
            state.save()
            log.info("🔄 [Переходы] Версия переходов изменилась: перестраиваю только общий монтаж, движение готовых слайдов не меняю.")

        state.set_stage("montage")
        all_parts: list[tuple[Path, float | None]] = []

        if state.data["intro"]["done"]:
            intro_path = Path(state.data["intro"]["output"])
            all_parts.append(
                (
                    intro_path,
                    min(intro_duration, get_duration(intro_path) or intro_duration),
                )
            )

        transition_specs = []
        if state.data["intro"]["done"]:
            transition_specs.append({"type": "cut", "duration": 0.0})
        for record in state.data["main_clips"]:
            all_parts.append((Path(record["output_path"]), float(record["duration"])))
            transition_specs.append({
                "type": str(record.get("transition_out", "cut")),
                "duration": float(record.get("transition_dur", 0.0) or 0.0),
            })

        no_audio = Path(state.data["montage"]["no_audio"])
        if not is_valid_video(no_audio):
            transition_start_index = (
                1 if state.data["intro"]["done"] and Path(state.data["intro"]["output"]).exists()
                else 0
            )
            if not concatenate_videos(
                all_parts,
                no_audio,
                state.work_dir,
                preserve_audio=False,
                crossfade_duration=0.0,
                transition_start_index=transition_start_index,
                transitions=transition_specs,
            ):
                raise RuntimeError("Не удалось собрать видеомонтаж")

            # Never shorten the visual montage to force it to audio_duration.
            # The audio mixer below pads audio to the full visual runtime.
            state.data["montage"]["done"] = True
            state.save()

        # Verify the completed silent montage against the exact mastered narration clock.
        audio_clock = get_duration(Path(str(state.data.get("audio", {}).get("path", ""))))
        video_clock = get_duration(no_audio)
        if audio_clock is not None and video_clock is not None:
            delta = abs(video_clock - audio_clock)
            log.info("[A/V Timing] video_no_audio=%.3fs | audio.wav=%.3fs | delta=%.3fs (target < 0.250s)", video_clock, audio_clock, delta)
            if delta >= 0.25:
                log.error("[A/V Timing] Длительности видео и озвучки расходятся на %.3fs; останавливаю сборку, чтобы не выпустить ролик с дрейфом синхронизации.", delta)
                raise RuntimeError(f"A/V duration mismatch: video_no_audio={video_clock:.3f}s, audio.wav={audio_clock:.3f}s, delta={delta:.3f}s (limit 0.250s)")
        # 8. Audio Mix
        state.set_stage("audio_mix")
        with_audio = Path(state.data["montage"]["with_audio"])
        if not is_valid_video(with_audio):
            fon_files = list(FON_AUDIO_DIR.glob("*.*")) if FON_AUDIO_DIR.exists() else []
            music = None
            if ENABLE_BACKGROUND_MUSIC:
                music = prepare_background_music(
                    fon_files,
                    get_duration(no_audio) or audio_duration,
                    state.work_dir,
                )
            else:
                log.info("🎵 [Audio] Фоновая музыка отключена")
            if not add_audio_to_video(no_audio, audio_path, music, with_audio):
                raise RuntimeError("Audio mix failed")

        log.info("[Pipeline] Audio mix stage completed; moving to title/preflight stage.")

        # 9. Title: use only a title generated from this story's content.
        final_state = state.data.setdefault("final", {})
        title_source_hash = str(final_state.get("title_source_sha256", "") or "")
        title_variants_hash = str(final_state.get("title_variants_source_sha256", "") or "")
        cached_title = str(final_state.get("title", "") or "").strip()
        title_cache_is_current = bool(cached_title and title_source_hash == story_source_sha256)
        title_was_refreshed = not title_cache_is_current
        variants_path = state.work_dir / "Title_Variants.txt"
        if not title_cache_is_current:
            # Legacy state did not store a source hash, so old unrelated titles are stale by definition.
            final_state["title"] = ""
            final_state["title_source_sha256"] = ""
            final_state["title_variants_source_sha256"] = ""
            variants_path.unlink(missing_ok=True)
            title_seed = generate_story_title_seed(text_content, project_lang, fallback_name=folder.name)
            title = translate_and_paraphrase_title(title_seed, state.work_dir, project_lang)
            final_state["title"] = title
            final_state["title_source_sha256"] = story_source_sha256
            final_state["title_variants_source_sha256"] = story_source_sha256
            state.save()
            log.info("[Titles] Regenerated title and variants from the current script; source hash=%s", story_source_sha256[:12])
        else:
            title = cached_title
            if (
                not variants_path.exists()
                or title_variants_hash != story_source_sha256
                or title_variants_need_russian_translation(variants_path, project_lang)
            ):
                translate_and_paraphrase_title(title, state.work_dir, project_lang)
                final_state["title_variants_source_sha256"] = story_source_sha256
                state.save()
        state.data["final"]["title"] = title

        # Audit the actual candidate title with narration context. Keep the report with the project.
        title_review = review_youtube_monetization_text(
            full_transcription[:3500], title=title, mode="title, packaging, and narration context"
        )
        monetization_report["title_review"] = title_review
        monetization_report["publication_checklist"].append(
            "Before upload, compare the final title and thumbnail with the actual events shown; do not imply real footage if scenes are AI-generated dramatizations."
        )
        report_path = save_youtube_monetization_preflight(folder, monetization_report)
        state.data["youtube_monetization_preflight"] = monetization_report
        state.save()
        log.info(
            "[YouTube Preflight] title decision=%s | safer suggestion=%s | report=%s",
            title_review.get("decision", "REVIEW"),
            title_review.get("safer_title_suggestion", "")[:120], report_path,
        )
        if title_review.get("decision") == "HIGH_RISK":
            raise FlowMonetizationReviewError(
                f"The candidate title/packaging was flagged as high risk. Read {report_path} and revise the title before retrying. "
                f"Reason: {title_review.get('reason', 'See report')}"
            )

        # Replace any old title appendix with variants tied to this script hash.
        if (
            not variants_path.exists()
            or state.data["final"].get("title_variants_source_sha256") != story_source_sha256
            or title_variants_need_russian_translation(variants_path, project_lang)
        ):
            translate_and_paraphrase_title(title, state.work_dir, project_lang)
            state.data["final"]["title_variants_source_sha256"] = story_source_sha256
            state.save()
        append_title_variants_to_source_file(text_file, variants_path, fallback_title=title, target_lang=project_lang)

        # 10. Subtitles
        state.set_stage("subtitles")
        final_render_dir = ensure_directory(OUTPUT_ROOT / channel_name / folder.name)
        variants_to_deliver = state.work_dir / "Title_Variants.txt"
        if variants_to_deliver.exists():
            shutil.copy2(variants_to_deliver, final_render_dir / "Title_Variants.txt")

        # Deliver the actual narration/story body, not the auto-appended title-variants block.
        # text_content was parsed from the selected source TXT before title variants are appended.
        story_text_target = final_render_dir / text_file.name
        story_body_for_delivery = str(text_content or "").strip()
        if not story_body_for_delivery:
            try:
                story_body_for_delivery = extract_script_body(
                    text_file.read_text(encoding="utf-8", errors="replace")
                ).strip()
            except Exception:
                story_body_for_delivery = ""
        if not story_body_for_delivery:
            raise FlowTechnicalError(
                f"Не удалось подготовить исходный текст истории для финальной папки: {text_file.name}"
            )
        try:
            story_text_target.write_text(story_body_for_delivery + "\n", encoding="utf-8")
            if not story_text_target.is_file() or story_text_target.stat().st_size <= 0:
                raise OSError("после записи файл истории пуст или отсутствует")
        except Exception as exc:
            raise FlowTechnicalError(
                f"Не удалось скопировать текст истории в папку финального видео: {exc}"
            ) from exc
        state.data["story_text_delivery"] = str(story_text_target.resolve())
        state.save()
        log.info("✅ [Story Delivery] Текст истории сохранён рядом с финальным видео: %s", story_text_target)

        # Character references and character_bible are internal production assets, not deliverables.
        # Remove stale copies from previous pipeline versions, without touching source-project assets.
        stale_bible = final_render_dir / "character_bible.json"
        if stale_bible.is_file():
            try:
                stale_bible.unlink()
                log.info("[Delivery Cleanup] Удалён внутренний файл из финальной папки: %s", stale_bible.name)
            except OSError as exc:
                log.warning("[Delivery Cleanup] Не удалось удалить %s: %s", stale_bible, exc)
        stale_references = final_render_dir / CHARACTER_REFERENCE_DIR_NAME
        if stale_references.is_dir():
            try:
                shutil.rmtree(stale_references)
                log.info("[Delivery Cleanup] Удалена внутренняя папка из финальной папки: %s", stale_references.name)
            except OSError as exc:
                log.warning("[Delivery Cleanup] Не удалось удалить %s: %s", stale_references, exc)

        final_name = safe_name(title, f"Documentary_{int(time.time())}")
        final_path = final_render_dir / f"{final_name}.mp4"
        if title_was_refreshed and final_path.is_file():
            stale_title_archive = ensure_directory(state.work_dir / "stale_title_output_archive")
            archive_candidate = stale_title_archive / f"{final_path.stem}_{int(time.time())}{final_path.suffix}"
            try:
                shutil.move(str(final_path), str(archive_candidate))
                log.warning("[Titles] Архивировал потенциально старый MP4 с совпавшим именем: %s", archive_candidate.name)
            except Exception as exc:
                log.warning("[Titles] Не удалось архивировать старый MP4 с совпавшим именем: %s", exc)

        subtitles_burned = bool(state.data["final"].get("subtitles_burned", False) and state.data["final"].get("done", False))
        if not is_valid_video(final_path) or not subtitles_burned:
            rendered = add_subtitles_to_video(
                with_audio,
                subtitle_path,
                final_render_dir,
                final_name,
            )
            if rendered is None or not is_valid_video(final_path):
                state.data["final"]["done"] = False
                state.data["final"]["subtitles_burned"] = False
                state.save()
                raise RuntimeError(
                    "Не удалось встроить субтитры в финальное видео. Финал не будет помечен готовым, "
                    "исходная папка сохранена для повтора. Проверьте файл .subtitle_burn.log в папке результата."
                )
            state.data["final"]["subtitles_burned"] = True
            state.save()

        if is_valid_video(final_path):
            if not ensure_final_full_hd(final_path):
                state.data["final"]["done"] = False
                state.data["final"]["subtitles_burned"] = False
                state.save()
                raise RuntimeError(
                    "Final video is valid but could not be confirmed/re-encoded to 1920x1080 Full HD. "
                    "The original file was preserved; check FFmpeg logs/encoder availability before marking the project complete."
                )
            container_issues = audit_mp4_container(final_path)
            if container_issues:
                log.warning("[MP4 Audit] Найдены UUID-боксы/служебные теги: %s; выполняю один remux.", ", ".join(container_issues))
                remux_temp = final_path.with_name(final_path.stem + ".clean_remux.tmp.mp4")
                remux_cmd = [FFMPEG_BIN, "-y", "-i", str(final_path), "-map", "0:v:0", "-map", "0:a?", "-c", "copy", *FFMPEG_CLEAN_META_AV, "-movflags", "+faststart", str(remux_temp)]
                try:
                    subprocess.run(remux_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800)
                    if not is_valid_video(remux_temp):
                        raise RuntimeError("remux output failed validation")
                    remux_temp.replace(final_path)
                    remaining = audit_mp4_container(final_path)
                    if remaining:
                        log.warning("[MP4 Audit] После remux остались замечания: %s", ", ".join(remaining))
                    else:
                        log.info("[MP4 Audit] Контейнер очищен после remux.")
                except Exception as audit_exc:
                    remux_temp.unlink(missing_ok=True)
                    log.warning("[MP4 Audit] Remux не выполнен: %s", audit_exc)

        state.data["final"]["video"] = str(final_path)
        state.data["final"]["subtitles"] = str(subtitle_path)
        state.data["final"]["done"] = is_valid_video(final_path) and bool(state.data["final"].get("subtitles_burned", False))
        state.save()

        # Review the assembled output as well as individual generated scenes and intro clips.
        # This samples representative frames only; it cannot guarantee that every frame is safe.
        if ENABLE_FINAL_VIDEO_SAFETY_REVIEW and is_valid_video(final_path):
            final_video_review = review_video_for_youtube(
                final_path,
                context=full_transcription[:1800],
                temp_dir=state.work_dir,
            )
            state.data["final"]["content_safety_review"] = final_video_review
            state.save()
            final_review_decision = str(final_video_review.get("decision", "REVIEW")).upper().strip()
            if final_review_decision == "REJECT":
                raise FlowContentSafetyReviewError(
                    f"Final assembled video was explicitly rejected by sampled-frame review. "
                    f"Reason: {final_video_review.get('reason', 'no reason')}. Review the generated output before publication."
                )
            elif final_review_decision != "PASS":
                log.warning(
                    "[Content Safety] Final video review=%s; the render is retained and the project continues. "
                    "Check the report before publishing. Reason: %s",
                    final_review_decision, final_video_review.get("reason", "no reason"),
                )

        # 11. Preview delivery: copy source preview into final output, and generate only two new thumbnails.
        state.set_stage("thumbnails")
        remembered_original = Path(str(state.data.get("source_preview_delivery", "") or ""))
        if state.data.get("source_preview_finished") is True and is_valid_image(remembered_original):
            original_preview_delivery = remembered_original
        else:
            original_preview_delivery = copy_original_preview(folder, final_render_dir)
        if original_preview_delivery is not None:
            state.data["source_preview_delivery"] = str(original_preview_delivery)
        elif state.data.get("source_preview_delivery"):
            remembered_preview = Path(str(state.data["source_preview_delivery"]))
            original_preview_delivery = remembered_preview if is_valid_image(remembered_preview) else None
        else:
            state.data["source_preview_delivery"] = ""

        existing_thumbnails = state.data.get("thumbnails", [])
        expected_thumbnail_names = {"Preview_Similar_To_Original.jpg", "Thumbnail_Clickable_Story.jpg"}
        thumbnail_manifest_needs_refresh = (
            not isinstance(existing_thumbnails, list)
            or len(existing_thumbnails) != 2
            or {Path(str(item.get("path", ""))).name for item in existing_thumbnails if isinstance(item, dict)} != expected_thumbnail_names
        )
        if state.data.get("thumbnail_delivery_version") != THUMBNAIL_DELIVERY_VERSION or thumbnail_manifest_needs_refresh:
            # Old thumbnails were generated without the current hero etalon mapping.
            # Remove only the two known generated thumbnail files so they are recreated
            # with the selected hero image; never touch the original preview copy.
            for stale_name in ("Preview_Similar_To_Original.jpg", "Thumbnail_Clickable_Story.jpg"):
                stale_path = final_render_dir / stale_name
                if stale_path.is_file():
                    try:
                        stale_path.unlink()
                        log.info("[Preview] Удаляю старое превью для регенерации с эталоном героя: %s", stale_path.name)
                    except OSError as stale_exc:
                        log.warning("[Preview] Не удалось удалить старое превью %s: %s", stale_path.name, stale_exc)
            source_preview_for_reference = original_preview_delivery
            if source_preview_for_reference is None:
                # If the source project has no named preview, use the first valid scene as a visual reference when available.
                scene_candidates = sorted(state.media_dir.glob("scene_*.jpg"))
                source_preview_for_reference = next(
                    (candidate for candidate in scene_candidates if is_valid_image(candidate)), None
                )
            similar_prompt = get_thumbnail_prompt_2(
                source_preview_for_reference.name if source_preview_for_reference else "the project's selected visual style",
                project_lang,
                story_text=full_transcription,
                title=title,
            )
            if not similar_prompt:
                similar_prompt = apply_visible_text_language_rule(
                    apply_youtube_content_safety_rules(
                        append_selected_visual_style(
                            f"Create a polished new documentary thumbnail inspired by the attached project preview, preserving its subject and visual style: {visual_style_description('thumbnail')}; clean 16:9 composition, textless, no logos.",
                            "thumbnail",
                        ),
                        "thumbnail",
                    ),
                    project_lang,
                )
            clickable_prompt = get_clickable_thumbnail_prompt(full_transcription, title, project_lang)
            hero_etalon_path, hero_etalon_entry = get_project_hero_etalon(analysis)
            similar_reference_images: list[str] = []
            clickable_reference_images: list[str] = []
            if source_preview_for_reference:
                # Put the original preview FIRST so Flow prioritizes its composition/layout.
                similar_reference_images.append(str(source_preview_for_reference))
            if hero_etalon_path:
                similar_prompt = append_thumbnail_hero_identity_instruction(
                    similar_prompt, hero_etalon_entry,
                    "attached image 1 is the source composition template (the original thumbnail when available); attached image 2 is the protagonist identity etalon only. Keep the source layout dominant while making any required story-specific corrections",
                )
                clickable_prompt = append_thumbnail_hero_identity_instruction(
                    clickable_prompt, hero_etalon_entry,
                    "attached image 1 is the protagonist identity etalon; feature this same recognizable protagonist as a major focal subject, with the scene and central conflict taken from the actual story",
                )
                similar_prompt += (
                    " The protagonist etalon is secondary to the original preview's composition. It fixes identity only; it must not replace the original scene layout."
                )
                similar_reference_images.append(str(hero_etalon_path))
                clickable_reference_images.append(str(hero_etalon_path))
            elif source_preview_for_reference:
                similar_prompt += " The attached original thumbnail is the primary composition reference. Keep the same layout and visual idea, adapting only factual details to the story."
            # Stable de-duplication: when the source preview happens to be the hero file,
            # never attach the same image twice.
            similar_reference_images = list(dict.fromkeys(similar_reference_images))
            clickable_reference_images = list(dict.fromkeys(clickable_reference_images))
            state.data["thumbnails"] = [
                {
                    "index": 1,
                    "role": "similar_to_original_preview",
                    "prompt": similar_prompt,
                    "path": str(final_render_dir / "Preview_Similar_To_Original.jpg"),
                    "reference_images": similar_reference_images,
                    "done": False,
                },
                {
                    "index": 2,
                    "role": "clickable_story_thumbnail",
                    "prompt": clickable_prompt,
                    "path": str(final_render_dir / "Thumbnail_Clickable_Story.jpg"),
                    "reference_images": clickable_reference_images,
                    "done": False,
                },
            ]
            state.data["thumbnail_delivery_version"] = THUMBNAIL_DELIVERY_VERSION
            state.save()
        else:
            # Re-copy the original preview on resume; it is a required delivery asset before cleanup.
            if original_preview_delivery is None and state.data.get("source_preview_delivery"):
                remembered_preview = Path(str(state.data["source_preview_delivery"]))
                original_preview_delivery = remembered_preview if is_valid_image(remembered_preview) else None

        for thumb in state.data.get("thumbnails", []):
            path = Path(thumb["path"])
            if is_valid_image(path) and ENABLE_YOUTUBE_CONTENT_SAFETY_REVIEW:
                thumb_review = review_image_with_cache(thumb, path, context=str(state.data.get("final", {}).get("title", "YouTube documentary thumbnail")), cache_field="content_safety_review")
                thumb_decision = str(thumb_review.get("decision", "REVIEW")).upper().strip()
                if thumb_decision == "PASS":
                    thumb["done"] = True
                    state.save()
                    continue
                if thumb_decision != "REJECT":
                    log.warning(
                        "[Content Safety] Thumbnail %s review=%s; keeping the existing file and continuing. Reason: %s",
                        path.name, thumb_decision, thumb_review.get("reason", "no reason"),
                    )
                    thumb["done"] = True
                    thumb["content_safety_review_advisory"] = {
                        "decision": thumb_decision,
                        "reason": str(thumb_review.get("reason", "no reason"))[:1000],
                        "reviewed_at": thumb_review.get("reviewed_at", now_iso()),
                    }
                    state.save()
                    continue
                log.warning("[Content Safety] Thumbnail %s explicitly rejected: %s. Regenerating neutrally.", path.name, thumb_review.get("reason", "no reason"))
                path.unlink(missing_ok=True)
                thumb["done"] = False
                thumb["prompt"] = apply_visible_text_language_rule(
                    apply_youtube_content_safety_rules(
                        append_selected_visual_style(
                            f"{visual_style_description('thumbnail').capitalize()} showing a calm, story-relevant subject in a peaceful setting, clean composition, textless, no sensationalized distress",
                            "thumbnail",
                        ),
                        "thumbnail",
                    ),
                    project_lang,
                )
                state.save()

            if not is_valid_image(path):
                thumbnail_reference_paths = [
                    Path(str(ref)) for ref in thumb.get("reference_images", [])
                    if Path(str(ref)).is_file()
                ]
                thumbnail_generation_prompt = prepare_thumbnail_prompt_for_submission(
                    str(thumb.get("prompt", "")), thumbnail_reference_paths, analysis, project_lang
                )
                await generate_flow_with_failover(
                    manager, playwright, "image",
                    thumbnail_generation_prompt,
                    path,
                    reference_images=thumbnail_reference_paths,
                )
            if not is_valid_image(path):
                raise FlowTechnicalError(f"Thumbnail did not generate as a valid image: {path.name}")

            if ENABLE_YOUTUBE_CONTENT_SAFETY_REVIEW:
                thumb_review = review_image_with_cache(thumb, path, context=str(state.data.get("final", {}).get("title", "YouTube documentary thumbnail")), cache_field="content_safety_review")
                thumb_decision = str(thumb_review.get("decision", "REVIEW")).upper().strip()
                if thumb_decision == "REJECT":
                    path.unlink(missing_ok=True)
                    safe_thumb_core = apply_visible_text_language_rule(
                        apply_youtube_content_safety_rules(
                            append_selected_visual_style(
                                f"{visual_style_description('thumbnail').capitalize()} of a calm, story-relevant subject in a peaceful setting, balanced composition, no visible suffering or injury, textless",
                                "thumbnail",
                            ),
                            "thumbnail",
                        ),
                        project_lang,
                    )
                    safe_thumb_refs = [
                        Path(str(ref)) for ref in thumb.get("reference_images", [])
                        if Path(str(ref)).is_file()
                    ]
                    safe_thumb_prompt = prepare_thumbnail_prompt_for_submission(
                        safe_thumb_core, safe_thumb_refs, analysis, project_lang
                    )
                    await generate_flow_with_failover(
                        manager, playwright, "image", safe_thumb_prompt, path,
                        reference_images=safe_thumb_refs,
                    )
                    if not is_valid_image(path):
                        raise FlowTechnicalError(f"Neutral replacement thumbnail was not generated: {path.name}")
                    thumb_review = review_image_with_cache(thumb, path, context="Neutral, non-graphic documentary thumbnail", cache_field="content_safety_review")
                    replacement_decision = str(thumb_review.get("decision", "REVIEW")).upper().strip()
                    if replacement_decision == "REJECT":
                        path.unlink(missing_ok=True)
                        raise FlowContentSafetyReviewError(
                            f"Neutral thumbnail {path.name} was explicitly rejected: {thumb_review.get('reason', 'no reason')}"
                        )
                    if replacement_decision != "PASS":
                        log.warning(
                            "[Content Safety] Neutral thumbnail %s review=%s; keeping it and continuing. Reason: %s",
                            path.name, replacement_decision, thumb_review.get("reason", "no reason"),
                        )
                        thumb["content_safety_review_advisory"] = {
                            "decision": replacement_decision,
                            "reason": str(thumb_review.get("reason", "no reason"))[:1000],
                            "reviewed_at": thumb_review.get("reviewed_at", now_iso()),
                        }
                elif thumb_decision != "PASS":
                    log.warning(
                        "[Content Safety] Thumbnail %s review=%s; keeping it and continuing. Reason: %s",
                        path.name, thumb_decision, thumb_review.get("reason", "no reason"),
                    )
                    thumb["content_safety_review_advisory"] = {
                        "decision": thumb_decision,
                        "reason": str(thumb_review.get("reason", "no reason"))[:1000],
                        "reviewed_at": thumb_review.get("reviewed_at", now_iso()),
                    }
            thumb["done"] = True
            state.save()

        # Final preview delivery format: every preview/thumbnail in the output folder
        # must be exactly 1280x720. This touches only delivery images; scene JPGs,
        # parallax renders, the montage, intro, and final video are left unchanged.
        preview_delivery_paths: list[Path] = [
            Path(str(item.get("path", "")))
            for item in state.data.get("thumbnails", [])
            if isinstance(item, dict) and str(item.get("path", "")).strip()
        ]
        original_preview_value = str(state.data.get("source_preview_delivery", "") or "").strip()
        if original_preview_value:
            preview_delivery_paths.append(Path(original_preview_value))
        for candidate in final_render_dir.iterdir():
            if is_preview_delivery_asset(candidate):
                preview_delivery_paths.append(candidate)
        seen_preview_paths: set[str] = set()
        for preview_path in preview_delivery_paths:
            try:
                preview_key = str(preview_path.resolve()).casefold()
            except OSError:
                preview_key = str(preview_path).casefold()
            if preview_key in seen_preview_paths:
                continue
            seen_preview_paths.add(preview_key)
            thumb_record = next((item for item in state.data.get("thumbnails", []) if isinstance(item, dict) and str(item.get("path", "")) and Path(str(item.get("path", ""))).resolve() == preview_path.resolve()), None)
            is_original_preview = bool(original_preview_value and preview_path.resolve() == Path(original_preview_value).resolve())
            already_finished = ((is_original_preview and state.data.get("source_preview_finished") is True) or (thumb_record is not None and thumb_record.get("finished") is True))
            if already_finished and preview_image_has_delivery_size(preview_path):
                continue
            if not normalize_preview_image_1280x720(preview_path, apply_finishing=not is_original_preview):
                raise FlowTechnicalError(
                    f"Не удалось подготовить превью 1280x720: {preview_path.name}. Исходная папка сохранена для возобновления."
                )
            if is_original_preview:
                state.data["source_preview_finished"] = True
            elif thumb_record is not None:
                thumb_record["finished"] = True
            state.save()

        # Delivery invariant: exactly two generated thumbnails, plus the copied original preview if one existed.
        expected_thumbnail_paths = [Path(str(item.get("path", ""))) for item in state.data.get("thumbnails", [])]
        if (
            len(expected_thumbnail_paths) != 2
            or not all(is_valid_image(item) and preview_image_has_delivery_size(item) for item in expected_thumbnail_paths)
        ):
            raise FlowTechnicalError("Не готовы оба обязательных превью; исходная папка останется для возобновления.")
        if state.data.get("source_preview_delivery"):
            original_delivery_path = Path(str(state.data["source_preview_delivery"]))
            if (
                not is_valid_image(original_delivery_path)
                or not preview_image_has_delivery_size(original_delivery_path)
            ):
                raise FlowTechnicalError("Оригинальное превью не подготовлено в формате 1280x720; исходники сохранены.")
        legacy_patterns = (
            "Thumb*_Flow_2K.jpg",
            "Thumb1*.jpg", "Thumb2*.jpg", "Thumb3*.jpg",
            "Thumbnail_1.jpg", "Thumbnail_2.jpg", "Thumbnail_3.jpg",
            "Preview_3.jpg", "Preview3.jpg",
        )
        legacy_seen: set[Path] = set()
        for pattern in legacy_patterns:
            for legacy_thumb in final_render_dir.glob(pattern):
                try:
                    resolved_legacy = legacy_thumb.resolve()
                    if resolved_legacy in legacy_seen:
                        continue
                    legacy_seen.add(resolved_legacy)
                    if legacy_thumb.name in {"Preview_Original.jpg", "Thumbnail_Clickable_Story.jpg", "Preview_Similar_To_Original.jpg"}:
                        continue
                    legacy_thumb.unlink(missing_ok=True)
                    log.info("[Preview] Удалено устаревшее лишнее превью: %s", legacy_thumb.name)
                except OSError as exc:
                    log.warning("[Preview] Не удалось убрать устаревшее превью %s: %s", legacy_thumb.name, exc)

        # 12. Complete
        if state.data["final"]["done"] and state.data["final"].get("subtitles_burned"):
            state.set_stage("done", "DONE")
            # User explicitly requested that completion must not open a File Explorer window.
            # Folder view can be configured manually in Windows Explorer; do not launch UI here.
            log.info("[Explorer] Автоматическое открытие финальной папки отключено.")
            if not finalize_project_folder(folder, final_path):
                state.set_stage("cleanup_pending", "CLEANUP_PENDING")
                return False

            log.info("🎉 ПРОЕКТ ГОТОВ: %s", final_path)
            return True

        return False

    except FlowMonetizationReviewError as e:
        state.set_stage("monetization_review", "REVIEW_REQUIRED")
        log.error("🟠 YouTube monetization preflight: %s", e)
        return False
    except FlowSuspiciousActivityError as e:
        # Defensive fallback: normal generation paths quarantine the active profile
        # and continue through failover; this only handles an unexpected escape.
        profile_name = manager.active_profile_name
        if profile_name in manager.states:
            manager.mark_suspicious_activity(profile_name, str(e))
        state.set_stage("flow_suspicious_activity", "PAUSED")
        log.error("⏸️ Flow: профиль временно исключён до следующего запуска: %s", e)
        return False
    except FlowPolicyBlockError as e:
        state.set_stage("flow_blocked", "BLOCKED")
        log.error("🛑 Flow profile требует ручного вмешательства: %s", e)
        return False
    except FlowQuotaError as e:
        # A quota banner can be detected deep inside the browser polling loop,
        # outside the account-selection wrapper. In that case the manager may
        # still show the active profile as WORKING and immediately reuse it for
        # the next unfinished project. Mark that profile as quota-limited here,
        # too, so the queue can move to another available account.
        quota_profile = manager.active_profile_name
        if not quota_profile and manager.active_controller is not None:
            try:
                controller_profile = getattr(manager.active_controller, "profile", None)
                if isinstance(controller_profile, dict):
                    quota_profile = controller_profile.get("name")
            except Exception:
                quota_profile = None
        if quota_profile in manager.states:
            if manager.states[quota_profile].status != "QUOTA_LIMIT":
                manager.mark_quota(quota_profile, str(e))
            log.warning(
                "[Flow Account] %s помечен QUOTA_LIMIT; для текущего проекта будет использован другой профиль, "
                "а при исчерпании всех профилей текущий проект останется на паузе до обнаружения сброса лимита.",
                quota_profile,
            )
        manager.last_project_stop_reason = "flow_quota"
        state.set_stage("flow_quota", "PAUSED")
        log.error("⏸️ Flow quota: %s", e)
        return False
    except Exception as e:
        state.set_stage("error", "ERROR")
        log.error("❌ Проект %s остановлен: %s", folder.name, e)
        log.exception(e)
        return False


# ============================================================================
# ACCOUNT STATUS
# ============================================================================


def print_account_status(manager: FlowAccountManager) -> None:
    print("\n" + "=" * 84)
    print("FLOW ACCOUNT STATUS")
    print("=" * 84)
    for name, state in manager.states.items():
        pause=""
        if state.status == "QUOTA_LIMIT":
            pause=f" qcheck={max(0, int(state.quota_next_probe_at-time.time()))}s"
        elif state.status == "SUSPICIOUS_ACTIVITY":
            pause=" until-next-run"
        elif state.paused_until > time.time():
            pause=f" pause={int(state.paused_until-time.time())}s"
        usage = manager.round_usage.get(name, {})
        print(
            f"{name:16} status={state.status:12} "
            f"images={int(usage.get('images', 0)):2d}/{FLOW_SESSION_IMAGE_LIMIT} "
            f"videos={int(usage.get('videos', 0)):2d}/{FLOW_SESSION_VIDEO_LIMIT} "
            f"total_i={state.images_done:3d} total_v={state.videos_done:3d} "
            f"failures={state.failures:2d}{pause}"
        )
    image_candidates = manager.available_profiles("image")
    video_candidates = manager.available_profiles("video")
    image_only = [p["name"] for p in image_candidates if not manager.can_do(p, "video")]
    print("-" * 84)
    print(f"ДОСТУПНО СЕЙЧАС: images={len(image_candidates)}/{len(manager.profiles)} | videos={len(video_candidates)}/{sum(1 for p in manager.profiles if manager.can_do(p, 'video'))}")
    print("Image-only profiles in current queue: " + (", ".join(image_only) if image_only else "none"))
    print("Профили READY с оставшейся квотой не обязаны запускаться в этом проекте: активный профиль удерживается до 20 изображений или 10 видео.")
    print("=" * 84)


# ============================================================================
# GUI SETTINGS + LIGHTWEIGHT SOURCE-LANGUAGE DETECTION
# ============================================================================


def detect_script_language(text: str) -> str | None:
    """Conservative language guess for menu defaults and mismatch warnings (not translation)."""
    sample = str(text or "")[:20000]
    letters = [char for char in sample if char.isalpha()]
    if not letters:
        return None
    cyrillic_count = sum("\u0400" <= char <= "\u04ff" for char in letters)
    cyrillic_ratio = cyrillic_count / max(1, len(letters))
    if cyrillic_ratio >= 0.18:
        # Polish is normally Latin-script; Cyrillic-heavy inputs are treated as Russian
        # among the three supported UI languages.
        return "ru"

    words = re.findall(r"[a-záéíóúüñąćęłńóśźż]+", sample.casefold())
    if not words:
        return None
    spanish_markers = {
        "el", "la", "de", "que", "y", "en", "los", "las", "una", "un", "es", "por", "con",
        "para", "del", "al", "se", "su", "pero", "como", "más", "fue", "era", "estaba", "cuando",
        "desde", "hasta", "también", "porque", "sobre", "entre", "había", "ella", "él", "ellos",
    }
    polish_markers = {
        "i", "że", "jest", "nie", "w", "na", "do", "się", "z", "to", "jak", "ale", "oraz",
        "był", "była", "przez", "który", "która", "jego", "jej", "dla", "także", "czy", "już",
    }
    spanish_score = sum(1 for word in words if word in spanish_markers)
    polish_score = sum(1 for word in words if word in polish_markers)
    polish_diacritics = sum(sample.casefold().count(char) for char in "ąćęłńóśźż")
    spanish_diacritics = sum(sample.casefold().count(char) for char in "áéíóúüñ")
    if polish_diacritics >= 3 and polish_score >= spanish_score:
        return "pl"
    if spanish_score >= 6 and spanish_score > polish_score + 1:
        return "es"
    if spanish_diacritics >= 2 and spanish_score >= 3:
        return "es"
    return None


def load_gui_settings() -> dict[str, Any]:
    try:
        value = json.loads(GUI_SETTINGS_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def save_gui_settings(settings: dict[str, Any]) -> None:
    try:
        atomic_write_json(GUI_SETTINGS_FILE, settings)
    except Exception as exc:
        log.warning("[GUI Settings] Не удалось сохранить выбор меню: %s", exc)


def infer_initial_language_for_channel(channel_dir: Path) -> str | None:
    try:
        projects = sorted(
            [item for item in channel_dir.iterdir() if item.is_dir() and not is_reserved_channel_asset_folder(item) and not is_completed_project_folder(item)],
            key=lambda item: item.stat().st_mtime,
        )
        for project in projects:
            text_files = sorted(project.glob("*.txt"))
            if not text_files:
                continue
            body = extract_script_body(text_files[0].read_text(encoding="utf-8", errors="ignore"))
            inferred = detect_script_language(body)
            if inferred:
                log.info("[Narration Language] First-run menu default inferred as %s from %s.", inferred.upper(), text_files[0].name)
                return inferred
    except Exception as exc:
        log.debug("[Narration Language] Could not infer source language for menu defaults: %s", exc)
    return None


# ============================================================================
# GUI & ENTRY POINT
# ============================================================================


def run_gui() -> None:
    import tkinter as tk
    from tkinter import ttk, messagebox

    if not INPUT_ROOT.exists():
        messagebox.showerror("Ошибка", f"Не найдена папка {INPUT_ROOT}")
        return

    channels = [
        p.name for p in INPUT_ROOT.iterdir()
        if p.is_dir() and not p.name.endswith("_DONE")
    ]
    saved_settings = load_gui_settings()

    root = tk.Tk()
    root.title("Flow Pipeline — Hybrid Intro Video + Images")
    root.geometry("610x630")
    root.resizable(False, False)

    ttk.Label(root, text="Рабочий канал:", font=("Arial", 10, "bold")).pack(pady=(14, 3))

    combo_channel = ttk.Combobox(root, values=channels, width=50, state="readonly")
    saved_channel = str(saved_settings.get("channel", ""))
    if saved_channel in channels:
        combo_channel.current(channels.index(saved_channel))
    elif channels:
        combo_channel.current(0)
    combo_channel.pack()

    ttk.Label(root, text="Язык озвучки / субтитров:", font=("Arial", 10, "bold")).pack(pady=(12, 3))

    language_map = {
        "Русский (RU)": "ru",
        "Польский (PL)": "pl",
        "Испанский (ES)": "es",
    }

    combo_lang = ttk.Combobox(root, values=list(language_map.keys()), width=50, state="readonly")
    lang_label_by_code = {code: label for label, code in language_map.items()}
    saved_lang = str(saved_settings.get("narration_language", "")).lower()
    if saved_lang in lang_label_by_code:
        combo_lang.current(list(language_map.keys()).index(lang_label_by_code[saved_lang]))
    else:
        channel_for_guess = INPUT_ROOT / combo_channel.get().strip() if combo_channel.get().strip() else None
        inferred_lang = infer_initial_language_for_channel(channel_for_guess) if channel_for_guess else None
        initial_label = lang_label_by_code.get(inferred_lang or "ru", "Русский (RU)")
        combo_lang.current(list(language_map.keys()).index(initial_label))
    combo_lang.pack()
    # On the very first launch, infer a default from the selected channel's source text.
    # After the user selects a language or saves settings, preserve that explicit choice.
    language_was_saved = saved_lang in lang_label_by_code
    language_choice_state = {"explicit": language_was_saved}
    combo_lang.bind("<<ComboboxSelected>>", lambda _event: language_choice_state.__setitem__("explicit", True))

    ttk.Label(root, text="ИИ для текста / раскадровки:", font=("Arial", 10, "bold")).pack(pady=(12, 3))
    text_provider_map = {
        "Gemini (по умолчанию)": "gemini",
        "Groq — openai/gpt-oss-120b": "groq",
    }
    text_provider_label_by_id = {provider_id: label for label, provider_id in text_provider_map.items()}
    combo_text_provider = ttk.Combobox(
        root, values=list(text_provider_map.keys()), width=50, state="readonly"
    )
    saved_text_provider = normalize_text_provider(str(saved_settings.get("text_provider", "gemini")))
    combo_text_provider.current(list(text_provider_map.keys()).index(text_provider_label_by_id[saved_text_provider]))
    combo_text_provider.pack()
    ttk.Label(
        root,
        text=(
            "Groq работает только с текстом/сценами. Превью — Gemini; картинки и видео — Flow; озвучка — Gemini TTS. "
            "Модель Groq: openai/gpt-oss-120b."
        ),
        justify="center", wraplength=560,
    ).pack(pady=(3, 2))

    def maybe_infer_language_for_channel(_event=None):
        if language_choice_state["explicit"]:
            return
        selected_channel = combo_channel.get().strip()
        if not selected_channel:
            return
        inferred = infer_initial_language_for_channel(INPUT_ROOT / selected_channel)
        label = lang_label_by_code.get(inferred or "ru", "Русский (RU)")
        combo_lang.current(list(language_map.keys()).index(label))

    combo_channel.bind("<<ComboboxSelected>>", maybe_infer_language_for_channel)

    ttk.Label(root, text="Старт Flow профиля:", font=("Arial", 10, "bold")).pack(pady=(12, 3))

    profile_names = [p["name"] for p in unique_enabled_flow_profiles(FLOW_PROFILES, warn_duplicates=False)]
    combo_profile = ttk.Combobox(root, values=profile_names, width=50, state="readonly")
    saved_profile = str(saved_settings.get("preferred_profile", ""))
    if saved_profile in profile_names:
        combo_profile.current(profile_names.index(saved_profile))
    elif profile_names:
        combo_profile.current(0)
    combo_profile.pack()

    ttk.Label(root, text="Визуальный стиль сцен:", font=("Arial", 10, "bold")).pack(pady=(12, 3))
    style_label_to_id = {label: style_id for style_id, label in VISUAL_STYLE_OPTIONS.items()}
    combo_style = ttk.Combobox(root, values=list(style_label_to_id.keys()), width=50, state="readonly")
    saved_style = normalize_visual_style(str(saved_settings.get("visual_style_id", "realistic")))
    styles_by_channel = saved_settings.get("visual_styles_by_channel", {})
    if not isinstance(styles_by_channel, dict):
        styles_by_channel = {}

    def _default_style_for_channel(channel: str) -> str:
        if channel in styles_by_channel:
            return normalize_visual_style(str(styles_by_channel[channel]))
        # First-time channels with a user-supplied hero etalon default to the new
        # YouTube storytime explainer style; other channels keep the prior/global choice.
        etalon_dir = INPUT_ROOT / channel / CHANNEL_HERO_REFERENCE_DIR_NAME
        if etalon_dir.is_dir():
            try:
                if any(p.is_file() and p.suffix.casefold() in SUPPORTED_REFERENCE_IMAGE_SUFFIXES and is_valid_image(p) for p in etalon_dir.iterdir()):
                    return "comic_explainer"
            except Exception:
                pass
        return saved_style

    def _apply_saved_style_for_selected_channel(_event=None):
        selected_channel = combo_channel.get().strip()
        chosen_style = _default_style_for_channel(selected_channel) if selected_channel else saved_style
        label = VISUAL_STYLE_OPTIONS.get(chosen_style, VISUAL_STYLE_OPTIONS["realistic"])
        combo_style.current(list(style_label_to_id.keys()).index(label))

    _apply_saved_style_for_selected_channel()
    combo_style.pack()
    combo_channel.bind("<<ComboboxSelected>>", _apply_saved_style_for_selected_channel, add="+")

    intro_default = saved_settings.get("intro_enabled", ENABLE_FLOW_INTRO_DEFAULT)
    intro_var = tk.BooleanVar(value=bool(intro_default))
    ttk.Checkbutton(
        root,
        text="Генерировать Flow intro (полная длительность, без обрезки до 20с)",
        variable=intro_var,
    ).pack(pady=12)

    ttk.Label(
        root,
        text=(
            f"Видео: профили с supports_video=True | максимум {FLOW_SESSION_VIDEO_LIMIT} видео за сессию\n"
            f"Изображения: все активные профили | максимум {FLOW_SESSION_IMAGE_LIMIT} изображений за сессию"
        ),
        justify="center",
    ).pack(pady=6)

    def start() -> None:
        channel = combo_channel.get().strip()
        if not channel:
            messagebox.showwarning("Внимание", "Выберите канал")
            return

        lang = language_map.get(combo_lang.get(), "ru")
        preferred = combo_profile.get().strip() or None
        intro_enabled = intro_var.get()
        visual_style_id = style_label_to_id.get(combo_style.get(), "realistic")
        text_provider = text_provider_map.get(combo_text_provider.get(), "gemini")
        if text_provider == "groq" and not GROQ_API_KEYS:
            messagebox.showerror(
                "Groq API-ключи не найдены",
                "Не удалось загрузить ни одного ключа из groq_keys.txt.\n\n"
                f"Проверь файл: {GROQ_KEYS_FILE}\n"
                "Также можно положить groq_keys.txt рядом со скриптом.",
            )
            return
        updated_styles_by_channel = dict(styles_by_channel)
        updated_styles_by_channel[channel] = visual_style_id
        save_gui_settings({
            **saved_settings,
            "channel": channel,
            "narration_language": lang,
            "preferred_profile": preferred or "",
            "visual_style_id": visual_style_id,
            "visual_styles_by_channel": updated_styles_by_channel,
            "intro_enabled": intro_enabled,
            "text_provider": text_provider,
            "saved_at": now_iso(),
        })
        log.info(
            "[GUI Settings] Saved: channel=%s narration_language=%s visual_style=%s intro=%s text_provider=%s",
            channel, lang.upper(), visual_style_id, intro_enabled, text_provider,
        )
        root.destroy()

        asyncio.run(run_pipeline(channel, lang, intro_enabled, preferred, visual_style_id, text_provider))

    ttk.Button(root, text="🚀 Запустить / Продолжить", command=start).pack(
        pady=18, ipadx=25, ipady=6
    )

    root.mainloop()


def _normalized_project_path(path_value: str | Path | None) -> str:
    """Normalize project paths so the persisted active-project marker survives restarts."""
    if not path_value:
        return ""
    try:
        return os.path.normcase(os.path.abspath(str(path_value)))
    except Exception:
        return os.path.normcase(str(path_value))


def load_active_project_path() -> str:
    try:
        document = json.loads(ACTIVE_PROJECT_MARKER_FILE.read_text(encoding="utf-8"))
        if isinstance(document, dict):
            return str(document.get("project_path", "") or "")
    except Exception:
        pass
    return ""


def save_active_project(folder: Path) -> None:
    """Persist the in-flight project before doing work; Ctrl+C intentionally leaves it."""
    try:
        atomic_write_json(ACTIVE_PROJECT_MARKER_FILE, {
            "project_path": str(folder.resolve()),
            "project_name": folder.name,
            "channel_path": str(folder.parent.resolve()),
            "updated_at": now_iso(),
            "note": "Resume this project first after an interrupted run.",
        })
    except Exception as exc:
        log.warning("[Resume Priority] Не удалось сохранить активный проект: %s", exc)


def clear_active_project(folder: Path) -> None:
    """Clear the marker only when it points at the project that just finished."""
    try:
        recorded = load_active_project_path()
        if recorded and _normalized_project_path(recorded) == _normalized_project_path(folder):
            ACTIVE_PROJECT_MARKER_FILE.unlink(missing_ok=True)
    except Exception as exc:
        log.warning("[Resume Priority] Не удалось очистить маркер завершённого проекта: %s", exc)


def project_resume_progress(folder: Path) -> tuple[int, str]:
    """Return an evidence-based score for resumable work already present on disk.

    Valid/recognizable media assets outrank a newly initialized project. This
    deliberately checks both state.json and the actual media directory so files
    survive a crash that happened before the latest checkpoint was saved.
    """
    score = 0
    reasons: list[str] = []
    media_dir = folder / MEDIA_DIR_NAME
    state_path = folder / STATE_DIR_NAME / "state.json"
    data: dict[str, Any] = {}
    if state_path.is_file():
        try:
            parsed = json.loads(state_path.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}

    def count_files(pattern: str, minimum_bytes: int = 4096) -> int:
        try:
            return sum(1 for item in media_dir.glob(pattern) if item.is_file() and item.stat().st_size >= minimum_bytes)
        except Exception:
            return 0

    scene_videos = count_files("scene_*.mp4")
    scene_images = count_files("scene_*.jpg", 2048)
    intro_raw = count_files("intro_raw_*.mp4")
    intro_clips = count_files("intro_clip_*.mp4")
    intro_outputs = sum(1 for pattern in ("intro_concat_full.mp4", "intro_64s.mp4") if count_files(pattern))
    final_outputs = count_files("final*.mp4") + count_files("video_final*.mp4")

    if scene_videos:
        score += scene_videos * 100
        reasons.append(f"scene videos={scene_videos}")
    if scene_images:
        score += scene_images * 25
        reasons.append(f"scene images={scene_images}")
    if intro_clips:
        score += intro_clips * 80
        reasons.append(f"intro clips={intro_clips}")
    if intro_raw:
        score += intro_raw * 35
        reasons.append(f"raw intro={intro_raw}")
    if intro_outputs:
        score += intro_outputs * 90
        reasons.append("intro assembled")
    if final_outputs:
        score += final_outputs * 300
        reasons.append(f"final media={final_outputs}")

    audio = data.get("audio", {}) if isinstance(data.get("audio"), dict) else {}
    whisper = data.get("whisper", {}) if isinstance(data.get("whisper"), dict) else {}
    analysis = data.get("analysis", {}) if isinstance(data.get("analysis"), dict) else {}
    intro = data.get("intro", {}) if isinstance(data.get("intro"), dict) else {}
    montage = data.get("montage", {}) if isinstance(data.get("montage"), dict) else {}
    final = data.get("final", {}) if isinstance(data.get("final"), dict) else {}

    if audio.get("done"):
        score += 8
        reasons.append("audio ready")
    elif audio.get("path"):
        try:
            audio_path = Path(str(audio.get("path")))
            if audio_path.is_file() and audio_path.stat().st_size > 100_000:
                score += 8
                reasons.append("audio file")
        except Exception:
            pass
    if whisper.get("done"):
        score += 8
        reasons.append("transcript ready")
    if analysis.get("done"):
        score += 6
        reasons.append("analysis ready")
    if intro.get("done"):
        score += 90
        reasons.append("intro done")
    if montage.get("done"):
        score += 120
        reasons.append("montage done")
    if final.get("done"):
        score += 300
        reasons.append("final checkpoint")

    records = data.get("main_clips", [])
    if isinstance(records, list):
        checkpoint_done = sum(1 for item in records if isinstance(item, dict) and item.get("done"))
        # Disk counts are already weighted; this checkpoint-only bonus recovers
        # priority when an asset was moved/renamed but the record remains valid.
        if checkpoint_done > scene_videos:
            score += (checkpoint_done - scene_videos) * 60
            reasons.append(f"checkpoint scene records={checkpoint_done}")

    if not reasons:
        stage = str(data.get("stage", "") or "")
        if stage and stage.lower() not in {"init", "new"}:
            score += 1
            reasons.append(f"stage={stage}")
    return score, ", ".join(reasons) if reasons else "no generated assets/checkpoint"


def project_resume_sort_key(folder: Path, active_path: str) -> tuple[int, int, int, float, str]:
    """Sort: explicitly interrupted project, any partial project, most progress, newest update."""
    score, _reason = project_resume_progress(folder)
    is_active = int(bool(active_path) and _normalized_project_path(folder) == _normalized_project_path(active_path))
    # An active marker with no evidence of work still receives slight preference,
    # but generated media/checkpoint evidence remains the main discriminator.
    active_with_work = int(bool(is_active and score > 0))
    updated_ts = 0.0
    try:
        if (folder / STATE_DIR_NAME / "state.json").is_file():
            doc = json.loads((folder / STATE_DIR_NAME / "state.json").read_text(encoding="utf-8", errors="ignore"))
            stamp = str(doc.get("updated_at", "") or "") if isinstance(doc, dict) else ""
            if stamp:
                try:
                    updated_ts = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                except Exception:
                    updated_ts = 0.0
        if not updated_ts:
            updated_ts = folder.stat().st_mtime
    except Exception:
        pass
    return active_with_work, int(score > 0), score, updated_ts, folder.name.casefold()


def cleanup_internal_character_delivery_artifacts(output_channel_dir: Path) -> None:
    """Remove internal character-reference artifacts from existing final-output folders.

    This only touches the dedicated OUTPUT_ROOT/channel/project delivery tree, never source projects.
    """
    if not output_channel_dir.is_dir():
        return
    for delivery_dir in output_channel_dir.iterdir():
        if not delivery_dir.is_dir():
            continue
        # Restrict cleanup to actual delivery folders containing a final video.
        if not any(p.is_file() and p.suffix.casefold() == ".mp4" for p in delivery_dir.iterdir()):
            continue
        stale_bible = delivery_dir / "character_bible.json"
        if stale_bible.is_file():
            try:
                stale_bible.unlink()
                log.info("[Delivery Cleanup] Удалён внутренний файл из старой финальной папки: %s", stale_bible)
            except OSError as exc:
                log.warning("[Delivery Cleanup] Не удалось удалить старый character_bible.json (%s): %s", stale_bible, exc)
        stale_refs = delivery_dir / CHARACTER_REFERENCE_DIR_NAME
        if stale_refs.is_dir():
            try:
                shutil.rmtree(stale_refs)
                log.info("[Delivery Cleanup] Удалена внутренняя папка эталонов из старой финальной папки: %s", stale_refs)
            except OSError as exc:
                log.warning("[Delivery Cleanup] Не удалось удалить старую папку character_references (%s): %s", stale_refs, exc)


async def run_pipeline(
    channel_name: str,
    project_lang: str,
    intro_enabled: bool,
    preferred_profile: str | None = None,
    visual_style_id: str = "realistic",
    text_provider: str = "gemini",
) -> None:
    global ACTIVE_TTS_CHANNEL_NAME
    ACTIVE_TTS_CHANNEL_NAME = str(channel_name or "").strip()
    log.info("[TTS Voice] Канал=%s | активный голос=%s", ACTIVE_TTS_CHANNEL_NAME or "(default)", get_channel_tts_voice(ACTIVE_TTS_CHANNEL_NAME))
    selected_text_provider = set_active_text_provider(text_provider)
    if selected_text_provider == "groq" and not GROQ_API_KEYS:
        raise RuntimeError(f"Groq выбран, но API-ключи не загружены. Проверь {GROQ_KEYS_FILE} или groq_keys.txt рядом со скриптом.")
    log.info("[Text Provider] Активен %s | model=%s", selected_text_provider.upper(), GROQ_TEXT_MODEL if selected_text_provider == "groq" else "Gemini model router")
    channel_dir=INPUT_ROOT/channel_name
    if not channel_dir.exists():
        raise RuntimeError(f"Канал не найден: {channel_dir}")

    # Clean obsolete internal character assets from already delivered folders too.
    cleanup_internal_character_delivery_artifacts(OUTPUT_ROOT / channel_name)

    log.info(
        "[Narration Language] Selected output language=%s (%s). The same language is enforced for captions and readable in-scene text.",
        str(project_lang).upper(), narration_language_name(project_lang),
    )

    if not ENABLE_YOUTUBE_CONTENT_SAFETY_REVIEW:
        log.warning(
            "[Content Safety] Remote image/video review is DISABLED after repeated API 503/timeouts. "
            "Safe prompts, Flow/provider safety filters, technical image QA, and text-based monetization preflight "
            "remain enabled. Manually inspect the final video and thumbnails before publishing."
        )

    # Keep FLOW_PROFILES in its configured order. The GUI-selected profile is
    # only a one-time first-use preference; FlowAccountManager continues from
    # that profile's actual successor without moving it to the front of the list.
    profiles = unique_enabled_flow_profiles([dict(p) for p in FLOW_PROFILES], warn_duplicates=True)
    if preferred_profile:
        preferred_profile = str(preferred_profile).strip() if preferred_profile else None
        matched = next((p for p in profiles if str(p["name"]).strip() == preferred_profile and p.get("enabled")), None)
        if matched is None:
            log.warning(
                "[Flow] Selected startup profile %s is not enabled/available; using configured order.",
                preferred_profile,
            )
        else:
            configured_names = [p["name"] for p in profiles if p.get("enabled")]
            start_pos = configured_names.index(matched["name"])
            successor = configured_names[(start_pos + 1) % len(configured_names)]
            log.info(
                "[Flow] GUI startup profile=%s will be used first once; configured round-robin order is preserved; "
                "its next successor is %s. Full order: %s",
                preferred_profile, successor, " -> ".join(configured_names),
            )

    manager=FlowAccountManager(profiles, startup_profile=preferred_profile)
    print_account_status(manager)
    log.info(
        "[Flow Rotation] Профиль удерживается до исчерпания квоты текущего типа: "
        "%s видео и %s изображений на профиль. Квоты независимы; после успешного медиа "
        "профиль не переключается. Forced-rotation=%s.",
        FLOW_SESSION_VIDEO_LIMIT, FLOW_SESSION_IMAGE_LIMIT, FLOW_ROTATE_AFTER_EACH_SUCCESS,
    )

    # Completion is controlled by the folder suffix: *_DONE is intentionally
    # skipped. If the user wants to repair a finished project, they can manually
    # remove that suffix and delete unwanted/missing media. Do not auto-rename
    # completed folders here; that made the scanner unexpectedly reopen projects.

    active_project_path = load_active_project_path()
    project_folders = [
        p for p in channel_dir.iterdir()
        if p.is_dir() and not is_reserved_channel_asset_folder(p) and not is_completed_project_folder(p)
    ]
    project_folders.sort(
        key=lambda p: project_resume_sort_key(p, active_project_path),
        reverse=True,
    )
    if active_project_path and not any(
        _normalized_project_path(p) == _normalized_project_path(active_project_path)
        for p in project_folders
    ):
        # The marker may point to a project that completed/renamed to *_DONE.
        try:
            ACTIVE_PROJECT_MARKER_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        active_project_path = ""
    log.info("[Resume Priority] Очередь: сначала активный прерванный проект, затем проекты с существующими медиа/чекпоинтами, затем новые.")
    for queued_folder in project_folders:
        progress_score, progress_reason = project_resume_progress(queued_folder)
        active_tag = " [ACTIVE RESUME]" if active_project_path and _normalized_project_path(queued_folder) == _normalized_project_path(active_project_path) else ""
        log.info("[Resume Priority] %s%s | score=%s | %s", queued_folder.name, active_tag, progress_score, progress_reason)
    if not project_folders:
        log.info("Нет незавершённых проектов в %s", channel_dir)
        return

    try:
        async with async_playwright() as playwright:
            for folder in project_folders:
                # Persist before any work. If Ctrl+C/terminal close happens, next launch
                # puts this same project first instead of restarting at a fresh folder.
                save_active_project(folder)
                print("\n"+"="*72)
                print("PROJECT:",folder.name)
                print("="*72)

                while True:
                    ok = await process_project(
                        folder, channel_name, project_lang, intro_enabled, manager, playwright, visual_style_id
                    )
                    print_account_status(manager)

                    if ok:
                        log.info("✅ %s: DONE", folder.name)
                        clear_active_project(folder)
                        break

                    if manager.last_project_stop_reason == "flow_quota":
                        # Quota is a profile-level pause, not a reason to abandon this
                        # project. Wait for Flow itself to clear at least one browser,
                        # then re-enter this same folder from its checkpoint.
                        quota_kind = manager.last_quota_kind or "image"
                        recovered = await manager.wait_for_quota_reset(playwright, quota_kind)
                        if recovered:
                            log.info(
                                "🔁 [Flow Quota Wait] Доступен профиль после перепроверки лимита; "
                                "возобновляю ТОТ ЖЕ проект: %s",
                                folder.name,
                            )
                            continue
                        log.error(
                            "[Flow Quota Wait] Не осталось профилей для %s. Проект не помечен DONE и не переключается на другой.",
                            folder.name,
                        )
                        break

                    log.warning(
                        "⏸️ %s: проект остановлен по другой причине; checkpoint сохранён. "
                        "Перейду к следующему проекту, но квота Flow сама по себе не вызывает такой переход.",
                        folder.name,
                    )
                    break
    finally:
        if manager.active_controller is not None:
            try:
                await manager.active_controller.disconnect(close_chrome=True)
            except Exception:
                pass
            manager.active_controller=None
            manager.active_profile_name=None


if __name__ == "__main__":
    # Apply Chrome policies before the GUI starts, including when the controller
    # later attaches to an already-running profile process. PromotionsEnabled is
    # dynamically refreshed by Chrome; newly launched profiles also reapply it.
    configure_chrome_suppress_prompts_policy()
    try:
        run_gui()
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
    except Exception as exc:
        log.exception(exc)
        raise
