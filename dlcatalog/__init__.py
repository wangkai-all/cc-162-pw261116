"""dlcatalog — digital collection catalog harvester for IIIF-style manifests.

Pure standard-library package: parses IIIF Presentation API v2/v3 collection
and manifest documents, crawls them recursively without entering reference
cycles, preserves every collection membership, tracks manifest revisions and
exposes the result through a local web application and an export pipeline.
"""

__version__ = "1.0.0"
