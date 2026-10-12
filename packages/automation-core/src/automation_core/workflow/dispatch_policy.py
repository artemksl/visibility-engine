"""Runtime-only exclusion of SMS, spending and unproven external effects.

An assessment is an exact, one-use capability from a registered source adapter.
It is not a button label, a durable permission, or proof of business success.
"""
from dataclasses import dataclass, field
from enum import Enum
import threading


class DispatchPurpose(str, Enum):
    ACCOUNT_NAVIGATION = "account_navigation"
    PROFILE_TAB_CLEANUP = "profile_tab_cleanup"
    ACCOUNT_ACCESS = "account_access"
    COOKIE_CONSENT = "cookie_consent"
    AUTHENTICATOR_GENERATION = "authenticator_generation"
    AUTHENTICATOR_VERIFICATION = "authenticator_verification"
    EMAIL_VERIFICATION = "email_verification"
    ACTING_PAGE_SELECTION = "acting_page_selection"
    PAGE_CREATION = "page_creation"
    PORTFOLIO_CREATION = "portfolio_creation"
    AD_ACCOUNT_CREATION = "ad_account_creation"
    ADMINISTRATOR_ACCESS = "administrator_access"
    ORGANIC_POST = "organic_post"
    MESSAGE_SUBMISSION = "message_submission"
    FREE_AD_PREPARATION = "free_ad_preparation"
    SMS_VERIFICATION = "sms_verification"
    ADVERTISING_BUDGET = "advertising_budget"
    ADVERTISING_SCHEDULE = "advertising_schedule"
    ADVERTISING_DELIVERY = "advertising_delivery"
    PAYMENT_CONFIGURATION = "payment_configuration"
    PURCHASE = "purchase"
    SUBSCRIPTION = "subscription"
    PLAN_UPGRADE = "plan_upgrade"
    PROFILE_PURCHASE = "profile_purchase"
    TAX_CONFIGURATION = "tax_configuration"


class DispatchCost(str, Enum):
    ZERO_EXTRA_COST = "zero_extra_cost"
    PAID = "paid"
    UNKNOWN = "unknown"


FREE_PURPOSES = frozenset({
    DispatchPurpose.ACCOUNT_NAVIGATION, DispatchPurpose.ACCOUNT_ACCESS, DispatchPurpose.PROFILE_TAB_CLEANUP,
    DispatchPurpose.COOKIE_CONSENT, DispatchPurpose.AUTHENTICATOR_GENERATION,
    DispatchPurpose.AUTHENTICATOR_VERIFICATION, DispatchPurpose.EMAIL_VERIFICATION,
    DispatchPurpose.ACTING_PAGE_SELECTION, DispatchPurpose.PAGE_CREATION, DispatchPurpose.PORTFOLIO_CREATION,
    DispatchPurpose.AD_ACCOUNT_CREATION, DispatchPurpose.ADMINISTRATOR_ACCESS,
    DispatchPurpose.ORGANIC_POST, DispatchPurpose.FREE_AD_PREPARATION, DispatchPurpose.MESSAGE_SUBMISSION,
})


def _context_identity(context):
    ticket = context.active_ticket
    if ticket is None:
        raise ValueError("An admitted operation is required.")
    return (context.checkpoint.current.scope, context.workflow_operation_id,
            ticket.operation_id, ticket.attempt_id)


def _verification_failure(error):
    """Keep typed execution holds separate from an adapter implementation failure."""
    from automation_core.browser.interaction import InteractionError
    from automation_core.storage.memory import StorageFailure, TransitionDenied
    from automation_core.workflow.runtime import stop_reason
    from automation_core.workflow.timing import BudgetAdmissionDenied
    from automation_core.storage.models import BudgetStopped, MailAccessError, NeedsAttention

    if isinstance(error, (TransitionDenied, StorageFailure)):
        return error.stop_reason.value
    if isinstance(error, (InteractionError, BudgetAdmissionDenied, BudgetStopped)):
        return stop_reason(error.reason).value
    if isinstance(error, (NeedsAttention, MailAccessError)):
        return "dispatch_policy_unknown_effect"
    return "implementation_error"


@dataclass(frozen=True, repr=False)
class DispatchAssessment:
    """Private capability; the issuer and boundary retain exact runtime objects."""
    _policy: object = field(repr=False)
    _source: object = field(repr=False)
    _request: object = field(repr=False)
    _binding: object = field(repr=False)
    _context: tuple = field(repr=False)
    _driver: object = field(repr=False)
    _document: int | None = field(repr=False)
    _deadline: float = field(repr=False)
    _verify: object = field(repr=False)
    purpose: DispatchPurpose
    cost: DispatchCost
    _owner: int = field(default_factory=threading.get_ident, repr=False)
    _used: bool = field(default=False, repr=False)

    def __reduce__(self):
        raise TypeError("Dispatch assessments are runtime-only.")


class ZeroSpendingDispatchPolicy:
    """Fail closed before browser input or configured provider invocation.

    Trusted sources are exact application adapter objects, registered by assembly.
    Their verifier must establish the actual purpose and zero extra cost from
    current controls/observations. New adapters gain no permission by naming an
    existing purpose. The technical driver still checks lease, origin and target.
    """
    def __init__(self, driver, trusted_sources=()):
        self.driver = driver
        self._sources = tuple(trusted_sources)
        self._issued = {}
        self._in_flight = {}

    def assessment(self, source, purpose, request, binding, context, deadline, *,
                   verify, cost=DispatchCost.ZERO_EXTRA_COST):
        if not any(source is registered for registered in self._sources) or not callable(verify):
            raise ValueError("A registered source verifier is required.")
        if context.executor.driver is not self.driver or context.active_request is not request:
            raise ValueError("Assessment must use the admitted request and owning driver.")
        ref = getattr(binding, "view", binding)
        document = (self.driver.document_generation(ref, deadline=deadline)
                    if ref is not None else None)
        assessment = DispatchAssessment(self, source, request, binding, _context_identity(context),
            self.driver, document, deadline, verify, purpose, cost)
        self._issued[id(assessment)] = assessment
        # Cap retained runtime capabilities. Eviction denies an old capability;
        # it never creates admission or resets a durable action allowance.
        while len(self._issued) > 128:
            self._issued.pop(next(iter(self._issued)))
        return assessment

    def rejection(self, request, binding, deadline, *, assessment=None, context=None,
                  provider=False):
        # The current configured provider command family is SMS activation.
        # Cancellation/finish may also settle a bill and remain outside this task.
        if provider:
            return "dispatch_policy_excluded"
        if type(assessment) is not DispatchAssessment:
            return "dispatch_policy_unknown_effect"
        if type(assessment.purpose) is not DispatchPurpose:
            return "dispatch_policy_unknown_effect"
        reason = self.effect_rejection(request, assessment)
        if reason is not None:
            return reason
        try:
            if (self._issued.get(id(assessment)) is not assessment
                    or assessment._used or assessment._owner != threading.get_ident()
                    or assessment._policy is not self or assessment._driver is not self.driver
                    or not any(assessment._source is source for source in self._sources)
                    or assessment._request is not request or assessment._binding is not binding
                    or context is None or context.active_request is not request
                    or context.executor.driver is not self.driver
                    or assessment._context != _context_identity(context)
                    or deadline > assessment._deadline):
                return "dispatch_policy_stale_assessment"
            object.__setattr__(assessment, "_used", True)
            self._issued.pop(id(assessment))
            self._in_flight[id(assessment)] = assessment
            while len(self._in_flight) > 128:
                self._in_flight.pop(next(iter(self._in_flight)))
            # Admission checks only local capability state. The executor checks
            # the live source once, after readiness and immediately before input.
            reason = self.native_rejection(request, binding, deadline,
                assessment=assessment, context=context, verify=False)
            if reason is not None:
                self.release(assessment)
            return reason
        except Exception as error:
            self.release(assessment)
            return _verification_failure(error)

    def native_rejection(self, request, binding, deadline, *, assessment, context,
                         verify=True):
        """Recheck the consumed original capability, without another admission.

        The executor releases this exact in-flight identity in finally. Calling
        this method cannot make a used assessment acceptable to execute again.
        """
        try:
            def current():
                ref = getattr(binding, "view", binding)
                return (self._in_flight.get(id(assessment)) is assessment
                    and type(assessment.purpose) is DispatchPurpose
                    and self.effect_rejection(request, assessment) is None
                    and assessment._used and assessment._owner == threading.get_ident()
                    and assessment._policy is self and assessment._driver is self.driver
                    and any(assessment._source is source for source in self._sources)
                    and assessment._request is request and assessment._binding is binding
                    and context.active_request is request and context.executor.driver is self.driver
                    and assessment._context == _context_identity(context)
                    and deadline <= assessment._deadline
                    and (ref is None or self.driver.document_generation(ref, deadline=deadline)
                         == assessment._document))
            if not current():
                return "dispatch_policy_stale_assessment"
            if verify and assessment._verify(deadline) is not True:
                return "dispatch_policy_unknown_effect"
            if not current():
                return "dispatch_policy_stale_assessment"
        except Exception as error:
            return _verification_failure(error)
        return None

    def effect_rejection(self, request, assessment):
        if assessment.purpose not in FREE_PURPOSES:
            return "dispatch_policy_excluded"
        if type(assessment.cost) is not DispatchCost or assessment.cost is not DispatchCost.ZERO_EXTRA_COST:
            return "dispatch_policy_cost_unverified"
        return None

    def release(self, assessment):
        if self._in_flight.get(id(assessment)) is assessment:
            self._in_flight.pop(id(assessment))

