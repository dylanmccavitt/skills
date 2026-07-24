"""Persisted handoff state for the release builder."""


def prepare(input_path, checkpoint_path):
    """Create a deterministic checkpoint from an input JSON document."""
    raise NotImplementedError


def load_checkpoint(checkpoint_path):
    """Load and verify a checkpoint."""
    raise NotImplementedError
