"""Copy Octo's supported fingerprint settings without profile/session secrets."""
from copy import deepcopy
import json

from automation_core.storage.models import NeedsAttention


FIELDS = frozenset({
    'os', 'os_arch', 'os_version', 'user_agent', 'screen', 'cpu', 'ram',
    'renderer', 'fonts', 'dns', 'noise', 'media_devices', 'languages',
    'timezone', 'geolocation', 'webrtc', 'device_type', 'device_model',
})


class OctoRequestRejected(NeedsAttention):
    """Octo explicitly refused the request; the mutation was not accepted."""


def fingerprint_payload(value):
    """Validate a creation payload or copy the writable fields of GET detail."""
    message = 'The saved Octo fingerprint could not be transferred.'
    if (type(value) is not dict or set(value) - FIELDS - {'user_agent_changed_by_user'}
            or type(value.get('os')) is not str or value['os'] not in {'win', 'mac', 'android'}):
        raise NeedsAttention(message)
    result = {key: deepcopy(item) for key, item in value.items() if key in FIELDS}
    try:
        if len(json.dumps(result, allow_nan=False).encode()) > 16384:
            raise ValueError()
        for key in ('os_arch', 'os_version', 'user_agent', 'screen', 'renderer', 'device_type', 'device_model'):
            if key in result and (type(result[key]) is not str or not result[key]
                    or len(result[key]) > 1024 or any(ord(c) < 32 for c in result[key])):
                raise ValueError()
        if 'os_arch' in result and result['os_arch'] not in {'x86', 'arm'}:
            raise ValueError()
        for key in ('cpu', 'ram'):
            if key in result and (type(result[key]) is not int or not 1 <= result[key] <= 256):
                raise ValueError()
        if 'fonts' in result and (type(result['fonts']) is not list
                or any(type(font) is not str or not font for font in result['fonts'])):
            raise ValueError()
        if 'dns' in result and result['dns'] is not None and type(result['dns']) is not str:
            raise ValueError()
        for key in ('languages', 'timezone', 'geolocation', 'webrtc'):
            if key not in result:
                continue
            config = result[key]
            allowed = {'ip', 'real', 'manual'} if key != 'webrtc' else {'ip', 'real', 'disable_non_proxied_udp'}
            if type(config) is not dict or set(config) - {'type', 'data'} or config.get('type') not in allowed:
                raise ValueError()
            # GET includes null data for automatically resolved settings.
            if config.get('data') is None:
                config.pop('data', None)
                if config['type'] == 'manual':
                    raise ValueError()
        if 'noise' in result and (type(result['noise']) is not dict
                or set(result['noise']) - {'webgl', 'canvas', 'audio', 'client_rects'}
                or any(type(flag) is not bool for flag in result['noise'].values())):
            raise ValueError()
        if 'media_devices' in result and (type(result['media_devices']) is not dict
                or set(result['media_devices']) - {'video_in', 'audio_in', 'audio_out'}
                or any(type(n) is not int or not 0 <= n <= 5 for n in result['media_devices'].values())):
            raise ValueError()
    except (TypeError, ValueError, RecursionError):
        raise NeedsAttention(message) from None
    return result
