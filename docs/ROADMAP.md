# Roadmap

## Immediate Foundation

1. Validate `make doctor` with NIC2 streaming Enobio EEG over LSL.
2. Run `make forward-dry-run` and confirm the BciPy-style session folders are created.
3. Run `make forward-pvt` with NIC2 streaming and confirm `raw/eeg.csv` receives samples.
4. Run PVT in PsychoPy mode with no subject, then with a photodiode timing check.
5. Decide whether LabRecorder/XDF is the canonical raw recording path or whether BciPy-style CSV should also be produced live.
6. Validate the managed `FeedbackManager` subprocess lifecycle with NIC2, including recorder status files and realtime worker startup.

## Task Reproduction

1. Baseline task -> typical experimental set up for baseline: closed-eye at rest, eye-open rest, etc for baseline & calibration (artifact correciton, etc.)
2. Add an affective/face-version of the go/no-go task, using a diffusion model to generate the images in real-time. 


## Closed Loop

Preprocessing:
1. Add LSL EEG inlet and live ring-buffer ingestion.
2. Add event-aligned windows from task markers.
3. Add artifact rejection and rolling baseline features.

Processing:
4. Add model selection: band-power baseline, ERP/RDA, scikit-learn, and later AI model adapters.
5. Add feedback policy gates and stimulation safety checks.
6. Promote the `labrecorder_xdf` hook into a real LabRecorder launcher once platform-specific installs and dependencies are confirmed.

## Analysis

1. Validate Go/No-go ERP/P300 analysis with real NIC2 recordings and compare CSV-local-time alignment against LabRecorder/XDF marker timing.
2. Build richer MNE reports from XDF or BciPy-style raw data.
3. Add scalp maps and power-band summaries.
4. Add behavioral task-performance dashboards.
5. Add classification performance reports.
6. Add reproducibility manifests with software versions, config diffs, stream metadata, and timing-offset estimates.
