# Security Policy

EEGle is research software and may handle sensitive EEG recordings, participant
metadata, and experiment logs. Do not publish generated session data without
explicit review.

## Supported Versions

Security reports are accepted for the current `0.1.x` development line.

## Reporting

Please report suspected vulnerabilities privately to the maintainer before
opening a public issue. Include:

- affected version or commit;
- steps to reproduce;
- whether participant data, generated artifacts, or local services are exposed;
- any suggested mitigation.

## Safety Boundaries

Closed-loop or stimulation-related behavior must remain opt-in, research-gated,
and replayable. Do not add code that silently adapts model weights or changes
task behavior from classifier predictions without explicit logging and config
gates.
