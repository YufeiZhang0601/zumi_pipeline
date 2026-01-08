from typing import Tuple
from dataclasses import dataclass
from multiprocessing import Value
import ctypes
import numpy as np
from multiprocessing.managers import SharedMemoryManager


@dataclass
class ArraySpec:
    name: str
    shape: Tuple[int]
    dtype: np.dtype


class SharedAtomicCounter:
    """
    Process-safe atomic counter using multiprocessing.Value.

    Note: shm_manager parameter is kept for API compatibility but no longer used.
    The counter is now backed by multiprocessing.Value which handles its own
    shared memory internally.
    """
    def __init__(self,
            shm_manager: SharedMemoryManager = None,
            size: int = 8  # 64bit int (unused, kept for compatibility)
            ):
        # Use multiprocessing.Value for atomic operations
        # c_ulonglong = unsigned 64-bit integer
        self._value = Value(ctypes.c_ulonglong, 0, lock=True)

    def load(self) -> int:
        return self._value.value

    def store(self, value: int):
        self._value.value = value

    def add(self, value: int):
        with self._value.get_lock():
            self._value.value += value