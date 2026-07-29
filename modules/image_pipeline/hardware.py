"""Hardware auto-detection for the pipeline's one GPU-capable stage
(rembg's segmentation model, via onnxruntime). rembg is ONNX-based, not
PyTorch — checking onnxruntime's execution providers is the correct probe
here, not torch.cuda.is_available().
"""
import onnxruntime


def select_device(use_gpu: bool = True) -> str:
    """Returns "cuda" if use_gpu is True AND onnxruntime reports a CUDA
    execution provider available, otherwise "cpu". On a machine with no
    NVIDIA GPU (confirmed: this dev machine has none — no nvidia-smi),
    this always resolves to "cpu"; the check still matters for a future
    deployment machine that does have one.
    """
    if not use_gpu:
        return "cpu"
    try:
        providers = onnxruntime.get_available_providers()
    except Exception:
        return "cpu"
    return "cuda" if "CUDAExecutionProvider" in providers else "cpu"
