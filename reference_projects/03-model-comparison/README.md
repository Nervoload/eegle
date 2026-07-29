# Model comparison

Compares independently implemented mean and peak model plugins on the same
event-locked window. Canonical manifests are included under `authoring/`; the
matching example plugin distribution must be installed before compilation.

```bash
python -m pip install ../../examples/plugins/eegle-example-models
eegle compile .
eegle explain .
eegle rehearse .
eegle inspect .
```
