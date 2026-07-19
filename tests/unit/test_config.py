import json

import pytest
from pydantic import ValidationError

from lb.config import Config, load


def write_config(tmp_path, content: dict) -> str:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(content))
    return str(path)


def test_load_valid_config(tmp_path):
    path = write_config(
        tmp_path,
        {
            "listen_host": "0.0.0.0",
            "listen_port": 8080,
            "algorithm": "round_robin",
            "backends": [
                {"url": "http://localhost:9001", "weight": 1},
                {"url": "http://localhost:9002", "weight": 2},
            ],
        },
    )
    cfg = load(path)
    assert cfg.listen_port == 8080
    assert len(cfg.backends) == 2
    assert cfg.backends[1].weight == 2


def test_load_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load("/nonexistent/path/config.json")


def test_load_malformed_json_raises(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not valid json")
    with pytest.raises(json.JSONDecodeError):
        load(str(path))


def test_no_backends_raises():
    with pytest.raises(ValidationError):
        Config(listen_port=8080, backends=[])


def test_default_weight_is_one():
    cfg = Config(backends=[{"url": "http://localhost:9001"}])
    assert cfg.backends[0].weight == 1


def test_zero_weight_defaults_to_one():
    cfg = Config(backends=[{"url": "http://localhost:9001", "weight": 0}])
    assert cfg.backends[0].weight == 1


def test_negative_weight_raises():
    with pytest.raises(ValidationError):
        Config(backends=[{"url": "http://localhost:9001", "weight": -1}])


def test_empty_backend_url_raises():
    with pytest.raises(ValidationError):
        Config(backends=[{"url": "   "}])


def test_unsupported_algorithm_raises():
    with pytest.raises(ValidationError):
        Config(algorithm="consistent_hashing", backends=[{"url": "http://localhost:9001"}])


def test_invalid_port_raises():
    with pytest.raises(ValidationError):
        Config(listen_port=70000, backends=[{"url": "http://localhost:9001"}])


def test_default_listen_values():
    cfg = Config(backends=[{"url": "http://localhost:9001"}])
    assert cfg.listen_host == "0.0.0.0"
    assert cfg.listen_port == 8080
    assert cfg.algorithm == "round_robin"
