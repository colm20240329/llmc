import torch
from torch import nn
from loguru import logger


class Quantizer:
    def __init__(self, bit, symmetric, granularity, **kwargs):
        self.bit = bit
        self.sym = symmetric
        self.granularity = granularity
        self.kwargs = kwargs

        if "calib_algo" in self.kwargs:
            self.calib_algo = self.kwargs["calib_algo"]
        else:
            self.calib_algo = "minmax"

        if self.sym:
            self.max_int = 2 ** (self.bit - 1) - 1
            self.min_int = -(2 ** (self.bit - 1))
        else:
            self.max_int = 2**self.bit - 1
            self.min_int = 0.0

        if self.granularity == "per_group":
            self.group_size = self.kwargs["group_size"]
        elif self.granularity == "per_head":
            self.head_num = self.kwargs["head_num"]

        if "ste" in self.kwargs and self.kwargs["ste"]:
            self.round_func = lambda x: (x.round() - x).detach() + x
        else:
            self.round_func = torch.round

        self.round_zp = "round_zp" not in self.kwargs or self.kwargs["round_zp"]
        self.sigmoid = torch.nn.Sigmoid()

    def get_tensor_range(self, tensor, args={}):
        if self.calib_algo == "minmax":
            return self.get_minmax_range(tensor)
        elif self.calib_algo == "mse":
            return self.get_mse_range(tensor, **args)
        elif self.calib_algo == "learnable":
            return self.get_learnable_range(tensor, **args)
        else:
            logger.info("Calibration Algorithm Not Found!")

    def get_minmax_range(self, tensor):
        if self.granularity == "per_tensor":
            max_val = torch.max(tensor)
            min_val = torch.min(tensor)
        else:
            max_val = tensor.amax(dim=-1, keepdim=True)
            min_val = tensor.amin(dim=-1, keepdim=True)

        return (min_val, max_val)

    def get_mse_range(self, tensor, grid=100, norm=2.4, maxshrink=0.8, bs=1024):
        assert tensor.shape[0] % bs == 0
        tensor = tensor.float()
        min_val, max_val = self.get_minmax_range(tensor)

        dev = tensor.device

        for b_num in range(tensor.shape[0] // bs):
            _tensor = tensor[b_num * bs : (b_num + 1) * bs, :]
            _min_val, _max_val = (
                min_val[b_num * bs : (b_num + 1) * bs, :],
                max_val[b_num * bs : (b_num + 1) * bs, :],
            )

            best = torch.full([_tensor.shape[0]], float("inf"), device=dev)

            best_min_val, best_max_val = _min_val, _max_val

            for i in range(int(maxshrink * grid)):
                p = 1 - i / grid

                xmin = p * _min_val
                xmax = p * _max_val

                scales, zeros, max_int, min_int = self.get_qparams((xmin, xmax), dev)
                q_tensor = self.quant_dequant(_tensor, scales, zeros, max_int, min_int)

                q_tensor -= _tensor
                q_tensor.abs_()
                q_tensor.pow_(norm)
                err = torch.sum(q_tensor, 1)

                tmp = err < best

                if torch.any(tmp):
                    best[tmp] = err[tmp]
                    best_min_val[tmp] = xmin[tmp]
                    best_max_val[tmp] = xmax[tmp]

            (
                min_val[b_num * bs : (b_num + 1) * bs, :],
                max_val[b_num * bs : (b_num + 1) * bs, :],
            ) = (best_min_val, best_max_val)

        return (min_val, max_val)

    def get_learnable_range(self, tensor, lowbound_factor=None, upbound_factor=None):
        min_val, max_val = self.get_minmax_range(tensor)
        if lowbound_factor is not None:
            min_val = self.sigmoid(lowbound_factor) * min_val
            max_val = self.sigmoid(upbound_factor) * max_val

        return (min_val, max_val)

    def get_qparams(self, tensor_range, device):
        min_val, max_val = tensor_range[0], tensor_range[1]
        max_int = self.max_int
        min_int = self.min_int
        if self.sym:
            abs_max = torch.max(max_val.abs(), min_val.abs())
            abs_max = abs_max.clamp(min=1e-5)
            scales = abs_max / max_int
            zeros = None
        else:
            scales = (max_val - min_val).clamp(min=1e-5) / max_int
            zeros = (-torch.round(min_val / scales)).clamp(min_int, max_int)
            if not self.round_zp:
                zeros = -min_val / scales
        return scales, zeros, max_int, min_int

    def get_tensor_qparams(self, tensor, args={}):
        tensor = self.reshape_tensor(tensor)
        tensor_range = self.get_tensor_range(tensor, args)
        scales, zeros, max_int, min_int = self.get_qparams(tensor_range, tensor.device)
        return tensor, scales, zeros, max_int, min_int

    def quant(self, tensor, scales, zeros, max_int, min_int):
        if zeros is None:
            tensor = torch.clamp(self.round_func(tensor / scales), min_int, max_int)
        elif self.round_zp:
            tensor = torch.clamp(
                self.round_func(tensor / scales) + zeros, min_int, max_int
            )
        else:
            tensor = torch.clamp(
                self.round_func(tensor / scales.clamp_min(1e-9) + zeros),
                min_int,
                max_int,
            )
        return tensor

    def dequant(self, tensor, scales, zeros):
        if zeros is None:
            tensor = tensor * scales
        else:
            tensor = (tensor - zeros) * scales
        return tensor

    def quant_dequant(self, tensor, scales, zeros, max_int, min_int):
        tensor = self.quant(tensor, scales, zeros, max_int, min_int)
        tensor = self.dequant(tensor, scales, zeros)
        return tensor

    def reshape_tensor(self, tensor):
        if self.granularity == "per_group":
            if tensor.shape[1] >= self.group_size:
                t = tensor.reshape(-1, self.group_size)
            else:
                t = tensor
        elif self.granularity == "per_head":
            t = tensor.reshape(self.heda_num, -1)
        else:
            t = tensor
        return t

    def restore_tensor(self, tensor, shape):
        if tensor.shape == shape:
            t = tensor
        else:
            t = tensor.reshape(shape)
        return t

    def fake_quant_act_static(self, act, args={}):
        if "int_indices" in args:
            q_act = act[:, :, args["int_indices"]]
            fp_act = act[:, :, args["fp_indices"]]
        else:
            q_act = act

        if "current_bit" in args:
            org_bit = self.bit
            self.bit = args["current_bit"]

        org_act_shape = q_act.shape
        org_act_dtype = q_act.dtype

        scales, zeros, max_int, min_int = (
            args["scales"],
            args["zeros"],
            args["max_int"],
            args["min_int"],
        )
        q_act = self.reshape_tensor(q_act)
        q_act = self.quant_dequant(q_act, scales, zeros, max_int, min_int)
        q_act = self.restore_tensor(q_act, org_act_shape).to(org_act_dtype)

        if "current_bit" in args:
            self.bit = org_bit

        if "int_indices" in args:
            mix_act = torch.zeros_like(act)
            mix_act[:, :, args["int_indices"]] = q_act
            mix_act[:, :, args["fp_indices"]] = fp_act
            return mix_act

        return q_act

    # support mix precison quant act
    def fake_quant_act_dynamic(self, act, args={}):
        if "int_indices" in args:
            q_act = act[:, :, args["int_indices"]]
            fp_act = act[:, :, args["fp_indices"]]
        else:
            q_act = act

        if "current_bit" in args:
            org_bit = self.bit
            self.bit = args["current_bit"]

        org_act_shape = q_act.shape
        org_act_dtype = q_act.dtype

        q_act, scales, zeros, max_int, min_int = self.get_tensor_qparams(q_act, args)
        q_act = self.quant_dequant(q_act, scales, zeros, max_int, min_int)
        q_act = self.restore_tensor(q_act, org_act_shape).to(org_act_dtype)

        if "current_bit" in args:
            self.bit = org_bit

        if "int_indices" in args:
            mix_act = torch.zeros_like(act)
            mix_act[:, :, args["int_indices"]] = q_act
            mix_act[:, :, args["fp_indices"]] = fp_act
            return mix_act

        return q_act

    def fake_quant_weight_static(self, weight, args):
        if "int_indices" in args:
            if self.granularity == "per_group":
                assert len(args["int_indices"]) % self.group_size == 0
            q_weight = weight[:, args["int_indices"]]
            fp_weight = weight[:, args["fp_indices"]]

        elif "dim" in args and "ic" in args["dim"]:
            q_weight = weight.T
        else:
            q_weight = weight

        org_w_shape = q_weight.shape
        org_w_dtype = q_weight.dtype
        scales, zeros, max_int, min_int = (
            args["scales"],
            args["zeros"],
            args["max_int"],
            args["min_int"],
        )
        q_weight = self.reshape_tensor(q_weight)
        q_weight = self.quant_dequant(q_weight, scales, zeros, max_int, min_int)
        q_weight = self.restore_tensor(q_weight, org_w_shape).to(org_w_dtype)

        if "int_indices" in args:
            mix_weight = torch.zeros_like(weight)
            mix_weight[:, args["int_indices"]] = q_weight
            mix_weight[:, args["fp_indices"]] = fp_weight
            return mix_weight

        elif "dim" in args and "ic" in args["dim"]:
            q_weight = q_weight.T

        return q_weight

    # support mix precison quant weight
    def fake_quant_weight_dynamic(self, weight, args={}):
        if "int_indices" in args:
            if self.granularity == "per_group":
                assert len(args["int_indices"]) % self.group_size == 0
            q_weight = weight[:, args["int_indices"]]
            fp_weight = weight[:, args["fp_indices"]]

        elif "dim" in args and "ic" in args["dim"]:
            q_weight = weight.T
        else:
            q_weight = weight

        if "current_bit" in args:
            org_bit = self.bit
            self.bit = args["current_bit"]

        org_w_shape = q_weight.shape
        org_w_dtype = q_weight.dtype
        q_weight, scales, zeros, max_int, min_int = self.get_tensor_qparams(
            q_weight, args
        )

        q_weight = self.quant_dequant(q_weight, scales, zeros, max_int, min_int)
        q_weight = self.restore_tensor(q_weight, org_w_shape).to(org_w_dtype)

        if "current_bit" in args:
            self.bit = org_bit

        if "int_indices" in args:
            mix_weight = torch.zeros_like(weight)
            mix_weight[:, args["int_indices"]] = q_weight
            mix_weight[:, args["fp_indices"]] = fp_weight
            return mix_weight

        elif "dim" in args and "ic" in args["dim"]:
            q_weight = q_weight.T

        return q_weight

    def real_quant_weight_static(self, weight, args):
        org_w_shape = weight.shape
        scales, zeros, max_int, min_int = (
            args["scales"],
            args["zeros"],
            args["max_int"],
            args["min_int"],
        )
        weight = self.reshape_tensor(weight)
        weight = self.quant(weight, scales, zeros, max_int, min_int)
        weight = self.restore_tensor(weight, org_w_shape)

        if self.bit == 8:
            if self.sym:
                dtype = torch.int8
            else:
                dtype = torch.uint8
        else:
            dtype = torch.int32
        weight = weight.to(dtype)
        if zeros is not None and self.round_zp:
            zeros = zeros.to(dtype)

        return weight, scales, zeros

    def real_quant_weight_dynamic(self, weight, args={}):
        org_w_shape = weight.shape
        weight, scales, zeros, max_int, min_int = self.get_tensor_qparams(weight, args)
        weight = self.quant(weight, scales, zeros, max_int, min_int)
        weight = self.restore_tensor(weight, org_w_shape)

        if self.bit == 8:
            if self.sym:
                dtype = torch.int8
            else:
                dtype = torch.uint8
        else:
            dtype = torch.int32
        weight = weight.to(dtype)
        if zeros is not None and self.round_zp:
            zeros = zeros.to(dtype)

        return weight, scales, zeros