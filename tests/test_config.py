"""Tests for .env loading and extraction defaults."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from mkea_extract.config import ConfigError, LLMConfig, load_dotenv


class ConfigTests(TestCase):
    def test_dotenv_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\nMKEA_LLM_MODEL=test-model\nMKEA_LLM_API_KEY='secret'\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                loaded = load_dotenv(path)
                self.assertEqual(loaded, path)
                self.assertEqual(os.environ["MKEA_LLM_MODEL"], "test-model")
                self.assertEqual(os.environ["MKEA_LLM_API_KEY"], "secret")

    def test_defaults(self):
        # from_env() loads the project .env internally, so point MKEA_ENV_FILE
        # at a nonexistent path to test the built-in defaults in isolation.
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / "absent.env"
            with patch.dict(
                os.environ,
                {"MKEA_LLM_API_KEY": "k", "MKEA_ENV_FILE": str(absent)},
                clear=True,
            ):
                config = LLMConfig.from_env()
        self.assertEqual(config.model, "deepseek-v4.1-flash")
        self.assertEqual(config.base_url, "https://opencode.ai/inference/go/openai/v1")
        self.assertTrue(config.session_id)

    def test_missing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.env"
            with patch.dict(os.environ, {"MKEA_ENV_FILE": str(missing)}, clear=True):
                with self.assertRaises(ConfigError):
                    LLMConfig.from_env()
                config = LLMConfig.from_env(require_key=False)
        self.assertEqual(config.api_key, "")

    def test_redacted(self):
        config = LLMConfig(api_key="secret")
        self.assertEqual(config.redacted()["api_key"], "***")


class MaxTokensTests(TestCase):
    """Guard the HTTP 400 "Range of max_tokens should be [1, 32768]" trap.

    Providers such as qwen-plus reject anything above 32768, and the whole
    extraction then degrades to the rule engine.
    """

    def test_default_is_within_common_provider_limits(self):
        self.assertLessEqual(LLMConfig(api_key="k").max_tokens, 32768)

    def test_env_default_also_stays_within_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            absent = Path(tmp) / "none.env"
            with patch.dict(
                os.environ,
                {"MKEA_LLM_API_KEY": "k", "MKEA_ENV_FILE": str(absent)},
                clear=True,
            ):
                self.assertLessEqual(LLMConfig.from_env().max_tokens, 32768)

    def test_shipped_env_example_does_not_exceed_the_limit(self):
        """A fresh clone must not start out broken."""
        root = Path(__file__).resolve().parents[1]
        for name in (".env.example", ".env"):
            path = root / name
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("MKEA_LLM_MAX_TOKENS="):
                    value = int(line.split("=", 1)[1])
                    self.assertLessEqual(value, 32768, f"{name} 配置超出常见服务商上限")
