class Adam:
    def __init__(self, lr=0.001, beta_1=0.9, beta_2=0.999, epsilon=1e-8, **kw):
        self.kw = dict(lr=lr, betas=(beta_1, beta_2), eps=epsilon)

    def make(self, params):
        import torch
        return torch.optim.Adam(params, **self.kw)


class SGD:
    def __init__(self, lr=0.01, momentum=0.0, decay=0.0, nesterov=False, **kw):
        assert decay == 0.0
        self.kw = dict(lr=lr, momentum=momentum, nesterov=nesterov)

    def make(self, params):
        import torch
        return torch.optim.SGD(params, **self.kw)
