"""Unit tests for Google Gemini & Veo provider adapters (no network calls)."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import config, providers  # noqa: E402


class GoogleProviderTests(unittest.TestCase):
    def test_catalog_and_providers_preserved_and_extended(self):
        p = config.providers()
        m = config.models()
        # Existing providers & models remain intact
        for pid in ("deepseek", "dashscope", "volcengine_official", "google_gemini", "google_veo"):
            self.assertIn(pid, p)
        for mid in ("deepseek-chat", "seedream-5.0-flash", "wan3.0-video", "seedance-2.5",
                    "gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-image",
                    "veo-3.1-fast", "veo-3.1"):
            self.assertIn(mid, m)

    def test_gemini_chat_multimodal_translation(self):
        prov = config.Provider(
            id="google_gemini", label="Google Gemini", kind="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key_env="GEMINI_API_KEY",
        )
        fake_resp = {
            "candidates": [{"content": {"parts": [{"text": '{"score": 92}'}]}}],
            "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 12},
            "modelVersion": "gemini-2.5-flash",
        }
        payload = {
            "messages": [
                {"role": "system", "content": "You are a QC judge."},
                {"role": "user", "content": [
                    {"type": "text", "text": "Check consistency"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ]},
            ],
            "temperature": 0.2,
            "max_tokens": 500,
        }
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "AGG_FORCE_MOCK": "0"}):
            with patch.object(providers, "_post", new=AsyncMock(return_value=fake_resp)) as mock_post:
                res = asyncio.run(providers.chat(prov, "gemini-2.5-flash", payload))
                self.assertEqual(res["content"], '{"score": 92}')
                self.assertEqual(res["usage"], {"prompt_tokens": 120, "completion_tokens": 12})
                _, path, body = mock_post.call_args[0]
                self.assertEqual(path, "/models/gemini-2.5-flash:generateContent")
                self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "You are a QC judge.")
                parts = body["contents"][0]["parts"]
                self.assertEqual(parts[1]["inlineData"], {"mimeType": "image/png", "data": "AAAA"})

    def test_gemini_image_generation(self):
        prov = config.Provider(
            id="google_gemini", label="Google Gemini", kind="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key_env="GEMINI_API_KEY",
        )
        fake_resp = {
            "candidates": [{"content": {"parts": [
                {"inlineData": {"mimeType": "image/jpeg", "data": "SU1BR0U="}}
            ]}}],
            "modelVersion": "gemini-2.5-flash-image",
        }
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "AGG_FORCE_MOCK": "0"}):
            with patch.object(providers, "_post", new=AsyncMock(return_value=fake_resp)) as mock_post:
                res = asyncio.run(providers.image(prov, "gemini-2.5-flash-image", {
                    "prompt": "cyberpunk street",
                    "size": "720x1280",
                    "image": ["data:image/png;base64,UkVG"],
                }))
                self.assertEqual(res["images"], ["data:image/jpeg;base64,SU1BR0U="])
                _, path, body = mock_post.call_args[0]
                self.assertEqual(path, "/models/gemini-2.5-flash-image:generateContent")
                self.assertEqual(body["generationConfig"]["imageConfig"]["aspectRatio"], "9:16")
                self.assertEqual(body["contents"][0]["parts"][1]["inlineData"]["data"], "UkVG")

    def test_veo_submit_and_poll(self):
        prov = config.Provider(
            id="google_veo", label="Google Veo", kind="gemini_veo",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key_env="GEMINI_API_KEY",
        )
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key", "AGG_FORCE_MOCK": "0"}):
            with patch.object(providers, "_post", new=AsyncMock(return_value={"name": "operations/veo-123"})) as mock_post:
                sub = asyncio.run(providers.submit_video(prov, "veo-3.1-fast-generate-preview", "720P", {
                    "prompt": "hero walks in rain",
                    "duration": 5,
                    "aspect_ratio": "9:16",
                    "images": [{"url": "data:image/png;base64,UkVG", "role": "reference_image"}],
                }))
                self.assertEqual(sub["provider_task_id"], "operations/veo-123")
                _, path, body = mock_post.call_args[0]
                self.assertEqual(path, "/models/veo-3.1-fast-generate-preview:predictLongRunning")
                self.assertEqual(body["parameters"]["durationSeconds"], 8)  # referenceImages requires 8s
                self.assertEqual(body["instances"][0]["referenceImages"][0]["referenceType"], "asset")

            poll_done = {
                "done": True,
                "response": {
                    "generateVideoResponse": {
                        "generatedSamples": [{"video": {"uri": "https://generativelanguage.googleapis.com/download/v1beta/files/abc:download?alt=media"}}]
                    }
                },
            }
            with patch.object(providers, "_get", new=AsyncMock(return_value=poll_done)):
                res = asyncio.run(providers.poll_video(prov, "operations/veo-123"))
                self.assertEqual(res["state"], "succeeded")
                self.assertIn("files/abc:download", res["url"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
