"""Bind accepted research results to exact frozen plans, sends and artifacts."""
from __future__ import annotations
from ..client_research import (IDENTIFICATION_CAPABILITY, validate_identification_judgment,
                              validate_recommendation_extraction, identification_judge_prompt, _json_object)
from ..contract_primitives import json_value, text_sha256
from ..errors import IntegrityError
from ..storage.send_attempts import SendAttemptStore, SendAttemptState
from ..names.check_chat_names import find_name_positions, MentionExtractionError


def validate_client_result(repository, run_id, observation, result, plan, attempt_records=None):
    if result.get('platform') != observation['platform']:
        raise IntegrityError('client_research_result_platform_mismatch')
    if result.get('status') not in {'succeeded','partial'} or not result.get('client_research'):
        if result.get('status')=='succeeded':
            raise IntegrityError('client_research_result_provenance_missing')
        return result
    metadata=result['client_research'];config=plan['scope_manifest']['client_research'];direct=observation['client_research_capability']==IDENTIFICATION_CAPABILITY
    expected={key:config[key] for key in ('workspace_id','directory_revision','config_revision')}
    expected['capability']=observation['client_research_capability']
    keys=('branch_id','account_id','source_provider','judge_provider','profile_id','judge_profile_id','direct_fingerprint','identity_fingerprint','matching_fingerprint') if direct else ('cell_id','area_id','treatment_id','query_fingerprint','profile_id')
    expected.update({key:observation[key] for key in keys if key in observation})
    if any(metadata.get(key)!=value for key,value in expected.items()):
        raise IntegrityError('client_research_result_provenance_mismatch')
    if attempt_records is None:
        with SendAttemptStore(repository.run_dir(run_id)/'fences',watch_changes=False) as store:
            attempts={r.fence_id:r for r in store.snapshot(observation_ids=(observation['observation_id'],)).records}
    else:
        attempts=attempt_records
    actions=('identification_source','identification_judge') if direct else ('measurement_turn_1','same_chat_turn_2')
    accepted=result.get('accepted_attempt_ids') or {};source=attempts.get(accepted.get(actions[0]))
    if source is None or source.state is not SendAttemptState.ACKNOWLEDGED or source.observation_id!=observation['observation_id'] or source.action!=actions[0] or source.prompt_sha256!=text_sha256(observation['prompt_text']):
        raise IntegrityError('client_research_source_send_binding_missing')
    artifacts=result.get('artifacts') or {};source_ref=artifacts.get('turn_1_response') or {}
    if source_ref.get('path')!=source.response_reference or source_ref.get('sha256')!=source.response_sha256:
        raise IntegrityError('client_research_source_artifact_mismatch')
    answer=repository.read_evidence(run_id,source_ref).decode('utf-8')
    if metadata.get('source_prompt_sha256')!=text_sha256(observation['prompt_text']) or metadata.get('source_answer_sha256')!=text_sha256(answer):
        raise IntegrityError('client_research_source_hash_mismatch')
    if result['status']!='succeeded':
        return result
    extraction=attempts.get(accepted.get(actions[1]));extraction_ref=artifacts.get('turn_2_response') or {}
    if extraction is None or extraction.state is not SendAttemptState.ACKNOWLEDGED or extraction.action!=actions[1] or extraction.observation_id!=observation['observation_id'] or extraction_ref.get('path')!=extraction.response_reference or extraction_ref.get('sha256')!=extraction.response_sha256:
        raise IntegrityError('client_research_extraction_send_binding_missing')
    response=repository.read_evidence(run_id,extraction_ref).decode('utf-8')
    source_identity=source.ack_identity or source.identity;judge_identity=extraction.ack_identity or extraction.identity
    if source_identity.get('profile_id')!=observation['profile_id']:
        raise IntegrityError('client_research_source_profile_mismatch')
    try:
        if direct:
            prompt=identification_judge_prompt(observation['prompt_text'],answer,observation['reference_identity'],observation['ambiguity_context'])
            judgment=validate_identification_judgment(response,answer,observation['reference_identity'],observation['ambiguity_context'],observation['prompt_text'])
            if (source.identity.get('platform')!=observation['source_provider']
                    or extraction.identity.get('platform')!=observation['judge_provider']
                    or judge_identity.get('profile_id')!=observation['judge_profile_id']
                    or observation['source_provider']==observation['judge_provider']
                    or extraction.prompt_sha256!=text_sha256(prompt)):
                raise IntegrityError('client_research_judge_binding_mismatch')
            prompt_ref=artifacts.get('judge_prompt') or {}
            if repository.read_evidence(run_id,prompt_ref).decode('utf-8')!=prompt:
                raise IntegrityError('client_research_judge_prompt_artifact_mismatch')
            if (metadata.get('judgment')!=_json_object(response)
                    or metadata.get('acceptance_decision')!=judgment['decision']
                    or metadata.get('acceptance_reason_code')!=judgment['reason_code']
                    or metadata.get('judge_request_sha256')!=text_sha256(prompt)
                    or metadata.get('judge_answer_sha256')!=text_sha256(response)):
                raise IntegrityError('client_research_judgment_mismatch')
        else:
            from ..client_research import RECOMMENDATION_EXTRACTION_PROMPT
            if extraction.prompt_sha256!=text_sha256(RECOMMENDATION_EXTRACTION_PROMPT) or any(source_identity.get(key)!=judge_identity.get(key) for key in ('profile_id','chrome_directory','browser_context_reference','tab_reference','conversation_reference')):
                raise IntegrityError('client_research_same_chat_binding_mismatch')
            value=validate_recommendation_extraction(response,answer)
            if metadata.get('extraction')!=value or result.get('grounded_occurrences')!=find_name_positions(answer,[row['name'] for row in value['recommendations']]):
                raise IntegrityError('client_research_recommendation_mismatch')
    except MentionExtractionError as exc:
        raise IntegrityError('client_research_grounded_validation_failed') from exc
    return result
