#!/usr/bin/env python3
import datetime
import os
import re
import time
import numpy as np
import torch
import torch.multiprocessing as mp
import torch.distributed as dist
import gradient_reducers
import tasks
from mean_accumulator import MeanAccumulator
from timer import Timer
"""
When you run this script, it uses the default parameters below.
To change them, you can make another script, say experiment.py
and write, e.g.

import train
train.config["num_epochs"] = 200
train.config["n_workers"] = 4
train.config["rank"] = 0
train.main()
The configuration overrides we used for all our experiments can be found in the folder schedule/neurips19.
"""

import socket

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))  # Bind to a free port provided by the host
        return s.getsockname()[1]


config = dict(
    average_reset_epoch_interval=30,
    # distributed_backend="nccl",
    distributed_backend="gloo",
    fix_conv_weight_norm=False,
    num_epochs=9,#CHANGE LATER
    checkpoints=[],
    num_train_tracking_batches=1,
    optimizer_batch_size=128,  # per worker
    optimizer_conv_learning_rate=0.1,  # tuned for batch size 128
    optimizer_decay_at_epochs=[150, 250],
    optimizer_decay_with_factor=10.0,
    optimizer_learning_rate=0.1,  # Tuned for batch size 128 (single worker)
    optimizer_memory=True,
    optimizer_momentum_type="nesterov",
    optimizer_momentum=0.9,
    optimizer_reducer="RankKReducer",
    # optimizer_reducer_compression=0.01,
    optimizer_reducer_rank=2,
    optimizer_reducer_reuse_query=True,
    optimizer_reducer_n_power_iterations=0,
    optimizer_scale_lr_with_factor=None,  # set to override world_size as a factor
    optimizer_scale_lr_with_warmup_epochs=5,  # scale lr by world size
    optimizer_mom_before_reduce=False,
    optimizer_wd_before_reduce=False,
    optimizer_weight_decay_conv=0.0001,
    optimizer_weight_decay_other=0.0001,
    optimizer_weight_decay_bn=0.0,
    task="Cifar",
    # task_architecture="ResNet18",
    task_architecture = "MobileNet",
    seed=42,
    rank=1,
    n_workers=2,
    distributed_init_file="/tmp/torch_dist_init",
    log_verbosity=2,
)
output_dir = "./output.tmp"  # will be overwritten by run.py

def metric_fn(rank):
    """Creates a rank-aware metric logging function."""
    def log_metric_with_rank(name, values, tags={}):
        value_list = []
        for key in sorted(values.keys()):
            value = values[key]
            value_list.append(f"{key}:{value:7.3f}")
        values_str = ", ".join(value_list)
        tag_list = []
        for key, tag in tags.items():
            tag_list.append(f"{key}:{tag}")
        tags_str = ", ".join(tag_list)
        print(f"(Rank {rank}) {name:30s} - {values_str} ({tags_str})")
    return log_metric_with_rank

def info_fn(rank):
    """Creates a rank-aware info logging function."""
    def log_info_with_rank(*args, **kwargs):
        if rank == 0:
            log_info(*args, **kwargs)
    return log_info_with_rank

def train_process(rank, world_size, config):
    # Clean up sync files only in rank 0 before proceeding
    if rank == 0:
        try:
            os.remove("/tmp/pg_port.txt")
        except FileNotFoundError:
            pass

        for i in range(world_size):
            sync_file = f"/tmp/dist_sync_rank_{i}.txt"
            if os.path.exists(sync_file):
                os.remove(sync_file)

    # Barrier or wait a bit to ensure cleanup completes
    time.sleep(1)

    # Only rank 0 finds a free port and stores it
    if rank == 0:
        config['port'] = find_free_port()
        print(f"Using free port: {config['port']}")
        with open('/tmp/pg_port.txt', 'w') as f:
            f.write(str(config['port']))
    else:
        # Other ranks wait until the port file is created
        timeout = 30  # seconds
        start = time.time()
        while not os.path.exists('/tmp/pg_port.txt'):
            if time.time() - start > timeout:
                raise RuntimeError("Timeout waiting for /tmp/pg_port.txt to appear.")
            time.sleep(0.1)
        with open('/tmp/pg_port.txt', 'r') as f:
            config['port'] = int(f.read().strip())

    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = str(config['port'])
    config["rank"] = rank
    config["n_workers"] = world_size

    torch.manual_seed(config["seed"] + config["rank"])
    np.random.seed(config["seed"] + config["rank"])

    device = torch.device("cpu")

    timer = Timer(verbosity_level=config["log_verbosity"], log_fn=metric_fn(rank))

    if torch.distributed.is_available():
        print(
            f"(Rank {rank}) Distributed init: rank {config['rank']}/{config['n_workers']} - "
            f"{os.path.abspath(config['distributed_init_file'])}"
        )
        if rank == 0 and os.path.exists(config["distributed_init_file"]):
            os.remove(config["distributed_init_file"])
    print(f"[Rank {rank}] About to initialize process group on port {config['port']}", flush=True)
    dist.init_process_group(
        backend=config["distributed_backend"],
        init_method=f"tcp://127.0.0.1:{config['port']}",
        timeout=datetime.timedelta(seconds=120),
        world_size=config["n_workers"],
        rank=config["rank"],
    )
    print(f"[Rank {rank}] Successfully initialized process group.", flush=True)

    if dist.get_rank() == 0:
        if config["task"] == "Cifar":
            # download_cifar()
            download_ministl()
        elif config["task"] == "LSTM":
            download_wikitext2()
    dist.barrier()
    torch.cuda.synchronize()

    task = tasks.build(task_name=config["task"], device=device, timer=timer, **config)
    reducer = get_reducer(device, timer)

    bits_communicated = 0
    runavg_model = MeanAccumulator()

    memories = [torch.zeros_like(param) for param in task.state]
    momenta = [torch.empty_like(param) for param in task.state]
    send_buffers = [torch.zeros_like(param) for param in task.state]
    for epoch in range(config["num_epochs"]):
        epoch_metrics = MeanAccumulator()
        info_fn(rank)({"state.progress": float(epoch) / config["num_epochs"], "state.current_epoch": epoch})

        wds = [get_weight_decay(epoch, name) for name in task.parameter_names]

        if epoch % config["average_reset_epoch_interval"] == 0:
            runavg_model.reset()

        train_loader = task.train_iterator(config["optimizer_batch_size"])
        for i, batch in enumerate(train_loader):
            epoch_frac = epoch + i / len(train_loader)
            lrs = [get_learning_rate(epoch_frac, name) for name in task.parameter_names]

            with timer("batch", epoch_frac):
                _, grads, metrics = task.batch_loss_and_gradient(batch)
                epoch_metrics.add(metrics)

                with timer("batch.reporting.lr", epoch_frac, verbosity=2):
                    for name, param, grad, lr in zip(task.parameter_names, task.state, grads, lrs):
                        if np.random.rand() < 0.001:
                            tags = {"weight": name.replace("module.", "")}
                            metric_fn(rank)(
                                "effective_lr",
                                {"epoch": epoch_frac, "value": lr / max(l2norm(param).item() ** 2, 1e-8)},
                                tags,
                            )
                            metric_fn(rank)(
                                "grad_norm",
                                {"epoch": epoch_frac, "value": l2norm(grad).item()},
                                tags,
                            )

                if config["optimizer_wd_before_reduce"]:
                    with timer("batch.weight_decay", epoch_frac, verbosity=2):
                        for grad, param, wd in zip(grads, task.state, wds):
                            if wd > 0:
                                grad.add_(param.detach(), alpha=wd)

                if config["optimizer_mom_before_reduce"]:
                    with timer("batch.momentum", epoch_frac, verbosity=2):
                        for grad, momentum in zip(grads, momenta):
                            if epoch == 0 and i == 0:
                                momentum.data = grad.clone().detach()
                            else:
                                if (
                                    config["optimizer_momentum_type"]
                                    == "exponential_moving_average"
                                ):
                                    momentum.mul_(config["optimizer_momentum"]).add_(
                                        grad, alpha=1 - config["optimizer_momentum"]
                                    )
                                else:
                                    momentum.mul_(config["optimizer_momentum"]).add_(grad)
                            replace_grad_by_momentum(grad, momentum)

                with timer("batch.accumulate", epoch_frac, verbosity=2):
                    for grad, memory, send_bfr in zip(grads, memories, send_buffers):
                        if config["optimizer_memory"]:
                            send_bfr.data[:] = grad + memory
                        else:
                            send_bfr.data[:] = grad

                with timer("batch.reduce", epoch_frac):
                    bits_communicated += reducer.reduce(send_buffers, grads, memories)

                if config["optimizer_memory"]:
                    with timer("batch.reporting.compr_err", verbosity=2):
                        for name, memory, send_bfr in zip(
                            task.parameter_names, memories, send_buffers
                        ):
                            if np.random.rand() < 0.001:
                                tags = {"weight": name.replace("module.", "")}
                                rel_compression_error = l2norm(memory) / l2norm(send_bfr)
                                metric_fn(rank)(
                                    "rel_compression_error",
                                    {"epoch": epoch_frac, "value": rel_compression_error.item()},
                                    tags,
                                )

                if not config["optimizer_wd_before_reduce"]:
                    with timer("batch.wd", epoch_frac, verbosity=2):
                        for grad, param, wd in zip(grads, task.state, wds):
                            if wd > 0:
                                grad.add_(param.detach(), alpha=wd)

                if not config["optimizer_mom_before_reduce"]:
                    with timer("batch.mom", epoch_frac, verbosity=2):
                        for grad, momentum in zip(grads, momenta):
                            if epoch == 0 and i == 0:
                                momentum.data = grad.clone().detach()
                            else:
                                if (
                                    config["optimizer_momentum_type"]
                                    == "exponential_moving_average"
                                ):
                                    momentum.mul_(config["optimizer_momentum"]).add_(
                                        grad, alpha=1 - config["optimizer_momentum"]
                                    )
                                else:
                                    momentum.mul_(config["optimizer_momentum"]).add_(grad)
                            replace_grad_by_momentum(grad, momentum)

                with timer("batch.step", epoch_frac, verbosity=2):
                    for param, grad, lr in zip(task.state, grads, lrs):
                        param.data.add_(grad, alpha=-lr)

                if config["fix_conv_weight_norm"]:
                    with timer("batch.normfix", epoch_frac, verbosity=2):
                        for param_name, param in zip(task.parameter_names, task.state):
                            if is_conv_param(param_name):
                                param.data[:] /= l2norm(param)

                with timer("batch.update_runavg", epoch_frac, verbosity=2):
                    runavg_model.add(task.state_dict())

                if config["optimizer_memory"]:
                    with timer("batch.reporting.memory_norm", epoch_frac, verbosity=2):
                        if np.random.rand() < 0.001:
                            sum_of_sq = 0.0
                            for parameter_name, memory in zip(task.parameter_names, memories):
                                tags = {"weight": parameter_name.replace("module.", "")}
                                sq_norm = torch.sum(memory ** 2)
                                sum_of_sq += torch.sqrt(sq_norm)
                                metric_fn(rank)(
                                    "memory_norm",
                                    {"epoch": epoch_frac, "value": torch.sqrt(sq_norm).item()},
                                    tags,
                                )
                            metric_fn(rank)(
                                "compression_error",
                                {"epoch": epoch_frac, "value": torch.sqrt(sum_of_sq).item()},
                            )

        with timer("epoch_metrics.collect", epoch + 1.0, verbosity=2):
            epoch_metrics.reduce()
            for key, value in epoch_metrics.value().items():
                metric_fn(rank)(
                    key,
                    {"value": value.item(), "epoch": epoch + 1.0, "bits": bits_communicated},
                    tags={"split": "train"},
                )
                metric_fn(rank)(
                    f"last_{key}",
                    {"value": value.item(), "epoch": epoch + 1.0, "bits": bits_communicated},
                    tags={"split": "train"},
                )

        with timer("test.last", epoch):
            test_stats = task.test()
            for key, value in test_stats.items():
                metric_fn(rank)(
                    f"last_{key}",
                    {"value": value.item(), "epoch": epoch + 1.0, "bits": bits_communicated},
                    tags={"split": "test"},
                )

        with timer("test.runavg", epoch):
            test_stats = task.test(state_dict=runavg_model.value())
            for key, value in test_stats.items():
                metric_fn(rank)(
                    f"runavg_{key}",
                    {"value": value.item(), "epoch": epoch + 1.0, "bits": bits_communicated},
                    tags={"split": "test"},
                )

        if epoch in config["checkpoints"] and dist.get_rank() == 0:
            with timer("checkpointing"):
                save(
                    os.path.join(output_dir, "epoch_{:03d}".format(epoch)),
                    task.state_dict(),
                    epoch + 1.0,
                    test_stats,
                )

        print(timer.summary())
        if config["rank"] == 0:
            timer.save_summary(os.path.join(output_dir, "timer_summary.json"))

        info_fn(rank)({"state.progress": 1.0})

def main():
    import torch.multiprocessing as mp

    # Use 'fork' to avoid some spawn-related bugs (Linux-only)
    mp.set_start_method('fork', force=True)

    n_workers = config["n_workers"]
    mp.spawn(train_process, args=(n_workers, config), nprocs=n_workers, join=True)


def save(destination_path, model_state, epoch, test_stats):
    """Save a checkpoint to disk"""
    time.sleep(1)
    torch.save(
        {"epoch": epoch, "test_stats": test_stats, "model_state_dict": model_state},
        destination_path,
    )

def get_weight_decay(epoch, parameter_name):
    if is_conv_param(parameter_name):
        return config["optimizer_weight_decay_conv"]
    elif is_batchnorm_param(parameter_name):
        return config["optimizer_weight_decay_bn"]
    else:
        return config["optimizer_weight_decay_other"]

def get_learning_rate(epoch, parameter_name):
    if is_conv_param(parameter_name):
        lr = config["optimizer_conv_learning_rate"]
    else:
        lr = config["optimizer_learning_rate"]

    if config["optimizer_scale_lr_with_warmup_epochs"]:
        warmup_epochs = config["optimizer_scale_lr_with_warmup_epochs"]
        max_factor = config.get("optimizer_scale_lr_with_factor", None)
        if max_factor is None:
            max_factor = (
                dist.get_world_size() if dist.is_available() else 1.0
            )
        factor = 1.0 + (max_factor - 1.0) * min(epoch / warmup_epochs, 1.0)
        lr *= factor

    for decay_epoch in config["optimizer_decay_at_epochs"]:
        if epoch >= decay_epoch:
            lr /= config["optimizer_decay_with_factor"]
        else:
            return lr
    return lr

def is_conv_param(parameter_name):
    return "conv" in parameter_name and "weight" in parameter_name

def is_batchnorm_param(parameter_name):
    return re.match(r""".*\.bn\d+\.(weight|bias)""", parameter_name)

def replace_grad_by_momentum(grad, momentum):
    if config["optimizer_momentum_type"] == "heavy-ball":
        grad.data[:] = momentum
    if config["optimizer_momentum_type"] == "exponential_moving_average":
        grad.data[:] = momentum
    elif config["optimizer_momentum_type"] == "nesterov":
        grad.data[:] += momentum
    else:
        raise ValueError("Unknown momentum type")

def get_reducer(device, timer):
    if config["optimizer_reducer"] in ["RankKReducer"]:
        return getattr(gradient_reducers, config["optimizer_reducer"])(
            random_seed=config["seed"],
            device = device,
            timer=timer,
            n_power_iterations=config["optimizer_reducer_n_power_iterations"],
            reuse_query=config["optimizer_reducer_reuse_query"],
            rank=config["optimizer_reducer_rank"],
        )
    elif config["optimizer_reducer"] == "AtomoReducer":
        return getattr(gradient_reducers, config["optimizer_reducer"])(
            random_seed=config["seed"],
            device=device,
            timer=timer,
            rank=config["optimizer_reducer_rank"],
        )
    elif config["optimizer_reducer"] == "RandomSparseReducer":
        return getattr(gradient_reducers, config["optimizer_reducer"])(
            random_seed=config["seed"],
            device=device,
            timer=timer,
            rank=config["optimizer_reducer_rank"],
        )
    elif config["optimizer_reducer"] == "RandomSparseBlockReducer":
        return getattr(gradient_reducers, config["optimizer_reducer"])(
            random_seed=config["seed"],
            device=device,
            timer=timer,
            rank=config["optimizer_reducer_rank"],
        )
    elif (
        config["optimizer_reducer"] == "GlobalTopKReducer"
        or config["optimizer_reducer"] == "TopKReducer"
        or config["optimizer_reducer"] == "UniformRandomSparseBlockReducer"
        or config["optimizer_reducer"] == "UniformRandomSparseReducer"
    ):
        return getattr(gradient_reducers, config["optimizer_reducer"])(
            random_seed=config["seed"],
            device=device,
            timer=timer,
            compression=config["optimizer_reducer_compression"],
        )
    elif config["optimizer_reducer"] == "HalfRankKReducer":
        return getattr(gradient_reducers, config["optimizer_reducer"])(
            random_seed=config["seed"],
            device=device,
            timer=timer,
            rank=config["optimizer_reducer_rank"],
        )
    elif config["optimizer_reducer"] == "SVDReducer":
        return getattr(gradient_reducers, config["optimizer_reducer"])(
            config["seed"], device, timer, config["optimizer_reducer_rank"]
        )
    else:
        return getattr(gradient_reducers, config["optimizer_reducer"])(
            config["seed"], device, timer
        )

@torch.jit.script
def l2norm(tensor):
    """Compute the L2 Norm of a tensor in a fast and correct way"""
    # tensor.norm(p=2) is buggy in Torch 1.0.0
    # tensor.norm(p=2) is really slow in Torch 1.0.1
    return torch.sqrt(torch.sum(tensor ** 2))

def log_info(info_dict):
    """Add any information to MongoDB
    This function will be overwritten when called through run.py"""
    pass

def log_metric(name, values, tags={}):
    """Log timeseries data
    This function will be overwritten when called through run.py"""
    value_list = []
    for key in sorted(values.keys()):
        value = values[key]
        value_list.append(f"{key}:{value:7.3f}")
    values_str = ", ".join(value_list)
    tag_list = []
    for key, tag in tags.items():
        tag_list.append(f"{key}:{tag}")
    tags_str = ", ".join(tag_list)
    print(f"{name:30s} - {values_str} ({tags_str})")

def info(*args, **kwargs):
    if config["rank"] == 0:
        log_info(*args, **kwargs)

def metric(*args, **kwargs):
    if config["rank"] == 0:
        log_metric(*args, **kwargs)

def download_cifar(data_root=os.path.join(os.getenv("DATA"), "data")):
    import torchvision
    dataset = torchvision.datasets.CIFAR10
    training_set = dataset(root=data_root, train=True, download=True)
    test_set = dataset(root=data_root, train=False, download=True)


from torchvision import datasets, transforms
def download_ministl(data_root=os.path.join(os.getenv("DATA", "./"), "data")):
    transform = transforms.Compose([
        transforms.Resize((32, 32)),  # Downscale from 96x96 to 48x48
        transforms.ToTensor(),
    ])
    
    train_set = datasets.STL10(root=data_root, split='train', download=True, transform=transform)
    test_set = datasets.STL10(root=data_root, split='test', download=True, transform=transform)
    
    return train_set, test_set


# def download_wikitext2(data_root=os.path.join(os.getenv("DATA"), "data")):
#     import torchtext
#     torchtext.datasets.WikiText2.splits(
#         torchtext.data.Field(lower=True), root=os.path.join(data_root, "wikitext2")
#     )

def check_model_consistency_across_workers(model, epoch):
    signature = []
    for name, param in model.named_parameters():
        signature.append(param.view(-1)[0].item())

    rank = config["rank"]
    signature = ",".join(f"{x:.4f}" for x in signature)
    print(f"Model signature for epoch {epoch:04d} / worker {rank:03d}:\n{signature}")

if __name__ == "__main__":
    main()