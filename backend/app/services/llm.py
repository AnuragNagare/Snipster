"""Gemini LLM service — clip selection and caption generation."""

import json
import re
from typing import Any

import google.generativeai as genai
from google.api_core.exceptions import NotFound as GeminiNotFound

from app.core.config import settings
from app.core.logging import logger


class LLMService:
    _model = None
    _model_name = None

    def reset_model(self):
        """Clear the cached model so next call re-selects a working one."""
        self._model = None
        self._model_name = None

    def _get_model(self):
        if self._model is None:
            genai.configure(api_key=settings.GEMINI_API_KEY)

            # Candidates in priority order — newest/most available first.
            # gemini-3.6-flash is what Google explicitly recommends for new API keys.
            candidates = [
                "gemini-3.6-flash",
                "gemini-3.7-flash",
                "gemini-3.8-flash",
                "gemini-3.5-flash",
                "gemini-flash-latest",
                "gemini-2.0-flash",
                "gemini-1.5-flash-latest",
                "gemini-1.5-flash",
            ]

            try:
                available = {
                    m.name.replace("models/", "")
                    for m in genai.list_models()
                    if "generateContent" in m.supported_generation_methods
                }
                logger.info(f"[LLM] Available Gemini models: {sorted(available)}")

                model_name = next(
                    (c for c in candidates if c in available),
                    next(iter(available), "gemini-3.6-flash"),
                )
            except Exception as e:
                logger.warning(f"[LLM] Could not list models ({e}), defaulting to gemini-3.6-flash")
                model_name = "gemini-3.6-flash"

            logger.info(f"[LLM] Using Gemini model: {model_name}")
            self._model_name = model_name
            self._model = genai.GenerativeModel(model_name)
        return self._model

    def _full_text_from_transcript(self, transcript: list[dict], start: float = 0, end: float = float("inf")) -> str:
        """Reconstruct text from word-level transcript within a time range."""
        words = [w["text"] for w in transcript if start <= w["start"] <= end]
        return " ".join(words)

    def select_clips(self, transcript: list[dict]) -> list[dict[str, Any]]:
        """
        Ask Gemini to identify 3–5 engaging clip segments from a transcript.

        Returns: [{"start": float, "end": float, "reason": str}, ...]
        """
        full_text = self._full_text_from_transcript(transcript)
        # Build a timestamped transcript for the LLM
        ts_lines = [f"[{w['start']:.1f}s] {w['text']}" for w in transcript[::3]]  # sample every 3rd word
        ts_text = "\n".join(ts_lines[:600])  # limit context length

        prompt = f"""You are a social media content expert. Analyze this video transcript and identify exactly 3-5 of the most engaging, self-contained moments that would work well as a 15-60 second YouTube Short or Instagram Reel.

Each clip should:
- Be self-contained (makes sense without context)
- Be engaging from the very first second
- Be between 15 and 60 seconds long
- Feature a clear point, insight, story, or emotional moment

Transcript (with timestamps):
{ts_text}

Respond ONLY with a JSON array, no other text:
[
  {{"start": <seconds>, "end": <seconds>, "reason": "<why this is a great clip>"}},
  ...
]"""

        model = self._get_model()
        logger.info("[LLM] Requesting clip selection from Gemini...")
        try:
            response = model.generate_content(prompt)
        except GeminiNotFound as e:
            # Model no longer available — bust cache so next retry re-selects.
            logger.warning(f"[LLM] Model {self._model_name!r} unavailable ({e}), resetting model cache.")
            self.reset_model()
            raise

        # Parse JSON from response
        text = response.text.strip()
        json_match = re.search(r"\[.*\]", text, re.DOTALL)
        if not json_match:
            raise ValueError(f"LLM did not return valid JSON: {text[:200]}")

        clips = json.loads(json_match.group())
        logger.info(f"[LLM] Selected {len(clips)} clips")
        return clips[:5]  # cap at 5

    def generate_caption(
        self,
        transcript: list[dict],
        start: float,
        end: float,
        reason: str,
    ) -> dict[str, str]:
        """
        Generate an engaging caption + hashtags for a clip.

        Returns: {"caption": str, "hashtags": str}
        """
        clip_text = self._full_text_from_transcript(transcript, start, end)

        prompt = f"""You are a social media copywriter. Write an engaging caption and relevant hashtags for a short video clip.

Clip content: {clip_text[:1000]}
Why this clip was selected: {reason}

Requirements:
- Caption: 1-3 punchy sentences, hooks the viewer immediately, no emojis needed
- Hashtags: 5-10 relevant hashtags as a single space-separated string

Respond ONLY with JSON:
{{"caption": "<caption text>", "hashtags": "<#tag1 #tag2 ...>"}}"""

        model = self._get_model()
        logger.info("[LLM] Generating caption...")
        try:
            response = model.generate_content(prompt)
        except GeminiNotFound as e:
            logger.warning(f"[LLM] Model {self._model_name!r} unavailable ({e}), resetting model cache.")
            self.reset_model()
            raise

        text = response.text.strip()
        json_match = re.search(r"\{.*\}", text, re.DOTALL)
        if not json_match:
            raise ValueError(f"LLM did not return valid JSON: {text[:200]}")

        result = json.loads(json_match.group())
        logger.info("[LLM] Caption generated")
        return result


llm_service = LLMService()
