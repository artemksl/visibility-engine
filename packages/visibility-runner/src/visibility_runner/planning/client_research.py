"""Pure local cell and per-branch planning, without London catalog dependencies."""
from __future__ import annotations
from collections import defaultdict
from collections.abc import Mapping
import json

from ..client_research import (CLIENT_RESEARCH_PROMPT_VERSION, CLIENT_RESEARCH_SCOPES,
    IDENTIFICATION_CAPABILITY, RECOMMENDATION_CAPABILITY, _host, normalise_identity)
from ..config import MeasurementConfig, COPILOT_BROWSER_MEASUREMENT_MODE
from .hashing import canonical_sha256, json_value, text_sha256

SCHEMAS = ('client_research.scope.v1','client_research.prompts.v1','client_research.observations.v1')


def is_research(value):
    return value in CLIENT_RESEARCH_SCOPES


def _source_text(config, branch):
    values = {key:config[key] for key in ('city','country_name','language')}
    values.update({key:json.dumps(branch[key], ensure_ascii=False) for key in ('branch_name','account_name')})
    values['area'] = branch['area']
    values['account_phrase'] = '' if normalise_identity(branch['branch_name']) == normalise_identity(branch['account_name']) else ' of ' + values['account_name']
    text = config['source_template'].format(**values)
    if branch.get('source_prompt') is not None and branch['source_prompt'] != text:
        raise ValueError('supplied source_prompt differs from rendered frozen template')
    return text


def prompt_rows(scope):
    config = scope['client_research']
    direct = scope['scope_kind']==CLIENT_RESEARCH_SCOPES[0]
    rows = []
    hosts = defaultdict(list)
    accounts = defaultdict(list)
    if direct:
        for branch in config['branches']:
            hosts[_host(branch['website'])].append(branch['branch_id'])
            accounts[branch['account_id']].append(branch['branch_id'])
    for item in config['branches'] if direct else config['cells']:
        if direct:
            platform = config['source_provider']
            text = _source_text(config,item)
            reference = {key:item[key] for key in ('account_id','account_name','branch_id','branch_name','area','address','website')}
            reference.update({key:config[key] for key in ('city','country_code','country_name')})
            context = json_value(item.get('ambiguity_context') or {})
            host = _host(item['website'])
            context['shared_hosts'] = sorted(set(context.get('shared_hosts', ())) | {key for key,values in hosts.items() if key and len(values)>1})
            context['website_host_shared'] = bool(context.get('website_host_shared')) or host in context['shared_hosts'] or len((context.get('host_index') or {}).get(host, ()))>1
            context['account_branch_ids'] = sorted(set(context.get('account_branch_ids', ())) | set(accounts[item['account_id']]))
            identity = {'capability':IDENTIFICATION_CAPABILITY,'branch_id':item['branch_id'],'text':text}
            metadata = {'branch_id':item['branch_id'],'account_id':item['account_id'],
                        'reference_identity':reference,'ambiguity_context':context,
                        'source_provider':platform,'judge_provider':config['judge_provider'],
                        **{key:item[key] for key in ('identity_fingerprint','matching_fingerprint','direct_fingerprint') if key in item}}
            platforms = (platform,)
        else:
            text = config['recommendation_template'].format(**{**{key:config[key] for key in ('city','country_name','language')},
                                                              **{key:item[key] for key in ('area','treatment_label')}})
            identity = {'capability':RECOMMENDATION_CAPABILITY,'cell_id':item['cell_id'],'text':text}
            metadata = {key:item[key] for key in ('cell_id','area_id','area','treatment_id','treatment_label')}
            metadata['query_fingerprint'] = item.get('query_fingerprint') or canonical_sha256({'cell':item,'text':text,'platforms':scope['platforms'],'policy':RECOMMENDATION_CAPABILITY})
            platforms = scope['platforms']
        semantic = 'client-' + canonical_sha256(identity)[:24]
        for platform in platforms:
            prompt_id = semantic + '-' + platform
            rows.append({'prompt_id':prompt_id,'semantic_prompt_id':semantic,'platform':platform,'text':text,
                'prompt_text_sha256':text_sha256(text),'collection_scope_id':semantic,'prompt_intent':'client_research',
                'client_research_capability':IDENTIFICATION_CAPABILITY if direct else RECOMMENDATION_CAPABILITY,
                'report_eligible':False,**metadata})
    return rows


def prompt_hash_payload(scope, rows):
    return {'schema_version':SCHEMAS[1],'snapshot_id':scope['snapshot_id'],'prompt_pack_version':CLIENT_RESEARCH_PROMPT_VERSION,
            'report_eligible':False,'prompts':rows}


def freeze(config, *, snapshot_id):
    measurement = config if isinstance(config, MeasurementConfig) else MeasurementConfig.from_mapping(config)
    scope = {'schema_version':SCHEMAS[0],'scope_kind':measurement.scope_kind,'snapshot_id':snapshot_id,
             'client_research':json_value(measurement.client_research),'platforms':[p.value for p in measurement.platforms],
             'platform_data_modes':{p.value:COPILOT_BROWSER_MEASUREMENT_MODE if p.value=='copilot' else 'browser_same_chat_v1' for p in measurement.platforms},
             'planned_runs':1,'test_mode':False,'outside_in':measurement.outside_in,
             'prompt_pack_version':CLIENT_RESEARCH_PROMPT_VERSION,'execution_policy_version':measurement.execution_policy_version,
             'publication_eligible':False}
    rows = prompt_rows(scope)
    scope['prompt_pack_hash'] = canonical_sha256(prompt_hash_payload(scope,rows))
    scope['scope_hash'] = canonical_sha256(scope)
    return scope,rows


def validate(scope):
    expected,rows = freeze({'scope_kind':scope.get('scope_kind'),'client_research':scope.get('client_research'),
                           'platforms':scope.get('platforms'),'outside_in':scope.get('outside_in'),
                           'repetitions':scope.get('planned_runs'),'prompt_catalog_version':scope.get('prompt_pack_version'),
                           'execution_policy_version':scope.get('execution_policy_version')},snapshot_id=scope.get('snapshot_id'))
    if json_value(scope)!=expected:
        raise ValueError('client research scope differs from frozen contract')
    return rows


def observation_plan(scope, prompts):
    rows = []
    for prompt in prompts['prompts']:
        row = {key:json_value(value) for key,value in prompt.items() if key!='text'}
        row.update({'observation_id':'obs-' + canonical_sha256({'snapshot_id':scope['snapshot_id'],'prompt_id':prompt['prompt_id']})[:24],
                    'scope_id':prompt['collection_scope_id'],'scope_hash':scope['scope_hash'],
                    'prompt_pack_hash':scope['prompt_pack_hash'],'prompt_text':prompt['text'],'run':1,
                    'access_mode':'browser','model_or_surface':prompt['platform'],'location_context':{},
                    'started_at':None,'collected_at':None,'status':None,'raw_answer_reference':None,
                    'raw_answer_sha256':None,'citations':[],'attempts':0})
        rows.append(row)
    payload = {'schema_version':SCHEMAS[2],'snapshot_id':scope['snapshot_id'],'scope_hash':scope['scope_hash'],
               'prompt_pack_hash':scope['prompt_pack_hash'],'prompt_pack_version':CLIENT_RESEARCH_PROMPT_VERSION,'observations':rows}
    return {**payload,'observation_plan_hash':canonical_sha256(payload),'planned_observations':len(rows)}


def bind_profiles(frozen, profiles):
    """Bind the exact per-role profiles before sealing artifacts and packets."""
    scope = frozen['scope_manifest'];config=scope['client_research']
    old_rows=frozen['observation_plan']['observations'];rows=[]
    direct=scope['scope_kind']==CLIENT_RESEARCH_SCOPES[0]
    for row in old_rows:
        if direct:
            assignments={role:profiles[int(canonical_sha256([config['workspace_id'],row['branch_id'],role]),16)%len(profiles)]
                         for role in ('source','judge')}
            row={**row,'profile_id':assignments['source'],'judge_profile_id':assignments['judge']}
            row['runner_direct_fingerprint']=canonical_sha256({'identity':row['reference_identity'],'ambiguity':row['ambiguity_context'],
                'prompt':row['prompt_text'],'source_provider':row['source_provider'],'judge_provider':row['judge_provider'],
                'source_profile':row['profile_id'],'judge_profile':row['judge_profile_id'],'policy':IDENTIFICATION_CAPABILITY,
                'host_fingerprint':row.get('direct_fingerprint')})
            row.setdefault('direct_fingerprint', row['runner_direct_fingerprint'])
            rows.append(row)
        else:
            for profile in profiles:
                rows.append({**row,'profile_id':profile,'observation_id':'obs-'+canonical_sha256({'base':row['observation_id'],'profile':profile})[:24]})
    payload={key:value for key,value in frozen['observation_plan'].items() if key not in {'observations','observation_plan_hash','planned_observations'}}
    payload['observations']=rows
    frozen['observation_plan']={**payload,'observation_plan_hash':canonical_sha256(payload),'planned_observations':len(rows)}
    return frozen
