"""Runtime-only blocker targets and authority from an admitted recovery action.

These objects contain no durable data and cannot be reconstructed from saved
evidence. A binding names an observation, never permission to send input.
"""

from dataclasses import dataclass, field
import re

from automation_core.contracts import EffectKind, InAction, RecoveryKind, TargetRef, ViewRef


class BlockerBindingError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _identifier(value):
    return type(value) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", value) is not None


@dataclass(frozen=True, repr=False)
class BlockerBinding:
    view: ViewRef
    origin: str
    document_generation: int
    root: TargetRef = field(repr=False)
    target: TargetRef = field(repr=False)
    rule_id: str
    rule_version: int
    observation_id: str
    choice_id: str
    retention_id: str = field(repr=False)

    def __repr__(self):
        return "BlockerBinding(<runtime>)"

    def __reduce__(self):
        raise TypeError("Runtime blocker bindings cannot be serialized.")


@dataclass(frozen=True, init=False, repr=False)
class BlockerInputAuthority:
    operation_id: str
    root_operation_id: str
    rule_id: str
    choice_id: str
    _binding: BlockerBinding = field(repr=False)
    _driver: object = field(repr=False)
    _check: object = field(repr=False)
    _claimed: bool = field(repr=False)

    @classmethod
    def from_context(cls, ctx, binding, choice_id):
        """Require the current persisted consent child and recovery admission.

        The authority rechecks the borrowed context on every use. It does not
        admit work, renew clocks or turn the recognizer into an effect owner.
        """
        from automation_core.contracts import ConsentInputs
        from automation_core.storage.memory import StorageFailure, TransitionDenied
        from automation_core.storage.persistence import validate
        from automation_core.workflow.runtime import RunContext
        from automation_core.workflow.timing import BudgetAdmissionDenied
        if type(ctx) is not RunContext or type(binding) is not BlockerBinding:
            raise BlockerBindingError("blocker_authority_required")
        driver = getattr(ctx.executor, "driver", None)
        ticket = ctx.active_ticket
        frame = ctx.checkpoint.current.recovery
        if driver is None or ticket is None or frame is None:
            raise BlockerBindingError("blocker_authority_required")
        operation_id, frame_id, root_id = ticket.operation_id, frame.frame_id, frame.root_operation_id
        recovery_rule = frame.rule_id

        def check():
            try:
                ctx.normal()
                cp = ctx.checkpoint.current
                current = cp.recovery
                if not ctx.memory.healthy:
                    raise BlockerBindingError("storage_unavailable")
                if (ctx.session is not driver.session
                        or ctx.permission.mode != "recovery" or ctx.permission.frame_id != frame_id
                        or current is None or current.frame_id != frame_id
                        or current.root_operation_id != root_id or current.rule_id != recovery_rule
                        or current.phase != "running" or current.episode_kind != RecoveryKind.CORRECTION
                        or not isinstance(current.position, InAction)
                        or current.position.operation_id != operation_id
                        or current.position.attempt_id != ticket.attempt_id
                        or ticket.lane != "recovery" or ticket.frame_id != frame_id
                        or ticket.workflow_id != cp.workflow_id
                        or ctx.active_ticket is not ticket):
                    raise BlockerBindingError("blocker_authority_invalid")
                intent = ctx.memory.operation(operation_id).intent
                validate(intent)
                inputs = intent.permitted_inputs
                if (intent.operation_id != operation_id or intent.workflow_id != cp.workflow_id or intent.scope != cp.scope
                        or intent.effect_kind != EffectKind.CONSEQUENTIAL
                        or intent.root_operation_id != root_id or intent.corrective_parent_id != root_id
                        or type(inputs) is not ConsentInputs or inputs.choice_id != choice_id
                        or choice_id != binding.choice_id or inputs.rule_id != binding.rule_id
                        or inputs.rule_version != binding.rule_version
                        or root_id in cp.pending_operation_ids):
                    raise BlockerBindingError("blocker_authority_invalid")
            except BlockerBindingError:
                raise
            except BudgetAdmissionDenied as error:
                raise BlockerBindingError(error.reason) from None
            except TransitionDenied as error:
                raise BlockerBindingError(error.stop_reason.value) from None
            except StorageFailure:
                raise BlockerBindingError("storage_unavailable") from None
            except Exception:
                raise BlockerBindingError("blocker_authority_invalid") from None

        check()
        authority = object.__new__(cls)
        for name, value in (("operation_id", operation_id), ("root_operation_id", root_id),
                            ("rule_id", recovery_rule), ("choice_id", choice_id),
                            ("_binding", binding), ("_driver", driver), ("_check", check), ("_claimed", False)):
            object.__setattr__(authority, name, value)
        return authority

    def require(self, driver, binding):
        if driver is not self._driver or binding is not self._binding:
            raise BlockerBindingError("blocker_authority_invalid")
        self._check()

    def claim(self, driver, binding):
        self.require(driver, binding)
        if self._claimed:
            raise BlockerBindingError("blocker_authority_invalid")
        object.__setattr__(self, "_claimed", True)

    def __repr__(self):
        return "BlockerInputAuthority(<admitted runtime>)"

    def __reduce__(self):
        raise TypeError("Runtime blocker authority cannot be serialized.")
