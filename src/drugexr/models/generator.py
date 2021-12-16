import torch
import pytorch_lightning as pl

from src.drugexr.config.constants import DEVICE


class Generator(pl.LightningModule):
    def __init__(self, vocabulary, embed_size=128, hidden_size=512, lr=1e-3):
        super().__init__()
        self.voc = vocabulary
        self.embed_size = embed_size
        self.hidden_size = hidden_size
        self.output_size = vocabulary.size

        self.embed = torch.nn.Embedding(vocabulary.size, embed_size)
        rnn_layer = torch.nn.LSTM
        self.rnn = rnn_layer(embed_size, hidden_size, num_layers=3, batch_first=True)
        self.linear = torch.nn.Linear(hidden_size, vocabulary.size)

    def forward(self, x, h):
        output = self.embed(x.unsqueeze(-1))
        output, h_out = self.rnn(output, h)
        output = self.linear(output).squeeze(1)
        return output, h_out

    def init_h(self, batch_size, labels=None):
        h = torch.rand(3, batch_size, 512).to(DEVICE)
        if labels is not None:
            h[0, batch_size, 0] = labels
        c = torch.rand(3, batch_size, self.hidden_size).to(DEVICE)
        return (h, c)

    def likelihood(self, target):
        batch_size, seq_len = target.size()
        x = torch.LongTensor([self.voc.tk2ix["GO"]] * batch_size).to(DEVICE)
        h = self.init_h(batch_size)
        scores = torch.zeros(batch_size, seq_len).to(DEVICE)
        for step in range(seq_len):
            logits, h = self(x, h)
            logits = logits.log_softmax(dim=-1)
            score = logits.gather(1, target[:, step : step + 1]).squeeze()
            scores[:, step] = score
            x = target[:, step]
        return scores

    def PGLoss(self, loader):
        for seq, reward in loader:
            self.zero_grad()
            score = self.likelihood(seq)
            loss = score * reward
            loss = -loss.mean()
            loss.backward()
            self.optim.step()

    def sample(self, batch_size):
        x = torch.LongTensor([self.voc.tk2ix["GO"]] * batch_size)
        h = self.init_h(batch_size)
        sequences = torch.zeros(batch_size, self.voc.max_len).long()
        is_end = torch.zeros(batch_size).bool()

        for step in range(self.voc.max_len):
            logit, h = self(x, h)
            proba = logit.softmax(dim=-1)
            x = torch.multinomial(proba, 1).view(-1)
            x[is_end] = self.voc.tk2ix["EOS"]
            sequences[:, step] = x

            end_token = x == self.voc.tk2ix["EOS"]
            is_end = torch.ge(is_end + end_token, 1)
            if (is_end == 1).all():
                break
        return sequences

    def evolve(self, batch_size, epsilon=0.01, crover=None, mutate=None):
        # Start tokens
        x = torch.LongTensor([self.voc.tk2ix["GO"]] * batch_size)
        # Hidden states initialization for exploitation network
        h = self.init_h(batch_size)
        # Hidden states initialization for exploration network
        h1 = self.init_h(batch_size)
        h2 = self.init_h(batch_size)
        # Initialization of output matrix
        sequences = torch.zeros(batch_size, self.voc.max_len).long()
        # labels to judge and record which sample is ended
        is_end = torch.zeros(batch_size).bool()

        for step in range(self.voc.max_len):
            logit, h = self(x, h)
            proba = logit.softmax(dim=-1)
            if crover is not None:
                ratio = torch.rand(batch_size, 1)
                logit1, h1 = crover(x, h1)
                proba = proba * ratio + logit1.softmax(dim=-1) * (1 - ratio)
            if mutate is not None:
                logit2, h2 = mutate(x, h2)
                is_mutate = (torch.rand(batch_size) < epsilon)
                proba[is_mutate, :] = logit2.softmax(dim=-1)[is_mutate, :]
            # sampling based on output probability distribution
            x = torch.multinomial(proba, 1).view(-1)

            is_end |= x == self.voc.tk2ix["EOS"]
            x[is_end] = self.voc.tk2ix["EOS"]
            sequences[:, step] = x
            if is_end.all():
                break
        return sequences

    def evolve1(self, batch_size, epsilon=0.01, crover=None, mutate=None):
        # Start tokens
        x = torch.LongTensor([self.voc.tk2ix["GO"]] * batch_size)
        # Hidden states initialization for exploitation network
        h = self.init_h(batch_size)
        # Hidden states initialization for exploration network
        h2 = self.init_h(batch_size)
        # Initialization of output matrix
        sequences = torch.zeros(batch_size, self.voc.max_len).long()
        # labels to judge and record which sample is ended
        is_end = torch.zeros(batch_size).bool()

        for step in range(self.voc.max_len):
            is_change = torch.rand(1) < 0.5
            if crover is not None and is_change:
                logit, h = crover(x, h)
            else:
                logit, h = self(x, h)
            proba = logit.softmax(dim=-1)
            if mutate is not None:
                logit2, h2 = mutate(x, h2)
                ratio = torch.rand(batch_size, 1) * epsilon
                proba = (
                    logit.softmax(dim=-1) * (1 - ratio) + logit2.softmax(dim=-1) * ratio
                )
            # sampling based on output probability distribution
            x = torch.multinomial(proba, 1).view(-1)

            x[is_end] = self.voc.tk2ix["EOS"]
            sequences[:, step] = x

            # Judging whether samples are end or not.
            end_token = x == self.voc.tk2ix["EOS"]
            is_end = torch.ge(is_end + end_token, 1)
            #  If all of the samples generation being end, stop the sampling process
            if (is_end == 1).all():
                break
        return sequences

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)
        return optimizer

    def training_step(self, batch, batch_idx):
        loss = self.likelihood(batch)
        loss = -loss.mean()
        self.log("train_loss", loss)
        return loss


if __name__ == "__main__":
    from src.drugexr.data_structs.vocabulary import Vocabulary
    from src.drugexr.config import constants as c
    from torch.utils.data import DataLoader

    import pandas as pd

    vocabulary = Vocabulary(vocabulary_path=c.PROC_DATA_PATH / "chembl_voc.txt")
    generator = Generator(vocabulary=vocabulary)

    chembl = pd.read_table(c.PROC_DATA_PATH / "chembl_corpus_DEV_1000.txt").Token
    chembl = torch.LongTensor(vocabulary.encode([seq.split(" ") for seq in chembl]))
    chembl = DataLoader(chembl, batch_size=512, shuffle=True, drop_last=True, pin_memory=True, num_workers=1)

    trainer = pl.Trainer(gpus=1, log_every_n_steps=1)

    trainer.fit(model=generator, train_dataloaders=chembl)
