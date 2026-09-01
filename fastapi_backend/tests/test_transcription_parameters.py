"""Engine parameter schema: coercion, range checks and unknown-key refusal.

Every operator-supplied engine option passes through ``ParameterSpec.coerce``
and ``validate_options``, from the settings form and from stored settings on
each run. Values arrive as JSON — a number may be a string, a boolean may be
"true" — so the coercion rules are the contract between the UI and the engines.
"""
from __future__ import annotations

import pytest

from app.pipeline.transcription.base import (
    EngineCapabilities,
    EngineDescriptor,
    ParameterSpec,
    ParameterType,
    TranscriptionOptionError,
    validate_options,
)

BATCH_SIZE = ParameterSpec(
    name="batchSize",
    label="Batch size",
    type=ParameterType.INTEGER,
    default=1,
    minimum=1,
    maximum=32,
)
CHUNK_SECONDS = ParameterSpec(
    name="chunkSeconds",
    label="Chunk length",
    type=ParameterType.FLOAT,
    default=30.0,
    minimum=5.0,
    maximum=40.0,
)
DIARIZE = ParameterSpec(name="diarize", label="Label speakers", type=ParameterType.BOOLEAN, default=True)
MODEL = ParameterSpec(name="model", label="Model", type=ParameterType.STRING, default="large-v3")
DEVICE = ParameterSpec(
    name="device",
    label="Device",
    type=ParameterType.ENUM,
    default="auto",
    options=("auto", "cuda", "cpu"),
)

DESCRIPTOR = EngineDescriptor(
    id="test-engine",
    label="Test Engine",
    vendor="Tests",
    description="Fixture engine.",
    capabilities=EngineCapabilities(),
    parameters=(BATCH_SIZE, CHUNK_SECONDS, DIARIZE, MODEL, DEVICE),
)


def test_integers_accept_json_strings() -> None:
    assert BATCH_SIZE.coerce(4) == 4
    assert BATCH_SIZE.coerce("4") == 4
    assert BATCH_SIZE.coerce(" 4 ") == 4


def test_floats_accept_integers_and_strings() -> None:
    assert CHUNK_SECONDS.coerce(20) == 20.0
    assert CHUNK_SECONDS.coerce("20.5") == 20.5


def test_booleans_accept_the_shapes_a_form_sends() -> None:
    assert DIARIZE.coerce(True) is True
    assert DIARIZE.coerce("false") is False
    assert DIARIZE.coerce("ON") is True
    assert DIARIZE.coerce(0) is False


def test_none_means_keep_the_default() -> None:
    assert BATCH_SIZE.coerce(None) == 1
    assert MODEL.coerce(None) == "large-v3"


def test_out_of_range_values_are_refused_with_the_bound_named() -> None:
    with pytest.raises(TranscriptionOptionError, match="at least 1"):
        BATCH_SIZE.coerce(0)
    with pytest.raises(TranscriptionOptionError, match="at most 32"):
        BATCH_SIZE.coerce(64)
    with pytest.raises(TranscriptionOptionError, match="at most 40"):
        CHUNK_SECONDS.coerce(45)


def test_wrong_type_names_the_parameter() -> None:
    with pytest.raises(TranscriptionOptionError, match="Batch size"):
        BATCH_SIZE.coerce("not-a-number")
    with pytest.raises(TranscriptionOptionError, match="Label speakers"):
        DIARIZE.coerce("maybe")


def test_enum_rejects_values_outside_its_options() -> None:
    assert DEVICE.coerce("cuda") == "cuda"
    with pytest.raises(TranscriptionOptionError, match="auto, cuda, cpu"):
        DEVICE.coerce("tpu")


def test_option_errors_are_client_errors_not_server_errors() -> None:
    # The job queue retries transport failures; a bad option can never succeed
    # on a retry, so it must not look like one.
    with pytest.raises(TranscriptionOptionError) as excinfo:
        DEVICE.coerce("tpu")
    assert excinfo.value.status_code == 422


def test_validate_options_coerces_every_supplied_value() -> None:
    validated = validate_options(DESCRIPTOR, {"batchSize": "2", "diarize": "false", "chunkSeconds": "12.5"})

    assert validated == {"batchSize": 2, "diarize": False, "chunkSeconds": 12.5}


def test_validate_options_refuses_unknown_keys() -> None:
    # Silently dropping a typo is indistinguishable from an option that did
    # nothing, which is the worst possible failure mode for a tuning screen.
    with pytest.raises(TranscriptionOptionError, match="not an option"):
        validate_options(DESCRIPTOR, {"btachSize": 2})


def test_validate_options_accepts_nothing_at_all() -> None:
    assert validate_options(DESCRIPTOR, None) == {}
    assert validate_options(DESCRIPTOR, {}) == {}


def test_validate_options_rejects_a_non_object() -> None:
    with pytest.raises(TranscriptionOptionError, match="must be an object"):
        validate_options(DESCRIPTOR, ["batchSize", 2])


def test_descriptor_publishes_everything_a_form_needs() -> None:
    public = DESCRIPTOR.to_public()
    batch = next(item for item in public["parameters"] if item["name"] == "batchSize")
    device = next(item for item in public["parameters"] if item["name"] == "device")

    assert batch["type"] == "int"
    assert batch["minimum"] == 1 and batch["maximum"] == 32
    assert device["options"] == ["auto", "cuda", "cpu"]
    assert public["capabilities"]["diarization"] is False
