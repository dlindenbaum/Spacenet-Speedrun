import numpy as np


class Callback:
    def set_model(self, m):
        self.model = m

    def on_train_begin(self):
        pass

    def on_epoch_end(self, epoch, logs):
        pass


class History(Callback):
    def on_train_begin(self):
        self.epoch, self.history = [], {}

    def on_epoch_end(self, epoch, logs):
        self.epoch.append(epoch)
        for k, v in logs.items():
            self.history.setdefault(k, []).append(v)


class ModelCheckpoint(Callback):
    def __init__(self, filepath, monitor="val_loss", verbose=0, save_best_only=False, mode="auto", **kw):
        assert not save_best_only
        self.filepath = filepath

    def on_epoch_end(self, epoch, logs):
        self.model.save_weights(self.filepath.format(epoch=epoch, **logs))  # keras-1: zero-based epoch


class EarlyStopping(Callback):
    def __init__(self, monitor="val_loss", patience=0, verbose=0, mode="auto", **kw):
        self.monitor, self.patience = monitor, patience
        self.better = np.greater if (mode == "max" or (mode == "auto" and "acc" in monitor)) else np.less

    def on_train_begin(self):
        self.wait = 0
        self.best = -np.inf if self.better is np.greater else np.inf

    def on_epoch_end(self, epoch, logs):  # keras-1.1 logic
        cur = logs.get(self.monitor)
        if self.better(cur, self.best):
            self.best, self.wait = cur, 0
        else:
            if self.wait >= self.patience:
                self.model.stop_training = True
            self.wait += 1
