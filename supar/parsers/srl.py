import os
import pdb
import subprocess
import torch
import torch.nn as nn
from supar.models import (BiaffineSrlModel, BiaffineSpanSrlModel,
                          VISrlModel)
from supar.parsers.parser import Parser
from supar.utils import Config, Dataset, Embedding
from supar.utils.common import bos, pad, unk
from supar.utils.field import ChartField, Field, SubwordField, SpanSrlFiled
from supar.utils.logging import get_logger, progress_bar
from supar.utils.logging import init_logger, logger
from supar.utils.metric import ChartMetric, SrlMetric
from supar.utils.transform import CoNLL
from torch.optim import Adam
from torch.optim.lr_scheduler import ExponentialLR
from torch.optim.lr_scheduler import _LRScheduler

logger = get_logger(__name__)

class VLR(_LRScheduler):
    def __init__(self, optimizer, warmup_steps=8000, last_epoch=-1):
        self.warmup_steps = warmup_steps
        super(VLR, self).__init__(optimizer, last_epoch)

    def get_lr(self):
        epoch = max(self.last_epoch, 1)
        scale = min(pow(epoch, -0.5), epoch * pow(self.warmup_steps, -1.5))
        return [base_lr * scale for base_lr in self.base_lrs]


class BiaffineSpanSrlParser(Parser):
    r"""
    The implementation of Biaffine Semantic Dependency Parser.

    References:
        - Timothy Dozat and Christopher D. Manning. 20178.
          `Simpler but More Accurate Semantic Dependency Parsing`_.

    .. _Simpler but More Accurate Semantic Dependency Parsing:
        https://www.aclweb.org/anthology/P18-2077/
    """

    NAME = 'biaffine-span_based-semantic-role-labeling'
    MODEL = BiaffineSpanSrlModel

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.WORD, self.CHAR, self.BERT = self.transform.FORM
        self.LEMMA = self.transform.LEMMA
        self.TAG = self.transform.POS
        self.EDGE, self.LABEL, self.SPAN = self.transform.PHEAD

    def train(self,
              train,
              dev,
              test,
              dev_pred=None,
              dev_gold=None,
              test_pred=None,
              test_gold=None,
              buckets=32,
              batch_size=5000,
              clip=5.0,
              epochs=5000,
              patience=100,
              **kwargs):
        
        return super().train(**Config().update(locals()))

    def evaluate(self,
                 data,
                 buckets=8,
                 batch_size=5000,
                 verbose=True,
                 **kwargs):
        r"""
        Args:
            data (str):
                The data for evaluation, both list of instances and filename are allowed.
            buckets (int):
                The number of buckets that sentences are assigned to. Default: 32.
            batch_size (int):
                The number of tokens in each batch. Default: 5000.
            verbose (bool):
                If ``True``, increases the output verbosity. Default: ``True``.
            kwargs (dict):
                A dict holding the unconsumed arguments that can be used to update the configurations for evaluation.

        Returns:
            The loss scalar and evaluation results.
        """

        return super().evaluate(**Config().update(locals()))

    def predict(self,
                data,
                pred=None,
                lang='en',
                buckets=8,
                batch_size=5000,
                verbose=True,
                **kwargs):
        r"""
        Args:
            data (list[list] or str):
                The data for prediction, both a list of instances and filename are allowed.
            pred (str):
                If specified, the predicted results will be saved to the file. Default: ``None``.
            lang (str):
                Language code (e.g., 'en') or language name (e.g., 'English') for the text to tokenize.
                ``None`` if tokenization is not required.
                Default: ``en``.
            buckets (int):
                The number of buckets that sentences are assigned to. Default: 32.
            batch_size (int):
                The number of tokens in each batch. Default: 5000.
            prob (bool):
                If ``True``, outputs the probabilities. Default: ``False``.
            verbose (bool):
                If ``True``, increases the output verbosity. Default: ``True``.
            kwargs (dict):
                A dict holding the unconsumed arguments that can be used to update the configurations for prediction.

        Returns:
            A :class:`~supar.utils.Dataset` object that stores the predicted results.
        """
        return super().predict(**Config().update(locals()))
        

    def _train(self, loader):
        self.model.train()

        bar = progress_bar(loader)

        for i, (words, *feats, edges, labels, spans) in enumerate(bar, 1):
            
            # pdb.set_trace()
            mask = words.ne(self.WORD.pad_index)
            # print(mask.shape)
            mask = mask.unsqueeze(1) & mask.unsqueeze(2)
            mask[:, 0] = 0
            # pred_mask = mask1 & edges[..., 0].eq(1)
            # pred_mask[:, 0] = 0
            s_edge, s_label, encoder_out = self.model(words, feats)
            two_stage_loss = self.model.loss(s_edge, s_label, edges, labels, mask)
            two_stage_loss = two_stage_loss / self.args.update_steps
            span_loss, k = self.model.span_loss(mask, spans, encoder_out)
            span_loss = span_loss / self.args.update_steps
            # k = 0
            if(k<=0):
                loss = two_stage_loss
            else:
                loss = self.args.weight * span_loss + (1-self.args.weight) * two_stage_loss
            loss.backward()
            # pdb.set_trace()
            nn.utils.clip_grad_norm_(self.model.parameters(), self.args.clip)
            if i % self.args.update_steps == 0:
                self.optimizer.step()
                self.scheduler.step()
                self.optimizer.zero_grad()

            # edge_preds, label_preds = self.model.decode(s_edge, s_label)
            # metric(label_preds.masked_fill(~(edge_preds.gt(0) & mask), -1),
            #        labels.masked_fill(~(edges.gt(0) & mask), -1))
            bar.set_postfix_str(
                f"lr: {self.scheduler.get_last_lr()[0]:.4e} - loss: {loss:.4f}"
            )

    @torch.no_grad()
    def _evaluate(self, loader):
        self.model.eval()

        total_loss, metric = 0, ChartMetric()
        metric2 = SrlMetric()
        prd_idx, B_idx, I_idx = self.LABEL.vocab.stoi['[prd]'], self.LABEL.vocab.stoi['B'], self.LABEL.vocab.stoi['I']
        for words, *feats, edges, labels, spans in loader:
            mask = words.ne(self.WORD.pad_index)
            mask = mask.unsqueeze(1) & mask.unsqueeze(2)
            mask[:, 0] = 0
            n_mask = mask.unsqueeze(1).expand(-1, words.shape[1], -1, -1)
            s_edge, s_label, encoder_out = self.model(words, feats)
            arg_preds = self.model.decode(s_edge, s_label, encoder_out, mask, prd_idx, B_idx, I_idx)
            # pdb.set_trace()
            metric(arg_preds,
                   spans.masked_fill(~n_mask, -1))
            edge_preds, label_preds = s_edge.argmax(-1), s_label.argmax(-1)
            metric2(label_preds.masked_fill(~(edge_preds.gt(0) & mask), -1),
                   labels.masked_fill(~(edges.gt(0) & mask), -1))
        
        return metric, metric2
    
    @torch.no_grad()
    def _evaluate2(self, loader):
        self.model.eval()

        total_loss, metric = 0, ChartMetric()
        metric2 = SrlMetric()
        prd_idx, B_idx, I_idx = self.LABEL.vocab.stoi['[prd]'], self.LABEL.vocab.stoi['B'], self.LABEL.vocab.stoi['I']
        for words, *feats, edges, labels, spans in loader:
            mask = words.ne(self.WORD.pad_index)
            mask = mask.unsqueeze(1) & mask.unsqueeze(2)
            mask[:, 0] = 0
            # n_mask = mask.unsqueeze(1).expand(-1, words.shape[1], -1, -1)
            s_edge, s_label, encoder_out = self.model(words, feats)
            # arg_preds = self.model.decode(s_edge, s_label, encoder_out, mask, prd_idx, B_idx, I_idx)
            # pdb.set_trace()
            # metric(arg_preds,
            #        spans.masked_fill(~n_mask, -1))
            edge_preds, label_preds = s_edge.argmax(-1), s_label.argmax(-1)
            metric2(label_preds.masked_fill(~(edge_preds.gt(0) & mask), -1),
                   labels.masked_fill(~(edges.gt(0) & mask), -1))
        
        return metric2

    @torch.no_grad()
    def _predict(self, loader):
        self.model.eval()

        preds = {}
        charts, probs = [], []
        for words, *feats in progress_bar(loader):
            mask = words.ne(self.WORD.pad_index)
            mask = mask.unsqueeze(1) & mask.unsqueeze(2)
            mask[:, 0] = 0
            lens = mask[:, 1].sum(-1).tolist()
            s_edge, s_label = self.model(words, feats)
            edge_preds, label_preds = self.model.decode(s_edge, s_label)
            chart_preds = label_preds.masked_fill(~(edge_preds.gt(0) & mask),
                                                  -1)
            charts.extend(chart[1:i, :i].tolist()
                          for i, chart in zip(lens, chart_preds.unbind()))
            if self.args.prob:
                probs.extend([
                    prob[1:i, :i].cpu()
                    for i, prob in zip(lens,
                                       s_edge.softmax(-1).unbind())
                ])
        charts = [
            CoNLL.build_relations(
                [[self.LABEL.vocab[i] if i >= 0 else None for i in row]
                 for row in chart]) for chart in charts
        ]
        preds = {'labels': charts}
        if self.args.prob:
            preds['probs'] = probs
        # pdb.set_trace()

        return preds

    @classmethod
    def build(cls,
              path,
              optimizer_args={
                  'lr': 1e-3,
                  'betas': (.0, .95),
                  'eps': 1e-12,
                  'weight_decay': 3e-9
              },
              scheduler_args={'gamma': .75**(1 / 5000)},
              fix_len=20,
              **kwargs):
        r"""
        Build a brand-new Parser, including initialization of all data fields and model parameters.

        Args:
            path (str):
                The path of the model to be saved.
            optimizer_args (dict):
                Arguments for creating an optimizer.
            scheduler_args (dict):
                Arguments for creating a scheduler.
            min_freq (str):
                The minimum frequency needed to include a token in the vocabulary. Default:7.
            fix_len (int):
                The max length of all subword pieces. The excess part of each piece will be truncated.
                Required if using CharLSTM/BERT.
                Default: 20.
            kwargs (dict):
                A dict holding the unconsumed arguments.
        """

        args = Config(**locals())
        interpolation = args.itp
        args.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and not args.build:
            parser = cls.load(**args)
            parser.model = cls.MODEL(**parser.args)
            parser.model.load_pretrained(parser.WORD.embed).to(args.device)
            return parser

        logger.info("Building the fields")
        WORD = Field('words', pad=pad, unk=unk, bos=bos, lower=True)
        TAG, CHAR, LEMMA, BERT = None, None, None, None
        if 'tag' in args.feat:
            TAG = Field('tags', bos=bos)
        if 'char' in args.feat:
            CHAR = SubwordField('chars',
                                pad=pad,
                                unk=unk,
                                bos=bos,
                                fix_len=args.fix_len)
        if 'lemma' in args.feat:
            LEMMA = Field('lemmas', pad=pad, unk=unk, bos=bos, lower=True)
        if 'bert' in args.feat:
            from transformers import AutoTokenizer, GPT2Tokenizer, GPT2TokenizerFast
            tokenizer = AutoTokenizer.from_pretrained(args.bert)
            BERT = SubwordField('bert',
                                pad=tokenizer.pad_token,
                                unk=tokenizer.unk_token,
                                bos=tokenizer.bos_token or tokenizer.cls_token,
                                fix_len=args.fix_len,
                                tokenize=tokenizer.tokenize)
            BERT.vocab = tokenizer.get_vocab()
        EDGE = ChartField('edges', use_vocab=False, fn=CoNLL.get_edges)
        LABEL = ChartField('labels', fn=CoNLL.get_BI_labels)
        SPAN = SpanSrlFiled('spans', build_fn=CoNLL.get_span_labels, fn=CoNLL.get_spans)
        transform = CoNLL(FORM=(WORD, CHAR, BERT),
                          LEMMA=LEMMA,
                          POS=TAG,
                          PHEAD=(EDGE, LABEL, SPAN))

        train = Dataset(transform, args.train)
        WORD.build(
            train, args.min_freq,
            (Embedding.load(args.embed, args.unk) if args.embed else None))
        if TAG is not None:
            TAG.build(train)
        if CHAR is not None:
            CHAR.build(train)
        if LEMMA is not None:
            LEMMA.build(train)
        LABEL.build(train)
        if(args.use_pred):
            LABEL.vocab.extend(['Other'])
        SPAN.build(train)
        args.update({
            'n_words': WORD.vocab.n_init,
            'n_labels': len(LABEL.vocab),
            'n_span_labels': len(SPAN.vocab),
            'n_tags': len(TAG.vocab) if TAG is not None else None,
            'n_chars': len(CHAR.vocab) if CHAR is not None else None,
            'char_pad_index': CHAR.pad_index if CHAR is not None else None,
            'n_lemmas': len(LEMMA.vocab) if LEMMA is not None else None,
            'bert_pad_index': BERT.pad_index if BERT is not None else None,
            'pad_index': WORD.pad_index,
            'unk_index': WORD.unk_index,
            'interpolation': interpolation,
            'encoder': args.encoder,
            'n_prd': args.n_prd
        })
        logger.info(f"{transform}")
        logger.info("Building the model")
        model = cls.MODEL(**args).load_pretrained(WORD.embed).to(args.device)
        logger.info(f"{model}\n")
        if (args.encoder != 'transformer'):
            optimizer = Adam(model.parameters(), **optimizer_args)
            scheduler = ExponentialLR(optimizer, **scheduler_args)
        else:
            optimizer = Adam(model.parameters(),
                             lr=0.04,
                             betas=(0.9, 0.98),
                             eps=1e-12)
            scheduler = VLR(optimizer, warmup_steps=8000)

        return cls(args, model, transform, optimizer, scheduler)


class BiaffineSrlParser(Parser):
    r"""
    The implementation of Biaffine Semantic Dependency Parser.

    References:
        - Timothy Dozat and Christopher D. Manning. 20178.
          `Simpler but More Accurate Semantic Dependency Parsing`_.

    .. _Simpler but More Accurate Semantic Dependency Parsing:
        https://www.aclweb.org/anthology/P18-2077/
    """

    NAME = 'biaffine-semantic-role-labeling'
    MODEL = BiaffineSrlModel

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.WORD, self.CHAR, self.BERT = self.transform.FORM
        self.LEMMA = self.transform.LEMMA
        self.TAG = self.transform.POS
        self.EDGE, self.LABEL = self.transform.PHEAD

    def train(self,
              train,
              dev,
              test,
              dev_pred=None,
              dev_gold=None,
              test_pred=None,
              test_gold=None,
              buckets=32,
              batch_size=5000,
              clip=5.0,
              epochs=5000,
              patience=100,
              **kwargs):
        
        return super().train(**Config().update(locals()))

    def evaluate(self,
                 data,
                 buckets=8,
                 batch_size=5000,
                 verbose=True,
                 **kwargs):
        r"""
        Args:
            data (str):
                The data for evaluation, both list of instances and filename are allowed.
            buckets (int):
                The number of buckets that sentences are assigned to. Default: 32.
            batch_size (int):
                The number of tokens in each batch. Default: 5000.
            verbose (bool):
                If ``True``, increases the output verbosity. Default: ``True``.
            kwargs (dict):
                A dict holding the unconsumed arguments that can be used to update the configurations for evaluation.

        Returns:
            The loss scalar and evaluation results.
        """

        return super().evaluate(**Config().update(locals()))

    def predict(self,
                data,
                pred=None,
                lang='en',
                buckets=8,
                batch_size=5000,
                verbose=True,
                **kwargs):
        r"""
        Args:
            data (list[list] or str):
                The data for prediction, both a list of instances and filename are allowed.
            pred (str):
                If specified, the predicted results will be saved to the file. Default: ``None``.
            lang (str):
                Language code (e.g., 'en') or language name (e.g., 'English') for the text to tokenize.
                ``None`` if tokenization is not required.
                Default: ``en``.
            buckets (int):
                The number of buckets that sentences are assigned to. Default: 32.
            batch_size (int):
                The number of tokens in each batch. Default: 5000.
            prob (bool):
                If ``True``, outputs the probabilities. Default: ``False``.
            verbose (bool):
                If ``True``, increases the output verbosity. Default: ``True``.
            kwargs (dict):
                A dict holding the unconsumed arguments that can be used to update the configurations for prediction.

        Returns:
            A :class:`~supar.utils.Dataset` object that stores the predicted results.
        """
        return super().predict(**Config().update(locals()))
        

    def _train(self, loader):
        self.model.train()

        bar, metric = progress_bar(loader), ChartMetric()

        for words, *feats, edges, labels in bar:
            self.optimizer.zero_grad()

            mask = words.ne(self.WORD.pad_index)
            mask = mask.unsqueeze(1) & mask.unsqueeze(2)
            mask[:, 0] = 0
            if(self.args.repr_gold):
                s_edge, s_label = self.model(words, feats, edges)
            else:
                s_edge, s_label = self.model(words, feats)
            loss = self.model.loss(s_edge, s_label, edges, labels, mask)
            loss.backward()
            nn.utils.clip_grad_norm_(self.model.parameters(), self.args.clip)
            self.optimizer.step()
            self.scheduler.step()

            edge_preds, label_preds = self.model.decode(s_edge, s_label)
            metric(label_preds.masked_fill(~(edge_preds.gt(0) & mask), -1),
                   labels.masked_fill(~(edges.gt(0) & mask), -1))
            bar.set_postfix_str(
                f"lr: {self.scheduler.get_last_lr()[0]:.4e} - loss: {loss:.4f} - {metric}"
            )

    @torch.no_grad()
    def _evaluate(self, loader):
        self.model.eval()

        total_loss, metric = 0, SrlMetric()

        for words, *feats, edges, labels in loader:
            mask = words.ne(self.WORD.pad_index)
            mask = mask.unsqueeze(1) & mask.unsqueeze(2)
            mask[:, 0] = 0
            s_edge, s_label = self.model(words, feats)
            # loss = self.model.loss(s_edge, s_label, edges, labels, mask)
            # total_loss += loss.item()

            edge_preds, label_preds = self.model.decode(s_edge, s_label)
            metric(label_preds.masked_fill(~(edge_preds.gt(0) & mask), -1),
                   labels.masked_fill(~(edges.gt(0) & mask), -1))
        # total_loss /= len(loader)

        # return total_loss, metric
        return metric

    @torch.no_grad()
    def _predict(self, loader):
        self.model.eval()

        preds = {}
        charts, probs = [], []
        for words, *feats in progress_bar(loader):
            # pdb.set_trace()
            mask = words.ne(self.WORD.pad_index)
            mask = mask.unsqueeze(1) & mask.unsqueeze(2)
            mask[:, 0] = 0
            lens = mask[:, 1].sum(-1).tolist()
            s_edge, s_label = self.model(words, feats)
            edge_preds, label_preds = self.model.decode(s_edge, s_label)
            chart_preds = label_preds.masked_fill(~(edge_preds.gt(0) & mask),
                                                  -1)
            charts.extend(chart[1:i, :i].tolist()
                          for i, chart in zip(lens, chart_preds.unbind()))
            if self.args.prob:
                probs.extend([
                    prob[1:i, :i].cpu()
                    for i, prob in zip(lens,
                                       s_edge.softmax(-1).unbind())
                ])
        charts = [
            CoNLL.build_relations(
                [[self.LABEL.vocab[i] if i >= 0 else None for i in row]
                 for row in chart]) for chart in charts
        ]
        preds = {'labels': charts}
        if self.args.prob:
            preds['probs'] = probs
        # pdb.set_trace()

        return preds

    @classmethod
    def build(cls,
              path,
              optimizer_args={
                  'lr': 1e-3,
                  'betas': (.0, .95),
                  'eps': 1e-12,
                  'weight_decay': 3e-9
              },
              scheduler_args={'gamma': .75**(1 / 5000)},
              fix_len=20,
              **kwargs):
        r"""
        Build a brand-new Parser, including initialization of all data fields and model parameters.

        Args:
            path (str):
                The path of the model to be saved.
            optimizer_args (dict):
                Arguments for creating an optimizer.
            scheduler_args (dict):
                Arguments for creating a scheduler.
            min_freq (str):
                The minimum frequency needed to include a token in the vocabulary. Default:7.
            fix_len (int):
                The max length of all subword pieces. The excess part of each piece will be truncated.
                Required if using CharLSTM/BERT.
                Default: 20.
            kwargs (dict):
                A dict holding the unconsumed arguments.
        """

        args = Config(**locals())
        interpolation = args.itp
        args.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and not args.build:
            parser = cls.load(**args)
            parser.model = cls.MODEL(**parser.args)
            parser.model.load_pretrained(parser.WORD.embed).to(args.device)
            return parser

        logger.info("Building the fields")
        WORD = Field('words', pad=pad, unk=unk, bos=bos, lower=True)
        TAG, CHAR, LEMMA, BERT = None, None, None, None
        if 'tag' in args.feat:
            TAG = Field('tags', bos=bos)
        if 'char' in args.feat:
            CHAR = SubwordField('chars',
                                pad=pad,
                                unk=unk,
                                bos=bos,
                                fix_len=args.fix_len)
        if 'lemma' in args.feat:
            LEMMA = Field('lemmas', pad=pad, unk=unk, bos=bos, lower=True)
        if 'bert' in args.feat:
            from transformers import AutoTokenizer, GPT2Tokenizer, GPT2TokenizerFast
            tokenizer = AutoTokenizer.from_pretrained(args.bert)
            BERT = SubwordField('bert',
                                pad=tokenizer.pad_token,
                                unk=tokenizer.unk_token,
                                bos=tokenizer.bos_token or tokenizer.cls_token,
                                fix_len=args.fix_len,
                                tokenize=tokenizer.tokenize)
            BERT.vocab = tokenizer.get_vocab()
        EDGE = ChartField('edges', use_vocab=False, fn=CoNLL.get_edges)
        LABEL = ChartField('labels', fn=CoNLL.get_labels)
        transform = CoNLL(FORM=(WORD, CHAR, BERT),
                          LEMMA=LEMMA,
                          POS=TAG,
                          PHEAD=(EDGE, LABEL))

        train = Dataset(transform, args.train)
        WORD.build(
            train, args.min_freq,
            (Embedding.load(args.embed, args.unk) if args.embed else None))
        if TAG is not None:
            TAG.build(train)
        if CHAR is not None:
            CHAR.build(train)
        if LEMMA is not None:
            LEMMA.build(train)
        LABEL.build(train)
        if(args.use_pred):
            LABEL.vocab.extend(['Other'])
        args.update({
            'n_words': WORD.vocab.n_init,
            'n_labels': len(LABEL.vocab),
            'n_tags': len(TAG.vocab) if TAG is not None else None,
            'n_chars': len(CHAR.vocab) if CHAR is not None else None,
            'char_pad_index': CHAR.pad_index if CHAR is not None else None,
            'n_lemmas': len(LEMMA.vocab) if LEMMA is not None else None,
            'bert_pad_index': BERT.pad_index if BERT is not None else None,
            'pad_index': WORD.pad_index,
            'unk_index': WORD.unk_index,
            'interpolation': interpolation,
            'encoder': args.encoder
        })
        logger.info(f"{transform}")
        logger.info("Building the model")
        model = cls.MODEL(**args).load_pretrained(WORD.embed).to(args.device)
        logger.info(f"{model}\n")
        if (args.encoder != 'transformer'):
            optimizer = Adam(model.parameters(), **optimizer_args)
            scheduler = ExponentialLR(optimizer, **scheduler_args)
        else:
            optimizer = Adam(model.parameters(),
                             lr=0.04,
                             betas=(0.9, 0.98),
                             eps=1e-12)
            scheduler = VLR(optimizer, warmup_steps=8000)

        return cls(args, model, transform, optimizer, scheduler)

class VISrlParser(BiaffineSrlParser):
    r"""
    The implementation of Semantic Dependency Parser using Variational Inference.

    References:
        - Xinyu Wang, Jingxian Huang and Kewei Tu. 2019.
          `Second-Order Semantic Dependency Parsing with End-to-End Neural Networks`_.

    .. _Second-Order Semantic Dependency Parsing with End-to-End Neural Networks:
        https://www.aclweb.org/anthology/P19-1454/
    """

    NAME = 'vi-semantic-role-labeling'
    MODEL = VISrlModel

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.WORD, self.CHAR, self.BERT = self.transform.FORM
        self.LEMMA = self.transform.LEMMA
        self.TAG = self.transform.POS
        self.EDGE, self.LABEL = self.transform.PHEAD

    def train(self,
              train,
              dev,
              test,
              buckets=32,
              batch_size=5000,
              verbose=True,
              **kwargs):
        r"""
        Args:
            train/dev/test (list[list] or str):
                Filenames of the train/dev/test datasets.
            buckets (int):
                The number of buckets that sentences are assigned to. Default: 32.
            batch_size (int):
                The number of tokens in each batch. Default: 5000.
            verbose (bool):
                If ``True``, increases the output verbosity. Default: ``True``.
            kwargs (dict):
                A dict holding the unconsumed arguments that can be used to update the configurations for training.
        """

        return super().train(**Config().update(locals()))

    def evaluate(self,
                 data,
                 buckets=8,
                 batch_size=5000,
                 verbose=True,
                 **kwargs):
        r"""
        Args:
            data (str):
                The data for evaluation, both list of instances and filename are allowed.
            buckets (int):
                The number of buckets that sentences are assigned to. Default: 32.
            batch_size (int):
                The number of tokens in each batch. Default: 5000.
            verbose (bool):
                If ``True``, increases the output verbosity. Default: ``True``.
            kwargs (dict):
                A dict holding the unconsumed arguments that can be used to update the configurations for evaluation.

        Returns:
            The loss scalar and evaluation results.
        """

        return super().evaluate(**Config().update(locals()))

    def predict(self,
                data,
                pred=None,
                lang='en',
                buckets=8,
                batch_size=5000,
                verbose=True,
                **kwargs):
        r"""
        Args:
            data (list[list] or str):
                The data for prediction, both a list of instances and filename are allowed.
            pred (str):
                If specified, the predicted results will be saved to the file. Default: ``None``.
            lang (str):
                Language code (e.g., 'en') or language name (e.g., 'English') for the text to tokenize.
                ``None`` if tokenization is not required.
                Default: ``en``.
            buckets (int):
                The number of buckets that sentences are assigned to. Default: 32.
            batch_size (int):
                The number of tokens in each batch. Default: 5000.
            prob (bool):
                If ``True``, outputs the probabilities. Default: ``False``.
            verbose (bool):
                If ``True``, increases the output verbosity. Default: ``True``.
            kwargs (dict):
                A dict holding the unconsumed arguments that can be used to update the configurations for prediction.

        Returns:
            A :class:`~supar.utils.Dataset` object that stores the predicted results.
        """

        return super().predict(**Config().update(locals()))

    def _train(self, loader):
        self.model.train()

        bar, metric = progress_bar(loader), ChartMetric()

        for i, (words, *feats, edges, labels) in enumerate(bar, 1):
            # self.optimizer.zero_grad()

            mask = words.ne(self.WORD.pad_index)
            mask = mask.unsqueeze(1) & mask.unsqueeze(2)
            mask[:, 0] = 0
            s_edge, s_sib, s_cop, s_grd, s_label = self.model(words, feats)
            loss, s_edge = self.model.loss(s_edge, s_sib, s_cop, s_grd,
                                           s_label, edges, labels, mask)
            loss = loss / self.args.update_steps
            loss.backward()
            nn.utils.clip_grad_norm_(self.model.parameters(), self.args.clip)
            if i % self.args.update_steps == 0:
                self.optimizer.step()
                self.scheduler.step()
                self.optimizer.zero_grad()
            # self.optimizer.step()
            # self.scheduler.step()

            # edge_preds, label_preds = self.model.decode(s_edge, s_label)
            # metric(label_preds.masked_fill(~(edge_preds.gt(0) & mask), -1),
            #        labels.masked_fill(~(edges.gt(0) & mask), -1))

            label_preds = self.model.decode(s_edge, s_label)
            metric(label_preds.masked_fill(~mask, -1),
                   labels.masked_fill(~mask, -1))

            bar.set_postfix_str(
                f"lr: {self.scheduler.get_last_lr()[0]:.4e} - loss: {loss:.4f} - {metric}"
            )

    @torch.no_grad()
    def _evaluate(self, loader):
        self.model.eval()

        total_loss, metric = 0, SrlMetric()

        for words, *feats, edges, labels in loader:
            mask = words.ne(self.WORD.pad_index)
            mask = mask.unsqueeze(1) & mask.unsqueeze(2)
            mask[:, 0] = 0
            s_edge, s_sib, s_cop, s_grd, s_label = self.model(words, feats)
            # loss, s_edge = self.model.loss(s_edge, s_sib, s_cop, s_grd,
            #                                s_label, edges, labels, mask)
            loss, s_edge = self.model.loss(s_edge, s_sib, s_cop, s_grd,
                                           s_label, edges, labels, mask)
            # total_loss += loss.item()

            # edge_preds, label_preds = self.model.decode(s_edge, s_label)
            # metric(label_preds.masked_fill(~(edge_preds.gt(0) & mask), -1),
            #        labels.masked_fill(~(edges.gt(0) & mask), -1))

            label_preds = self.model.decode(s_edge, s_label)
            metric(label_preds.masked_fill(~mask, -1),
                   labels.masked_fill(~mask, -1))

        # total_loss /= len(loader)

        # return total_loss, metric
        return metric

    @torch.no_grad()
    def _predict(self, loader):

        self.model.eval()

        preds = {'labels': [], 'probs': [] if self.args.prob else None}
        for words, *feats in progress_bar(loader):
            word_mask = words.ne(self.args.pad_index)
            mask = word_mask if len(words.shape) < 3 else word_mask.any(-1)
            mask = mask.unsqueeze(1) & mask.unsqueeze(2)
            mask[:, 0] = 0
            lens = mask[:, 1].sum(-1).tolist()
            s_edge, s_sib, s_cop, s_grd, s_label = self.model(words, feats)
            s_edge = self.model.vi((s_edge, s_sib, s_cop, s_grd), mask)
            label_preds = self.model.decode(s_edge,
                                            s_label).masked_fill(~mask, -1)
            preds['labels'].extend(chart[1:i, :i].tolist()
                                   for i, chart in zip(lens, label_preds))
            if self.args.prob:
                preds['probs'].extend([
                    prob[1:i, :i].cpu()
                    for i, prob in zip(lens, s_edge.unbind())
                ])
        preds['labels'] = [
            CoNLL.build_relations(
                [[self.LABEL.vocab[i] if i >= 0 else None for i in row]
                 for row in chart]) for chart in preds['labels']
        ]

        return preds

    @classmethod
    def build(cls,
              path,
              optimizer_args={
                  'lr': 1e-3,
                  'betas': (.0, .95),
                  'eps': 1e-12
              },
              scheduler_args={'gamma': .75**(1 / 5000)},
              min_freq=7,
              fix_len=20,
              **kwargs):
        r"""
        Build a brand-new Parser, including initialization of all data fields and model parameters.

        Args:
            path (str):
                The path of the model to be saved.
            optimizer_args (dict):
                Arguments for creating an optimizer.
            scheduler_args (dict):
                Arguments for creating a scheduler.
            min_freq (str):
                The minimum frequency needed to include a token in the vocabulary. Default:7.
            fix_len (int):
                The max length of all subword pieces. The excess part of each piece will be truncated.
                Required if using CharLSTM/BERT.
                Default: 20.
            kwargs (dict):
                A dict holding the unconsumed arguments.
        """

        args = Config(**locals())
        args.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and not args.build:
            parser = cls.load(**args)
            parser.model = cls.MODEL(**parser.args)
            parser.model.load_pretrained(parser.WORD.embed).to(args.device)
            return parser

        logger.info("Building the fields")
        WORD = Field('words', pad=pad, unk=unk, bos=bos, lower=True)
        TAG, CHAR, LEMMA, BERT = None, None, None, None
        if 'tag' in args.feat:
            TAG = Field('tags', bos=bos)
        if 'char' in args.feat:
            CHAR = SubwordField('chars',
                                pad=pad,
                                unk=unk,
                                bos=bos,
                                fix_len=args.fix_len)
        if 'lemma' in args.feat:
            LEMMA = Field('lemmas', pad=pad, unk=unk, bos=bos, lower=True)
        if 'bert' in args.feat:
            from transformers import AutoTokenizer, GPT2Tokenizer, GPT2TokenizerFast
            tokenizer = AutoTokenizer.from_pretrained(args.bert)
            BERT = SubwordField('bert',
                                pad=tokenizer.pad_token,
                                unk=tokenizer.unk_token,
                                bos=tokenizer.bos_token or tokenizer.cls_token,
                                fix_len=args.fix_len,
                                tokenize=tokenizer.tokenize)
            BERT.vocab = tokenizer.get_vocab()
        EDGE = ChartField('edges', use_vocab=False, fn=CoNLL.get_edges)
        LABEL = ChartField('labels', fn=CoNLL.get_labels)
        transform = CoNLL(FORM=(WORD, CHAR, BERT),
                          LEMMA=LEMMA,
                          POS=TAG,
                          PHEAD=(EDGE, LABEL))

        train = Dataset(transform, args.train)
        WORD.build(
            train, args.min_freq,
            (Embedding.load(args.embed, args.unk) if args.embed else None))
        if TAG is not None:
            TAG.build(train)
        if CHAR is not None:
            CHAR.build(train)
        if LEMMA is not None:
            LEMMA.build(train)
        LABEL.build(train)
        args.update({
            'n_words': WORD.vocab.n_init,
            'n_labels': len(LABEL.vocab),
            'n_tags': len(TAG.vocab) if TAG is not None else None,
            'n_chars': len(CHAR.vocab) if CHAR is not None else None,
            'char_pad_index': CHAR.pad_index if CHAR is not None else None,
            'n_lemmas': len(LEMMA.vocab) if LEMMA is not None else None,
            'bert_pad_index': BERT.pad_index if BERT is not None else None,
            'pad_index': WORD.pad_index,
            'unk_index': WORD.unk_index,
            'interpolation': args.itp
        })
        logger.info(f"{transform}")

        logger.info("Building the model")
        model = cls.MODEL(**args).load_pretrained(WORD.embed).to(args.device)
        logger.info(f"{model}\n")

        optimizer = Adam(model.parameters(), **optimizer_args)
        scheduler = ExponentialLR(optimizer, **scheduler_args)

        return cls(args, model, transform, optimizer, scheduler)
