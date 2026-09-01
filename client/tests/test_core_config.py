# -*- coding: utf-8 -*-
"""
tests/test_core_config.py
=========================
Non-régression de la configuration : chargement, sauvegarde, validation,
normalisation et robustesse aux clés inconnues.
"""

import json

import pytest

from app.core.config import (
    CONFIG_PATH,
    DEFAULT_CONFIG,
    load_config,
    save_config,
    validate_config,
)


class TestConfigFile:
    """config.json doit rester cohérent avec le roadmap (§5.8)."""

    def test_config_has_all_expected_keys(self):
        cfg = load_config()  # fichier absent -> défauts
        expected = {
            "server_url", "server_api_key", "server_timeout", "ws_port",
            "model", "language", "task", "vad_filter", "hotkey",
            "input_mode", "audio_device", "inject_text", "add_space",
            "keep_in_clipboard", "auto_start", "max_history",
            "continuous_mode", "compute_type",
        }
        missing = expected - set(cfg)
        assert not missing, f"Clés manquantes : {missing}"
        assert cfg["server_url"] == "http://192.168.1.50:8000"
        assert cfg["server_timeout"] == 30
        assert cfg["ws_port"] == 9090
        assert cfg["model"] == "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
        assert cfg["language"] == "fr"
        assert cfg["input_mode"] in ("push_to_talk", "toggle")
        assert cfg["task"] in ("transcribe", "translate")

    def test_defaults_used_when_file_absent(self, tmp_path):
        cfg = load_config(tmp_path / "inexistant.json")
        assert cfg == DEFAULT_CONFIG

    def test_save_roundtrip(self, tmp_path):
        path = tmp_path / "config.json"
        cfg = dict(DEFAULT_CONFIG, server_timeout=60, model="Systran/faster-whisper-medium",
                   language="auto")
        save_config(cfg, path)
        loaded = load_config(path)
        assert loaded["server_timeout"] == 60
        assert loaded["model"] == "Systran/faster-whisper-medium"
        assert loaded["language"] == "auto"

    def test_saved_file_is_valid_json(self, tmp_path):
        path = tmp_path / "config.json"
        save_config(dict(DEFAULT_CONFIG), path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw == DEFAULT_CONFIG

    def test_unknown_keys_ignored(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({**DEFAULT_CONFIG,
                                    "cle_inconnue": 42,
                                    "autre": {"nested": True}}),
                        encoding="utf-8")
        cfg = load_config(path)
        assert "cle_inconnue" not in cfg
        assert "autre" not in cfg
        assert set(cfg) == set(DEFAULT_CONFIG)

    def test_corrupted_json_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("{ pas du json", encoding="utf-8")
        assert load_config(path) == DEFAULT_CONFIG

    def test_non_object_json_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert load_config(path) == DEFAULT_CONFIG


class TestValidateConfig:
    def test_valid_config_passes(self):
        cfg = validate_config(dict(DEFAULT_CONFIG))
        assert cfg["server_url"] == DEFAULT_CONFIG["server_url"]

    def test_empty_server_url_raises(self):
        with pytest.raises(ValueError):
            validate_config({**DEFAULT_CONFIG, "server_url": ""})

    def test_whitespace_server_url_raises(self):
        with pytest.raises(ValueError):
            validate_config({**DEFAULT_CONFIG, "server_url": "   "})

    def test_server_timeout_too_low_raises(self):
        with pytest.raises(ValueError):
            validate_config({**DEFAULT_CONFIG, "server_timeout": 2})

    def test_server_timeout_string_normalized_to_int(self):
        cfg = validate_config({**DEFAULT_CONFIG, "server_timeout": "45"})
        assert cfg["server_timeout"] == 45
        assert isinstance(cfg["server_timeout"], int)

    def test_ws_port_default(self):
        cfg = validate_config({**DEFAULT_CONFIG, "ws_port": 9090})
        assert cfg["ws_port"] == 9090
        assert isinstance(cfg["ws_port"], int)

    def test_ws_port_string_normalized_to_int(self):
        cfg = validate_config({**DEFAULT_CONFIG, "ws_port": "9090"})
        assert cfg["ws_port"] == 9090
        assert isinstance(cfg["ws_port"], int)

    @pytest.mark.parametrize("bad", [0, -1, "abc", None])
    def test_ws_port_invalid_raises(self, bad):
        with pytest.raises(ValueError):
            validate_config({**DEFAULT_CONFIG, "ws_port": bad})

    def test_ws_port_float_truncated_like_server_timeout(self):
        """Un float est tronqué par int() — même comportement que
        server_timeout (1.5 -> 1, valide car >= 1)."""
        cfg = validate_config({**DEFAULT_CONFIG, "ws_port": 9090.9})
        assert cfg["ws_port"] == 9090

    def test_invalid_task_raises(self):
        with pytest.raises(ValueError):
            validate_config({**DEFAULT_CONFIG, "task": "summarize"})

    def test_invalid_input_mode_raises(self):
        with pytest.raises(ValueError):
            validate_config({**DEFAULT_CONFIG, "input_mode": "hold"})

    def test_empty_model_accepted(self):
        """Un model vide est accepté (aucun modèle installé/sélectionné).

        Le frontend affiche un warning mais la sauvegarde doit réussir.
        """
        cfg = validate_config({**DEFAULT_CONFIG, "model": ""})
        assert cfg["model"] == ""

    def test_language_auto_to_none(self):
        cfg = validate_config({**DEFAULT_CONFIG, "language": "auto"})
        assert cfg["language"] is None

    def test_language_empty_to_none(self):
        cfg = validate_config({**DEFAULT_CONFIG, "language": ""})
        assert cfg["language"] is None

    def test_language_kept(self):
        cfg = validate_config({**DEFAULT_CONFIG, "language": "en"})
        assert cfg["language"] == "en"

    def test_bools_normalized(self):
        cfg = validate_config({**DEFAULT_CONFIG,
                               "vad_filter": 0, "inject_text": 1,
                               "add_space": "1", "keep_in_clipboard": 0,
                               "auto_start": 1, "continuous_mode": 0})
        assert cfg["vad_filter"] is False
        assert cfg["inject_text"] is True
        assert cfg["add_space"] is True
        assert cfg["keep_in_clipboard"] is False
        assert cfg["auto_start"] is True
        assert cfg["continuous_mode"] is False

    def test_max_history_normalized_and_floored(self):
        cfg = validate_config({**DEFAULT_CONFIG, "max_history": "100"})
        assert cfg["max_history"] == 100
        cfg2 = validate_config({**DEFAULT_CONFIG, "max_history": 0})
        assert cfg2["max_history"] == 1

    def test_audio_device_to_int(self):
        cfg = validate_config({**DEFAULT_CONFIG, "audio_device": "3"})
        assert cfg["audio_device"] == 3
        cfg2 = validate_config({**DEFAULT_CONFIG, "audio_device": ""})
        assert cfg2["audio_device"] is None
        cfg3 = validate_config({**DEFAULT_CONFIG, "audio_device": None})
        assert cfg3["audio_device"] is None

    def test_validate_does_not_mutate_input(self):
        original = dict(DEFAULT_CONFIG, server_timeout="15", language="auto")
        validate_config(original)
        assert original["server_timeout"] == "15"
        assert original["language"] == "auto"

    def test_errors_are_json_serializable(self):
        with pytest.raises(ValueError) as exc_info:
            validate_config({**DEFAULT_CONFIG, "server_url": "",
                             "server_timeout": 1, "ws_port": 0})
        errors = json.loads(exc_info.value.args[0])
        assert "server_url" in errors
        assert "server_timeout" in errors
        assert "ws_port" in errors

    def test_compute_type_default_is_int8(self):
        assert DEFAULT_CONFIG["compute_type"] == "int8"

    @pytest.mark.parametrize("ct", ["int8", "int8_float16", "float16", "float32"])
    def test_compute_type_valid_accepted(self, ct):
        cfg = validate_config({**DEFAULT_CONFIG, "compute_type": ct})
        assert cfg["compute_type"] == ct

    @pytest.mark.parametrize("bad", ["int4", "fp16", "", None, 8, "INT8"])
    def test_compute_type_invalid_raises(self, bad):
        with pytest.raises(ValueError):
            validate_config({**DEFAULT_CONFIG, "compute_type": bad})

    def test_compute_type_normalized_to_str(self):
        cfg = validate_config(dict(DEFAULT_CONFIG))
        assert isinstance(cfg["compute_type"], str)
        assert cfg["compute_type"] == "int8"
