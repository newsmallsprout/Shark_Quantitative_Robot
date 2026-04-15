from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Split:
    train_idx: List[int]
    test_idx: List[int]


def walk_forward_splits(
    n_samples: int,
    *,
    train_size: int,
    test_size: int,
    step: Optional[int] = None,
    embargo: int = 0,
) -> List[Split]:
    """
    Walk-forward 切分：每一折固定长度训练 + 固定长度测试。
    embargo: 在 train 与 test 之间插入隔离区，避免泄露。
    """
    if n_samples <= 0:
        return []
    step = int(step or test_size)
    splits: List[Split] = []
    start = 0
    while True:
        train_end = start + int(train_size)
        test_start = train_end + int(embargo)
        test_end = test_start + int(test_size)
        if test_end > n_samples:
            break
        train_idx = list(range(start, train_end))
        test_idx = list(range(test_start, test_end))
        splits.append(Split(train_idx=train_idx, test_idx=test_idx))
        start += step
    return splits


def purged_kfold_splits(
    n_samples: int,
    *,
    n_splits: int = 5,
    t1: Optional[Sequence[int]] = None,
    embargo: int = 0,
) -> List[Split]:
    """
    Purged K-Fold（Lopez de Prado）：
    - 先按 index 均匀切 test block
    - 从 train 中剔除任何与 test 标签区间重叠的样本（purge）
    - 再加 embargo（隔离区）
    """
    if n_samples <= 0 or n_splits <= 1:
        return []

    # label end time/index; if missing treat as self index
    if t1 is None:
        t1 = list(range(n_samples))
    else:
        t1 = [int(x) for x in t1]

    fold_sizes = [n_samples // n_splits] * n_splits
    for i in range(n_samples % n_splits):
        fold_sizes[i] += 1

    splits: List[Split] = []
    test_start = 0
    for fs in fold_sizes:
        test_end = test_start + fs
        test_idx = list(range(test_start, test_end))

        # Test label window is [test_start, max(t1[test_idx])]
        test_t1_max = max(t1[i] for i in test_idx) if test_idx else test_end - 1
        embargo_end = min(n_samples - 1, test_t1_max + int(embargo))

        train_idx: List[int] = []
        for i in range(n_samples):
            if test_start <= i < test_end:
                continue
            # purge: remove samples whose label window overlaps test window
            i_t1 = t1[i]
            if i <= embargo_end and i_t1 >= test_start:
                continue
            train_idx.append(i)

        splits.append(Split(train_idx=train_idx, test_idx=test_idx))
        test_start = test_end

    return splits

