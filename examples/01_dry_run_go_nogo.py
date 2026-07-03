"""Run a two-trial dry Go/No-go session without EEG hardware."""

from __future__ import annotations

import copy

from eegle.config import load_config
from eegle.experiment import ForwardExperimentRunner


def main() -> None:
    config = copy.deepcopy(load_config("configs/forward_go_nogo_classifier8.json"))
    config["processes"]["recorder"]["enabled"] = False
    config["processes"]["realtime_processor"]["enabled"] = False
    config["processes"]["dashboard"]["enabled"] = False
    config["realtime"]["enabled"] = False
    result = ForwardExperimentRunner(
        config,
        task_name="go_nogo",
        task_mode="dry-run",
        participant_id="example-dry-run",
        trials=2,
        record_eeg=False,
        require_eeg=False,
    ).run()
    print(result.session_dir)


if __name__ == "__main__":
    main()
