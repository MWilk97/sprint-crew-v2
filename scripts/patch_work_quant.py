#!/usr/bin/env python3
"""Make the Work-lane (Qwen3-30B-A3B-Thinking-2507 NVFP4) quantization auto-detectable
on the pinned vLLM 26.04 image (v0.19).

The upstream ModelOpt checkpoint ships a hybrid `config.json` quantization_config
that carries `quant_algo: NVFP4` + `quant_library: modelopt` (and compressed-tensors
style `config_groups`), but omits the `quant_method` key. vLLM's ModelOpt detector
only triggers when `quant_method == "modelopt"` (see
vllm/model_executor/layers/quantization/modelopt.py::_extract_modelopt_quant_algo),
so without it the model loads unquantized and dies with
`KeyError: layers.0.mlp.experts.w2_weight_scale_2`.

We add `quant_method: "modelopt"`; vLLM then selects the `modelopt_fp4` backend
(NVFP4 Marlin MoE on GB10). Idempotent; re-run after any fresh `hf download`.

Run with write access to the HF cache (the vLLM container writes it as root):

  docker run --rm -i -e HF_HOME=/models -v "$HOME/vllm-models":/models \
    --entrypoint python3 nvcr.io/nvidia/vllm:26.04-py3 \
    - < scripts/patch_work_quant.py

Or locally if the cache is user-writable:

  HF_HOME=~/vllm-models python scripts/patch_work_quant.py
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

DEFAULT_MODEL = "NVFP4/Qwen3-30B-A3B-Thinking-2507-FP4"
TARGET_METHOD = "modelopt"


def _cache_dir(hf_home: str, model_id: str) -> str:
    folder = "models--" + model_id.replace("/", "--")
    return os.path.join(hf_home, "hub", folder, "snapshots")


def patch(hf_home: str, model_id: str) -> int:
    snap_root = _cache_dir(hf_home, model_id)
    snaps = sorted(glob.glob(os.path.join(snap_root, "*/")))
    if not snaps:
        print(f"NOT FOUND: no snapshot for {model_id} under {snap_root}")
        return 1
    changed = 0
    for snap in snaps:
        path = os.path.join(snap, "config.json")
        if not os.path.exists(path):
            continue
        with open(os.path.realpath(path), encoding="utf-8") as fh:
            cfg = json.load(fh)
        qc = cfg.get("quantization_config")
        if not isinstance(qc, dict):
            print(f"SKIP (no quantization_config): {path}")
            continue
        if qc.get("quant_method") == TARGET_METHOD:
            print(f"OK (already patched): {path}")
            continue
        qc["quant_method"] = TARGET_METHOD
        cfg["quantization_config"] = qc
        # Replace the snapshot symlink with a real file; leave the blob store intact.
        if os.path.islink(path):
            os.unlink(path)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        changed += 1
        print(f"PATCHED quant_method -> {TARGET_METHOD}: {path}")
    print(f"done ({changed} file(s) patched)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hf-home",
        default=os.environ.get("HF_HOME", os.path.expanduser("~/vllm-models")),
    )
    parser.add_argument("--model-id", default=DEFAULT_MODEL)
    args = parser.parse_args()
    return patch(args.hf_home, args.model_id)


if __name__ == "__main__":
    sys.exit(main())
