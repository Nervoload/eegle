# Phase 7 closure evidence

**Status:** Local closure audit complete; Phase 7 remains blocked on two
external records
**Date:** 2026-07-29
**Phase plan:** [PHASE7_AUTHORING_OPERATIONS.md](PHASE7_AUTHORING_OPERATIONS.md)
**Machine record:**
[phase7_closure_evidence.json](migration/phase7_closure_evidence.json)
**Public surface:**
[phase7_public_surface.json](migration/phase7_public_surface.json)

This document is the P7-014 gate-to-evidence matrix. A `pass` means durable
repository evidence exercises the stated claim. `Pending external` is not a
waiver: the phase stays open until the named retained record exists.

## Exit-gate matrix

| Gate | State | Durable evidence |
|---|---|---|
| P7-EG-01 — clean create-to-replay simulation | pass | `test_phase5_packaging`, `test_phase7_project_cli` |
| P7-EG-02 — deterministic Python/YAML authoring without graph wiring | pass | `test_phase7_authoring_surfaces`, `test_phase7_templates` |
| P7-EG-03 — custom named topology, shared model input, observe-only structured action | pass | `test_phase7_compositional_authoring`, `test_phase7_reference_projects` |
| P7-EG-04 — separated authoring/default provenance and source diagnostics | pass | `test_phase7_draft_lowering`, `test_phase7_explanations`, `test_phase7_compositional_authoring` |
| P7-EG-05 — recording/preprocessing without model or action declarations | pass | `test_phase5_plan_execution`, recording and observation references |
| P7-EG-06 — LSL simulation plus real EEG observe-only acceptance | **pending external** | simulated discovery/source/outlet/clock/reconnect/loss/portability passes in `test_phase7_lsl_integration`; an installed wheel also passes native `pylsl` full-channel-metadata and packet transport; the retained real record required by `LSL_INTEGRATION.md` does not exist |
| P7-EG-07 — optional integrations are base-import safe | pass | LSL blocked-import test, MNE dependency-lazy test, clean package journey |
| P7-EG-08 — independently installed plugin | pass | installed Phase 2 transform wheel, P7-013A harness, installed example-model wheel |
| P7-EG-09 — callable plus dependency-backed model adapter | pass | Phase 6 generality and P7-011 sklearn package/replacement tests |
| P7-EG-10 — rehearsal/live semantic parity with distinct plan identities | pass | LSL portability and preflight/rehearsal tests |
| P7-EG-11 — no recipe commands in the public CLI | pass | public inventory and clean-wheel command tests |
| P7-EG-12 — replaced facades/runtime paths absent | pass | source-boundary, wheel/sdist-content, and Phase 0 disposition tests |
| P7-EG-13 — current tested commands/imports in documentation | pass | release-integrity and public-boundary tests |
| P7-EG-14 — transactional generated-project publication | pass | successful revision and failed-compile preservation regression |
| P7-EG-15 — one release identity, installed artifacts, remote matrix | **pending external** | local wheel/sdist/plugin/all-reference journey passes; the prior matrix is green, but the current closure candidate still requires its post-publication remote runs |

The eleven closure items in Section 8 of the Phase 7 plan map to these gates in
the machine record. Ten are locally complete; the LSL hardware item remains
pending. P7-014 additionally requires the current candidate's remote matrix,
which is kept as a separate blocker so a prior green run cannot silently stand
in for changed release artifacts.

## Installed-artifact evidence

The clean packaging acceptance builds and installs the base wheel and source
distribution, verifies that migration-only packages cannot import, builds the
example plugin as a separate wheel, and runs the project, model-comparison, and
adaptation paths outside the checkout. P7-014 extends that proof across all six
materialized reference projects: recording, event-locked observation, model
comparison, adaptation, simulated closed-loop action, and the simulation
deployment of the portable LSL observe-only project.

The release workflow exposes base wheel, sdist, YAML, native `pylsl`
local-network metadata/packet transport plus MNE conversion, plugin, and all-
reference-project failures as independently attributable jobs. The native client is
`tests/fixtures/phase7_native_lsl_smoke.py`; it runs outside the checkout
against the installed wheel and confirms exact `C3`/`C4` labels, `uV` units,
an explicitly timed two-sample dense packet, and MNE channel/scaling output.
The default manual workflow stops after those evidence jobs. Its separate
TestPyPI job requires the explicit `publish_testpypi` input, which defaults to
false. The package remains a pre-alpha, simulation-first candidate; TestPyPI
rehearsal and public-alpha promotion remain Phase 8 work.

## Supported remote matrix record

The corrected baseline workflow at commit
`417d8a44ed2e2bc39bdef990b8cfc16ce12c2740` completed successfully on
2026-07-29: [GitHub Actions run 30491548423](https://github.com/Nervoload/eegle/actions/runs/30491548423).
Its five successful jobs were Linux Python 3.11, 3.12, and 3.13; macOS Python
3.12; and Windows Python 3.12. This proves the corrected smoke path and is
retained historical evidence. Because P7-013A, P7-013B, and this closure work
postdate that commit, both `tests.yml` and the artifact-oriented `package.yml`
must pass after the candidate is published before P7-EG-15 can close. The
Phase 7 package run uses the default non-publishing dispatch.

## External closure protocol

Two records remain:

1. Complete the non-participant real EEG observe-only procedure in
   [LSL_INTEGRATION.md](LSL_INTEGRATION.md), record its non-sensitive identity,
   and promote LSL only to `live_observe_only_validated`.
2. Publish the current candidate and retain successful current-commit results
   for the supported `tests` matrix and the independently attributable package
   workflow jobs.

No participant data, credentials, or sensitive stream identity belongs in the
repository. Until both records exist, P7-010 and P7-014 remain `in progress`,
Phase 7 remains active, and all public language stays at
`simulated_validated`/pre-alpha.

## Local closure verification

The final local candidate ran 385 repository tests successfully on Python
3.14.4. That aggregate includes the clean wheel/sdist, external plugin, and all
six reference-project artifact journeys. The separately installed wheel passed
the native `pylsl 1.18.2` local-network smoke. Focused closure/LSL/release/public
tests, compile-all, focused Ruff `E9`/`F`/`I`, workflow YAML and evidence JSON
parsing, and `git diff --check` also pass.

## Final surface and deferrals

The final Phase 7 inventory retains eleven stable-alpha low-level packages and
four provisional packages (`authoring`, `operations`, `integrations`, and
`integrations.lsl`). Phase 8 now includes MNE raw, annotation, admitted-window
epoch, and replay-input adapters plus a machine-readable research support
matrix. Their expanded installed-artifact workflow result remains an external
Phase 8 qualification record.

Comprehensive validation, research-integration qualification, scale/fault
budgets, TestPyPI rehearsal, and public-alpha promotion also remain Phase 8.
