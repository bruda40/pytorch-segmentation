#
# Note -- this training script is tweaked from the original version at:
#
#           https://github.com/pytorch/vision/tree/v0.3.0/references/segmentation
#
#
import argparse
import datetime
import time
import math
import os
import shutil
import sys

import torch
import torch.utils.data
from torch import nn
import torchvision
from models import segmentation

#from datasets.coco_utils import get_coco
from datasets.cityscapes_utils import get_cityscapes
#from datasets.deepscene import DeepSceneSegmentation
from datasets.custom_dataset import CustomSegmentation
#from datasets.mhp import MHPSegmentation
#from datasets.nyu import NYUDepth
#from datasets.sun import SunRGBDSegmentation

import transforms as T
import utils

model_names = sorted(name for name in segmentation.__dict__
                     if name.islower() and not name.startswith("__")
                     and callable(segmentation.__dict__[name]))


#
# parse command-line arguments
#
def parse_args():
    parser = argparse.ArgumentParser(description='PyTorch Segmentation Training')

    parser.add_argument('data', metavar='DIR', help='path to dataset')
    parser.add_argument('--dataset', default='voc',
                        help='dataset type: voc, voc_aug, coco, cityscapes, deepscene, mhp, nyu, sun, custom (default: voc)')
    parser.add_argument('-a', '--arch', metavar='ARCH', default='fcn_resnet18',
                        choices=model_names,
                        help='model architecture: ' +
                             ' | '.join(model_names) +
                             ' (default: fcn_resnet18)')
    parser.add_argument('--classes', default=21, type=int, metavar='C',
                        help='number of classes in your dataset (outputs)')
    parser.add_argument('--aux-loss', action='store_true', help='train with auxilliary loss')
    parser.add_argument('--resolution', default=320, type=int, metavar='N',
                        help='NxN resolution used for scaling the training dataset (default: 320x320) '
                             'to specify a non-square resolution, use the --width and --height options')
    parser.add_argument('--width', default=argparse.SUPPRESS, type=int, metavar='X',
                        help='desired width of the training dataset. if this option is not set, --resolution will be used')
    parser.add_argument('--height', default=argparse.SUPPRESS, type=int, metavar='Y',
                        help='desired height of the training dataset. if this option is not set, --resolution will be used')
    parser.add_argument('--device', default='cuda', help='device')
    parser.add_argument('-b', '--batch-size', default=10, type=int)
    parser.add_argument('--epochs', default=30, type=int, metavar='N', help='number of total epochs to run')
    parser.add_argument('-j', '--workers', default=16, type=int, metavar='N',
                        help='number of data loading workers (default: 16)')
    parser.add_argument('--lr', default=0.01, type=float, help='initial learning rate')
    parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                        help='momentum')
    parser.add_argument('--wd', '--weight-decay', default=1e-4, type=float,
                        metavar='W', help='weight decay (default: 1e-4)',
                        dest='weight_decay')
    parser.add_argument('--print-freq', default=10, type=int, help='print frequency')
    parser.add_argument('--model-dir', default='.', help='path where to save output models')
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument("--test-only", dest="test_only", help="Only test the model", action="store_true")
    parser.add_argument("--pretrained", dest="pretrained",
                        help="Use pre-trained models (only supported for fcn_resnet101)", action="store_true")

    # distributed training parameters
    parser.add_argument('--world-size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist-url', default='env://', help='url used to set up distributed training')
    parser.add_argument('--lock', default=0, type=int, help='0: unlocks backbone layers, 1:freeze backbone layers, 2: (fine-tuning) freezes backone except last layer')
    parser.add_argument('--clear-classifier', default=0, type=int, help='0: keeps classifier, 1: replace classifier ')

    args = parser.parse_args()
    return args


#
# load desired dataset
#
def get_dataset(name, path, image_set, transform, num_classes):
    def sbd(*args, **kwargs):
        return torchvision.datasets.SBDataset(*args, mode='segmentation', **kwargs)

    paths = {
        #"voc": (path, torchvision.datasets.VOCSegmentation, num_classes),
        #"voc_aug": (path, sbd, num_classes),
        #"coco": (path, get_coco, num_classes),
        "cityscapes": (path, get_cityscapes, num_classes),
        #"deepscene": (path, DeepSceneSegmentation, 5),
        #"mhp": (path, MHPSegmentation, num_classes),
        #"nyu": (path, NYUDepth, num_classes),
        #"sun": (path, SunRGBDSegmentation, num_classes),
        "custom": (path, CustomSegmentation, num_classes)
    }
    p, ds_fn, num_classes = paths[name]

    ds = ds_fn(p, image_set=image_set, transforms=transform)
    return ds, num_classes


#
# create data transform
#
def get_transform(train, resolution):
    transforms = []

    # if square resolution, perform some aspect cropping
    # otherwise, resize to the resolution as specified
    if resolution[0] == resolution[1]:
        base_size = resolution[0] + 32  # 520
        crop_size = resolution[0]  # 480

        min_size = int((0.5 if train else 1.0) * base_size)
        max_size = int((2.0 if train else 1.0) * base_size)

        transforms.append(T.RandomResize(min_size, max_size))

        # during training mode, perform some data randomization
        if train:
            transforms.append(T.RandomHorizontalFlip(0.5))
            transforms.append(T.RandomCrop(crop_size))
    else:
        transforms.append(T.Resize(resolution))

        if train:
            transforms.append(T.RandomHorizontalFlip(0.5))

    transforms.append(T.ToTensor())
    transforms.append(T.Normalize(mean=[0.485, 0.456, 0.406],
                                  std=[0.229, 0.224, 0.225]))

    return T.Compose(transforms)


#
# define the loss functions
#
def criterion(inputs, target):
    losses = {}
    for name, x in inputs.items():
        losses[name] = nn.functional.cross_entropy(x, target, ignore_index=255)

    if len(losses) == 1:
        return losses['out']

    return losses['out'] + 0.5 * losses['aux']


#
# evaluate model IoU (intersection over union)
#
def evaluate(model, data_loader, device, num_classes):
    model.eval()
    confmat = utils.ConfusionMatrix(num_classes)
    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Test:'
    with torch.no_grad():
        for image, target in metric_logger.log_every(data_loader, 100, header):
            image, target = image.to(device), target.to(device)
            output = model(image)
            output = output['out']

            confmat.update(target.flatten(), output.argmax(1).flatten())

        confmat.reduce_from_all_processes()

    return confmat


#
# train for one epoch over the dataset
#
def train_one_epoch(model, criterion, optimizer, data_loader, lr_scheduler, device, epoch, print_freq):
    model.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value}'))
    header = 'Epoch: [{}]'.format(epoch)
    for image, target in metric_logger.log_every(data_loader, print_freq, header):
        image, target = image.to(device), target.to(device)
        output = model(image)
        loss = criterion(output, target)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        lr_scheduler.step()

        metric_logger.update(loss=loss.item(), lr=optimizer.param_groups[0]["lr"])


def replace_relu_to_leackyRelu(model, classifier):

    for child_name, child in model.named_children():
        if child_name == 'classifier':
           classifier = 1

        if isinstance(child, nn.ReLU):
            if classifier == 1:
                setattr(model, child_name, nn.LeakyReLU(0.1))
        else:
            # recurse
            replace_relu_to_leackyRelu(child, classifier)


def locking_layers(model, lock):

    backbone_locked_layers = 0
    # classifier_layer_ct = 0
    backbone_layer_ct = 0
    if lock == 1:
        backbone_locked_layers = 60
    elif lock == 2:
        backbone_locked_layers = 45

    for name, child in model.named_children():
        if name == 'backbone':
            for layer_name, layer in child.named_parameters():
                if backbone_layer_ct < backbone_locked_layers:
                    layer.requires_grad = False
                backbone_layer_ct += 1

        # if name == 'classifier':
        #     for classifier_name, params in child.named_parameters():
        #         if classifier_layer_ct < 15:
        #             params.requires_grad = True
        #         classifier_layer_ct += 1

    print(model)
    print('______ Overview lockstatus of layers ______')
    for name, child in model.named_children():
        for name2, params in child.named_parameters():
            print(name, name2, params.requires_grad)


#
# main training function
#
def main(args):
    if args.model_dir:
        utils.mkdir(args.model_dir)

    utils.init_distributed_mode(args)
    print(args)

    device = torch.device(args.device)

    # determine the desired resolution
    resolution = (args.resolution, args.resolution)

    if "width" in args and "height" in args:
        resolution = (args.height, args.width)

        # load the train and val datasets
    dataset, num_classes = get_dataset(args.dataset, args.data, "train",
                                       get_transform(train=True, resolution=resolution), args.classes)
    dataset_test, _ = get_dataset(args.dataset, args.data, "val", get_transform(train=False, resolution=resolution),
                                  args.classes)

    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(dataset)
        test_sampler = torch.utils.data.distributed.DistributedSampler(dataset_test)
    else:
        train_sampler = torch.utils.data.RandomSampler(dataset)
        test_sampler = torch.utils.data.SequentialSampler(dataset_test)

    data_loader = torch.utils.data.DataLoader(
        dataset, batch_size=args.batch_size,
        sampler=train_sampler, num_workers=args.workers,
        collate_fn=utils.collate_fn, drop_last=True)

    data_loader_test = torch.utils.data.DataLoader(
        dataset_test, batch_size=1,
        sampler=test_sampler, num_workers=args.workers,
        collate_fn=utils.collate_fn)

    print(
        "=> training with dataset: '{:s}' (train={:d}, val={:d})".format(args.dataset, len(dataset), len(dataset_test)))
    print("=> training with resolution: {:d}x{:d}, {:d} classes".format(resolution[1], resolution[0], num_classes))
    print("=> training with model: {:s}".format(args.arch))

    # create the segmentation model
    model = segmentation.__dict__[args.arch](num_classes=num_classes,
                                             aux_loss=args.aux_loss,
                                             pretrained=args.pretrained)

    model.to(device)
    print(model)
    if args.distributed:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)

    if args.resume:
        checkpoint = torch.load(args.resume, map_location='cpu')

        model_od = checkpoint['model']

        # delete classifier weights
        if args.clear_classifier:
            model_od_copy = model_od.copy()
            for layer_name, layer in model_od_copy.items():
                if layer_name.startswith('classifier'):
                    model_od.pop(layer_name)

        model.load_state_dict(checkpoint['model'], strict=False)

    # locking parts of network
    if args.lock:
        locking_layers(model, args.lock)

    model_without_ddp = model

    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        model_without_ddp = model.module

    # eval-only mode
    if args.test_only:
        confmat = evaluate(model, data_loader_test, device=device, num_classes=num_classes)
        print(confmat)
        return

    # create the optimizer
    params_to_optimize = [
        {"params": [p for p in model_without_ddp.backbone.parameters() if p.requires_grad]},
        {"params": [p for p in model_without_ddp.classifier.parameters() if p.requires_grad]},
    ]

    if args.aux_loss:
        params = [p for p in model_without_ddp.aux_classifier.parameters() if p.requires_grad]
        params_to_optimize.append({"params": params, "lr": args.lr * 10})

    optimizer = torch.optim.SGD(
        params_to_optimize,
        lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)

    lr_scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer,
        lambda x: (1 - x / (len(data_loader) * args.epochs)) ** 0.9)

    # training loop
    start_time = time.time()
    best_IoU = 0.0

    for epoch in range(args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)

        # train the model over the next epoc
        train_one_epoch(model, criterion, optimizer, data_loader, lr_scheduler, device, epoch, args.print_freq)

        # test the model on the val dataset
        confmat = evaluate(model, data_loader_test, device=device, num_classes=num_classes)
        print(confmat)

        # save model checkpoint
        checkpoint_path = os.path.join(args.model_dir, 'model_{}.pth'.format(epoch))

        utils.save_on_master(
            {
                'model': model_without_ddp.state_dict(),
                'optimizer': optimizer.state_dict(),
                'epoch': epoch,
                'args': args,
                'arch': args.arch,
                'dataset': args.dataset,
                'num_classes': num_classes,
                'resolution': resolution,
                'accuracy': confmat.acc_global,
                'mean_IoU': confmat.mean_IoU
            },
            checkpoint_path)

        print(
            'saved checkpoint to:  {:s}  ({:.3f}% mean IoU, {:.3f}% accuracy)'.format(checkpoint_path, confmat.mean_IoU,
                                                                                      confmat.acc_global))

        if confmat.mean_IoU > best_IoU:
            best_IoU = confmat.mean_IoU
            best_path = os.path.join(args.model_dir, 'model_best.pth')
            shutil.copyfile(checkpoint_path, best_path)
            
            if os.path.isfile(checkpoint_path):
                os.remove(checkpoint_path)
            else:  ## Show an error ##
                print("Error: %s file not found" % checkpoint_path)            
            
            print('saved best model to:  {:s}  ({:.3f}% mean IoU, {:.3f}% accuracy)'.format(best_path, best_IoU,
                                                                                            confmat.acc_global))
        else:
            os.remove(checkpoint_path)

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


def redirect_stdout(file_path):
    sys.stdout = open(file_path, "w")


if __name__ == "__main__":
    print('Start')
    output_path = 'output.txt'
    redirect_stdout(output_path)
    args = parse_args()
    main(args)
