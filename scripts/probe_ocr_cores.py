"""Why plate recognition latency on this machine is bimodal (Phase 5, section 11).

An identical 48x320 crop times at ~13 ms for the first couple of seconds of a
run and ~150 ms afterwards, which looks like the model slowing down. It is not
the model: this is a hybrid CPU (P-cores + E-cores) and Windows migrates the
ONNX Runtime worker threads onto the E-cores once load is sustained.

Pinning answers it. The two numbers are the same inference on the same tensor:

    python scripts/probe_ocr_cores.py fff    # CPUs 0-11  -> ~11-14 ms/crop
    python scripts/probe_ocr_cores.py f000   # CPUs 12-15 -> ~150 ms/crop

so any latency figure quoted for the ANPR stage must say which cores produced
it. The repository does not pin threads - that is a deployment decision, and an
unpinned run reports the honest mixed figure.
"""

from __future__ import annotations

import ctypes
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from smart_parking.anpr.engine import build_ocr  # noqa: E402
from smart_parking.core.config import load_config  # noqa: E402

CALLS = 300


def pin(mask: int) -> None:
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    if not kernel32.SetProcessAffinityMask(kernel32.GetCurrentProcess(), mask):
        raise SystemExit(f"cannot pin to mask {mask:#x}")


def main() -> int:
    if len(sys.argv) > 1:
        pin(int(sys.argv[1], 16))
    print(f"{os.cpu_count()} logical CPUs, mask {sys.argv[1] if len(sys.argv) > 1 else 'default'}")

    # The recognition input is padded to a fixed tensor, so the only variable
    # left is which physical cores the ORT thread pool is running on.
    rec = build_ocr(load_config(root=ROOT).anpr).text_rec
    crop = np.full((48, 320, 3), 128, np.uint8)

    ts: list[float] = []
    t0 = time.perf_counter()
    for i in range(CALLS):
        started = time.perf_counter()
        rec([crop])
        ts.append((time.perf_counter() - started) * 1000)
        if (i + 1) % 30 == 0:
            print(
                f"t={time.perf_counter() - t0:6.1f}s  calls {i + 1:>4}  "
                f"median {statistics.median(ts[-30:]):6.1f} ms"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
