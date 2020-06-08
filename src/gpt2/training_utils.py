import time
import torch
import torch.nn as nn
import torch.optim as optim
from .data.serving import DataLoader
from typing import Optional, Callable, Dict, Any

# Try to import `apex` library for mixed-precision training. Note that the
# library should be installed when ``use_amp=True``.`
try:
    from apex import amp
except ModuleNotFoundError:
    pass


class Recorder(object):
    def __init__(self):
        self.metrics_train = [[]]
        self.metrics_eval = []
        self.stamps = [(0, time.time())]

    def add_train_metrics(self, **metrics: float):
        if not isinstance(self.metrics_train[-1], list):
            self.metrics_train.append([])
        self.metrics_train[-1].append(metrics)

    def add_eval_metrics(self, **metrics: float):
        self.metrics_eval.append(metrics)

    def stamp(self, step: int):
        metrics = {name: [] for name in self.metrics_train[-1][0]}

        # Average last collected training metrics.
        for m in self.metrics_train[-1]:
            for k, v in m.items():
                metrics[k].append(v)
        for k, v in metrics.items():
            metrics[k] = sum(v) / len(v)

        # Change gathered metrics to the averaged metrics.
        self.metrics_train[-1] = metrics

        # Stamp current step.
        self.stamps.append((step, time.time()))

    def format(self, fstring: str) -> str:
        train_params = {f'train_{k}': v
                        for k, v in self.metrics_train[-1].items()}
        eval_params = {f'eval_{k}': v
                       for k, v in self.metrics_eval[-1].items()}

        return fstring.format(**train_params, **eval_params)

    def summarize(self) -> Dict[str, Any]:
        steps, times = list(zip(*self.stamps))
        steps = list(steps[1:])
        times = [times[i + 1] - times[i] for i in range(len(times) - 1)]

        return {'steps': steps,
                'times': times,
                'train': self.metrics_train,
                'eval': self.metrics_eval}

    def state_dict(self) -> Dict[str, Any]:
        return {'metrics_train': self.metrics_train,
                'metrics_eval': self.metrics_eval,
                'stamps': self.stamps}

    def load_state_dict(self, state_dict: Dict[str, Any]):
        self.metrics_train = state_dict['metrics_train']
        self.metrics_eval = state_dict['metrics_eval']
        self.stamps = state_dict['stamps']


class Trainer(object):
    def __init__(self,
                 train_loader: DataLoader,
                 eval_loader: DataLoader,
                 model: nn.Module,
                 optimizer: optim.Optimizer,
                 scheduler: optim.lr_scheduler._LRScheduler,
                 criterion: Callable[..., torch.Tensor],
                 recorder: Recorder,
                 use_amp: bool = False):
        # Convert model and optimizer for mixed-precision training if
        # ``use_amp=True``.
        if use_amp:
            model, optimizer = amp.initialize(
                model, optimizer, opt_level='O1', verbosity=0)

        self.train_loader = train_loader
        self.eval_loader = eval_loader
        self.model = model
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.criterion = criterion
        self.recorder = recorder
        self.use_amp = use_amp

    def train(self, batch: Optional[int] = None):
        # Prepare training.
        self.model.train()
        self.optimizer.zero_grad()

        # Fetch training data.
        data = self.train_loader.fetch(batch)

        # Predict next words and calculate loss.
        preds, _ = self.model(data['input'].cuda())
        loss = self.criterion(preds.transpose(1, 2), data['output'].cuda())

        # Calculate gradients.
        if self.use_amp:
            # Calculate gradients through scaled loss rather than the original
            # loss to prevent underflowing in mixed precision.
            with amp.scale_loss(loss, self.optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

        # Update variables and learning rate.
        self.optimizer.step()
        self.scheduler.step()

        # Record metrics for training.
        self.recorder.add_train_metrics(loss=loss.item())

    def evaluate(self, batch: Optional[int] = None):
        with torch.no_grad():
            # Prepare evaluation.
            self.model.eval()

            # Fetch evaluation data.
            data = self.eval_loader.fetch(batch)

            # Predict next words and calculate loss.
            preds, _ = self.model(data['input'].cuda())
            loss = self.criterion(preds.transpose(1, 2), data['output'].cuda())

        # Record metrics for evaluation.
        self.recorder.add_eval_metrics(loss=loss.item())

    def state_dict(self) -> Dict[str, Any]:
        state_dict = {'train_loader': self.train_loader.tell(),
                      'eval_loader': self.eval_loader.tell(),
                      'model': self.model.state_dict(),
                      'optimizer': self.optimizer.state_dict(),
                      'scheduler': self.scheduler.state_dict(),
                      'recorder': self.recorder.state_dict()}

        # Save automatic mixed-precision states as well.
        if self.use_amp:
            state_dict['amp'] = amp.state_dict()

        return state_dict

    def load_state_dict(self, state_dict: Dict[str, Any]):
        self.train_loader.seek(state_dict['train_loader'])
        self.eval_loader.seek(state_dict['eval_loader'])
        self.model.load_state_dict(state_dict['model'])
        self.optimizer.load_state_dict(state_dict['optimizer'])
        self.scheduler.load_state_dict(state_dict['scheduler'])
        self.recorder.load_state_dict(state_dict['recorder'])

        # Restore automatic mixed-precision states as well.
        if self.use_amp:
            amp.load_state_dict(state_dict['amp'])
