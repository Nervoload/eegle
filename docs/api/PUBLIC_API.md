# EEGle Public API

> **Legacy current-implementation document.** This describes the pre-migration
> import surface and remains only as compatibility evidence. The future public
> API is governed by [EEGLE.md](../EEGLE.md) and [MIGRATION.md](../MIGRATION.md).

EEGle's pip-facing API is organized around stable scientific primitives rather
than task-specific scripts.

## Core

Use `eegle.core` for session layout, config loading, telemetry, provenance, and
artifact hashing.

```python
from eegle.core import Session, create_session, load_config
```

The session directory and model-bundle formats are public library concepts. The
legacy imports such as `eegle.session.create_session` remain available during
the `0.1.x` compatibility window.

## Streams

Use `eegle.streams` for LSL stream discovery, marker outlets, marker events,
recorders, and simulated streams.

```python
from eegle.streams import LslStream, MarkerEvent, resolve_streams
```

Hardware-specific profiles stay outside core session/model contracts.

## Realtime

Use `eegle.realtime` for marker-locked epoching, model prediction payloads, and
decision-policy action records.

```python
from eegle.realtime import EpochingConfig, MarkerEvent, ModelPrediction, TaskAction
```

Worker subprocess details remain internal. Dashboard and task workflows should
be optional observers/recipes over these primitives.

## Models

Use `eegle.models` for model specs, dynamic registration, model bundles,
contracts, metrics, targets, adapters, and calibration state.

```python
from eegle.models import ModelBundle, ModelContract, register_model_spec
```

Foundation models should register through optional adapter packages and remain
shadow-first until their input contract, latency, and replay behavior are
validated.

## Protocols

Use `eegle.protocols` for machine-readable scientific protocol files that state
the task, target, prediction window, prediction horizon, split strategy,
baselines, and metrics.

```python
from eegle.protocols import ScientificProtocol
```
