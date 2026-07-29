# MNE export bridge

`eegle.integrations.dense_batch_to_mne_raw()` is a dependency-lazy, one-way
bridge for analysis. It converts a regular `DenseSampleBatch` plus its exact
`StreamSpec` to an MNE `RawArray`, preserving channel order and explicitly
scaling V/mV/uV/nV values to volts.

```python
from eegle.integrations import dense_batch_to_mne_raw

exported = dense_batch_to_mne_raw(batch, stream)
raw = exported.raw
```

Install it with `python -m pip install "eegle[analysis]"`. Importing EEGle or
`eegle.integrations` does not import MNE.

MNE cannot carry EEGle’s full clock and boundary-availability semantics. The
returned `MneRawExport` sidecar therefore retains the source clock, first-sample
time, availability clock, and availability time. The bridge is for downstream
analysis; it is not a recording, replay, synchronization, or runtime authority.
