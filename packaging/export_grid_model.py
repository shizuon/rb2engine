"""Maintainer-only small0 export; users run the checked-in offline ONNX model.

Build environment: beat-this==1.1.0, torch==2.13.0, onnx==1.20.1,
onnxruntime==1.24.4. Supply the official small0 checkpoint as the first argument.
No training data, user audio or private analysis files are included.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
import torch
from beat_this.inference import load_model
from beat_this.preprocessing import LogMelSpect

checkpoint = Path(sys.argv[1])
assets = Path(__file__).resolve().parent.parent / 'src/rb2engine/gridrepair/assets'
manifest_path = assets / 'manifest.json'
manifest = json.loads(manifest_path.read_text())
assert hashlib.sha256(checkpoint.read_bytes()).hexdigest() == manifest['checkpoint_sha256']
torch.set_num_threads(4)
model = load_model(str(checkpoint))
for module in model.modules():
    if hasattr(module, 'cache_if_possible'):
        module.cache_if_possible = False


class Outputs(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.model = model

    def forward(self, spectrogram):
        result = self.model(spectrogram)
        return result['beat'], result['downbeat']


wrapper = Outputs().eval()
torch.onnx.export(
    wrapper, torch.zeros(1, 300, 128), assets / 'small0.onnx', opset_version=17,
    dynamo=False, input_names=['spectrogram'], output_names=['beat', 'downbeat'],
    dynamic_axes={name: {1: 'frames'} for name in ('spectrogram', 'beat', 'downbeat')},
)
frontend = LogMelSpect()
np.savez(assets / 'frontend.npz', window=frontend.spect_class.spectrogram.window.numpy(),
         mel=frontend.spect_class.mel_scale.fb.numpy())
options = ort.SessionOptions()
options.intra_op_num_threads = 2
session = ort.InferenceSession(str(assets / 'small0.onnx'), options,
                               providers=['CPUExecutionProvider'])
for frames in (100, 333, 1500):
    x = np.random.default_rng(frames).standard_normal((1, frames, 128)).astype(np.float32)
    with torch.inference_mode():
        expected = wrapper(torch.from_numpy(x))
    actual = session.run(None, {'spectrogram': x})
    errors = [float(np.max(abs(a - b.numpy()))) for a, b in zip(actual, expected, strict=True)]
    assert max(errors) < .001, errors
    print(frames, errors)
manifest['files'] = {name: hashlib.sha256((assets / name).read_bytes()).hexdigest()
                     for name in ('small0.onnx', 'frontend.npz')}
manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
