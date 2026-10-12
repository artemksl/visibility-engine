"""Synthetic local inputs: no Chrome account or network endpoint is consulted."""
import json
from visibility_panel.integrity import canonical_sha256


def _fixture_inputs(tmp_path):
    profiles = [{'id': 'synthetic_offline', 'display_name': 'Synthetic Offline',
                 'chrome_directory': 'Synthetic Profile', 'project_url': ''}]
    manifest = tmp_path / 'synthetic-profiles.json'
    manifest.write_text(json.dumps(profiles))
    return {'artifact_root': str(tmp_path / 'artifacts'),
            'profiles': {'selected_profile_ids': ['synthetic_offline'], 'selected_profiles': profiles,
                         'manifest_path': str(manifest), 'manifest_sha256': canonical_sha256(profiles),
                         'lease_timeout_seconds': 300},
            'browser': {'cdp_url': 'http://127.0.0.1:1', 'launch_policy': 'attach_only'},
            'max_concurrency': 1}, {}
