import torch.nn as nn


class KTensor:
    """Symbolic tensor: (layer that produced it, input KTensors, channels)."""

    def __init__(self, layer, inputs, channels):
        self.layer, self.inputs, self.channels = layer, inputs, channels


def Input(shape):
    return KTensor(None, [], shape[0])


class _Layer:
    def __call__(self, x):
        xs = x if isinstance(x, list) else [x]
        return KTensor(self, xs, self.out_channels([t.channels for t in xs]))


class Convolution2D(_Layer):
    def __init__(self, nb_filter, nb_row, nb_col, activation=None, border_mode="valid", **kw):
        assert nb_row == nb_col
        self.f, self.k, self.act, self.pad = nb_filter, nb_row, activation, border_mode

    def out_channels(self, c):
        return self.f

    def build(self, c):
        conv = nn.Conv2d(c[0], self.f, self.k, padding=(self.k // 2 if self.pad == "same" else 0))
        nn.init.xavier_uniform_(conv.weight)  # keras-1 glorot_uniform (fan = ch*k*k)
        nn.init.zeros_(conv.bias)
        act = {"relu": nn.ReLU(), "sigmoid": nn.Sigmoid(), None: nn.Identity(), "linear": nn.Identity()}[self.act]
        return nn.Sequential(conv, act)


class MaxPooling2D(_Layer):
    def __init__(self, pool_size=(2, 2), **kw):
        self.p = pool_size

    def out_channels(self, c):
        return c[0]

    def build(self, c):
        return nn.MaxPool2d(self.p)


class UpSampling2D(_Layer):
    def __init__(self, size=(2, 2), **kw):
        self.s = size

    def out_channels(self, c):
        return c[0]

    def build(self, c):
        return nn.Upsample(scale_factor=self.s, mode="nearest")


class _Concat(nn.Module):
    def __init__(self, axis):
        super().__init__()
        self.axis = axis

    def forward(self, *xs):
        import torch
        return torch.cat(xs, self.axis)


class _Merge(_Layer):
    def __init__(self, mode="concat", concat_axis=1):
        assert mode == "concat"
        self.axis = concat_axis

    def out_channels(self, c):
        return sum(c)

    def build(self, c):
        return _Concat(self.axis)


def merge(inputs, mode="concat", concat_axis=1, **kw):
    return _Merge(mode, concat_axis)(inputs)


# imported (unused) by XD_XD's scripts
Reshape = Dropout = Activation = BatchNormalization = core = None
