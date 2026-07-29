# Phase 7 reference projects

These six projects are generated from exact compositional preset revision
`2.0.0`. Each project contains a normalized design source, canonical protocol
and suite, portable deployment requirements, a deterministic simulation
deployment, authoring provenance/explanation, and a content-hashed project
index. The original exact template `1.0.0` revisions remain unchanged.

The recording and event-locked observation projects run with the base wheel.
The model comparison, adaptation, and closed-loop projects intentionally name
separately installed model plugins and include their canonical manifests; they
do not hide model code inside authoring. The LSL project keeps the portable
suite separate from live binding discovery.

The adaptation project keeps permission out of its portable design. Its
separate simulation deployment was created with the explicit
`--grant-simulated-adaptation` scaffold flag and produces
eligible, requested, and applied transition evidence under equivalent replay;
live deployment authorization must be reviewed independently.

Run a base-only project from a clean environment:

```bash
eegle compile reference_projects/01-recording
eegle explain reference_projects/01-recording
eegle preflight reference_projects/01-recording
eegle rehearse reference_projects/01-recording
eegle inspect reference_projects/01-recording
eegle replay reference_projects/01-recording
```

All commands are non-overwriting. Inspection and replay return structured
partial/unavailable results by default and never terminate another recording,
processing, or training process.
