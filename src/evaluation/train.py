import os
import sys
import numpy as np
import torch
import json
import codecs
import yaml
import logging
import torch.nn as nn
import torch.utils
import torch.backends.cudnn as cudnn
from torch.optim.lr_scheduler import CosineAnnealingLR

sys.path.insert(0, '../RobustDARTS')

from src import utils
from src.evaluation.model import Network
from src.evaluation.args import Helper

TORCH_VERSION = torch.__version__


helper = Helper()
args = helper.config

log_format = '%(asctime)s %(message)s'
logging.basicConfig(stream=sys.stdout, level=logging.INFO,
    format=log_format, datefmt='%m/%d %I:%M:%S %p')
fh = logging.FileHandler(os.path.join(args.save,
                                      'log_{}_{}.txt'.format(args.search_task_id, args.task_id)))
fh.setFormatter(logging.Formatter(log_format))
logging.getLogger().addHandler(fh)

device = torch.device('cuda:{}'.format(args.gpu) if torch.cuda.is_available() else 'cpu')


def main():
  np.random.seed(args.seed)
  cudnn.benchmark = True
  torch.manual_seed(args.seed)
  cudnn.enabled = True
  torch.cuda.manual_seed(args.seed)
  logging.info('gpu device = %d' % args.gpu)
  logging.info("args = %s", args)

  # load search configuration file holding the found architectures
  configuration = '_'.join([args.space, args.dataset])
  settings = '_'.join([str(args.search_dp), str(args.search_wd)])
  with open(args.archs_config_file, 'r') as f:
    cfg = yaml.load(f)
    arch = dict(cfg)[configuration][settings][args.search_task_id]

  print(arch)
  genotype = eval(arch)
  model = Network(args.init_channels, args.n_classes, args.layers, args.auxiliary, genotype).to(device)

  if args.model_path is not None:
    utils.load(model, args.model_path, genotype)

  logging.info("param size = %fMB", utils.count_parameters_in_MB(model))

  criterion = nn.CrossEntropyLoss().to(device)

  optimizer = torch.optim.SGD(
      model.parameters(),
      args.learning_rate,
      momentum=args.momentum,
      weight_decay=args.weight_decay
      )

  scheduler = CosineAnnealingLR(
      optimizer, float(args.epochs))

  train_queue, valid_queue, _, _ = helper.get_train_val_loaders()

  errors_dict = {'train_acc': [], 'train_loss': [], 'valid_acc': [],
                 'valid_loss': []}

  for epoch in range(args.epochs):
    scheduler.step()
    logging.info('epoch %d lr %e', epoch, scheduler.get_lr()[0])
    model.drop_path_prob = args.drop_path_prob * epoch / args.epochs

    # training
    train_acc, train_obj = train(train_queue, model, criterion, optimizer)
    logging.info('train_acc %f', train_acc)

    # evaluation
    valid_acc, valid_obj = infer(valid_queue, model, criterion)
    logging.info('valid_acc %f', valid_acc)

    # update the errors dictionary
    errors_dict['train_acc'].append(100 - train_acc)
    errors_dict['train_loss'].append(train_obj)
    errors_dict['valid_acc'].append(100 - valid_acc)
    errors_dict['valid_loss'].append(valid_obj)

  with codecs.open(os.path.join(args.save,
                                'errors_{}_{}.json'.format(args.search_task_id, args.task_id)),
                   'w', encoding='utf-8') as file:
    json.dump(errors_dict, file, separators=(',', ':'))

  utils.write_yaml_results_eval(args, args.results_test, 100-valid_acc)


def train(train_queue, model, criterion, optimizer):
  objs = utils.AvgrageMeter()
  top1 = utils.AvgrageMeter()
  top5 = utils.AvgrageMeter()
  model.train()

  for step, (input_tensor, target) in enumerate(train_queue):
    input_tensor = input_tensor.to(device)
    target = target.to(device)

    optimizer.zero_grad()
    logits, logits_aux = model(input_tensor)
    loss = criterion(logits, target)
    if args.auxiliary:
      loss_aux = criterion(logits_aux, target)
      loss += args.auxiliary_weight*loss_aux
    loss.backward()

    nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)
    optimizer.step()

    prec1, prec5 = utils.accuracy(logits, target, topk=(1, 5))
    n = input_tensor.size(0)
    objs.update(loss.item(), n)
    top1.update(prec1.item(), n)
    top5.update(prec5.item(), n)

    if step % args.report_freq == 0:
      logging.info('train %03d %e %f %f', step, objs.avg, top1.avg, top5.avg)
      if args.debug:
        break

  return top1.avg, objs.avg


def infer(valid_queue, model, criterion):
  objs = utils.AvgrageMeter()
  top1 = utils.AvgrageMeter()
  top5 = utils.AvgrageMeter()
  model.eval()

  with torch.no_grad():
    for step, (input_tensor, target) in enumerate(valid_queue):
      input_tensor = input_tensor.to(device)
      target = target.to(device)

      logits, _ = model(input_tensor)
      loss = criterion(logits, target)

      prec1, prec5 = utils.accuracy(logits, target, topk=(1, 5))
      n = input_tensor.size(0)
      objs.update(loss.item(), n)
      top1.update(prec1.item(), n)
      top5.update(prec5.item(), n)

      if step % args.report_freq == 0:
        logging.info('valid %03d %e %f %f', step, objs.avg, top1.avg, top5.avg)
        if args.debug:
          break
  return top1.avg, objs.avg


if __name__ == '__main__':
  main()

