"""Frozen visibility execution settings; original plans remain immutable."""
from __future__ import annotations
import json
from pathlib import Path
from ..errors import IntegrityError, StateTransitionError
from ..planning.hashing import canonical_sha256
from ..recovery.durable import publish_json

FILENAME='execution-policy.json'


def _driver_contract(runtime):
    browser = runtime.browser
    if not browser.has_driver_contract:
        return None
    contract = {
        'driver': browser.driver.value,
        'driver_version': browser.driver_version,
        'expected_browser_version': browser.expected_browser_version,
        'driver_experiment': browser.driver_experiment,
    }
    if browser.driver_experiment:
        contract.update(cdp_url=browser.cdp_url, user_data_dir=str(browser.user_data_dir))
    return contract


def make_policy(runtime, plan, *, source):
    settings={key:getattr(runtime,key) for key in (
        'max_concurrency','tabs_per_provider','packet_start_stagger_seconds',
        'worker_timeout_seconds','cleanup_timeout_seconds','cancellation_settlement_seconds')}
    settings['interaction_min_delay_seconds']=runtime.browser.interaction_min_delay_seconds
    settings['interaction_max_delay_seconds']=runtime.browser.interaction_max_delay_seconds
    settings['selected_profile_ids']=list(runtime.selected_profile_ids)
    settings['profile_manifest_sha256']=runtime.profile_manifest_sha256
    if runtime.browser.copilot_verification_policy != 'deadline_cleanup_v1':
        settings['copilot_verification'] = {
            'policy': runtime.browser.copilot_verification_policy,
            'background_only': runtime.browser.background_only,
            'guard_namespace': str(runtime.browser.user_data_dir or 'default-chrome-user-data'),
            'lease_lock_root': str(runtime.profiles.lease_lock_root) if runtime.profiles.lease_lock_root is not None else None,
        }
    if (plan.get('measurement_config') or {}).get('github_copilot'):
        from ..planning.hashing import json_value
        settings['github_copilot']=json_value(getattr(runtime,'github_copilot',None))
    driver_contract = _driver_contract(runtime)
    if driver_contract is not None:
        settings['browser_driver'] = driver_contract
    body={'schema_version':2 if driver_contract is not None else 1,'execution_control_required':True,'plan_sha256':str(plan['plan_sha256']),'settings':settings,'source':source}
    return {**body,'policy_sha256':canonical_sha256(body)}


def read_policy(repository,run_id,plan,*,runtime=None,state=None):
    path=repository.run_dir(run_id)/FILENAME
    required=(state or {}).get('execution_policy_sha256')
    try:
        value=json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise IntegrityError('frozen_execution_policy_missing') from None
        # No file means there is no evidence of a selected experimental driver.
        # Do not let legacy attestation silently turn stock history into a new arm.
        if runtime is not None and _driver_contract(runtime) is not None:
            raise StateTransitionError('frozen_execution_policy_mismatch') from None
        if runtime is not None and runtime.browser.copilot_verification_policy != 'deadline_cleanup_v1':
            raise StateTransitionError('frozen_execution_policy_mismatch') from None
        return make_policy(runtime,plan,source='legacy_runtime_attestation') if runtime is not None else None
    except (OSError,ValueError,TypeError) as exc:
        raise IntegrityError('frozen_execution_policy_unreadable') from exc
    if not isinstance(value,dict) or type(value.get('schema_version')) is not int or value.get('schema_version') not in (1, 2):
        raise IntegrityError('frozen_execution_policy_invalid')
    digest=canonical_sha256({key:item for key,item in value.items() if key!='policy_sha256'})
    if value.get('policy_sha256')!=digest or value.get('plan_sha256')!=plan['plan_sha256'] or (required and required!=digest):
        raise IntegrityError('frozen_execution_policy_hash_mismatch')
    # Interpret absent driver identity only after validating the original hash.
    # Schema 1 remains byte-for-byte the original stock policy, including when
    # projected into a recovery manifest; no normalized fields are added here.
    settings = value.get('settings')
    if not isinstance(settings, dict):
        raise IntegrityError('frozen_execution_policy_invalid')
    if 'copilot_verification' in settings:
        verification = settings['copilot_verification']
        if (not isinstance(verification, dict) or set(verification) != {'policy', 'background_only', 'guard_namespace', 'lease_lock_root'}
                or verification['policy'] not in {'manual_hold_v1', 'automatic_hold_v1'}
                or type(verification['background_only']) is not bool
                or not isinstance(verification['guard_namespace'], str) or not verification['guard_namespace']
                or (verification['lease_lock_root'] is not None and
                    (not isinstance(verification['lease_lock_root'], str) or not verification['lease_lock_root']))):
            raise IntegrityError('frozen_execution_policy_invalid')
    if value['schema_version'] == 1 and 'browser_driver' in settings:
        raise IntegrityError('frozen_execution_policy_invalid')
    if value['schema_version'] == 2:
        contract = settings.get('browser_driver')
        if not isinstance(contract, dict) or not {
            'driver', 'driver_version', 'expected_browser_version', 'driver_experiment'
        }.issubset(contract):
            raise IntegrityError('frozen_execution_policy_invalid')
        from ..config import BrowserConfig
        from ..errors import ConfigurationError
        try:
            configured = BrowserConfig.from_mapping(contract)
        except ConfigurationError as exc:
            raise IntegrityError('frozen_execution_policy_invalid') from exc
        if not configured.has_driver_contract:
            raise IntegrityError('frozen_execution_policy_invalid')
    if runtime is not None and value.get('settings')!=make_policy(runtime,plan,source=value.get('source'))['settings']:
        raise StateTransitionError('frozen_execution_policy_mismatch')
    return value


def publish_policy(repository,run_id,policy,owner):
    owner.assert_owned()
    path=repository.run_dir(run_id)/FILENAME
    if not (repository.run_dir(run_id)/'execution-control.json').exists():
        state=repository.read_progress(run_id)
        repository.write_execution_control(run_id,state)
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8"))!=policy:
            raise IntegrityError('frozen_execution_policy_changed')
    else:
        publish_json(path,policy,exclusive=True)
    def bind(state):
        owner.assert_owned()
        prior=state.get('execution_policy_sha256')
        if prior and prior!=policy['policy_sha256']:
            raise IntegrityError('frozen_execution_policy_changed')
        state['execution_policy_sha256']=policy['policy_sha256']
    if repository.read_progress(run_id).get('execution_policy_sha256')!=policy['policy_sha256']:
        repository.update_state(run_id,bind)
