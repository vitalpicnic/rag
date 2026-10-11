"""Reject filesystem links before canonicalizing legitimate platform path aliases."""
from pathlib import Path


def unlinked_path(value):
    path=Path(value).absolute()
    if '..' in path.parts: raise ValueError('Parent traversal forbidden')
    for component in (path,*path.parents):
        if component.is_symlink() or component.is_junction():
            raise ValueError('Linked path forbidden')
    # Windows 8.3 spellings are aliases, not symlinks/junctions.
    return path.resolve()
