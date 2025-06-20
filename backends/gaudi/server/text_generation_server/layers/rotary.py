import os
import math
import torch
from torch import nn
from habana_frameworks.torch.hpex.kernels import (
    RotaryPosEmbeddingMode,
    apply_rotary_pos_emb,
)


def _create_inv_freq(dim, base, device):
    inv_freq = 1.0 / (
        base ** (torch.arange(0, dim, 2, device=device, dtype=torch.float32) / dim)
    )
    return inv_freq


def _get_rope_config(config):
    if os.getenv("ROPE_SCALING", None) is not None:
        rope_scaling = {
            "type": os.environ["ROPE_SCALING"],
            "factor": float(os.environ["ROPE_FACTOR"]),
        }
        return rope_scaling
    return getattr(config, "rope_scaling", None)


class PositionRotaryEmbedding(nn.Module):
    def __init__(self, inv_freq, scaling_factor, max_position_embeddings):
        super().__init__()
        self.inv_freq = inv_freq
        self._seq_len_cached = 0
        self._cos_cached = None
        self._sin_cached = None
        self._cos_k_cached = None
        self._sin_k_cached = None
        self.scaling_factor = scaling_factor
        self.dynamic_args = None
        self.max_position_embeddings = max_position_embeddings

    def forward(
        self,
        query: torch.Tensor,
        key: torch.Tensor,
        cos: torch.Tensor,
        sin: torch.Tensor,
    ):
        num_tokens = query.shape[0]
        head_size = query.shape[-1]
        # HPU RoPE kernel requires hidden dimension for cos and sin to be equal
        # to query hidden dimension, so the original tensors need to be
        # expanded
        # GPT-NeoX kernel requires position_ids = None, offset, mode = BLOCKWISE
        # and expansion of cos/sin tensors via concatenation
        rope_mode = RotaryPosEmbeddingMode.BLOCKWISE
        cos = torch.cat((cos, cos), dim=-1)
        sin = torch.cat((sin, sin), dim=-1)
        rotary_dim = cos.shape[-1]
        query_shape = query.shape
        query = query.view(num_tokens, -1, head_size)
        query_rot = query[..., :rotary_dim]
        query_pass = query[..., rotary_dim:]
        query_rot = apply_rotary_pos_emb(query_rot, cos, sin, None, 0, rope_mode)
        query.copy_(torch.cat((query_rot, query_pass), dim=-1).reshape(query_shape))

        key_shape = key.shape
        key = key.view(num_tokens, -1, head_size)
        key_rot = key[..., :rotary_dim]
        key_pass = key[..., rotary_dim:]
        key_rot = apply_rotary_pos_emb(key_rot, cos, sin, None, 0, rope_mode)
        key.copy_(torch.cat((key_rot, key_pass), dim=-1).reshape(key_shape))

    @classmethod
    def static(cls, config, dim, base, device):
        inv_freq = _create_inv_freq(dim, base, device)
        scaling_factor = None
        rope_scaling = _get_rope_config(config)
        if not hasattr(config, "max_position_embeddings") and hasattr(
            config, "max_seq_len"
        ):
            # handling for dbrx
            config.max_position_embeddings = config.max_seq_len
        if rope_scaling is not None:
            # `rope_type` is now standard in transformers, but some existing models
            # have `type` instead.
            rope_type = rope_scaling.get("rope_type", rope_scaling.get("type", None))

            if rope_type == "linear":
                pass
            elif rope_type == "default":
                pass
            elif rope_type == "mrope":
                mrope_section = rope_scaling["mrope_section"]
                if mrope_section is not None:
                    return RotaryPositionEmbeddingMultimodalSections(
                        inv_freq,
                        scaling_factor,
                        mrope_section,
                        config.max_position_embeddings,
                    )
            elif rope_type == "dynamic":
                scaling_factor = rope_scaling["factor"]
                return DynamicPositionRotaryEmbedding(
                    dim=dim,
                    max_position_embeddings=config.max_position_embeddings,
                    base=base,
                    device=inv_freq.device,
                    scaling_factor=scaling_factor,
                )
            elif rope_type == "llama3":
                inv_freq = apply_llama3_scaling(
                    inv_freq,
                    scaling_factor=rope_scaling["factor"],
                    low_freq_factor=rope_scaling["low_freq_factor"],
                    high_freq_factor=rope_scaling["high_freq_factor"],
                    original_max_position_embeddings=rope_scaling[
                        "original_max_position_embeddings"
                    ],
                )

                return cls(inv_freq, scaling_factor, config.max_position_embeddings)

            elif rope_type == "yarn":
                scaling_factor = rope_scaling["factor"]
                mscale = rope_scaling.get("mscale", 1.0)
                mscale_all_dim = rope_scaling.get("mscale_all_dim", 0.0)
                return YarnPositionRotaryEmbedding(
                    dim=2 * inv_freq.shape[0],
                    max_position_embeddings=rope_scaling[
                        "original_max_position_embeddings"
                    ],
                    base=base,
                    device=inv_freq.device,
                    scaling_factor=scaling_factor,
                    extrapolation_factor=1,
                    attn_factor=1,
                    beta_fast=32,
                    beta_slow=1,
                    mscale=mscale,
                    mscale_all_dim=mscale_all_dim,
                )
            elif rope_type in ["su", "longrope"]:
                short_factor = torch.tensor(
                    rope_scaling["short_factor"], dtype=torch.float32, device=device
                )
                short_inv_freq = 1.0 / (
                    short_factor
                    * base
                    ** (
                        torch.arange(0, dim, 2, device=device, dtype=torch.float32)
                        / dim
                    )
                )
                long_factor = torch.tensor(
                    rope_scaling["long_factor"], dtype=torch.float32, device=device
                )
                long_inv_freq = 1.0 / (
                    long_factor
                    * base
                    ** (
                        torch.arange(0, dim, 2, device=device, dtype=torch.float32)
                        / dim
                    )
                )

                original_max_position_embeddings = (
                    config.original_max_position_embeddings
                )
                max_position_embeddings = config.max_position_embeddings
                if max_position_embeddings <= original_max_position_embeddings:
                    scaling_factor = 1.0
                else:
                    scale = max_position_embeddings / original_max_position_embeddings
                    scaling_factor = math.sqrt(
                        1 + math.log(scale) / math.log(original_max_position_embeddings)
                    )

                # if short_mscale and long_mscale are provided we need to scale the freqs
                # using the Phi3LongRoPEScaledRotaryEmbedding
                if ("short_mscale" in rope_scaling) and ("long_mscale" in rope_scaling):
                    short_mscale = rope_scaling["short_mscale"]
                    long_mscale = rope_scaling["long_mscale"]
                    return Phi3LongRoPEScaledRotaryEmbedding(
                        short_inv_freq=short_inv_freq,
                        long_inv_freq=long_inv_freq,
                        max_position_embeddings=config.max_position_embeddings,
                        short_mscale=short_mscale,
                        long_mscale=long_mscale,
                        original_max_position_embeddings=original_max_position_embeddings,
                    )

                return SuRotaryEmbedding(
                    short_inv_freq=short_inv_freq,
                    long_inv_freq=long_inv_freq,
                    scaling_factor=scaling_factor,
                    original_max_position_embeddings=original_max_position_embeddings,
                    max_position_embeddings=config.max_position_embeddings,
                )
            else:
                raise NotImplementedError(
                    f"rope scaling type {rope_scaling['type']} is not implemented or invalid"
                )
        return cls(inv_freq, scaling_factor, config.max_position_embeddings)

    @classmethod
    def load(cls, config, prefix, weights):
        # XXX: Always load this in float32 !
        dtype = weights.dtype
        weights.dtype = torch.float32
        inv_freq = weights.get_tensor(f"{prefix}.inv_freq")
        weights.dtype = dtype

        scaling_factor = None
        rope_scaling = _get_rope_config(config)
        if rope_scaling is not None:
            scaling_factor = rope_scaling["factor"]
            if rope_scaling["type"] == "linear":
                pass
            elif rope_scaling["type"] == "dynamic":
                return DynamicPositionRotaryEmbedding(
                    dim=2 * inv_freq.shape[0],
                    max_position_embeddings=config.max_position_embeddings,
                    base=10000.0,
                    device=inv_freq.device,
                    scaling_factor=scaling_factor,
                )
            elif rope_scaling["type"] == "yarn":
                mscale = rope_scaling.get("mscale", 1.0)
                mscale_all_dim = rope_scaling.get("mscale_all_dim", 0.0)
                return YarnPositionRotaryEmbedding(
                    dim=2 * inv_freq.shape[0],
                    max_position_embeddings=rope_scaling[
                        "original_max_position_embeddings"
                    ],
                    base=10000.0,
                    device=inv_freq.device,
                    scaling_factor=scaling_factor,
                    extrapolation_factor=1,
                    attn_factor=1,
                    beta_fast=32,
                    beta_slow=1,
                    mscale=mscale,
                    mscale_all_dim=mscale_all_dim,
                )
            else:
                raise NotImplementedError(
                    f"rope scaling type {rope_scaling['type']} is not implemented or invalid"
                )
        return cls(inv_freq, scaling_factor, config.max_position_embeddings)

    def _update_cos_sin_cache(self, dtype, device, seqlen):
        # Reset the tables if the sequence length has changed,
        # or if we're on a new device (possibly due to tracing for instance)
        if (
            seqlen > self._seq_len_cached
            or self._cos_cached.device != device
            or self._cos_cached.dtype != dtype
        ):
            self._seq_len_cached = seqlen
            t = torch.arange(seqlen, device=device, dtype=self.inv_freq.dtype)
            if self.scaling_factor is not None:
                t /= self.scaling_factor
            # Don't do einsum, it converts fp32 to fp16
            # freqs = torch.einsum("i,j->ij", t, self.inv_freq)

            freqs = torch.outer(t, self.inv_freq.to(device=t.device))
            self._cos_cached = torch.cos(freqs).to(dtype)
            self._sin_cached = torch.sin(freqs).to(dtype)

    def get_cos_sin(self, position_ids: torch.Tensor):
        self._update_cos_sin_cache(
            torch.float32, position_ids.device, seqlen=self.max_position_embeddings
        )
        cos = torch.index_select(self._cos_cached, 0, position_ids)
        sin = torch.index_select(self._sin_cached, 0, position_ids)

        # Note: this unsqueeze is not necessary on RoCm + VLLM ROPE implementation, but we leave it as is to avoid yet an other controlflow.
        return cos.unsqueeze(1), sin.unsqueeze(1)


class SuRotaryEmbedding(PositionRotaryEmbedding):
    def __init__(
        self,
        short_inv_freq,
        long_inv_freq,
        scaling_factor,
        original_max_position_embeddings,
        max_position_embeddings,
    ):
        super(PositionRotaryEmbedding, self).__init__()
        self.short_inv_freq = short_inv_freq
        self.long_inv_freq = long_inv_freq
        self.scaling_factor = scaling_factor
        self.original_max_position_embeddings = original_max_position_embeddings
        self._seq_len_cached = 0
        self._cos_cached = None
        self._sin_cached = None
        self._cos_k_cached = None
        self._sin_k_cached = None
        self.dynamic_args = None

    def _update_cos_sin_cache(self, dtype, device, seqlen):
        # Reset the tables if the sequence length has changed,
        # or if we're on a new device (possibly due to tracing for instance)
        if (
            seqlen > self._seq_len_cached
            or self._cos_cached is None
            or self._cos_cached.device != device
            or self._cos_cached.dtype != dtype
        ):
            self._seq_len_cached = seqlen

            t = torch.arange(seqlen, device=device, dtype=self.short_inv_freq.dtype)
            short_freqs = torch.outer(
                t[: self.original_max_position_embeddings],
                self.short_inv_freq.to(device=t.device),
            )
            long_freqs = torch.outer(
                t[self.original_max_position_embeddings :],
                self.long_inv_freq.to(device=t.device),
            )

            freqs = torch.cat([short_freqs, long_freqs])

            self._cos_cached = (torch.cos(freqs) * self.scaling_factor).to(dtype)
            self._sin_cached = (torch.sin(freqs) * self.scaling_factor).to(dtype)


class Phi3LongRoPEScaledRotaryEmbedding(PositionRotaryEmbedding):
    def __init__(
        self,
        short_inv_freq: torch.Tensor,
        long_inv_freq: torch.Tensor,
        max_position_embeddings: int,
        short_mscale: float,
        long_mscale: float,
        original_max_position_embeddings: int,
    ):
        super(PositionRotaryEmbedding, self).__init__()
        self.short_inv_freq = short_inv_freq
        self.long_inv_freq = long_inv_freq
        self.max_position_embeddings = max_position_embeddings
        self.short_mscale = short_mscale
        self.long_mscale = long_mscale
        self.original_max_position_embeddings = original_max_position_embeddings

        # cache
        self._seq_len_cached = 0
        self._cos_cached = None
        self._sin_cached = None
        self._cos_k_cached = None
        self._sin_k_cached = None
        self.dynamic_args = None

    def _update_cos_sin_cache(self, dtype, device, seqlen):
        if (
            seqlen > self._seq_len_cached
            or self._cos_cached is None
            or self._cos_cached.device != device
            or self._cos_cached.dtype != dtype
        ):
            self._seq_len_cached = seqlen
            t = torch.arange(seqlen, device=device, dtype=self.short_inv_freq.dtype)

            short_freqs = torch.outer(
                t[: self.original_max_position_embeddings],
                self.short_inv_freq.to(device=t.device),
            )

            long_freqs = torch.outer(
                t[self.original_max_position_embeddings :],
                self.long_inv_freq.to(device=t.device),
            )

            short_freqs = short_freqs * self.short_mscale
            long_freqs = long_freqs * self.long_mscale

            freqs = torch.empty((seqlen, short_freqs.shape[1]), device=device)
            freqs[: self.original_max_position_embeddings] = short_freqs
            freqs[self.original_max_position_embeddings :] = long_freqs

            self._cos_cached = torch.cos(freqs).to(dtype)
            self._sin_cached = torch.sin(freqs).to(dtype)


class DynamicPositionRotaryEmbedding(PositionRotaryEmbedding):
    def __init__(self, dim, max_position_embeddings, base, device, scaling_factor):
        inv_freq = _create_inv_freq(dim, base, device)
        super().__init__(inv_freq, scaling_factor, max_position_embeddings)
        self.dim = dim
        self.max_position_embeddings = max_position_embeddings
        self.base = base

    def _update_cos_sin_cache(self, dtype, device, seqlen):
        # Reset the tables if the sequence length has changed,
        # or if we're on a new device (possibly due to tracing for instance)
        if (
            seqlen > self._seq_len_cached
            or self._cos_cached.device != device
            or self._cos_cached.dtype != dtype
        ):
            if seqlen > self.max_position_embeddings:
                newbase = self.base * (
                    (self.scaling_factor * seqlen / self.max_position_embeddings)
                    - (self.scaling_factor - 1)
                ) ** (self.dim / (self.dim - 2))
                self.inv_freq = _create_inv_freq(
                    self.dim, newbase, self.inv_freq.device
                )
            self._seq_len_cached = seqlen
            t = torch.arange(seqlen, device=device, dtype=self.inv_freq.dtype)
            # Don't do einsum, it converts fp32 to fp16
            # freqs = torch.einsum("i,j->ij", t, self.inv_freq)

            freqs = torch.outer(t, self.inv_freq.to(device=t.device))
            self._cos_cached = torch.cos(freqs).to(dtype)
            self._sin_cached = torch.sin(freqs).to(dtype)


def find_correction_dim(num_rotations, dim, base=10000, max_position_embeddings=2048):
    return (dim * math.log(max_position_embeddings / (num_rotations * 2 * math.pi))) / (
        2 * math.log(base)
    )


# Find dim range bounds based on rotations
def find_correction_range(
    low_rot, high_rot, dim, base=10000, max_position_embeddings=2048
):
    low = math.floor(find_correction_dim(low_rot, dim, base, max_position_embeddings))
    high = math.ceil(find_correction_dim(high_rot, dim, base, max_position_embeddings))
    return max(low, 0), min(high, dim - 1)  # Clamp values just in case


def linear_ramp_mask(min, max, dim):
    if min == max:
        max += 0.001  # Prevent singularity

    linear_func = (torch.arange(dim, dtype=torch.float32) - min) / (max - min)
    ramp_func = torch.clamp(linear_func, 0, 1)
    return ramp_func


def get_mscale(scale: float = 1.0, mscale: float = 1.0):
    if scale <= 1:
        return 1.0
    return 0.1 * mscale * math.log(scale) + 1.0


class YarnPositionRotaryEmbedding(PositionRotaryEmbedding):
    def __init__(
        self,
        dim,
        max_position_embeddings,
        base,
        device,
        scaling_factor,
        *,
        extrapolation_factor,
        attn_factor,
        beta_fast,
        beta_slow,
        mscale: float,
        mscale_all_dim: float,
    ):
        inv_freq = _create_inv_freq(dim, base, device)
        self.dim = dim
        self.max_position_embeddings = max_position_embeddings
        self.base = base
        self.extrapolation_factor = extrapolation_factor
        self.attn_factor = attn_factor
        self.beta_fast = beta_fast
        self.beta_slow = beta_slow
        self.mscale_all_dim = mscale_all_dim
        self.scaling_factor = scaling_factor
        self.mscale = float(
            get_mscale(self.scaling_factor, mscale)
            / get_mscale(self.scaling_factor, mscale_all_dim)
            * self.attn_factor
        )  # Get n-d magnitude scaling corrected for interpolation
        super().__init__(inv_freq, scaling_factor, max_position_embeddings)

    def _update_cos_sin_cache(self, dtype, device, seqlen):
        # Reset the tables if the sequence length has changed,
        # or if we're on a new device (possibly due to tracing for instance)
        if (
            seqlen > self._seq_len_cached
            or self._cos_cached.device != device
            or self._cos_cached.dtype != dtype
        ):
            if seqlen > self.max_position_embeddings or True:
                inv_freq_extrapolation = _create_inv_freq(
                    self.dim, self.base, self.inv_freq.device
                )
                freqs = 1.0 / inv_freq_extrapolation
                inv_freq_interpolation = 1.0 / (self.scaling_factor * freqs)
                low, high = find_correction_range(
                    self.beta_fast,
                    self.beta_slow,
                    self.dim,
                    self.base,
                    self.max_position_embeddings,
                )

                inv_freq_mask = (
                    1 - linear_ramp_mask(low, high, self.dim // 2).float().to(device)
                ) * self.extrapolation_factor  # Get n-d rotational scaling corrected for extrapolation
                inv_freq = (
                    inv_freq_interpolation * (1 - inv_freq_mask)
                    + inv_freq_extrapolation * inv_freq_mask
                )

                self.inv_freq = inv_freq

            self._seq_len_cached = seqlen
            t = torch.arange(seqlen, device=device, dtype=self.inv_freq.dtype)
            # Don't do einsum, it converts fp32 to fp16
            # freqs = torch.einsum("i,j->ij", t, self.inv_freq)

            freqs = torch.outer(t, self.inv_freq.to(device=t.device))
            self._cos_cached = (torch.cos(freqs) * self.mscale).to(dtype)
            self._sin_cached = (torch.sin(freqs) * self.mscale).to(dtype)


def apply_llama3_scaling(
    freqs: torch.Tensor,
    *,
    scaling_factor: int,
    low_freq_factor: int,
    high_freq_factor: int,
    original_max_position_embeddings: int,
):
    low_freq_wavelen = original_max_position_embeddings / low_freq_factor
    high_freq_wavelen = original_max_position_embeddings / high_freq_factor
    new_freqs = []

    for freq in freqs:
        wavelen = 2 * math.pi / freq

        if wavelen < high_freq_wavelen:
            new_freqs.append(freq)
        elif wavelen > low_freq_wavelen:
            new_freqs.append(freq / scaling_factor)
        else:
            assert low_freq_wavelen != high_freq_wavelen
            smooth = (original_max_position_embeddings / wavelen - low_freq_factor) / (
                high_freq_factor - low_freq_factor
            )
            new_freqs.append((1 - smooth) * freq / scaling_factor + smooth * freq)

    return torch.tensor(new_freqs, dtype=freqs.dtype, device=freqs.device)


class RotaryPositionEmbeddingMultimodalSections(PositionRotaryEmbedding):
    def __init__(
        self,
        inv_freq: torch.Tensor,
        scaling_factor: float,
        sections: list,
        max_position_embeddings,
    ):
        self.sections = sections
        self._cos_cached = None
        self._sin_cached = None
        self.section_indices = (
            torch.arange(len(self.sections))
            .repeat_interleave(torch.tensor(self.sections))
            .view(1, 1, -1)
            .to(inv_freq.device)
        )
        super().__init__(inv_freq, scaling_factor, max_position_embeddings)

    def _update_cos_sin_cache(
        self, dtype: torch.dtype, device: torch.device, seqlen: int
    ):
        # always cache the cos/sin for the full sequence length to avoid
        # recomputing if the sequence length is smaller than the cached one
        if (
            seqlen > self._seq_len_cached
            or self._cos_cached.device != device
            or self._cos_cached.dtype != dtype
        ):
            self._seq_len_cached = seqlen
            t = torch.arange(seqlen, device=device, dtype=self.inv_freq.dtype)
            freqs = torch.outer(t, self.inv_freq.to(device=t.device))
            self._cos_cached = torch.cos(freqs).to(dtype)
            self._sin_cached = torch.sin(freqs).to(dtype)
            self._sections = self.section_indices.expand(seqlen, -1, -1)

    def get_cos_sin(
        self,
        position_ids: torch.Tensor,
    ):
        slen = position_ids.shape[0]
        self._update_cos_sin_cache(
            torch.float32, position_ids.device, seqlen=self.max_position_embeddings
        )

        cos = self._cos_cached[position_ids].gather(1, self._sections[:slen])
        sin = self._sin_cached[position_ids].gather(1, self._sections[:slen])
        return cos, sin
