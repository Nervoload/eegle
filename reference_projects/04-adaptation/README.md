# Delayed-outcome adaptation

Runs prediction/outcome enrollment and adaptation against the snapshot/restore
`eegle.example_models.adaptive_mean` implementation. The portable design does
not authorize mutation. Its separate simulation deployment was created with
the explicit `--grant-simulated-adaptation` flag so the example can produce
eligible, requested, and applied state-transition evidence; a live site must
review and supply its own grant.

```bash
python -m pip install ../../examples/plugins/eegle-example-models
eegle compile .
eegle preflight .
eegle run . --session-id session.reference.adaptation
eegle inspect sessions/session.reference.adaptation
eegle replay sessions/session.reference.adaptation
```

To reproduce the scaffold explicitly:

```bash
eegle new adaptation --id reference.adaptation \
  --preset eegle.preset.adaptation \
  --grant-simulated-adaptation
```
