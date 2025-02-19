"""Communication utilities.

This file currently provides the following functionalities, with code mainly
sourced from PyTorch:

1. Provides init process group capability without being restricted by default
   process group. PyTorch assumes users always use its default process group.
2. Provides CUDAIPC capability, which allows bypassing torch's multiprocessing
   to use GPU shared memory, for example to communicate with vllm workers using
   shared memory.
"""

from __future__ import annotations

import socket
import warnings
from datetime import timedelta
from typing import Any, Optional, Union

import torch
import torch.distributed
from torch.distributed.distributed_c10d import (
    Backend,
    GroupMember,
    PrefixStore,
    ProcessGroup,
    Store,
    _new_process_group_helper,
    _shutdown_backend,
    _unregister_all_process_groups,
    _unregister_process_group,
    _update_default_pg,
    _world,
    default_pg_timeout,
    rendezvous,
)
from torch.multiprocessing.reductions import rebuild_cuda_tensor


def get_free_port():
    """获取一个空闲的端口号"""
    with socket.socket() as sock:
        sock.bind(("", 0))  # 绑定到一个随机端口
        return sock.getsockname()[1]  # 返回分配的端口号


# Copy from pytorch to allow creating multiple main groups.
# https://github.com/pytorch/pytorch/blob/main/torch/distributed/distributed_c10d.py
# https://github.com/OpenRLHF/OpenRLHF/blob/main/openrlhf/utils/distributed_util.py
def orz_init_process_group(
    backend: Union[str, Backend] = None,
    init_method: Optional[str] = None,
    timeout: Optional[timedelta] = None,
    world_size: int = -1,
    rank: int = -1,
    store: Optional[Store] = None,
    group_name: str = None,
    pg_options: Optional[Any] = None,
):
    assert (store is None) or (init_method is None), "Cannot specify both init_method and store."

    if store is not None:
        assert world_size > 0, "world_size must be positive if using store"
        assert rank >= 0, "rank must be non-negative if using store"
    elif init_method is None:
        init_method = "env://"

    if backend:
        backend = Backend(backend)
    else:
        backend = Backend("undefined")

    if timeout is None:
        timeout = default_pg_timeout

    # backward compatible API
    if store is None:
        rendezvous_iterator = rendezvous(init_method, rank, world_size, timeout=timeout)
        store, rank, world_size = next(rendezvous_iterator)
        store.set_timeout(timeout)

        # Use a PrefixStore to avoid accidental overrides of keys used by
        # different systems (e.g. RPC) in case the store is multi-tenant.
        store = PrefixStore(group_name, store)

    pg_options_param_name = "backend_options" if str(torch.__version__) >= "2.6" else "pg_options"
    pg, _ = _new_process_group_helper(
        world_size,
        rank,
        [],
        backend,
        store,
        group_name=group_name,
        **{pg_options_param_name: pg_options},
        timeout=timeout,
    )

    _world.pg_group_ranks[pg] = {i: i for i in range(world_size)}

    return pg


def orz_destroy_process_group(group: Optional[ProcessGroup] = None):
    """
    Destroy a given process group, and deinitialize the distributed package.

    Args:
        group (ProcessGroup, optional): The process group to be destroyed, if
                                        group.WORLD is given, all process
                                        groups including the default one will
                                        be destroyed.
    """
    global _world

    if group == GroupMember.NON_GROUP_MEMBER:
        return

    if group is None:
        pg = GroupMember.WORLD
    else:
        pg = group

    assert pg is not None
    if _world.pg_map.get(pg, None) is None:
        raise ValueError("Invalid process group specified")

    # When users register Python onCompletion hooks, those hooks will run on a
    # different thread than the main thread. Today, the ProcessGroup dtor does
    # wait for that thread. However, the dtor might finish after the Python
    # Interpreter exits. After that grabbing the GIL for the Python hook will
    # crash. We can either revive the interpreter when running hooks or keep
    # the main one alive until all works and hooks are done. The current
    # implementation does the latter. Therefore, we explicitly call
    # _wait_for_pending_works() here to wait for the pending hooks to finish.
    if pg.name().lower() == "nccl" and pg._has_hooks():
        pg._wait_for_pending_works()

    if group is None or group == GroupMember.WORLD:
        # shutdown all backends in the order of pg names. shutting down in
        # order because ncclCommAbort() was a 'collective' call in some
        # versions of NCCL.
        for pg_to_shutdown in sorted(_world.pg_names, key=lambda x: _world.pg_names[x], reverse=True):
            _shutdown_backend(pg_to_shutdown)

        _update_default_pg(None)
        _world.pg_map.clear()
        _world.pg_names.clear()
        _world.pg_group_ranks.clear()
        _world.pg_backend_config.clear()
        _world.pg_to_tag.clear()
        _world.tags_to_pg.clear()
        _world.pg_coalesce_state.clear()
        _world.pg_default_device.clear()
        _unregister_all_process_groups()

        # when process group doesn't have an explicit name (only WORLD
        # (default) process group can have an explicit name), we use global
        # _world.group_count to generate the name. We need to reset the counter
        # on destruction to allow consistent value to be generated when we
        # re-create process groups after some trainers recover from failure.
        #
        # We only reset this when WORLD is being destroyed because if this
        # process group is in good state, we aren't dealing with failures.
        _world.group_count = 0
    else:
        _shutdown_backend(pg)
        del _world.pg_map[pg]
        del _world.pg_names[pg]
        del _world.pg_group_ranks[pg]
        del _world.pg_backend_config[pg]
        if pg in _world.pg_default_device:
            del _world.pg_default_device[pg]
        if pg in _world.pg_coalesce_state.keys():
            warnings.warn(
                "Some coalesced collectives haven't been launched when "
                "ProcessGroup is destroyed. They will be cleaned."
            )
            del _world.pg_coalesce_state[pg]

        tag = _world.pg_to_tag.get(pg)
        del _world.pg_to_tag[pg]
        if tag is not None:
            try:
                _world.tags_to_pg[tag].remove(pg)
                if tag.startswith("ptd:"):
                    _world.tags_to_pg[""].remove(pg)
            except Exception:
                pass
        _unregister_process_group(pg.group_name)


class CUDAIPCHandle:
    def __init__(
        self,
        tensor_type: type,
        size: tuple,
        stride: tuple,
        offset: int,
        storage_type: type,
        dtype: torch.dtype,
        device: torch.device,
        handle: bytes,
        storage_size_bytes: bytes,
        storage_offset_bytes: bytes,
        requires_grad: bool,
        ref_counter_handle: bytes,
        ref_counter_offset: bytes,
        event_handle: bytes,
        event_sync_required: bool,
    ):
        self.tensor_type = tensor_type
        self.size = size
        self.stride = stride
        self.offset = offset
        self.storage_type = storage_type
        self.dtype = dtype
        self.device = device
        self.handle = handle
        self.storage_size_bytes = storage_size_bytes
        self.storage_offset_bytes = storage_offset_bytes
        self.requires_grad = requires_grad
        self.ref_counter_handle = ref_counter_handle
        self.ref_counter_offset = ref_counter_offset
        self.event_handle = event_handle
        self.event_sync_required = event_sync_required

    def rebuild(self) -> torch.Tensor:
        # NOTE: Rebuild within the same process is not thread-safe and will
        #       likely crash (segfault core dump).
        return rebuild_cuda_tensor(
            self.tensor_type,
            self.size,
            self.stride,
            self.offset,
            self.storage_type,
            self.dtype,
            self.device,
            self.handle,
            self.storage_size_bytes,
            self.storage_offset_bytes,
            self.requires_grad,
            self.ref_counter_handle,
            self.ref_counter_offset,
            self.event_handle,
            self.event_sync_required,
        )

    @staticmethod
    def from_tensor(tensor: torch.Tensor) -> CUDAIPCHandle:
        if not tensor.is_cuda:
            raise ValueError("Tensor must be on CUDA device to use CUDAIPC")
        tensor = tensor.share_memory_()
        storage = tensor._typed_storage()
        (
            device,
            handle,
            storage_size_bytes,
            storage_offset_bytes,
            ref_counter_handle,
            ref_counter_offset,
            event_handle,
            event_sync_required,
        ) = storage._share_cuda_()
        tensor_offset = tensor.storage_offset()
        return CUDAIPCHandle(
            tensor_type=type(tensor),
            size=tensor.size(),
            stride=tensor.stride(),
            # tensor offset in its storage
            offset=tensor_offset,
            storage_type=type(storage),
            dtype=tensor.dtype,
            device=device,
            # identifier which CUDA allocation is the storage in.
            handle=handle,
            # size(in bytes) of the storage
            storage_size_bytes=storage_size_bytes,
            # offset(in bytes) of the storage in the CUDA allocation
            storage_offset_bytes=storage_offset_bytes,
            requires_grad=tensor.requires_grad,
            ref_counter_handle=ref_counter_handle,
            ref_counter_offset=ref_counter_offset,
            event_handle=event_handle,
            event_sync_required=event_sync_required,
        )
