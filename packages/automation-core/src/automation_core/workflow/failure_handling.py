from automation_core import contracts as c

HOLD_ERRORS = frozenset({
    'captcha', 'account_security', 'session_expired',
    'resource_access_denied', 'resource_unavailable',
    'business_name_rejected', 'business_portfolio_limit',
    'resource_name_ambiguous', 'unsupported_business_settings',
    'ad_account_creation_unavailable', 'free_boost_post_ineligible',
    'implementation_error', 'deadline_exhausted', 'recovery_exhausted', 'admission_exhausted',
    'identity_mismatch', 'ownership_lost', 'interaction_unsettled', 'storage_unavailable',
    'cancelled', 'input_unavailable', 'input_expired', 'clock_untrusted',
    'login_credentials_required', 'login_credentials_rejected',
    'workflow_incompatible', 'intent_mismatch',
})

_STOPS = {
    'connection_lost': 'navigation', 'network_failed': 'navigation',
    'navigation_failed': 'navigation', 'page_not_ready': 'loading',
    'stale_view': 'unexpected_page', 'prerequisite_unsatisfied': 'unexpected_page',
    'unexpected_dialog': 'unknown_popup', 'dialog_limit': 'unknown_popup',
}

def identify_error(result, observations=()):
    """Classify facts, never exception text or the number of diagnostic proofs."""
    reason = getattr(getattr(result, 'stop_reason', None), 'value', None)
    if reason == 'input_unavailable' and any(
            isinstance(detail := getattr(proof, 'details', proof), c.ObservationDetails)
            and detail.state == 'login_credentials_required' for proof in observations):
        return 'login_credentials_required'
    if reason in HOLD_ERRORS:
        return reason
    found = set()
    for proof in observations:
        detail = getattr(proof, 'details', proof)
        if isinstance(detail, c.ObservationDetails) and detail.state in {
                'human_confirmation_required', 'video_selfie_required', 'captcha'}:
            found.add('captcha' if detail.state == 'captcha' else 'account_security')
        if isinstance(detail, c.ObservationDetails) and detail.state in {
                'business_page_not_ready', 'administrator_page_not_ready', 'page_not_ready', 'rendering'}:
            found.add('loading')
        if isinstance(detail, c.ObservationDetails) and detail.state == 'unknown_popup':
            found.add('unknown_popup')
        elif isinstance(detail, c.ObservationDetails) and detail.state == 'current_page_target_changed':
            found.add('unexpected_page')
        elif isinstance(detail, c.ObservationDetails) and detail.state in {
                'resource_access_denied', 'resource_unavailable', 'business_portfolio_limit', 'business_name_rejected',
                'resource_name_ambiguous', 'unsupported_business_settings',
                'ad_account_creation_unavailable', 'free_boost_post_ineligible', 'login_credentials_rejected'}:
            found.add(detail.state)
        elif isinstance(detail, c.InterruptionReport):
            for item in detail.conditions:
                if item.relation == 'obstructs':
                    found.add(_condition(item.kind, item.recognition == 'supported'))
        elif isinstance(detail, c.BlockerEvidence):
            for item in detail.candidates:
                if item.blocking:
                    found.add(_condition(item.kind, item.classification == 'recognized'))
            if detail.status == 'rendering':
                found.add('loading')
    # A positively recognized human challenge takes precedence over an overlay
    # around it. Conflicting/unknown controls never authorize a popup response.
    for code in ('captcha', 'account_security', 'resource_access_denied', 'resource_unavailable',
                 'business_portfolio_limit', 'business_name_rejected',
                 'resource_name_ambiguous', 'unsupported_business_settings',
                 'ad_account_creation_unavailable', 'free_boost_post_ineligible', 'login_credentials_rejected',
                 'unknown_popup', 'cookie_consent', 'known_popup', 'unexpected_page', 'navigation', 'loading'):
        if code in found:
            return code
    receipt = getattr(result, 'receipt', None)
    if (getattr(receipt, 'phase', None) == 'navigate'
            and getattr(receipt, 'reason', None) in {'network_failed', 'navigation_failed'}):
        return 'navigation'
    if reason in _STOPS:
        return _STOPS[reason]
    return 'uncertain' if getattr(result, 'outcome', None) == c.Outcome.UNKNOWN else 'unknown'

def _condition(kind, recognized):
    if kind == 'page_availability':
        return 'navigation' if recognized else 'unknown'
    if not recognized or kind in {'unknown_overlay', 'native_dialog'}:
        return 'unknown_popup'
    if kind in {'captcha', 'account_security', 'cookie_consent'}:
        return kind
    return 'known_popup'
