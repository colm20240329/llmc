import torch
import os
import random
import numpy as np


def seed_all(seed):
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True


def check_config(config):
    def check_weight_setting(weight_setting):
        if weight_setting.granularity == "per_group":
            assert weight_setting.group_size > 0
        elif weight_setting.granularity == "per_head":
            assert weight_setting.head_num > 0

    if config.quant.weight.get("granularity", False):
        weight_setting = config.quant.weight
        check_weight_setting(weight_setting)
    if config.quant.weight.get("w_1", False):
        weight_setting = config.quant.weight.w_1
        check_weight_setting(weight_setting)
    if config.quant.weight.get("w_2", False):
        weight_setting = config.quant.weight.w_2
        check_weight_setting(weight_setting)
    if "eval" in config and "fake_quant" in config.eval.eval_pos:
        if "save" in config:
            assert not config.save.get(
                "save_quant", False
            ), "Fake_quant—eval and save_quant conflict now."
            assert not (
                config.save.get("save_fake", False)
                and config.save.get("save_quant", False)
            ), "Saving fake quant and saving real quant conflict now."


def mkdirs(path):
    if not os.path.exists(path):
        os.makedirs(path)
    else:
        raise Exception(f"{path} existed before. Need check.")
