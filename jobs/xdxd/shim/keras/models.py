import os
import sys
import time

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

DEV = "cuda" if torch.cuda.is_available() else "cpu"
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True
torch.backends.cudnn.benchmark = True
MAX_EPOCHS = int(os.environ.get("XDXD_MAX_EPOCHS", "0"))  # smoke tests only
EPS = 1e-7  # keras-1 epsilon used to clip probabilities in binary_crossentropy


class _Net(nn.Module):
    def __init__(self, inp, out):
        super().__init__()
        order, seen = [], set()

        def visit(t):
            if id(t) in seen:
                return
            seen.add(id(t))
            for i in t.inputs:
                visit(i)
            order.append(t)

        visit(out)
        self.order, self.inp, self.out = order, inp, out
        self.mods = nn.ModuleList()
        self.idx = {}
        for t in order:
            if t.layer is not None:
                self.idx[id(t)] = len(self.mods)
                self.mods.append(t.layer.build([i.channels for i in t.inputs]))

    def forward(self, x):
        val = {id(self.inp): x}
        for t in self.order:
            if t.layer is not None:
                val[id(t)] = self.mods[self.idx[id(t)]](*[val[id(i)] for i in t.inputs])
        return val[id(self.out)]


def _bce(p, y):
    p = p.clamp(EPS, 1 - EPS)
    return -(y * torch.log(p) + (1 - y) * torch.log(1 - p)).mean()


class Model:
    def __init__(self, input=None, output=None, **kw):  # noqa: A002
        self.net = _Net(input, output).to(DEV)
        self.stop_training = False

    # ---------------------------------------------------------------- compile / metrics
    def compile(self, optimizer, loss="binary_crossentropy", metrics=()):
        assert loss == "binary_crossentropy"
        self.opt = optimizer.make(self.net.parameters())
        self.metrics = []
        for m in metrics:
            if m in ("accuracy", "acc"):
                self.metrics.append(("acc", lambda y, p: (torch.round(p) == y).float().mean()))
            else:
                self.metrics.append((m.__name__, m))

    def _metrics(self, y, p):
        return [("loss", _bce(p, y))] + [(n, f(y, p)) for n, f in self.metrics]

    @staticmethod
    def _t(a):
        return torch.as_tensor(np.ascontiguousarray(a), dtype=torch.float32, device=DEV)

    def _step(self, X, y):
        self.net.train()
        p = self.net(self._t(X))
        yt = self._t(y)
        out = self._metrics(yt, p)
        self.opt.zero_grad(set_to_none=True)
        out[0][1].backward()
        self.opt.step()
        return [(n, float(v.detach())) for n, v in out]

    @torch.no_grad()
    def _evaluate(self, val, nb_val_samples=None, batch_size=32):
        """val = (X, y) arrays (batches of 32) or a generator (keras-1 evaluate_generator:
        consume batches until nb_val_samples seen). Metrics are batch-size-weighted means."""
        self.net.eval()
        if isinstance(val, tuple):
            X, y = val
            batches = ((X[i:i + batch_size], y[i:i + batch_size]) for i in range(0, len(X), batch_size))
        else:
            def gen():
                seen = 0
                while seen < nb_val_samples:
                    b = next(val)
                    seen += len(b[0])
                    yield b
            batches = gen()
        acc, n = None, 0
        for Xb, yb in batches:
            p = self.net(self._t(Xb))
            r = [float(v) * len(p) for _, v in self._metrics(self._t(yb), p)]
            acc = r if acc is None else [a + b for a, b in zip(acc, r)]
            n += len(p)
        names = ["loss"] + [nm for nm, _ in self.metrics]
        return {"val_" + nm: a / n for nm, a in zip(names, acc)}

    # ---------------------------------------------------------------- training loops
    def _run(self, epoch_iter, nb_epoch, validation_data, callbacks, nb_val_samples=None):
        callbacks = list(callbacks or [])
        for cb in callbacks:
            cb.set_model(self)
            cb.on_train_begin()
        self.stop_training = False
        if MAX_EPOCHS:
            nb_epoch = min(nb_epoch, MAX_EPOCHS)
        for epoch in range(nb_epoch):
            t0, sums, n = time.time(), {}, 0
            for X, y in epoch_iter():
                r = self._step(X, y)
                for k, v in r:
                    sums[k] = sums.get(k, 0.0) + v * len(X)
                n += len(X)
            logs = {k: v / n for k, v in sums.items()}
            if validation_data is not None:
                logs.update(self._evaluate(validation_data, nb_val_samples))
            print(f"[keras-shim] epoch {epoch} {time.time() - t0:.0f}s " +
                  " ".join(f"{k}={v:.4f}" for k, v in logs.items()), flush=True)
            for cb in callbacks:
                cb.on_epoch_end(epoch, logs)
            if self.stop_training:
                break

    def fit(self, X, y, batch_size=32, nb_epoch=10, verbose=1, callbacks=None, validation_data=None,
            shuffle=True, **kw):
        def it():
            idx = np.random.permutation(len(X)) if shuffle else np.arange(len(X))
            for i in range(0, len(X), batch_size):
                b = np.sort(idx[i:i + batch_size])
                yield X[b], y[b]

        self._run(it, nb_epoch, validation_data, callbacks)

    def fit_generator(self, generator, samples_per_epoch, nb_epoch, verbose=1, callbacks=None,
                      validation_data=None, nb_val_samples=None, **kw):
        def it():  # keras-1: an epoch ends once samples_per_epoch samples have been seen
            seen = 0
            while seen < samples_per_epoch:
                X, y = next(generator)
                seen += len(X)
                yield X, y

        self._run(it, nb_epoch, validation_data, callbacks, nb_val_samples)

    # ---------------------------------------------------------------- inference / io
    @torch.no_grad()
    def predict(self, X, batch_size=32, verbose=0):
        self.net.eval()
        return np.concatenate([self.net(self._t(X[i:i + batch_size])).cpu().numpy()
                               for i in range(0, len(X), batch_size)])

    @torch.no_grad()
    def predict_generator(self, generator, val_samples, **kw):
        self.net.eval()
        out, seen = [], 0
        while seen < val_samples:
            b = next(generator)
            X = b[0] if isinstance(b, tuple) else b
            out.append(self.net(self._t(X)).cpu().numpy())
            seen += len(X)
        return np.concatenate(out)

    def save_weights(self, path, overwrite=True):
        torch.save(self.net.state_dict(), path)

    def load_weights(self, path):
        self.net.load_state_dict(torch.load(path, map_location=DEV))
