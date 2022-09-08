# Fast and Accurate End-to-End Span-based Semantic Role Labeling as Word-based Graph Parsing
This is the repo for SRLasSDGP, a novel approach to form span-based SRL as word-based graph parsing, to be presented at [COLING2022](https://coling2022.org/coling). A preprint of the paper can be found at the [following location on arxiv](https://arxiv.org/abs/2112.02970).

## Abstract
This paper proposes to cast end-to-end span-based SRL as a  word-based graph parsing task. 
The major challenge is how to represent spans at the word level. 
Borrowing ideas from research on Chinese word  segmentation and named entity recognition, 
we propose and compare four different schemata of graph representation, i.e., BES, BE, BIES, and BII,
among which we find that the BES schema performs the best. 
We further gain interesting insights through detailed analysis. 
Moreover, we propose a simple constrained Viterbi procedure to ensure the legality of the output  graph in the sense of SRL. 
We conduct experiments on two widely used benchmark datasets, i.e., CoNLL05 and CoNLL12. 
Results show that our word-based graph parsing approach achieves new state-of-the-art (SOTA) performance under the end-to-end setting, both without and with pre-trained language models (PLMs). 
More importantly, our model can parse 669/252 sentences per second, without and with PLMs respectively. Under the simpler predicate-given setting, our approach, after certain model modification,  achieves comparable performance with previous SOTA.  

## Installation
```shell
pip install -r  requirements.txt
```

## Preprocess data
Under construction.

## Training and Prediction
### Training models without pre-trained language models
```
python -m supar.cmds.vi_SRL train -b \
        --train data/BES/conll05/BES-train.conllu \
        --dev data/BES/conll05/BES-dev.conllu \
        --test data/BES/conll05/BES-wsj.conllu \
        --batch-size 3000 \
        --n-embed 100 \
        --feat lemma,char \
        --itp 0.06 \
        --embed data/glove.6B.300d.txt \
        --n_pretrained_embed 300 \
        --min_freq 7 \
        --split \
        --seed 1 \
        --schema BES \
        -p exp/exp1/model \
        -d 5
```
* You can add the "--train_given_prd" in the script to train a predicate-given model.
* "--schema" to specify which schema to use.
* "-d" to specify which gpu to use.
* Detailed description about args can be found in "supar/cmds/cmd.py" and "supar/cmds/vi_SRL.py".

### Prediction and Evaluation
```
python -m supar.cmds.vi_SRL predict --data data/BES/conll05/BES-wsj.conllu \
        -p exp/exp1/model \
        --pred BES-wsj.pred \
        --gold data/wsj.final \
        --task 05 \
        --schema BES \
        --vtb \
        -d 4
```
* "--vtb" means whether to use constrained viterbi.

### Training models with pre-trained language models
```shell
# switch to the finetune-given_prd
git checkout finetune-given_prd
# training
python -m supar.cmds.vi_srl train -b \
        --train data/BES/conll05/BES-train.conllu \
        --dev data/BES/conll05/BES-dev.conllu \
        --test data/BES/conll05/BES-wsj.conllu \
        --batch-size 500 \
        --encoder bert \
        --itp 0.06 \
        --bert bert-large-uncased \
        --split \
        --lr_rate 1 \
        --seed 1 \
        --train_given_prd \
        -p exp/exp2/model \
        -d 4 

# prediction and evaluation
python -m supar.cmds.vi_srl predict --data data/BES/conll05/BES-wsj.conllu \
        -p exp/exp2/model \
        --pred BES-wsj-bert.pred \
        --gold data/wsj.final \
        --task 05 \
        --schema BES \
        --vtb \
        -d 4

```
* use "--bert" to specify which language model, currently support [bert](https://huggingface.co/bert-large-uncased), [roberta](https://huggingface.co/hfl/chinese-roberta-wwm-ext-large), [xlnet](https://huggingface.co/xlnet-large-cased).
* You can delete the "--train_given_prd" in the script to train a end-to-end model.

## Trained Models
* Here, we currently provide two model trained on conll2005 with bert in BES schema, i.e., [end-to-end model](https://drive.google.com/drive/folders/1DYF0DNhgrZKJUae2lNljU2v3xEO7N00R?usp=sharing), [predicate-given model](https://drive.google.com/drive/folders/1M7ukkIIj80UlALP7hoiWVfkSVBBXOeiD?usp=sharing).
