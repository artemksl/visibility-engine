"""A configured provider command port at the existing ActionBlock boundary.

This seam supplies no Hero mapping, purchase policy, lookup or retry guarantee.
Read-only provider components use a separate observation dependency.
"""
from typing import Protocol
import threading

from automation_core.contracts import ProviderCommandRequest, ProviderReceipt
from automation_core.storage.persistence import validate
from automation_core.storage.memory import StorageFailure, TransitionDenied
from automation_core.workflow.timing import BudgetAdmissionDenied


class ProviderCommandPort(Protocol):
    def dispatch(self, request: ProviderCommandRequest, *, context, deadline: float) -> ProviderReceipt: ...


class ProviderDispatchPermit:
    """A process-local, owner-thread, one-use grant for one admitted attempt."""
    __slots__ = ("_owner", "_operation", "_attempt", "_request", "_until", "_used", "_possible")

    def __init__(self, ticket, request, deadline):
        self._owner = threading.get_ident()
        self._operation, self._attempt = ticket.operation_id, ticket.attempt_id
        self._request, self._used = request, False
        self._until = deadline
        self._possible = False

    def consume(self, ticket, request, deadline):
        if (self._used or threading.get_ident() != self._owner or request is not self._request
                or deadline > self._until
                or (ticket.operation_id, ticket.attempt_id) != (self._operation, self._attempt)):
            return False
        self._used = True
        return True

    def command_entered(self):
        self._possible = True

    def record_response(self, receipt):
        self._possible = receipt.possibly_dispatched

    @property
    def possibly_dispatched(self):
        return self._possible

    def __reduce__(self):
        raise TypeError("Provider dispatch permits are runtime-only.")


class ConfiguredProviderPort:
    """Guard one command call; malformed or thrown calls remain possibly attempted."""
    def __init__(self, provider_id, command, *, dispatch_policy=None):
        self.provider_id, self.command = provider_id, command
        self.dispatch_policy = dispatch_policy
        self._active = False

    def dispatch(self, request, *, context, deadline, dispatch_assessment=None):
        def receipt(status, reason):
            return ProviderReceipt(status, request.inputs.provider_id,
                request.inputs.command, request.inputs.request_id, reason=reason)
        if type(request) is not ProviderCommandRequest:
            raise TypeError("A typed provider request is required.")
        validate(request.inputs)
        policy = self.dispatch_policy or getattr(context.executor, "dispatch_policy", None)
        if policy is not None:
            reason = policy.rejection(request, None, deadline, assessment=dispatch_assessment,
                                      context=context, provider=True)
            if reason is not None:
                return receipt("not_attempted", reason)
        if request.inputs.provider_id != self.provider_id:
            return receipt("not_attempted", "provider_origin_mismatch")
        if self._active:
            return receipt("not_attempted", "input_busy")
        if (not context.provider_dispatch_allowed or context.active_request is not request or context.active_ticket is None
                or context.permission.mode == "completion"):
            return receipt("not_attempted", "needs_input")
        try:
            driver = getattr(context.executor, "driver", None)
            if driver is not None:
                driver.assert_owner()
                if driver._input_active or driver._quarantined:
                    return receipt("not_attempted", "input_busy")
            context.call_budget(deadline).checkpoint()
            ticket = context.active_ticket
            record = context.memory.operation(ticket.operation_id)
        except BudgetAdmissionDenied as error:
            from automation_core.workflow.runtime import stop_reason
            return receipt("not_attempted", stop_reason(error.reason).value)
        except StorageFailure:
            return receipt("not_attempted", "storage_unavailable")
        except TransitionDenied as error:
            return receipt("not_attempted", error.stop_reason.value)
        except Exception:
            return receipt("not_attempted", "ownership_lost")
        if (record is None or record.current_phase != "intent"
                or record.current_attempt_id != ticket.attempt_id
                or record.intent.permitted_inputs != request.inputs
                or (request.authorization_ref is not None
                    and request.authorization_ref not in record.intent.private_inputs)):
            return receipt("not_attempted", "intent_mismatch")
        permit = context.provider_dispatch_permit
        if type(permit) is not ProviderDispatchPermit or not permit.consume(ticket, request, deadline):
            return receipt("not_attempted", "needs_input")
        self._active = True
        try:
            context.call_budget(deadline).checkpoint()
        except BudgetAdmissionDenied as error:
            self._active = False
            from automation_core.workflow.runtime import stop_reason
            return receipt("not_attempted", stop_reason(error.reason).value)
        except Exception:
            self._active = False
            return receipt("not_attempted", "ownership_lost")
        try:
            permit.command_entered()
            result = self.command(request, deadline)
            validate(result)
            if (type(result) is not ProviderReceipt or
                    (result.provider_id, result.command, result.request_id) !=
                    (request.inputs.provider_id, request.inputs.command, request.inputs.request_id)):
                raise ValueError("Invalid provider response.")
            if ((request.inputs.activation is not None and result.activation is not None
                    and result.activation != request.inputs.activation)
                    or (request.inputs.command in {"finish", "cancel"} and result.status == "returned"
                        and result.activation != request.inputs.activation)):
                raise ValueError("Invalid provider activation response.")
            permit.record_response(result)
            return result
        except Exception:
            result = receipt("attempted", "provider_failed")
            permit.record_response(result)
            return result
        finally:
            self._active = False


