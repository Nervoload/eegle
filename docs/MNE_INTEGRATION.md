# MNE research bridge

The dependency-lazy `eegle.integrations` bridge projects EEGle capture and
admission values into MNE analysis objects, and can turn an MNE `Raw` object
back into explicit replay inputs. Install it with
`python -m pip install "eegle[analysis]"`. Importing EEGle or
`eegle.integrations` does not import MNE.

## Dense capture and markers

`dense_batch_to_mne_raw()` converts a `DenseSampleBatch` plus its exact
`StreamSpec` to an MNE `RawArray`, preserving channel order and explicitly
scaling V/mV/uV/nV values to volts. `sparse_events_to_mne_annotations()` maps
marker event times to raw-relative MNE annotations; use
`attach_annotations_to_mne_raw()` to attach them to a copied `Raw` value.

```python
from eegle.integrations import (
    attach_annotations_to_mne_raw,
    dense_batch_to_mne_raw,
    sparse_events_to_mne_annotations,
)

exported = dense_batch_to_mne_raw(batch, stream)
annotations = sparse_events_to_mne_annotations(
    marker_batch,
    marker_stream,
    raw_first_sample_time=exported.first_sample_time,
)
raw = attach_annotations_to_mne_raw(exported, annotations)
```

MNE cannot carry EEGle’s full clock and boundary-availability semantics. The
returned `MneRawExport` and `MneAnnotationsExport` sidecars therefore retain
the exact source times, clocks, availability times, marker IDs, and marker
payloads. This includes explicitly timed dense batches produced by the LSL
source; MNE still represents the `RawArray` on its declared regular sampling
grid. Marker metadata never enters the dense model-input path.

## Admitted windows and replay inputs

`dense_windows_to_mne_epochs()` converts already-admitted EEGle `DenseWindow`
values to `mne.EpochsArray`. Its sidecar retains window IDs, packet sequences,
input IDs, availability times, and the source clock. Windows must have the same
channels, shape, units, clock, and regular sampling period; the adapter does
not silently resample or align them.

`mne_raw_to_replay_inputs()` converts an EEG-only MNE `Raw` value into one
dense `StreamSpec` and `DenseSampleBatch`. Annotations, when present, become a
separate sparse stream and packet. The caller must explicitly provide the
absolute first-sample, received, and available times because MNE's analysis
object is not the authority for EEGle boundary availability. This adapter
accepts raw-relative annotations; annotations with an independent `orig_time`
must be normalized by the caller first.

These are analysis and replay-input adapters, not recording,
synchronization, resampling, or runtime authorities. MNE documents the
underlying `RawArray`, `Annotations`, and `EpochsArray` structures in its
[official API](https://mne.tools/stable/generated/mne.io.RawArray.html),
[annotation API](https://mne.tools/stable/generated/mne.Annotations.html), and
[epoch API](https://mne.tools/stable/generated/mne.EpochsArray.html).

## Machine-readable support claims

`research_integration_support_matrix()` loads the packaged
`eegle.research_integration_support.v1` matrix. Every capability reports four
independent booleans: `representable`, `adapter_available`, `validated`, and
`reference_supported`, together with the validation level, evidence paths, and
limitations. Representability or an installed dependency never implies that
an adapter, validation record, or reference workflow exists.

The package workflow installs the exact wheel with native `pylsl` and MNE,
receives a local-network two-channel LSL batch, and exercises raw export,
marker annotations, admitted-window epochs, and replay-input construction.
That acceptance is hardware-free; the current remote workflow result remains
a separate release record and is not claimed by the support matrix until it is
retained.
