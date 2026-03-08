"""Video quality filter using LLM evaluation."""

import json
from llm.client import OpenRouterClient
from llm.prompts import VIDEO_FILTER_PROMPT


class VideoFilter:
    """Filter YouTube videos by quality using LLM scoring."""

    def __init__(self, client: OpenRouterClient | None = None):
        self._client = client or OpenRouterClient()

    def _score_video(self, video: dict) -> dict:
        """Score a single video. Returns video dict augmented with quality_score and is_clickbait."""
        prompt = VIDEO_FILTER_PROMPT.format(
            title=video.get("title", ""),
            description=video.get("description", ""),
            view_count=video.get("view_count", 0),
        )
        try:
            raw = self._client.chat(
                [{"role": "user", "content": prompt}],
                model="fast",
                max_tokens=256,
            )
            # Strip markdown fences if present
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            result = json.loads(raw)
            return {
                **video,
                "quality_score": int(result.get("quality_score", 5)),
                "is_clickbait": bool(result.get("is_clickbait", False)),
                "filter_reason": result.get("reason", ""),
            }
        except (json.JSONDecodeError, KeyError, ValueError):
            # Default to mid-score on parse failure
            return {**video, "quality_score": 5, "is_clickbait": False, "filter_reason": "parse_error"}

    def filter_videos(self, videos: list[dict]) -> list[dict]:
        """Filter and rank videos by quality.

        Args:
            videos: List of dicts with keys: title, description, view_count, url.

        Returns:
            Filtered list (non-clickbait, quality_score >= 6) sorted by quality_score descending.
        """
        scored = [self._score_video(v) for v in videos]
        filtered = [v for v in scored if not v["is_clickbait"] and v["quality_score"] >= 6]
        return sorted(filtered, key=lambda v: v["quality_score"], reverse=True)
