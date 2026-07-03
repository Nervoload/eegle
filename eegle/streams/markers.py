"""Public marker stream types."""

from eegle.realtime.epoching import MarkerEvent, marker_matches, parse_marker_label, should_epoch_marker


__all__ = ["MarkerEvent", "marker_matches", "parse_marker_label", "should_epoch_marker"]
