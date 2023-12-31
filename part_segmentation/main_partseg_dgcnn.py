import argparse
import os, sys

parser = argparse.ArgumentParser(description='Point Cloud Part Segmentation using DGCNN backbone')
parser.add_argument('--rot_type', type=str, default='custom')
parser.add_argument('--activation', type=int, default=0)
parser.add_argument('--seed', type=int, default=1)
parser.add_argument('--rot_order', type=str, default='1-2')
parser.add_argument('--psi_scale_type', type=int, default=3)
parser.add_argument('--negative_slope', type=float, default=0.2)
parser.add_argument('--model', type=str, default='evnet', metavar='N',
                    choices=['original', 'vn', 'evnet'])
parser.add_argument('--binary', action='store_true', 
                    help='build binary nn')
parser.add_argument('--batch-size', type=int, default=32, metavar='batch_size',
                    help='Size of batch)')
parser.add_argument('--epochs', type=int, default=200, metavar='N',
                    help='number of episode to train ')
parser.add_argument('--lr', type=float, default=0.001, metavar='LR',
                    help='learning rate (default: 0.001, 0.1 if using sgd)')
parser.add_argument('--momentum', type=float, default=0.9, metavar='M',
                    help='SGD momentum (default: 0.9)')
parser.add_argument('--wd', type=float, default=1e-4, metavar='WD',
                    help='weight decay')
parser.add_argument('--num-points', type=int, default=2048,
                    help='num of points to use')
parser.add_argument('--dropout', type=float, default=0.5,
                    help='dropout rate')
parser.add_argument('--k', type=int, default=40, metavar='N',
                    help='Num of nearest neighbors to use')
parser.add_argument('--rot', type=str, default='z', metavar='N',
                    choices=['aligned', 'z', 'so3'],
                    help='Rotation augmentation to input data')
parser.add_argument('--rot-test', type=str, default='so3', metavar='N',
                    choices=['aligned', 'z', 'so3'],
                    help='Rotation augmentation to input data during testing')
parser.add_argument('--smoothing', action='store_true', 
                    help='using smoothing during training')
parser.add_argument('--test', metavar='PATH', default=None,
                    help='evaluate a trained model')
parser.add_argument('--data-dir', metavar='DATADIR', type=str, default=os.path.join(os.path.dirname(__file__),'data'),
                    help='data dir to load datasets')
parser.add_argument('--save-dir', metavar='SAVEDIR', type=str, default=os.path.join(os.path.dirname(__file__),'results'),
                    help='dir to save logs and model checkpoints')
args = parser.parse_args()

import time
import numpy as np
import sklearn.metrics as metrics
import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data import DataLoader
import models.utils.rotm_util as rmutil

from data import ShapeNetPart
import models
import utils
from utils import cal_loss, calculate_shape_IoU

try:
    import vessl
    vessl_on = True
    vessl.init()
except:
    vessl_on = False

# args.seed = int(time.time())
torch.manual_seed(args.seed)
torch.cuda.manual_seed_all(args.seed)
np.random.seed(args.seed)

log_string = utils.configure_logging(args.save_dir, 'pseg')
epoch_string = utils.configure_logging(args.save_dir, 'pseg', 'log')

def main():
    args.use_sgd =  False if args.binary else True
    epoch_string(str(args))

    #Try to load models
    if args.model == 'dgcnn':
        model = models.DGCNN_partseg(args, 50)
    elif args.model == 'vn':
        model = models.VN_DGCNN_PSEG(args, 50)
    elif args.model == 'evnet':
        model = models.EV_DGCNN_PSEG(args, 50)
    else:
        raise Exception("Not implemented")

    train_dataset = ShapeNetPart(data_dir=args.data_dir, partition='trainval', num_points=args.num_points)
    if (len(train_dataset) < 100):
        drop_last = False
    else:
        drop_last = True
    train_loader = DataLoader(train_dataset, num_workers=8, batch_size=args.batch_size, shuffle=True, drop_last=drop_last)
    test_loader = DataLoader(ShapeNetPart(data_dir=args.data_dir, partition='test', num_points=args.num_points), num_workers=8, batch_size=args.batch_size, shuffle=True, drop_last=False)
    seg_num_all = train_loader.dataset.seg_num_all
    seg_start_index = train_loader.dataset.seg_start_index
    log_string(f'trainloader: {len(train_loader.dataset)}, test_loader: {len(test_loader.dataset)}')

    device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
    model.to(device)
    # model = nn.DataParallel(model.to(device))
    # log_string("Let's use {} GPUs!".format(torch.cuda.device_count()))

    if args.use_sgd:
        log_string("Use SGD")
        optimizer = optim.SGD(model.parameters(), lr=args.lr*100, momentum=args.momentum, weight_decay=args.wd)
    else:
        log_string("Use Adam")
        optimizer = optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.wd)

    scheduler = CosineAnnealingLR(optimizer, args.epochs, eta_min=args.lr if args.use_sgd else 0)
    criterion = cal_loss

    start_epoch = 0
    best_test_iou = 0

    saveID = None
    print_freq = len(train_loader) // 10
    for epoch in range(start_epoch, args.epochs):
        lr = optimizer.param_groups[0]['lr']
        train_loss = 0.0
        count = 0.0
        model.train()
        train_true_cls = []
        train_pred_cls = []
        train_true_seg = []
        train_pred_seg = []
        train_label_seg = []
        for i, (data, label, seg) in enumerate(train_loader):
            
            trot = None
            if args.rot == 'z':
                zang = torch.rand(data.shape[0]) * 2*np.pi
                trot = rmutil.aa2q(torch.stack([torch.zeros_like(zang), torch.zeros_like(zang), zang], -1)).to(device)
            elif args.rot == 'so3':
                trot = rmutil.qrand((data.shape[0],)).to(device)

            seg = seg - seg_start_index
            label_one_hot = np.zeros((label.shape[0], 16))
            for idx in range(label.shape[0]):
                label_one_hot[idx, label[idx]] = 1
            label_one_hot = torch.from_numpy(label_one_hot.astype(np.float32))
            data, label_one_hot, seg = data.to(device), label_one_hot.to(device), seg.to(device)
            if trot is not None:
                data = rmutil.qaction(trot[...,None,:], data)
            data = data.permute(0, 2, 1)
            batch_size = data.size()[0]
            optimizer.zero_grad()
            seg_pred = model(data, label_one_hot)
            seg_pred = seg_pred.permute(0, 2, 1).contiguous()
            loss = criterion(seg_pred.view(-1, seg_num_all), seg.view(-1,1).squeeze(), smoothing=args.smoothing)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
            optimizer.step()
            pred = seg_pred.max(dim=2)[1]               # (batch_size, num_points)
            count += batch_size
            train_loss += loss.item() * batch_size
            seg_np = seg.cpu().numpy()                  # (batch_size, num_points)
            pred_np = pred.detach().cpu().numpy()       # (batch_size, num_points)
            train_true_cls.append(seg_np.reshape(-1))       # (batch_size * num_points)
            train_pred_cls.append(pred_np.reshape(-1))      # (batch_size * num_points)
            train_true_seg.append(seg_np)
            train_pred_seg.append(pred_np)
            train_label_seg.append(label.reshape(-1))
            if (i + 1) % print_freq == 0:
                log_string(f"EPOCH {epoch:03d}/{args.epochs:03d} Batch {i:05d}/{len(train_loader):05d}: Loss {train_loss/count:.8f}")
        scheduler.step()
        train_loss = train_loss / count
        train_true_cls = np.concatenate(train_true_cls)
        train_pred_cls = np.concatenate(train_pred_cls)
        train_acc = metrics.accuracy_score(train_true_cls, train_pred_cls)
        avg_per_class_acc = metrics.balanced_accuracy_score(train_true_cls, train_pred_cls)
        train_true_seg = np.concatenate(train_true_seg, axis=0)
        train_pred_seg = np.concatenate(train_pred_seg, axis=0)
        train_label_seg = np.concatenate(train_label_seg)
        train_ious = calculate_shape_IoU(train_pred_seg, train_true_seg, train_label_seg)
        train_iou = np.mean(train_ious)
        log_string(f"TRAIN: loss {train_loss:.6f}, acc {train_acc:.6f}, avg acc {avg_per_class_acc:.6f}, train iou {train_iou:.6f}")

        # update R params
        if args.tune_D==3:
            with torch.no_grad():
                for name, param in model.named_parameters():
                    if 'R' in name:
                        U,_, V = torch.svd(param)
                        param.copy_(U@V.T)

        is_best = False
        test_acc, test_avg_acc, test_iou, test_loss = test(model, test_loader, criterion, device)
        if test_iou >= best_test_iou:
            best_test_iou = test_iou
            is_best = True
        saveID = utils.save_checkpoint({
            'epoch': epoch,
            'state_dict': model.state_dict(),
            'optimizer': optimizer.state_dict(),
            'scheduler': scheduler.state_dict(),
            'best_test_iou': best_test_iou,
            }, epoch, args.save_dir, is_best, saveID)

        epoch_string(f"EPOCH {epoch:03d}/{args.epochs:03d} | Test: loss {test_loss:.6f}, acc {test_acc:.6f}, avg acc {test_avg_acc:.6f}, iou {test_iou:.6f}, best iou {best_test_iou:.6f} | Train: loss {train_loss:.6f}, acc {train_acc:.6f}, avg acc {avg_per_class_acc:.6f}, iou {train_iou:.6f} | lr {lr:.8f} | {time.strftime('%Y-%m-%d-%H-%M-%S')}")

        if vessl_on:
            base_name = f'seg{args.num_points}/'
            log_dict = {'loss':test_loss, "acc":test_acc, "avg_acc":test_avg_acc, "test_iou":test_iou, "best_iou":best_test_iou}
            log_dict = {base_name+k: log_dict[k] for k in log_dict}
            vessl.log(step=epoch, payload=log_dict)

def test(model, test_loader, criterion, device):
    test_loss = 0.0
    count = 0.0
    model.eval()
    test_true_cls = []
    test_pred_cls = []
    test_true_seg = []
    test_pred_seg = []
    test_label_seg = []
    seg_num_all = test_loader.dataset.seg_num_all
    seg_start_index = test_loader.dataset.seg_start_index
    for data, label, seg in test_loader:
        
        trot = None
        if args.rot_test == 'z':
            zang = torch.rand(data.shape[0]) * 2*np.pi
            trot = rmutil.aa2q(torch.stack([torch.zeros_like(zang), torch.zeros_like(zang), zang], -1)).to(device)
        elif args.rot_test == 'so3':
            trot = rmutil.qrand((data.shape[0],)).to(device)

        seg = seg - seg_start_index
        label_one_hot = np.zeros((label.shape[0], 16))
        for idx in range(label.shape[0]):
            label_one_hot[idx, label[idx]] = 1
        label_one_hot = torch.from_numpy(label_one_hot.astype(np.float32))
        data, label_one_hot, seg = data.to(device), label_one_hot.to(device), seg.to(device)
        if trot is not None:
            # data = trot.transform_points(data)
            data = rmutil.qaction(trot[...,None,:], data)
        data = data.permute(0, 2, 1)
        batch_size = data.size()[0]
        with torch.no_grad():
            seg_pred = model(data, label_one_hot)
            seg_pred = seg_pred.permute(0, 2, 1).contiguous()
            loss = criterion(seg_pred.view(-1, seg_num_all), seg.view(-1,1).squeeze())
            pred = seg_pred.max(dim=2)[1]
        count += batch_size
        test_loss += loss.item() * batch_size
        seg_np = seg.cpu().numpy()
        pred_np = pred.detach().cpu().numpy()
        test_true_cls.append(seg_np.reshape(-1))
        test_pred_cls.append(pred_np.reshape(-1))
        test_true_seg.append(seg_np)
        test_pred_seg.append(pred_np)
        test_label_seg.append(label.reshape(-1))
    test_loss = test_loss / count
    test_true_cls = np.concatenate(test_true_cls)
    test_pred_cls = np.concatenate(test_pred_cls)
    test_acc = metrics.accuracy_score(test_true_cls, test_pred_cls)
    avg_per_class_acc = metrics.balanced_accuracy_score(test_true_cls, test_pred_cls)
    test_true_seg = np.concatenate(test_true_seg, axis=0)
    test_pred_seg = np.concatenate(test_pred_seg, axis=0)
    test_label_seg = np.concatenate(test_label_seg)
    test_ious = calculate_shape_IoU(test_pred_seg, test_true_seg, test_label_seg)
    test_iou = np.mean(test_ious)
    log_string(f"TEST: loss {test_loss:.6f}, acc {test_acc:.6f}, avg acc {avg_per_class_acc:.6f}, iou {test_iou:.6f}")
    return test_acc, avg_per_class_acc, test_iou, test_loss


if __name__ == "__main__":
    main()
