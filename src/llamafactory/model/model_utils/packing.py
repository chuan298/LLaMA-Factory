# Copy from original implementation of src/axolotl/monkeypatch/multipack.py and src/axolotl/monkeypatch/utils.py from axolotl library with some changes
"""
Shared utils for the monkeypatches
"""
from typing import Optional, TYPE_CHECKING

import torch
import torch.nn.functional as F

import importlib

import transformers
from accelerate import init_empty_weights
from transformers import AutoConfig, AutoModelForCausalLM
from transformers.modeling_attn_mask_utils import (
    _prepare_4d_causal_attention_mask,
    _prepare_4d_causal_attention_mask_for_sdpa,
)
from transformers.utils import is_torch_bf16_gpu_available

from ...extras.logging import get_logger
from ...extras.constants import SUPPORTED_CLASS_EFFECIENT_PACKING

if TYPE_CHECKING:
    from transformers import PretrainedConfig

    from ...hparams import ModelArguments, DataArguments


logger = get_logger(__name__)


@torch.jit.script
def get_max_seqlen_in_batch(attention_mask: torch.Tensor) -> torch.Tensor:
    max_num = int(torch.max(attention_mask).item())
    batch_size, _ = attention_mask.shape
    counts = torch.zeros((batch_size, max_num), dtype=torch.int32)

    for i in range(1, max_num + 1):
        mask = attention_mask == i
        counts[:, i - 1] = torch.sum(mask, dim=-1).to(dtype=torch.int32)

    result = counts.flatten()
    nonzero_indices = torch.nonzero(result).squeeze(-1)
    return result[nonzero_indices]


@torch.jit.script
def get_unpad_data(attention_mask: torch.Tensor):
    device = attention_mask.device
    seqlens_in_batch = get_max_seqlen_in_batch(attention_mask)
    indices = torch.nonzero(attention_mask.flatten()).flatten()
    max_seqlen_in_batch = seqlens_in_batch.max().item()
    cu_seqlens = (
        F.pad(torch.cumsum(seqlens_in_batch, dim=0, dtype=torch.int32), (1, 0))
        .to(device=device)
        .detach()
    )
    return (
        indices,
        cu_seqlens,
        max_seqlen_in_batch,
    )

def mask_2d_to_4d(
    mask: torch.Tensor, dtype: torch.dtype, tgt_len: Optional[int] = None
):
    """
    Expands attention_mask from `[bsz, seq_len]` to `[bsz, 1, tgt_seq_len, src_seq_len]`.
    This expansion handles packed sequences so that sequences share the same attention mask integer value
    when they attend to each other within that sequence.
    This expansion transforms the mask to lower triangular form to prevent future peeking.
    """
    bsz, src_len = mask.size()
    tgt_len = tgt_len if tgt_len is not None else src_len

    mask = mask.unsqueeze(1).unsqueeze(2)
    mask = mask.expand(bsz, 1, tgt_len, src_len)

    # Create a binary mask from the original mask where zeros remain zeros and all other values are set to one
    binary_mask = torch.where(
        mask != 0,
        torch.tensor(1, device=mask.device).to(dtype),
        torch.tensor(0, device=mask.device).to(dtype),
    )

    # Create a block-diagonal mask.
    # we multiply by the binary mask so that 0's in the original mask are correctly excluded
    zero_one_mask = torch.eq(mask, mask.transpose(-1, -2)).int() * binary_mask

    # Now let's create a lower triangular mask of ones that will zero out the upper triangular part
    lower_triangular_ones = torch.tril(torch.ones((tgt_len, src_len), dtype=dtype)).to(
        mask.device
    )

    # Use the lower triangular mask to zero out the upper triangular part of the zero_one_mask
    masked_zero_one_mask = zero_one_mask * lower_triangular_ones

    return masked_zero_one_mask


def patched_prepare_4d_causal_attention_mask(
    attention_mask: Optional[torch.Tensor],
    *args,
):
    dtype = torch.bfloat16 if is_torch_bf16_gpu_available() else torch.float32
    return _prepare_4d_causal_attention_mask(
        mask_2d_to_4d(attention_mask, dtype=dtype),
        *args,
    )


def patched_prepare_4d_causal_attention_mask_for_sdpa(
    attention_mask: Optional[torch.Tensor],
    *args,
):
    dtype = torch.bfloat16 if is_torch_bf16_gpu_available() else torch.float32
    return _prepare_4d_causal_attention_mask_for_sdpa(
        mask_2d_to_4d(attention_mask, dtype=dtype),
        *args,
    )


def set_module_name(model, name, value):
    if "." in name:
        parent_name = name.rsplit(".", 1)[0]
        child_name = name[len(parent_name) + 1 :]
        parent = model.get_submodule(parent_name)
    else:
        parent_name = ""
        parent = model
        child_name = name

    setattr(parent, child_name, value)


# Copy from original implementation of modeling_mixtral.py from transformers, Just change a little bit with new_attention_mask
def load_balancing_loss_func(
    gate_logits: torch.Tensor,
    num_experts: torch.Tensor = None,
    top_k=2,
    attention_mask: Optional[torch.Tensor] = None,
) -> float:
    r"""
    Computes auxiliary load balancing loss as in Switch Transformer - implemented in Pytorch.

    See Switch Transformer (https://arxiv.org/abs/2101.03961) for more details. This function implements the loss
    function presented in equations (4) - (6) of the paper. It aims at penalizing cases where the routing between
    experts is too unbalanced.

    Args:
        gate_logits (Union[`torch.Tensor`, Tuple[torch.Tensor]):
            Logits from the `gate`, should be a tuple of model.config.num_hidden_layers tensors of
            shape [batch_size X sequence_length, num_experts].
        attention_mask (`torch.Tensor`, None):
            The attention_mask used in forward function
            shape [batch_size X sequence_length] if not None.
        num_experts (`int`, *optional*):
            Number of experts

    Returns:
        The auxiliary loss.
    """
    if gate_logits is None or not isinstance(gate_logits, tuple):
        return 0

    if isinstance(gate_logits, tuple):
        compute_device = gate_logits[0].device
        concatenated_gate_logits = torch.cat(
            [layer_gate.to(compute_device) for layer_gate in gate_logits], dim=0
        )

    routing_weights = torch.nn.functional.softmax(concatenated_gate_logits, dim=-1)

    _, selected_experts = torch.topk(routing_weights, top_k, dim=-1)

    expert_mask = torch.nn.functional.one_hot(selected_experts, num_experts)

    if attention_mask is None:
        # Compute the percentage of tokens routed to each experts
        tokens_per_expert = torch.mean(expert_mask.float(), dim=0)

        # Compute the average probability of routing to these experts
        router_prob_per_expert = torch.mean(routing_weights, dim=0)
    else:
        # ONLY ADD THIS LINE OF CODE, AND REPLACE attention_mask WITH new_attention_mask
        new_attention_mask = (attention_mask != 0).int().to(attention_mask.device)
        batch_size, sequence_length = new_attention_mask.shape
        num_hidden_layers = concatenated_gate_logits.shape[0] // (
            batch_size * sequence_length
        )

        # Compute the mask that masks all padding tokens as 0 with the same shape of expert_mask
        expert_attention_mask = (
            new_attention_mask[None, :, :, None, None]
            .expand(
                (num_hidden_layers, batch_size, sequence_length, top_k, num_experts)
            )
            .reshape(-1, top_k, num_experts)
            .to(compute_device)
        )

        # Compute the percentage of tokens routed to each experts
        tokens_per_expert = torch.sum(
            expert_mask.float() * expert_attention_mask, dim=0
        ) / torch.sum(expert_attention_mask, dim=0)

        # Compute the mask that masks all padding tokens as 0 with the same shape of tokens_per_expert
        router_per_expert_attention_mask = (
            new_attention_mask[None, :, :, None]
            .expand((num_hidden_layers, batch_size, sequence_length, num_experts))
            .reshape(-1, num_experts)
            .to(compute_device)
        )

        # Compute the average probability of routing to these experts
        router_prob_per_expert = torch.sum(
            routing_weights * router_per_expert_attention_mask, dim=0
        ) / torch.sum(router_per_expert_attention_mask, dim=0)

    overall_loss = torch.sum(tokens_per_expert * router_prob_per_expert.unsqueeze(0))
    return overall_loss * num_experts


def patch_for_multipack(model_type, model_name, attn_implementation):
    if attn_implementation == "flash_attention_2":
        if model_type == "llama":
            transformers.models.llama.modeling_llama._get_unpad_data = (  # pylint: disable=protected-access
                get_unpad_data
            )
        elif model_type == "mistral":
            transformers.models.mistral.modeling_mistral._get_unpad_data = (  # pylint: disable=protected-access
                get_unpad_data
            )
        elif model_type == "mixtral":
            transformers.models.mixtral.modeling_mixtral._get_unpad_data = (  # pylint: disable=protected-access
                get_unpad_data
            )
            transformers.models.mixtral.modeling_mixtral.load_balancing_loss_func = (  # pylint: disable=protected-access
                load_balancing_loss_func
            )
        elif model_type == "qwen2":
            transformers.models.qwen2.modeling_qwen2._get_unpad_data = (  # pylint: disable=protected-access
                get_unpad_data
            )
        elif model_type == "qwen2_moe":
            transformers.models.qwen2_moe.modeling_qwen2_moe._get_unpad_data = (  # pylint: disable=protected-access
                get_unpad_data
            )
            transformers.models.qwen2_moe.modeling_qwen2_moe.load_balancing_loss_func = (  # pylint: disable=protected-access
                load_balancing_loss_func
            )
        elif model_type == "falcon":
            transformers.models.falcon.modeling_falcon._get_unpad_data = (  # pylint: disable=protected-access
                get_unpad_data
            )
        elif model_type == "phi":
            transformers.models.phi.modeling_phi._get_unpad_data = (  # pylint: disable=protected-access
                get_unpad_data
            )
        elif model_type == "phi3":
            transformers.models.phi3.modeling_phi3._get_unpad_data = (  # pylint: disable=protected-access
                get_unpad_data
            )
        elif model_type == "gemma":
            transformers.models.gemma.modeling_gemma._get_unpad_data = (  # pylint: disable=protected-access
                get_unpad_data
            )
        elif model_type == "starcoder2":
            transformers.models.starcoder2.modeling_starcoder2._get_unpad_data = (  # pylint: disable=protected-access
                get_unpad_data
            )
        elif model_type == "gemmoe":
            patch_remote(model_name, ".configuration_gemmoe", ".modeling_gemmoe")
        elif model_type == "jamba":
            patch_remote(model_name, ".configuration_jamba", ".modeling_jamba")
    else:
        transformers.modeling_attn_mask_utils._prepare_4d_causal_attention_mask_for_sdpa = (  # pylint: disable=protected-access
            patched_prepare_4d_causal_attention_mask_for_sdpa
        )
        transformers.modeling_attn_mask_utils._prepare_4d_causal_attention_mask = (  # pylint: disable=protected-access
            patched_prepare_4d_causal_attention_mask
        )


def patch_remote(model_name, config_name, modeling_name):
    model_config = AutoConfig.from_pretrained(model_name, trust_remote_code=True)
    # we need to load the model here in order for modeling_* to be available
    with init_empty_weights():
        AutoModelForCausalLM.from_pretrained(model_name, trust_remote_code=True)
    module_name = model_config.__class__.__module__.replace(config_name, modeling_name)
    modeling_arch = importlib.import_module(module_name)
    modeling_arch._get_unpad_data = get_unpad_data  # pylint: disable=protected-access

    # check exist load_balancing_loss_func for moe model
    if hasattr(modeling_arch, "load_balancing_loss_func"):
        modeling_arch.load_balancing_loss_func = load_balancing_loss_func


def configure_packing(config: "PretrainedConfig", model_args: "ModelArguments") -> None:
    if getattr(config, "model_type", None) == "internlm2":  # special case for custom models
        attn_implementation = getattr(config, "attn_implementation", "")
    else:
        attn_implementation = getattr(config, "_attn_implementation", "")

    if getattr(config, "model_type", None) in SUPPORTED_CLASS_EFFECIENT_PACKING:
        patch_for_multipack(getattr(config, "model_type", None), model_args.model_name_or_path, attn_implementation)
        logger.info("Using packing sequences without cross-contamination attention for efficient training.")
    else:
        raise ValueError("Current model does not support packing sequences for efficient training. Please set config `efficient_packing` is False")