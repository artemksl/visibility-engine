"""Durable browser source → different browser judge, with independent fences.

Both stages use the ordinary guarded website send path. A saved source is reused
verbatim for judge retries and recovery; an uncertain stage is never resubmitted.
"""
from __future__ import annotations
import asyncio

from ..client_research import (identification_judge_prompt, validate_identification_judgment,
                              IDENTIFICATION_CAPABILITY, MAX_SOURCE_ANSWER_BYTES, _json_object)
from ..contract_primitives import text_sha256
from ..errors import AmbiguousSendError, IntegrityError
from ..names.check_chat_names import MentionExtractionError
from ..storage.send_attempts import SendAttemptStore, SendAttemptState
from ..websites.chat_actions import BrowserIdentity


def _records(worker, run_id, item):
    with SendAttemptStore(worker.repository.run_dir(run_id)/'fences',watch_changes=False) as store:
        return sorted(store.snapshot(observation_ids=(item,)).records,key=lambda r:(r.prepared_at,r.normalized_attempt_ordinal,r.fence_id))


def _result_attempts(worker, run_id, item, *, accepted_attempt, accepted_attempts):
    from .run_measurement_group import _attempt_result_metadata
    # Accepted source evidence deliberately survives into later passes. The
    # current pass's dispatch filter must not remove its original fence IDs.
    with SendAttemptStore(worker.repository.run_dir(run_id)/'fences',watch_changes=False) as store:
        return _attempt_result_metadata(store, item, accepted_attempt=accepted_attempt,
                                        accepted_attempts=accepted_attempts)


def _read(worker, run_id, record):
    path=worker.repository.run_dir(run_id)/record.response_reference
    ref={'path':record.response_reference,'sha256':record.response_sha256,'byte_length':path.stat().st_size,'media_type':'text/plain; charset=utf-8'}
    return worker.repository.read_evidence(run_id,ref).decode('utf-8'),ref


def _failed_before_send(worker, record):
    context=worker.recovery
    if context is not None:
        attempt=context.attempts.get(record.fence_id)
        if attempt is not None:
            context.coordinator.finish_attempt(attempt.attempt_id,'failed',context.owner,reason_code='client_identification_pre_send_failed')


def _all_uncertain(worker, run_id, item):
    with SendAttemptStore(worker.repository.run_dir(run_id)/'fences',watch_changes=False) as store:
        return tuple(r for r in store.iter_records() if r.action in {'identification_source','identification_judge'}
                     and r.observation_id != item and r.state in {SendAttemptState.SEND_STARTED,SendAttemptState.RESPONSE_CAPTURED})


async def _stage(worker, run_id, observation, plan, packet, *, action, provider, profile_id, prompt, validator):
    from .run_measurement_group import _retry_policy, _send_attempt_id, _ConfirmedNoResponse, _record_authorizes_retry
    item=observation['observation_id'];fences=worker._send_attempts(run_id);policy=_retry_policy(plan)
    records=[r for r in _records(worker,run_id,item) if r.action==action]
    accepted=[r for r in records if r.state is SendAttemptState.ACKNOWLEDGED]
    if accepted:
        text,ref=_read(worker,run_id,accepted[-1]);validator(text)
        return accepted[-1],text,ref
    if any(r.state in {SendAttemptState.SEND_STARTED,SendAttemptState.RESPONSE_CAPTURED} for r in records):
        raise AmbiguousSendError('client_identification_original_response_unresolved')
    logical=worker.recovery.logical_id(item,action) if worker.recovery else f'{item}:{action}'
    current=[r for r in records if r.normalized_logical_turn_id==logical]
    driver=worker.providers.require(provider)
    directory=next(row['chrome_directory'] for row in plan['selected_profiles'] if row['id']==profile_id)
    last=None
    if current and current[-1].state is SendAttemptState.CONFIRMED_NO_RESPONSE and not _record_authorizes_retry(current[-1]):
        raise MentionExtractionError('client_identification_nonretryable_response')
    while len(current)<policy.authorized_max_attempts:
        if _all_uncertain(worker,run_id,item):
            raise AmbiguousSendError('client_identification_peer_response_unresolved')
        ordinal=len(current)+1
        submission=await worker._open_website_page(run_id,lambda:driver.open(profile_id=profile_id,chrome_directory=directory,observation_id=item,fresh=True))
        worker._observation_providers[item]=provider
        record=fences.prepare(fence_id=_send_attempt_id(logical,ordinal),observation_id=item,action=action,
            prompt_sha256=text_sha256(prompt),identity={**submission.identity.to_mapping(), 'platform':provider},logical_turn_id=logical,
            attempt_ordinal=ordinal,observation_attempt_ordinal=ordinal,retried_from=current[-1].fence_id if current else None)
        try:
            try:
                text,ref,identity=await worker._send(run_id,fences,record,submission,prompt,packet_id=packet['packet_id'],
                                                   acknowledge=False,policy=policy,allow_fresh_retry=ordinal<policy.authorized_max_attempts)
            except _ConfirmedNoResponse as exc:
                if exc.terminal_nonretryable or not _record_authorizes_retry(fences.load(record.fence_id)):
                    raise MentionExtractionError("client_identification_nonretryable_response") from exc
                current.append(fences.load(record.fence_id));last='client_identification_no_response'
                continue
            except BaseException:
                updated=fences.load(record.fence_id)
                if updated.state is SendAttemptState.PRE_SEND_FAILED:
                    _failed_before_send(worker,updated);current.append(updated)
                    if len(current)<policy.authorized_max_attempts:
                        continue
                raise
            try:
                validator(text)
            except MentionExtractionError as exc:
                terminal=fences.acknowledge(record.fence_id,validation_status='rejected',detail=str(exc),identity=identity.to_mapping())
                current.append(terminal);last=str(exc)
                if len(current)<policy.authorized_max_attempts:
                    continue
                return terminal,text,ref
            terminal=fences.acknowledge(record.fence_id,validation_status='collected' if action=='identification_source' else 'accepted',identity=identity.to_mapping())
            return terminal,text,ref
        finally:
            # Provider close only releases this owned submission; retained
            # challenges/uncertain effects remain governed by runner cleanup.
            await submission.close()
    if current and current[-1].state is SendAttemptState.REJECTED:
        text,ref=_read(worker,run_id,current[-1]);return current[-1],text,ref
    raise MentionExtractionError(last or 'client_identification_attempts_exhausted')


def _validate_source(text):
    if not isinstance(text,str) or not text.strip():
        raise MentionExtractionError('source_answer_uncaptured')
    if len(text.encode('utf-8'))>MAX_SOURCE_ANSWER_BYTES:
        raise MentionExtractionError('source_answer_too_large')


async def execute_observation(worker, run_id, observation, *, plan, packet, resume):
    from .run_measurement_group import _issue
    source,answer,answer_ref=await _stage(worker,run_id,observation,plan,packet,action='identification_source',
        provider=observation['source_provider'],profile_id=observation['profile_id'],prompt=observation['prompt_text'],validator=_validate_source)
    if source.state is SendAttemptState.REJECTED:
        return {'observation_id':observation['observation_id'],'platform':observation['platform'],'status':'partial',
            'collection_status':'failed','extraction_status':'failed','grounded_occurrences':[],
            'artifacts':{'turn_1_response':answer_ref},'issues':[_issue(observation['observation_id'],str(source.detail),'Source answer could not be captured in full',recoverable=False)],
            **_result_attempts(worker,run_id,observation['observation_id'],accepted_attempt=source,accepted_attempts={})}
    prompt=identification_judge_prompt(observation['prompt_text'],answer,observation['reference_identity'],observation['ambiguity_context'])
    prompt_ref=worker.repository.write_evidence(run_id,prompt,suffix='.identification-judge-prompt.txt')
    validate=lambda text:validate_identification_judgment(text,answer,observation['reference_identity'],observation['ambiguity_context'],observation['prompt_text'])
    judge,judgment,judgment_ref=await _stage(worker,run_id,observation,plan,packet,action='identification_judge',
        provider=observation['judge_provider'],profile_id=observation['judge_profile_id'],prompt=prompt,validator=validate)
    rejected=judge.state is SendAttemptState.REJECTED
    config=plan['scope_manifest']['client_research']
    metadata={key:config[key] for key in ('workspace_id','directory_revision','config_revision')}
    metadata.update({key:observation[key] for key in ('branch_id','account_id','source_provider','judge_provider','profile_id','judge_profile_id','direct_fingerprint')})
    metadata.update({key:observation[key] for key in ('identity_fingerprint','matching_fingerprint') if key in observation})
    metadata.update(capability=IDENTIFICATION_CAPABILITY,source_prompt_sha256=text_sha256(observation['prompt_text']),
        source_answer_sha256=text_sha256(answer),judge_request_sha256=text_sha256(prompt),judge_answer_sha256=text_sha256(judgment))
    if not rejected:
        accepted=validate(judgment)
        metadata['judgment']=_json_object(judgment)
        metadata['acceptance_decision']=accepted['decision']
        metadata['acceptance_reason_code']=accepted['reason_code']
    return {'observation_id':observation['observation_id'],'platform':observation['platform'],
        'status':'partial' if rejected else 'succeeded','collection_status':'collected','extraction_status':'rejected' if rejected else 'complete',
        'grounded_occurrences':[],'artifacts':{'turn_1_response':answer_ref,'turn_2_response':judgment_ref,'judge_prompt':prompt_ref},
        'client_research':metadata,'issues':[_issue(observation['observation_id'],'identification_judgment_rejected','Judge response failed grounded validation',recoverable=False)] if rejected else [],
        **_result_attempts(worker,run_id,observation['observation_id'],accepted_attempt=source if rejected else judge,
                                   accepted_attempts={'identification_source':source.fence_id,**({'identification_judge':judge.fence_id} if not rejected else {})})}


async def reconcile_identification(worker, context, observation, packet, records, uncertain):
    """Only retrieve the exact pending stage; never open a replacement or send."""
    if not uncertain:
        return True
    run_id=context.snapshot.manifest.run_id;item=observation['observation_id']
    with SendAttemptStore(worker.repository.run_dir(run_id)/'fences',watch_changes=False) as fences:
        for original in uncertain:
            attempt=context.coordinator.begin_attempt(context.pass_id,f'{item}:{original.action}',context.owner,
                                                       action='reconcile',predecessor_ids=(original.fence_id,))
            submission=None;effect=None;retrieval_completed=False
            try:
                current=fences.load(original.fence_id)
                prompt=observation['prompt_text']
                source_text=None
                if original.action=='identification_judge':
                    source=next(r for r in reversed(records) if r.action=='identification_source' and r.state is SendAttemptState.ACKNOWLEDGED)
                    source_text,_=_read(worker,run_id,source)
                    prompt=identification_judge_prompt(prompt,source_text,observation['reference_identity'],observation['ambiguity_context'])
                if text_sha256(prompt)!=original.prompt_sha256:
                    raise IntegrityError('identification_recovery_prompt_mismatch')
                if current.state is SendAttemptState.RESPONSE_CAPTURED:
                    text,ref=_read(worker,run_id,current);identity=current.identity
                else:
                    binding=context.correlation(original.fence_id)
                    provider=observation['judge_provider'] if original.action=='identification_judge' else observation['source_provider']
                    driver=worker.providers.require(provider)
                    if not binding or not callable(getattr(driver,'reopen',None)):
                        raise IntegrityError('identification_conversation_binding_missing')
                    effect=context.coordinator.admit_effect(context.pass_id,f'{item}:{original.action}','retrieve_existing',
                        context.operations[f'{item}:{original.action}'].input_digest,context.owner,attempt_id=attempt.attempt_id)
                    context.coordinator.mark_dispatched(effect.effect_id,context.owner)
                    slot=await worker._acquire_page_open_slot(run_id, context.page_open_limit)
                    try:
                        submission=await worker._await_with_cancellation(run_id,driver.reopen(binding,observation_id=item),detail='identification_reopen_cancelled')
                    finally:
                        slot.release()
                    submission.restore_recovery_attempt(prompt=prompt,response_baseline=binding['response_baseline'],binding=binding)
                    outcome=await worker._await_with_cancellation(run_id,submission.reconcile_no_response(context.operations[f'{item}:{original.action}'].deadline_seconds),detail='identification_reconcile_cancelled')
                    answer=getattr(outcome,'answer',None)
                    if answer is None:
                        raise AmbiguousSendError('identification_original_response_unresolved')
                    text=answer.text;identity=answer.identity.to_mapping()
                    expected=BrowserIdentity.from_mapping(binding['identity'])
                    canonical=getattr(driver,'canonical_conversation_reference',lambda value:value)
                    if (expected.profile_id != answer.identity.profile_id
                            or expected.chrome_directory != answer.identity.chrome_directory
                            or not expected.conversation_reference
                            or canonical(expected.conversation_reference) != canonical(answer.identity.conversation_reference)):
                        raise IntegrityError('identification_recovery_conversation_mismatch')
                    submission.identity.assert_same_tab(answer.identity,require_same_conversation=True,canonical=canonical)
                    ref=worker.repository.write_evidence(run_id,text,suffix='.identification-recovered.txt')
                    fences.capture_response(original.fence_id,response_reference=ref['path'],response_sha256=ref['sha256'])
                    context.coordinator.record_effect_outcome(effect.effect_id,'succeeded',context.owner)
                    retrieval_completed=True
                try:
                    if original.action=='identification_source':
                        _validate_source(text);validation='collected'
                    else:
                        validate_identification_judgment(text,source_text,observation['reference_identity'],observation['ambiguity_context'],observation['prompt_text']);validation='accepted'
                except MentionExtractionError:
                    validation='rejected'
                record=fences.acknowledge(original.fence_id,validation_status=validation,identity=identity)
                context.resolve_original_submission(original,attempt,ref)
                context.coordinator.finish_attempt(attempt.attempt_id,'completed',context.owner)
                context.recovered.add(original.fence_id)
                records=[record if r.fence_id==original.fence_id else r for r in records]
            except asyncio.CancelledError:
                raise
            except Exception:
                if effect is not None and not retrieval_completed:
                    context.coordinator.record_effect_outcome(effect.effect_id,'unknown',context.owner)
                context.coordinator.finish_attempt(attempt.attempt_id,'failed',context.owner,reason_code='identification_original_unresolved')
                context.failure(item,'identification_original_unresolved',checked=(original.fence_id,))
                return False
            finally:
                if submission is not None:
                    await submission.close()
    return True
