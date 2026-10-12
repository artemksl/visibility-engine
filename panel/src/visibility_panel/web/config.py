"""Explicit local profile selection; this module never discovers or opens Chrome."""
from pathlib import Path
import re
from urllib.parse import urlsplit

from visibility_panel.paths import root_or_default
from visibility_panel.persistence.json_store import read_json_or_none


def load_settings(root=None):
    value = read_json_or_none(root_or_default(root) / 'config/settings.json')
    if not isinstance(value, dict):
        raise ValueError('Create config/settings.json from its example before testing.')
    allowed = {'selected_profile_ids', 'tabs_per_provider', 'packet_start_stagger_seconds', 'cdp_url', 'user_data_dir'}
    if set(value) - allowed:
        raise ValueError('settings.json contains unsupported fields.')
    ids = value.get('selected_profile_ids')
    if (not isinstance(ids, list) or not ids or any(not isinstance(item, str) or not re.fullmatch(r'[a-z0-9]+(?:_[a-z0-9]+)*', item) for item in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('Select explicit unique profile IDs in config/settings.json.')
    result = {'tabs_per_provider': 2, 'packet_start_stagger_seconds': 10,
              'cdp_url': 'http://127.0.0.1:9222', **value}
    if type(result['tabs_per_provider']) is not int or not 1 <= result['tabs_per_provider'] <= 20:
        raise ValueError('tabs_per_provider must be a whole number from 1 to 20.')
    stagger = result['packet_start_stagger_seconds']
    if isinstance(stagger, bool) or not isinstance(stagger, (int, float)) or not 0 <= stagger <= 300:
        raise ValueError('packet_start_stagger_seconds must be between 0 and 300.')
    try:
        endpoint = urlsplit(result['cdp_url'])
        if (endpoint.scheme != 'http' or endpoint.hostname not in {'localhost', '127.0.0.1'}
            or not endpoint.port or endpoint.username or endpoint.password
            or endpoint.path not in {'', '/'} or endpoint.query or endpoint.fragment):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError('cdp_url must be an HTTP loopback URL with an explicit port.') from None
    if 'user_data_dir' in result:
        directory = result['user_data_dir']
        if not isinstance(directory, str) or not directory.strip() or not Path(directory).expanduser().is_absolute():
            raise ValueError('user_data_dir must be an absolute local Chrome data directory.')
        result['user_data_dir'] = str(Path(directory).expanduser())
    return result


def snapshot_profile_rows(root=None):
    value = read_json_or_none(root_or_default(root) / 'config/profiles.json')
    if not isinstance(value, list) or not value:
        raise ValueError('Create config/profiles.json with explicit Chrome directory bindings.')
    rows, ids, directories = [], set(), set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {'id', 'display_name', 'chrome_directory', 'project_url'}:
            raise ValueError('Profile catalog contains an invalid row.')
        profile_id, directory = item.get('id'), item.get('chrome_directory')
        if (not isinstance(profile_id, str) or not re.fullmatch(r'[a-z0-9]+(?:_[a-z0-9]+)*', profile_id)
                or not isinstance(directory, str) or not directory.strip()
                or '/' in directory or '\\' in directory or directory in {'.', '..'}
                or profile_id in ids or directory in directories):
            raise ValueError('Profile IDs and exact Chrome directories must be unique.')
        display = item.get('display_name', profile_id)
        project = item.get('project_url', '')
        if not isinstance(display, str) or not display.strip() or not isinstance(project, str):
            raise ValueError('Profile names and project URLs must be strings.')
        rows.append({'id': profile_id, 'display_name': display,
                     'chrome_directory': directory, 'project_url': project})
        ids.add(profile_id); directories.add(directory)
    return rows


def selected_profile_ids(root=None):
    return list(load_settings(root)['selected_profile_ids'])


def selected_snapshot_profile_rows(profile_ids, root=None):
    if not isinstance(profile_ids, (list, tuple)) or not profile_ids or len(set(profile_ids)) != len(profile_ids):
        raise ValueError('Select explicit unique profile IDs; empty and all-profile modes are disabled.')
    catalog = {row['id']: row for row in snapshot_profile_rows(root)}
    if any(not isinstance(item, str) or item not in catalog for item in profile_ids):
        raise ValueError('Selected profiles must have exact bindings in config/profiles.json.')
    return [catalog[item] for item in profile_ids]


def github_copilot_defaults(root=None):
    from visibility_panel.execution.copilot_settings import runtime_settings, source_settings
    path = root_or_default(root) / 'config/github-copilot.json'
    if not path.exists():
        return None
    value = read_json_or_none(path)
    try:
        if not isinstance(value, dict) or set(value) != {'schema_version', 'source', 'runtime'} or value['schema_version'] != 'github_copilot.setup.v1':
            raise ValueError
        source = source_settings({'request_policy_version': 'github_copilot.cost_optimized.v1', **value['source']})
        runtime = runtime_settings(value['runtime'], source)
    except (TypeError, ValueError):
        raise ValueError('GitHub Copilot setup is invalid; configure account settings and a Keychain reference only.') from None
    return {**source, 'credential_ref': runtime['credential_ref']}
