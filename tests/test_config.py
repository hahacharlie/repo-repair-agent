import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents import ModelAgents
from config import load_config, load_env


CONFIG = Path(__file__).resolve().parents[1] / "config.json"


class ConfigTests(unittest.TestCase):
    def test_env_file_is_loaded_without_replacing_set_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "# Local keys\nNEW_KEY=new-value\nKEEP_KEY=file-value\n")
            with patch.dict(
                    "os.environ", {"KEEP_KEY": "set-value"}, clear=True):
                load_env(path)
                self.assertEqual(os.environ["NEW_KEY"], "new-value")
                self.assertEqual(os.environ["KEEP_KEY"], "set-value")

    def test_config_rejects_mistakes_before_api_calls(self):
        config = load_config(CONFIG)
        invalid = [
            [],
            {},
            dict(config, fixer_model=""),
            dict(config, judge_model=4),
            dict(config, unexpected="typo"),
            dict(config, endpoint="http://example.test"),
            dict(config, endpoint="https://user:secret@example.test"),
            *[
                dict(config, api_timeout_seconds=value)
                for value in (0, -1, True, "300", 3601)
            ],
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            for value in invalid:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    path.write_text(json.dumps(value))
                    load_config(path)
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(ValueError):
            ModelAgents(config)


if __name__ == "__main__":
    unittest.main()
