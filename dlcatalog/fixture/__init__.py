"""Fixture package: a local IIIF-style server with controllable edge cases.

The generator writes static collection/manifest documents (including
revisions) and the server serves them while maintaining a small mutable
state file that drives reference cycles, transient failures and manifest
revisions.
"""
