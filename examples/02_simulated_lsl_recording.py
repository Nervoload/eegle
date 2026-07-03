"""Start a short simulated EEG LSL stream for development.

Install the runtime extra first:

    python -m pip install -e ".[runtime]"
"""

from __future__ import annotations

from eegle.streams.simulated import run_simulated_eeg


def main() -> None:
    run_simulated_eeg(name="EEGleExample", channel_count=8, sample_rate_hz=250.0, duration_seconds=5.0)


if __name__ == "__main__":
    main()
