"""Hindi / language-aware behaviour: prompts, subtitle font, transcription result, migration chain."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.config import settings
from app.services import llm as llm_module
from app.services import transcription as transcription_module
from app.services.llm import LLMService, _language_rule
from app.services.renderer import renderer_service, subtitle_style


# ─── Gemini prompts ──────────────────────────────────────────────────────────

def test_language_rule_hindi_asks_for_devanagari():
    assert "Devanagari" in _language_rule("hi")


def test_language_rule_english_and_unknown():
    assert "English" in _language_rule("en")
    assert _language_rule(None) == ""
    assert _language_rule("") == ""


def _llm_with_reply(reply: str) -> tuple[LLMService, MagicMock]:
    service = LLMService()
    model = MagicMock()
    model.generate_content.return_value = SimpleNamespace(text=reply)
    service._model = model
    return service, model


def test_caption_prompt_carries_the_language():
    service, model = _llm_with_reply('{"caption": "नमस्ते", "hashtags": "#हिंदी"}')
    words = [{"start": 1.0, "end": 1.5, "text": "नमस्ते"}]
    result = service.generate_caption(words, 0, 5, "greeting", language="hi")
    prompt = model.generate_content.call_args.args[0]
    assert "Hindi" in prompt and "Devanagari" in prompt
    assert result["caption"] == "नमस्ते"


def test_select_clips_prompt_carries_the_language():
    service, model = _llm_with_reply('[{"start": 1, "end": 20, "reason": "good"}]')
    words = [{"start": float(i), "end": i + 0.5, "text": "शब्द"} for i in range(30)]
    service.select_clips(words, language="hi")
    assert "Hindi" in model.generate_content.call_args.args[0]


def test_prompts_unchanged_without_language():
    service, model = _llm_with_reply('{"caption": "x", "hashtags": "#x"}')
    service.generate_caption([{"start": 0, "end": 1, "text": "hi"}], 0, 5, "r")
    assert "The video is in" not in model.generate_content.call_args.args[0]


# ─── Subtitle font ───────────────────────────────────────────────────────────

def test_subtitle_style_uses_devanagari_font_for_hindi_only():
    assert f"FontName={settings.SUBTITLE_FONT_DEVANAGARI}" in subtitle_style("hi")
    assert "FontName" not in subtitle_style("en")
    assert "FontName" not in subtitle_style(None)


def test_ffmpeg_command_gets_the_hindi_font():
    cmd = renderer_service._build_cmd("in.mp4", "out.mp4", 0, 10, "subs.srt", "libx264", "hi")
    vf = cmd[cmd.index("-vf") + 1]
    assert settings.SUBTITLE_FONT_DEVANAGARI in vf
    cmd_en = renderer_service._build_cmd("in.mp4", "out.mp4", 0, 10, "subs.srt", "libx264", "en")
    assert "FontName" not in cmd_en[cmd_en.index("-vf") + 1]


# ─── Transcription result ────────────────────────────────────────────────────

def _fake_whisper(language: str):
    word = SimpleNamespace(start=0.0, end=0.5, word=" नमस्ते ")
    segment = SimpleNamespace(words=[word])
    info = SimpleNamespace(language=language, language_probability=0.99)
    model = MagicMock()
    model.transcribe.return_value = (iter([segment]), info)
    return model


def test_transcribe_returns_words_and_detected_language(monkeypatch):
    service = transcription_module.TranscriptionService()
    service._model = _fake_whisper("hi")
    monkeypatch.setattr(service, "_extract_audio", lambda _p: None)
    monkeypatch.setattr(settings, "WHISPER_LANGUAGE", "")
    words, language = service.transcribe("clip.mp4")
    assert language == "hi"
    assert words == [{"start": 0.0, "end": 0.5, "text": "नमस्ते"}]
    assert "language" not in service._model.transcribe.call_args.kwargs  # auto-detect


def test_transcribe_forces_language_when_configured(monkeypatch):
    service = transcription_module.TranscriptionService()
    service._model = _fake_whisper("hi")
    monkeypatch.setattr(service, "_extract_audio", lambda _p: None)
    monkeypatch.setattr(settings, "WHISPER_LANGUAGE", "HI")
    service.transcribe("clip.mp4")
    assert service._model.transcribe.call_args.kwargs["language"] == "hi"


# ─── Migration chain ─────────────────────────────────────────────────────────

def test_migrations_form_one_chain_including_the_language_column():
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
    assert script.get_revision("8c1f2a9d4e7b").down_revision == "54b0d5b7980b"
