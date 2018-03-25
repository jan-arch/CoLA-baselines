import torch
import os
import sys
import torchtext

from torch import nn
from acceptability.utils import get_parser, get_model_instance, get_experiment_name
from acceptability.utils import seed_torch, pad_sentences
from .dataset import get_datasets, get_iter
from .meter import Meter
from .early_stopping import EarlyStopping
from .logger import Logger
from acceptability.utils import Checkpoint
from acceptability.utils import Timer

# TODO: Add __init__ for all modules and then __all__ in all of them
# to faciliate easy loading


class Trainer:
    def __init__(self):
        parser = get_parser()
        self.args = parser.parse_args()
        self.args.gpu = self.args.gpu and torch.cuda.is_available()

        seed_torch(self.args)

        if self.args.experiment_name is None:
            self.args.experiment_name = get_experiment_name(self.args)
        self.checkpoint = Checkpoint(self)
        self.num_classes = 2
        self.meter = Meter(self.num_classes)
        self.writer = Logger(self.args)
        self.writer.write(self.args)
        self.timer = Timer()
        self.load_datasets()

    def load_datasets(self):
        self.train_dataset, self.val_dataset, self.test_dataset, \
            sentence_field = get_datasets(self.args)

        if self.args.glove:
            vocab = sentence_field.vocab
            self.embedding = nn.Embedding(len(vocab), len(vocab.vectors[0]))
            self.embedding.weight.data.copy_(vocab.vectors)
            self.embedding.weight.requires_grad = False
            self.train_loader = get_iter(self.args, self.train_dataset)
            self.val_loader = get_iter(self.args, self.val_dataset)
            self.test_loader = get_iter(self.args, self.test_dataset)
        else:
            self.vocab = sentence_field
            self.embedding = nn.Embedding(vocab.get_size(), self.args.emb_dim)
            self.train_loader = torch.utils.data.DataLoader(
                self.train_dataset,
                batch_size=self.args.batch_size,
                shuffle=True,
                pin_memory=self.args.gpu
            )
            self.val_loader = torch.utils.data.DataLoader(
                self.val_dataset,
                batch_size=self.args.batch_size,
                pin_memory=self.args.gpu
            )
            self.test_loader = torch.utils.data.DataLoader(
                self.test_dataset,
                batch_size=self.args.batch_size,
                pin_memory=self.args.gpu
            )


    def load(self):
        self.model = get_model_instance(self.args)

        if self.model is None:
            # TODO: Add logger statement for valid model here
            sys.exit(1)

        self.early_stopping = EarlyStopping(self.model, self.checkpoint, self.args.patience)

        self.optimizer = torch.optim.Adam(filter(lambda p: p.requires_grad,
                                                 self.model.parameters()),
                                          lr=self.args.learning_rate)
        self.criterion = torch.nn.BCELoss()

        self.current_epoch = 0
        self.checkpoint.load_state_dict()

        if self.args.gpu:
            self.model = self.model.cuda()


    def train(self):
        self.print_start_info()
        log_interval = len(self.train_loader) // self.args.stages_per_epoch

        if log_interval <= 0:
            log_interval = 1

        for i in range(self.current_epoch + 1, self.args.epochs + 1):
            self.current_epoch = i
            self.writer.write("========= Epoch %d =========" % i)
            self.train_loader.init_epoch()
            for idx, data in enumerate(self.train_loader):
                if self.args.glove:
                    x, y = data.sentence, data.label
                    x = self.embedding(x)
                else:
                    x, y, _ = data
                    x, sizes = pad_sentences(x, self.vocab)
                    x = torch.from_numpy(x)
                    y = torch.LongTensor(y)

                self.optimizer.zero_grad()

                output = self.model(x)

                if type(output) == tuple:
                    output = output[0]
                output = output.squeeze()

                loss = self.criterion(output, y.float())
                loss.backward()

                self.optimizer.step()

                if idx % log_interval == 0 and idx > 0:
                    acc, loss, matthews, confusion = self.validate(self.val_loader)
                    other_metrics = {
                        'acc': acc,
                        'val_loss': loss,
                        'confusion_matrix': confusion
                    }
                    stop = self.early_stopping(matthews, other_metrics, i)
                    if stop:
                        self.writer.write("Early Stopping activated")
                        break
                    else:
                        self.print_current_info(idx, len(self.train_loader),
                                                matthews, other_metrics)


            self.print_epoch_info()

            if self.args.evaluate_train and i % self.args.train_evaluate_interval == 0:
                # At the some interval validate train loader
                self.writer.write("Evaluating training set")
                acc, loss, matthews, confusion = self.validate(self.train_loader)
                other_metrics = {
                    'acc': acc,
                    'val_loss': loss,
                    'confusion_matrix': confusion
                }
                self.writer.write("Epoch:")
                self.print_current_info(i, self.args.epochs, matthews, other_metrics)

            if self.early_stopping.is_activated():
                break

        self.checkpoint.restore()
        self.checkpoint.finalize()

    def validate(self, loader: torchtext.data.Iterator):
        self.model.eval()
        self.meter.reset()
        correct = 0
        total = 0
        total_loss = 0
        loader.init_epoch()
        for data in loader:
            x, y = data.sentence, data.label
            x = self.embedding(x)
            output = self.model(x)

            if type(output) == tuple:
                output = output[0]
            output = output.squeeze()

            loss = nn.functional.binary_cross_entropy(output, y.float(),
                                                      size_average=False)
            total_loss = loss.data[0]
            total += len(y)
            output = (output > 0.5).long()

            self.meter.add(output.data, y.data)
            if not self.args.gpu:
                correct += (y ==
                            output).data.cpu().numpy().sum()
            else:
                correct += (y == output).data.sum()
        self.model.train()

        avg_loss = total_loss / total

        return correct / total * 100, avg_loss, \
               self.meter.matthews(), self.meter.confusion()

    def print_epoch_info(self):
        self.writer.write_new_line()
        self.writer.write(self.early_stopping.get_info())
        self.writer.write("Time Elasped: %s" % self.timer.get_current())

    def print_current_info(self, it, total, matthews, other_metrics):
        self.writer.write("%d/%d: Matthews %.5f, Accuracy: %.5f, Loss: %.9f" %
              (it, total, matthews,
               other_metrics['acc'], other_metrics['val_loss']))

    def print_start_info(self):
        self.writer.write("======== General =======")
        self.writer.write("Model: %s" % self.args.model)
        self.writer.write("GPU: %s" % self.args.gpu)
        self.writer.write("Experiment Name: %s" % self.args.experiment_name)
        self.writer.write("Save location: %s" % self.args.save_loc)
        self.writer.write("Logs dir: %s" % self.args.logs_dir)
        self.writer.write("Timestamp: %s" % self.timer.get_time_hhmmss())
        self.writer.write_new_line()

        self.writer.write("======== Data =======")
        self.writer.write("Training set: %d examples" % (len(self.train_dataset)))
        self.writer.write("Validation set: %d examples" % (len(self.val_dataset)))
        self.writer.write("Test set: %d examples" % (len(self.test_dataset)))
        self.writer.write_new_line()

        self.writer.write("======= Parameters =======")
        self.writer.write("Learning Rate: %f" % self.args.learning_rate)
        self.writer.write("Batch Size: %d" % self.args.batch_size)
        self.writer.write("Epochs: %d" % self.args.epochs)
        self.writer.write("Patience: %d" % self.args.patience)
        self.writer.write("Stages per Epoch: %d" % self.args.stages_per_epoch)

        if self.args.glove:
            self.writer.write("Embedding: %s" % self.args.embedding)
        else:
            self.writer.write("Embedding: %d x %d" % self.embedding.weight.size)
        self.writer.write("Number of layers: %d" % self.args.num_layers)
        self.writer.write("Hidden Size: %d" % self.args.hidden_size)
        self.writer.write("Encoder Size: %d" % self.args.encoding_size)
        self.writer.write("Resume: %s" % self.args.resume)
        self.writer.write_new_line()

        self.writer.write("======= Model =======")
        self.writer.write(self.model)
        self.writer.write_new_line()
