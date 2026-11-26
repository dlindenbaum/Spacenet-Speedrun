import torch


def _ax(axis):
    return tuple(axis) if isinstance(axis, (list, tuple)) else axis


def sum(x, axis=None):  # noqa: A001
    return x.sum() if axis is None else x.sum(dim=_ax(axis))


def mean(x, axis=None):
    return x.mean() if axis is None else x.mean(dim=_ax(axis))


def round(x):  # noqa: A001
    return torch.round(x)


def clip(x, lo, hi):
    return torch.clamp(x, lo, hi)


def image_dim_ordering():
    return "th"
