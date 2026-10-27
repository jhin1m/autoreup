"""Bandit v2 network, reduced to the music estimate.

A trimmed re-implementation of Bandit (Watcharasupat, Wu & Orife, "Remastering Divide
and Remaster: A Cinematic Audio Source Separation Dataset with Multilingual Support",
2024; https://github.com/kwatcharasupat/bandit-v2, Apache-2.0). The published model
estimates speech, music and sfx; only the music mask head is built here, so the
weights load from the checkpoint's ``model.*`` keys minus the other two heads.

Parameter names mirror the original modules so the checkpoint loads as-is:
``band_split.norm_fc_modules.{b}.combined``, ``tf_model.seqband.{i}``,
``mask_estim.music.norm_mlp.{b}.combined`` and ``mask_estim.music.freq_weights/{b}``.
"""

from __future__ import annotations

import math

import numpy as np
import torch
from torch import nn

SAMPLE_RATE = 48000
N_FFT = 2048
HOP = 512
N_BANDS = 64
EMB_DIM = 128
RNN_DIM = 256
MLP_DIM = 512
N_SQM_MODULES = 8


def musical_band_specs(n_bands: int = N_BANDS, fs: int = SAMPLE_RATE, n_fft: int = N_FFT) -> tuple[list[tuple[int, int]], list[torch.Tensor]]:
    """Overlapping log-spaced bands ``[start, end)`` in STFT bins, plus each bin's share
    of every band it sits in (the "musical" band split of the original)."""
    n_freqs = n_fft // 2 + 1
    df = fs / n_fft
    f_min, f_max = df, fs / 2
    bandwidth_mult = 2.0 ** (math.log2(f_max / f_min) / n_bands)
    midi = np.linspace(max(0.0, _hz_to_midi(f_min)), _hz_to_midi(f_max), n_bands)
    hz = 440.0 * 2.0 ** ((midi - 69.0) / 12.0)
    low_bins = np.floor(hz / bandwidth_mult / df).astype(int)
    high_bins = np.ceil(hz * bandwidth_mult / df).astype(int)
    bank = np.zeros((n_bands, n_freqs))
    for band in range(n_bands):
        bank[band, low_bins[band]: high_bins[band] + 1] = 1.0
    bank[0, : low_bins[0]] = 1.0
    bank[-1, high_bins[-1] + 1:] = 1.0
    filterbank = torch.as_tensor(bank)
    normalized = filterbank / filterbank.sum(dim=0, keepdim=True)
    specs, weights = [], []
    for band in range(n_bands):
        active = torch.nonzero(filterbank[band]).squeeze(1).tolist()
        if not active:
            continue
        specs.append((active[0], active[-1] + 1))
        weights.append(normalized[band, active[0]: active[-1] + 1].float())
    return specs, weights


def _hz_to_midi(hz: float) -> float:
    return 12.0 * math.log2(hz / 440.0) + 69.0


class _NormFC(nn.Module):
    def __init__(self, bandwidth: int):
        super().__init__()
        self.combined = nn.Sequential(nn.LayerNorm(bandwidth * 2), nn.Linear(bandwidth * 2, EMB_DIM))

    def forward(self, xb: torch.Tensor) -> torch.Tensor:
        return self.combined(xb)


class _BandSplit(nn.Module):
    def __init__(self, specs: list[tuple[int, int]]):
        super().__init__()
        self.specs = specs
        self.norm_fc_modules = nn.ModuleList([_NormFC(end - start) for start, end in specs])

    def forward(self, spec: torch.Tensor) -> torch.Tensor:
        # spec: (batch, 1, freq, time) complex -> (batch, bands, time, emb)
        batch, _, _, n_time = spec.shape
        frames = spec.permute(0, 3, 1, 2)  # (batch, time, 1, freq)
        bands = []
        for (start, end), module in zip(self.specs, self.norm_fc_modules):
            xb = torch.view_as_real(frames[..., start:end]).reshape(batch, n_time, -1)
            bands.append(module(xb))
        return torch.stack(bands, dim=1)


class _ResidualRNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.norm = nn.LayerNorm(EMB_DIM)
        self.rnn = nn.GRU(EMB_DIM, RNN_DIM, num_layers=1, batch_first=True, bidirectional=True)
        self.fc = nn.Linear(RNN_DIM * 2, EMB_DIM)

    def forward(self, z: torch.Tensor) -> torch.Tensor:
        batch, outer, inner, emb = z.shape
        out = self.rnn(self.norm(z).reshape(batch * outer, inner, emb))[0]
        return self.fc(out.reshape(batch, outer, inner, -1)) + z


class _Transpose(nn.Module):
    def forward(self, z: torch.Tensor) -> torch.Tensor:
        return z.transpose(1, 2)


class _TfModel(nn.Module):
    """Alternating RNNs across time and across bands (2 per module)."""

    def __init__(self):
        super().__init__()
        layers: list[nn.Module] = []
        for _ in range(2 * N_SQM_MODULES):
            layers += [_ResidualRNN(), _Transpose()]
        self.seqband = nn.Sequential(*layers)

    def forward(self, z: torch.Tensor) -> torch.Tensor:
        return self.seqband(z)


class _NormMLP(nn.Module):
    def __init__(self, bandwidth: int):
        super().__init__()
        self.bandwidth = bandwidth
        self.combined = nn.Sequential(
            nn.LayerNorm(EMB_DIM),
            nn.Sequential(nn.Linear(EMB_DIM, MLP_DIM), nn.Tanh()),
            nn.Sequential(nn.Linear(MLP_DIM, bandwidth * 2 * 2), nn.GLU(dim=-1)),
        )

    def forward(self, qb: torch.Tensor) -> torch.Tensor:
        # (batch, time, emb) -> complex mask (batch, 1, bandwidth, time)
        batch, n_time, _ = qb.shape
        mask = self.combined(qb).float().reshape(batch, n_time, 1, self.bandwidth, 2)
        return torch.view_as_complex(mask.contiguous()).permute(0, 2, 3, 1)


class _MaskEstim(nn.Module):
    def __init__(self, specs: list[tuple[int, int]], weights: list[torch.Tensor]):
        super().__init__()
        self.specs = specs
        self.n_freq = N_FFT // 2 + 1
        self.norm_mlp = nn.ModuleList([_NormMLP(end - start) for start, end in specs])
        for band, weight in enumerate(weights):
            self.register_buffer(f"freq_weights/{band}", weight)

    def forward(self, q: torch.Tensor) -> torch.Tensor:
        batch, _, n_time, _ = q.shape
        masks = torch.zeros((batch, 1, self.n_freq, n_time), device=q.device, dtype=torch.complex64)
        for band, ((start, end), module) in enumerate(zip(self.specs, self.norm_mlp)):
            masks[:, :, start:end, :] += module(q[:, band]) * self.get_buffer(f"freq_weights/{band}")[:, None]
        return masks


class BanditMusic(nn.Module):
    """Mono 48 kHz audio (batch, 1, samples) -> its music estimate, same shape."""

    def __init__(self):
        super().__init__()
        specs, weights = musical_band_specs()
        self.band_split = _BandSplit(specs)
        self.tf_model = _TfModel()
        self.mask_estim = nn.ModuleDict({"music": _MaskEstim(specs, weights)})
        self.register_buffer("window", torch.hann_window(N_FFT), persistent=False)

    def forward(self, audio: torch.Tensor) -> torch.Tensor:
        batch, _, length = audio.shape
        # torchaudio's Spectrogram(normalized=True): scale by the window's energy
        scale = self.window.pow(2).sum().sqrt()
        with torch.autocast(audio.device.type, enabled=False):
            spec = torch.stft(audio.reshape(batch, length).float(), N_FFT, HOP, N_FFT, self.window,
                              center=True, pad_mode="reflect", normalized=False, onesided=True, return_complex=True)
            spec = (spec / scale).unsqueeze(1)
        mask = self.mask_estim["music"](self.tf_model(self.band_split(spec)))
        with torch.autocast(audio.device.type, enabled=False):
            music = (spec * mask).squeeze(1) * scale
            out = torch.istft(music, N_FFT, HOP, N_FFT, self.window, center=True, normalized=False, onesided=True, length=length)
        return out.unsqueeze(1)


def music_state_dict(checkpoint: dict) -> dict[str, torch.Tensor]:
    """The tensors ``BanditMusic`` needs, from a published Bandit v2 Lightning checkpoint
    (``state_dict`` with ``model.`` keys; other stems, aliases and losses dropped)."""
    wanted = set(BanditMusic().state_dict().keys())
    state = checkpoint.get("state_dict", checkpoint)
    return {key[len("model."):]: value for key, value in state.items() if key[len("model."):] in wanted}
