#The majority of this file is code from https://github.com/IwasakiYuuki/Bert-abstractive-text-summarization/blob/master/train.py

import argparse
import math
import time

from tqdm import tqdm
import torch
import torch.nn.functional as F
import torch.optim as optim #i should also probably import our implementation of adam optimizer
import torch.utils.data
import transformer.Constants as Constants
from dataloader import TextSummarizationDataset, paired_collate_fn
from transformer.Optim import ScheduledOptim
import tensorboardX as tbx
from bert import BertModel, Transformer
from multitask_classifer import MultiTaskBERT


def cal_performance(pred, x, gold, smoothing=False):
    ''' Apply label smoothing if needed '''

    loss = cal_loss(pred, x, gold, smoothing)

    pred = pred.max(1)[1]
    gold = gold.contiguous().view(-1)
    non_pad_mask = gold.ne(Constants.PAD)
    n_correct = pred.eq(gold)
    n_correct = n_correct.masked_select(non_pad_mask).sum().item()

    return loss, n_correct


def cal_loss(pred, x, gold, smoothing):
    ''' Calculate cross entropy loss, apply label smoothing if needed. '''

    gold = gold.contiguous().view(-1)

    if smoothing:
        eps = 0.1
        n_class = pred.size(1)

        one_hot = torch.zeros_like(pred).scatter(1, gold.view(-1, 1), 1)
        one_hot = one_hot * (1 - eps) + (1 - one_hot) * eps / (n_class - 1)
        #        x = x.repeat(1, int(a.size(0)/x.size(0))).view(-1, a.size(1))
        #        prb = (1-p_gen)*F.softmax(pred, dim=1)
        #        a = p_gen*F.softmax(a, dim=1)
        prb = F.softmax(pred, dim=1)
        #        prb = prb.scatter_add(1, x, a)
        log_prb = torch.log(prb)
        non_pad_mask = gold.ne(Constants.PAD)
        loss = -(one_hot * log_prb).sum(dim=1)
        loss = loss.masked_select(non_pad_mask).sum()  # average later
    else:
        loss = F.cross_entropy(pred, gold, ignore_index=Constants.PAD, reduction='sum')

    return loss


def train_epoch(model, training_data, optimizer, device, smoothing, step):
    ''' Epoch operation in training phase'''

    model.train()

    total_loss = 0
    n_word_total = 0
    n_word_correct = 0

    for i, batch in enumerate(tqdm(
            training_data, mininterval=2,
            desc='  - (Training)   ', leave=False, ascii=True)):

        # prepare data
        src_seq, src_pos, tgt_seq, tgt_pos = map(lambda x: x.to(device), batch)
        gold = tgt_seq[:, 1:]

        # forward
        optimizer.zero_grad()
        #        pred, a, p_gen = model(src_seq, src_pos, tgt_seq, tgt_pos)
        pred = model(src_seq, src_pos, tgt_seq, tgt_pos)

        # backward
        loss, n_correct = cal_performance(pred, src_seq, gold, smoothing=smoothing)
        loss.backward()

        # update parameters
        optimizer.step_and_update_lr()

        # note keeping
        total_loss += loss.item()

        non_pad_mask = gold.ne(Constants.PAD)
        n_word = non_pad_mask.sum().item()
        n_word_total += n_word
        n_word_correct += n_correct
        writer.add_scalars('data/train_loss', {'train_loss_each_batch': loss.item() / n_word}, i+step)
        writer.add_scalars('data/train_accu', {'train_accu_each_batch': n_correct / n_word}, i+step)

    loss_per_word = total_loss/n_word_total
    accuracy = n_word_correct/n_word_total
    return loss_per_word, accuracy, step + i


def eval_epoch(model, validation_data, device, step):
    ''' Epoch operation in evaluation phase '''

    model.eval()

    total_loss = 0
    n_word_total = 0
    n_word_correct = 0

    with torch.no_grad():
        for i, batch in enumerate(tqdm(
                validation_data, mininterval=2,
                desc='  - (Validation) ', leave=False, ascii=True)):

            # prepare data
            src_seq, src_pos, tgt_seq, tgt_pos = map(lambda x: x.to(device), batch)
            gold = tgt_seq[:, 1:]

            # forward
            pred = model(src_seq, src_pos, tgt_seq, tgt_pos)
            loss, n_correct = cal_performance(pred, src_seq, gold, smoothing=False)

            # note keeping
            total_loss += loss.item()

            non_pad_mask = gold.ne(Constants.PAD)
            n_word = non_pad_mask.sum().item()
            n_word_total += n_word
            n_word_correct += n_correct
            writer.add_scalars('data/valid_loss', {'valid_loss_each_batch': loss.item() / n_word}, i+step)
            writer.add_scalars('data/valid_accu', {'valid_accu_each_batch': n_correct / n_word}, i+step)

    loss_per_word = total_loss/n_word_total
    accuracy = n_word_correct/n_word_total
    return loss_per_word, accuracy, step + i


def train(model, training_data, validation_data, optimizer, device, opt):
    ''' Start training '''

    log_train_file = None
    log_valid_file = None

    if opt.log:
        log_train_file = opt.log + '.train.log'
        log_valid_file = opt.log + '.valid.log'

        print('[Info] Training performance will be written to file: {} and {}'.format(
            log_train_file, log_valid_file))

        with open(log_train_file, 'w') as log_tf, open(log_valid_file, 'w') as log_vf:
            log_tf.write('epoch,loss,ppl,accuracy\n')
            log_vf.write('epoch,loss,ppl,accuracy\n')

    valid_accus = []
    train_step = 0
    valid_step = 0
    for epoch_i in range(opt.epoch):
        print('[ Epoch', epoch_i, ']')

        start = time.time()
        train_loss, train_accu, train_step = train_epoch(
            model, training_data, optimizer, device, opt.label_smoothing, train_step)
        print('  - (Training)   ppl: {ppl: 8.5f}, accuracy: {accu:3.3f} %, ' \
              'elapse: {elapse:3.3f} min'.format(
            ppl=math.exp(min(train_loss, 100)), accu=100*train_accu,
            elapse=(time.time()-start)/60))

        start = time.time()
        valid_loss, valid_accu, valid_step = eval_epoch(model, validation_data, device, valid_step)
        print('  - (Validation) ppl: {ppl: 8.5f}, accuracy: {accu:3.3f} %, ' \
              'elapse: {elapse:3.3f} min'.format(
            ppl=math.exp(min(valid_loss, 100)), accu=100*valid_accu,
            elapse=(time.time()-start)/60))

        valid_accus += [valid_accu]

        model_state_dict = model.state_dict()
        checkpoint = {
            'model': model_state_dict,
            'settings': opt,
            'epoch': epoch_i}

        if opt.save_model:
            if opt.save_mode == 'all':
                model_name = 'data/checkpoint/trained/' + opt.save_model + '_accu_{accu:3.3f}.chkpt'.format(accu=100*valid_accu)
                torch.save(checkpoint, model_name)
            elif opt.save_mode == 'best':
                model_name = 'data/checkpoint/trained/' + opt.save_model + '.chkpt'
                if valid_accu >= max(valid_accus):
                    torch.save(checkpoint, model_name)
                    print('    - [Info] The checkpoint file has been updated.')

        if log_train_file and log_valid_file:
            with open(log_train_file, 'a') as log_tf, open(log_valid_file, 'a') as log_vf:
                log_tf.write('{epoch},{loss: 8.5f},{ppl: 8.5f},{accu:3.3f}\n'.format(
                    epoch=epoch_i, loss=train_loss,
                    ppl=math.exp(min(train_loss, 100)), accu=100*train_accu))
                log_vf.write('{epoch},{loss: 8.5f},{ppl: 8.5f},{accu:3.3f}\n'.format(
                    epoch=epoch_i, loss=valid_loss,
                    ppl=math.exp(min(valid_loss, 100)), accu=100*valid_accu))

def main():
    ''' Main function '''
    parser = argparse.ArgumentParser()

    parser.add_argument('-data', required=True)
    parser.add_argument('-bert_path', required=True)

    parser.add_argument('-epoch', type=int, default=10)
    parser.add_argument('-batch_size', type=int, default=64)

    parser.add_argument('-d_model', type=int, default=768)
    parser.add_argument('-d_inner_hid', type=int, default=3072)
    parser.add_argument('-d_k', type=int, default=64)
    parser.add_argument('-d_v', type=int, default=64)

    parser.add_argument('-n_head', type=int, default=12)
    parser.add_argument('-n_layers', type=int, default=8)
    parser.add_argument('-n_warmup_steps', type=int, default=4000)

    parser.add_argument('-dropout', type=float, default=0.1)
    parser.add_argument('-embs_share_weight', action='store_true')
    parser.add_argument('-proj_share_weight', action='store_true')

    parser.add_argument('-log', default=None)
    parser.add_argument('-save_model', default=None)
    parser.add_argument('-save_mode', type=str, choices=['all', 'best'], default='best')

    parser.add_argument('-no_cuda', action='store_true')
    parser.add_argument('-label_smoothing', action='store_true')

    opt = parser.parse_args()
    opt.cuda = not opt.no_cuda
    opt.d_word_vec = opt.d_model

    #========= Loading Dataset =========#
    data = torch.load(opt.data)
    opt.max_token_seq_len = data['settings'].max_token_seq_len

    training_data, validation_data = prepare_dataloaders(data, opt)

    opt.src_vocab_size = training_data.dataset.src_vocab_size
    opt.tgt_vocab_size = training_data.dataset.tgt_vocab_size

    #========= Preparing Model =========#
    if opt.embs_share_weight:
        assert training_data.dataset.src_word2idx == training_data.dataset.tgt_word2idx, \
            'The src/tgt word2idx table are different but asked to share word embedding.'

    print(opt)

    device = torch.device('cuda' if opt.cuda else 'cpu')
    real_model = Transformer
    model = MultiTaskBERT.abs_summarization()

        ##figure out what the stuff here should be for BertModel
        """"
        opt.bert_path,
        opt.tgt_vocab_size,
        opt.max_token_seq_len,
        d_k=opt.d_k,
        d_v=opt.d_v,
        d_model=opt.d_model,
        d_word_vec=opt.d_word_vec,
        d_inner=opt.d_inner_hid,
        n_layers=opt.n_layers,
        n_head=opt.n_head,
        dropout=opt.dropout).to(device)
        " "" "

    optimizer = ScheduledOptim(
        optim.Adam(
            filter(lambda x: x.requires_grad, real_model.parameters()),
            betas=(0.9, 0.999), eps=1e-09),
        opt.d_model, opt.n_warmup_steps)
    """"

    train(model, training_data, validation_data, optimizer, device ,opt)


def prepare_dataloaders(data, opt):
# ========= Preparing DataLoader =========#
train_loader = torch.utils.data.DataLoader(
    TextSummarizationDataset(
        src_word2idx=data['dict']['src'],
        tgt_word2idx=data['dict']['tgt'],
        src_insts=data['train']['src'],
        tgt_insts=data['train']['tgt']),
    num_workers=2,
    batch_size=opt.batch_size,
    collate_fn=paired_collate_fn,
    shuffle=True)

valid_loader = torch.utils.data.DataLoader(
    TextSummarizationDataset(
        src_word2idx=data['dict']['src'],
        tgt_word2idx=data['dict']['tgt'],
        src_insts=data['valid']['src'],
        tgt_insts=data['valid']['tgt']),
    num_workers=2,
    batch_size=opt.batch_size,
    collate_fn=paired_collate_fn)
return train_loader, valid_loader


if __name__ == '__main__':
writer = tbx.SummaryWriter(log_dir='data/tensorboardx/runs')
main()
writer.export_scalars_to_json("data/all_scalars.json")
writer.close()