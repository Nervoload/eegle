PYTHON ?= python3
CONFIG ?= configs/default_experiment.json
SESSION ?=

.PHONY: check-setup check-enobio8 doctor doctor-enobio8 list-tasks init-session dry-run forward-dry-run forward-pvt forward-pvt-8 forward-go-nogo-dry-run forward-go-nogo-alpha-dry-run forward-go-nogo-8 alpha8-full alpha8-dry-run inhibition8-full inhibition8-dry-run classify8-collect-dry-run simulate-eeg analyze report-html compile

check-setup:
	$(PYTHON) -m reproduce.cli check-setup --config $(CONFIG)

check-enobio8:
	$(PYTHON) -m reproduce.cli check-setup --config configs/forward_pvt_enobio8.json --require-eeg

doctor: check-setup

doctor-enobio8: check-enobio8

list-tasks:
	$(PYTHON) -m reproduce.cli list-tasks

init-session:
	$(PYTHON) -m reproduce.cli init-session --config $(CONFIG) --task pvt

dry-run:
	$(PYTHON) -m reproduce.cli run-task --config $(CONFIG) --task pvt --mode dry-run

forward-dry-run:
	$(PYTHON) -m reproduce.cli run-forward --config $(CONFIG) --task pvt --task-mode dry-run --skip-eeg

forward-pvt:
	$(PYTHON) -m reproduce.cli run-forward --config configs/forward_pvt_enobio.json --task pvt --task-mode psychopy --require-eeg

forward-pvt-8:
	$(PYTHON) -m reproduce.cli run-forward --config configs/forward_pvt_enobio8.json --task pvt --task-mode psychopy --require-eeg

forward-go-nogo-dry-run:
	$(PYTHON) -m reproduce.cli run-forward --config configs/forward_go_nogo_enobio8.json --task go_nogo --task-mode dry-run --skip-eeg

forward-go-nogo-alpha-dry-run:
	$(PYTHON) -m reproduce.cli run-forward --config configs/forward_go_nogo_enobio8.json --task go_nogo --task-mode dry-run --skip-eeg --allow-missing-eeg --calibration-suite posterior_alpha

forward-go-nogo-8:
	$(PYTHON) -m reproduce.cli run-forward --config configs/forward_go_nogo_enobio8.json --task go_nogo --task-mode psychopy --require-eeg

alpha8-full:
	./alpha8 full

alpha8-dry-run:
	./alpha8 full --task-mode dry-run --skip-eeg --allow-missing-eeg --trials 2

inhibition8-full:
	./inhibition8 full

inhibition8-dry-run:
	./inhibition8 full --task-mode dry-run --skip-eeg --allow-missing-eeg --trials 2

classify8-collect-dry-run:
	$(PYTHON) -m reproduce.pipelines.classify8 collect --task-mode dry-run --skip-eeg --allow-missing-eeg --trials 2

simulate-eeg:
	$(PYTHON) -m reproduce.cli simulate-eeg --duration 30 --channels 32 --sample-rate 500

analyze:
	$(PYTHON) -m reproduce.cli analyze --session-dir $(SESSION)

report-html:
	$(PYTHON) -m reproduce.cli report-html --session-dir $(SESSION)

compile:
	$(PYTHON) -m compileall reproduce
