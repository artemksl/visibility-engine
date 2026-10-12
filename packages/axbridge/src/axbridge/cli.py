"""JSON commands for Accessibility observations and deliberate desktop input."""

import argparse
import json
import math
from pathlib import Path
import sys

from .actions import Controller, Selector, preserve_motion_result
from .models import BridgeError
from .reader import MacReader, doctor


def _wait_seconds(value):
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number of seconds") from error
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return seconds


def _reader_arguments(parser):
    app = parser.add_mutually_exclusive_group()
    app.add_argument("--pid", type=int, help="Target application PID; use this for a specific browser profile process")
    app.add_argument("--bundle-id", default="com.google.Chrome",
                     help="Running application's bundle ID (default: com.google.Chrome)")
    parser.add_argument("--window", help="Unique substring of the target application window title")
    parser.add_argument("--max-nodes", type=int, default=1500, help="Maximum traversal nodes (default: 1500)")
    parser.add_argument("--max-depth", type=int, default=40, help="Maximum traversal depth (default: 40)")
    parser.add_argument("--timeout", type=float, default=8, help="Snapshot time limit in seconds (default: 8)")
    parser.add_argument("--output", type=Path, help="Also save this command's JSON result to a file")


def _execution_arguments(parser):
    parser.add_argument("--execute", action="store_true", help="Send input (default: describe a dry run)")
    parser.add_argument("--trace", type=Path, help="Explicitly save complete pointer event timing to a JSON file")


def _selector_arguments(parser, *, prefix="", role="button", required=True):
    parser.add_argument(f"--{prefix}name", required=required, help="Exact accessible target name")
    parser.add_argument(f"--{prefix}role", default=role, help=f"Accessible role or alias (default: {role})")
    parser.add_argument(f"--{prefix}parent", help="Exact accessible ancestor name to disambiguate matches")


def _verification_arguments(parser):
    parser.add_argument("--expect", help="After execution, wait for this exact accessible name or value")
    parser.add_argument("--wait-timeout", type=_wait_seconds, default=5,
                        help="Verification timeout in seconds (default: 5)")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="axbridge",
        description="Read browser web areas through macOS Accessibility and act through the human mouse service.",
        epilog="Actions default to a dry run. Add --execute to send input.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    diagnostic = commands.add_parser("doctor", help="Check dependencies, Accessibility permission, and display")
    diagnostic.add_argument("--request-access", action="store_true", help="Ask macOS to show its permission prompt")
    inspect = commands.add_parser("inspect", help="Print the active page's accessibility snapshot as JSON")
    _reader_arguments(inspect)

    for command, role, description in (
        ("click", "button", "Reach and click a named control with randomized movement"),
        ("type", "textbox", "Reach a named field and replace its text"),
        ("move", "button", "Move to a named control without clicking"),
    ):
        action = commands.add_parser(command, help=description)
        _reader_arguments(action)
        _execution_arguments(action)
        _selector_arguments(action, role=role)
        _verification_arguments(action)
        if command == "type":
            action.add_argument("--text", required=True, help="Printable ASCII replacement text")
        elif command == "click":
            action.add_argument("--button", choices=("left", "right", "middle"), default="left")
            action.add_argument("--count", type=int, choices=(1, 2), default=1)

    drag = commands.add_parser("drag", help="Drag from one named control to another")
    _reader_arguments(drag)
    _execution_arguments(drag)
    _selector_arguments(drag, prefix="source-")
    _selector_arguments(drag, prefix="destination-")
    _verification_arguments(drag)
    drag.add_argument("--button", choices=("left", "right", "middle"), default="left")

    scroll = commands.add_parser("scroll", help="Send individual wheel steps in randomized bursts")
    _reader_arguments(scroll)
    _execution_arguments(scroll)
    _selector_arguments(scroll, prefix="over-", role="AXGroup", required=False)
    _verification_arguments(scroll)
    scroll.add_argument("--notches", type=int, required=True, help="Positive is up; negative is down")

    for command in ("wander", "idle"):
        action = commands.add_parser(command, help="Make one random movement" if command == "wander" else "Pause, sometimes wandering")
        _execution_arguments(action)
        action.add_argument("--region", nargs=4, type=float, metavar=("LEFT", "TOP", "WIDTH", "HEIGHT"),
                            help="Constrain the entire path to this primary-screen rectangle")
        action.add_argument("--output", type=Path, help="Also save the command's JSON result")
        if command == "wander":
            action.add_argument("--distance", nargs=2, type=float, default=(20., 120.), metavar=("MIN", "MAX"))
        else:
            action.add_argument("--wander-probability", type=float, default=.2)

    demo = commands.add_parser("demo", help="Open or run the isolated local demo")
    demo_commands = demo.add_subparsers(dest="demo_command", required=True)
    demo_commands.add_parser("open", help="Launch the local demo in a separate Chrome profile")
    run = demo_commands.add_parser("run", help="Plan or execute Name → Greet → verify")
    run.add_argument("--name", default="Ada", help="Name to enter (default: Ada)")
    _execution_arguments(run)
    run.add_argument("--output", type=Path, help="Also save the workflow JSON result")
    return parser


def _reader(args):
    return MacReader(pid=args.pid, bundle_id=args.bundle_id, window=args.window, max_nodes=args.max_nodes,
                     max_depth=args.max_depth, timeout=args.timeout)


def _selector(args, prefix=""):
    return Selector(role=getattr(args, prefix + "role"), name=getattr(args, prefix + "name"),
                    parent_name=getattr(args, prefix + "parent"))


def _collect_trace(controller, traces):
    if traces is not None:
        result = getattr(controller.mouse, "last_result", None)
        if result is not None:
            traces.append(result.to_dict(include_trace=True))


def _run(args, *, traces=None):
    if args.command == "doctor":
        report = doctor(request_access=args.request_access)
        return report, 0 if report.get("ready") else 2
    if args.command == "demo":
        from .demo import open_demo, run_demo
        if args.demo_command == "open":
            return open_demo(), 0
        return run_demo(name=args.name, execute=args.execute, trace=traces), 0

    geometry_only = args.command in ("wander", "idle") or (
        args.command == "scroll" and args.over_name is None and not args.execute)
    reader = None if geometry_only else _reader(args)
    if args.command == "inspect":
        return reader.snapshot().to_dict(), 0
    controller = Controller(reader)
    try:
        with preserve_motion_result(controller):
            if args.command == "click":
                report = controller.click(_selector(args), execute=args.execute, button=args.button, count=args.count)
            elif args.command == "type":
                report = controller.type_text(_selector(args), args.text, execute=args.execute)
            elif args.command == "move":
                report = controller.move(_selector(args), execute=args.execute)
            elif args.command == "drag":
                report = controller.drag(_selector(args, "source_"), _selector(args, "destination_"),
                                         execute=args.execute, button=args.button)
            elif args.command == "scroll":
                over = _selector(args, "over_") if args.over_name is not None else None
                if over is None and (args.over_parent is not None or args.over_role != "AXGroup"):
                    raise BridgeError("--over-role and --over-parent require --over-name.")
                report = controller.scroll(args.notches, over=over, execute=args.execute)
            elif args.command == "wander":
                report = controller.random_move(region=args.region, distance=args.distance, execute=args.execute)
            else:
                report = controller.idle(region=args.region, wander_probability=args.wander_probability,
                                         execute=args.execute)
            if getattr(args, "expect", None) is not None:
                report["expected_text"] = args.expect
                if args.execute:
                    snapshot = controller.wait_for_text(args.expect, timeout=args.wait_timeout)
                    report["verification"] = {"matched": True, "captured_at": snapshot.captured_at}
                else:
                    report["verification"] = {"matched": None, "reason": "Dry run; action was not executed."}
            return report, 0
    finally:
        _collect_trace(controller, traces)


def _write_json(path, report):
    try:
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as error:
        raise BridgeError(f"Could not save JSON to {path}: {error}") from error


def _write_trace(args, traces, *, error=None):
    path = getattr(args, "trace", None)
    if path is not None:
        report = {"status": "failed" if error is not None else "completed", "actions": traces or []}
        if error is not None:
            report["error"] = str(error)
        _write_json(path, report)


def main(argv=None):
    args, traces = None, None
    try:
        args = build_parser().parse_args(argv)
        traces = [] if getattr(args, "trace", None) is not None else None
        report, exit_code = _run(args, traces=traces)
        _write_trace(args, traces)
        output = getattr(args, "output", None)
        if output is not None:
            _write_json(output, report)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return exit_code
    except BridgeError as error:
        report = {"error": str(error)}
        partial = getattr(error, "result", None)
        if partial is not None:
            report["partial_result"] = partial.to_dict()
            if traces is not None:
                entry = partial.to_dict(include_trace=True)
                if not traces or traces[-1] != entry:
                    traces.append(entry)
        try:
            _write_trace(args, traces, error=error)
        except BridgeError as trace_error:
            report["trace_error"] = str(trace_error)
        print(json.dumps(report, ensure_ascii=False), file=sys.stderr)
        from .mouse.models import Cancelled
        return 130 if isinstance(error, Cancelled) else 2
    except KeyboardInterrupt:
        try:
            _write_trace(args, traces, error="Interrupted.")
        except BridgeError:
            pass
        print(json.dumps({"error": "Interrupted."}), file=sys.stderr)
        return 130
