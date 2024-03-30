import torch
import torch.nn as nn
from loguru import logger
import gc
from transformers.models.llama.modeling_llama import LlamaRMSNorm
from transformers.models.mistral.modeling_mistral import MistralRMSNorm
from .base_blockwise_quantization import BaseBlockwiseQuantization
from llmc.utils.registry_factory import ALGO_REGISTRY
from .module_utils import FakeQuantLinear


@ALGO_REGISTRY
class HQQ(BaseBlockwiseQuantization):
    def __init__(self, model, quant_config, input=None, config=None):
        super().__init__(model, quant_config, input, config)
        self.add_quant_config()

    @torch.no_grad()
    def add_quant_config(self):
        self.lp_norm = self.quant_config["special"]["lp_norm"]
        self.beta = self.quant_config["special"]["beta"]
        self.kappa = self.quant_config["special"]["kappa"]
        self.iters = self.quant_config["special"]["iters"]
        self.axis = self.quant_config["special"]["axis"]
        if self.lp_norm == 1:
            self.shrink_op = lambda x, beta: torch.sign(x) * torch.nn.functional.relu(
                torch.abs(x) - 1.0 / self.beta
            )
        else:
            self.shrink_op = lambda x, beta, p=self.lp_norm: torch.sign(
                x
            ) * torch.nn.functional.relu(
                torch.abs(x) - (1.0 / self.beta) * torch.pow(torch.abs(x), p - 1)
            )

    @torch.no_grad()
    def optimize_weights_proximal(self, W_f, scales, zeros, max_int, min_int):
        best_error = 1e4
        current_beta = self.beta
        current_kappa = self.kappa
        scales = 1 / scales
        for i in range(self.iters):
            W_q = torch.round(W_f * scales + zeros).clamp(min_int, max_int)
            W_r = (W_q - zeros) / scales
            W_e = self.shrink_op(W_f - W_r, current_beta)

            zeros = torch.mean(W_q - (W_f - W_e) * scales, axis=-1, keepdim=True)
            current_beta *= current_kappa
            current_error = float(torch.abs(W_f - W_r).mean())

            logger.info(f"iter : {i}, error : {current_error}")

            if current_error < best_error:
                best_error = current_error
            else:
                break

        torch.cuda.empty_cache()
        scales = 1 / scales

        return scales, zeros

    @torch.no_grad()
    def block_opt(self, block, idx):
        block = block.cuda()
        named_linears = self.model.get_block_linears(block)
        logger.info(f"named_linears: {named_linears}")

        for name in named_linears:
            logger.info(f"Optimize weights proximal of {name}")
            layer = named_linears[name]

            tensor = layer.weight.data.float()
            if self.axis == 0:
                tensor = tensor.T
            (
                tensor,
                org_scales,
                org_zeros,
                max_int,
                min_int,
            ) = self.wquantizer.get_tensor_qparams(tensor)

            best_scales, best_zeros = self.optimize_weights_proximal(
                tensor, org_scales, org_zeros, max_int, min_int
            )
            layer.register_buffer("buf_scales", best_scales)
            layer.register_buffer("buf_zeros", best_zeros)
            layer.register_buffer("buf_max_int", torch.tensor(max_int))
            layer.register_buffer("buf_min_int", torch.tensor(min_int))

        block = block.cpu()
        gc.collect()
        torch.cuda.empty_cache()

    def w_qdq(self, module):
        args = {}
        if self.axis == 0:
            args["dim"] = "ic"
        args["scales"] = module.buf_scales
        args["zeros"] = module.buf_zeros
        args["max_int"] = module.buf_max_int
        args["min_int"] = module.buf_min_int

        return self.wquantizer.fake_quant_weight_static(module.weight, args)
