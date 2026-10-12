# AXBridge 0.2.3

Optional macOS accessibility reader and guarded native mouse controller used
only for supported Copilot verification. The source is byte-identical to the
release already used by the visibility application. Install it using the root
setup script's desktop-verification option; it is not needed for ChatGPT or
Gemini browser measurement.

Its external dependencies are `atomacos==3.3.0` and `PyAutoGUI==0.9.41`. They may
require macOS Accessibility permission. The runner still owns the FIFO desktop
queue, process lock, exact tab checks, cancellation and the verification deadline.
Installing AXBridge does not authorize a live run or arbitrary desktop input.

See [the root README](../../README.md) and [PROVENANCE.md](../PROVENANCE.md).
