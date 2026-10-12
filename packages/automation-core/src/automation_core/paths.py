"""Workspace locations shared by local and packaged applications."""
import os
from pathlib import Path

def workspace_root():
    configured = os.environ.get('ACCOUNT_GLUING_WORKSPACE')
    return Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parent.parent
