# --------------------------------------------------------
# RepVGG: Making VGG-style ConvNets Great Again (https://openaccess.thecvf.com/content/CVPR2021/papers/Ding_RepVGG_Making_VGG-Style_ConvNets_Great_Again_CVPR_2021_paper.pdf)
# Github source: https://github.com/DingXiaoH/RepVGG
# Licensed under The MIT License [see LICENSE for details]
# The training script is based on the code of Swin Transformer (https://github.com/microsoft/Swin-Transformer)
# --------------------------------------------------------
import torch
import numpy as np
import torch.distributed as dist
from torchvision import datasets, transforms
from timm.data.constants import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD
from timm.data import Mixup
from timm.data import create_transform
from train.logger import create_logger
try:
    from timm.data.transforms import str_to_pil_interp as _pil_interp
except:
    from timm.data.transforms import _pil_interp
from .cached_image_folder import CachedImageFolder
from .samplers import SubsetRandomSampler
import os 
from PIL import Image
from torch.utils.data import Dataset



class CustomDataset(Dataset):
    """this data set accept the data in yolo format"""
    def __init__(self, image_dir, label_dir, transform=None):
        self.image_dir = image_dir
        self.label_dir = label_dir
        self.transform = transform
        self.image_filenames = [f for f in os.listdir(image_dir) if f.endswith('.jpg') or f.endswith('.png')]

    def __len__(self):
        return len(self.image_filenames)

    def __getitem__(self, idx):
        image_filename = self.image_filenames[idx]
        image_path = os.path.join(self.image_dir, image_filename)
        label_path = os.path.join(self.label_dir, os.path.splitext(image_filename)[0] + '.txt')

        # Load image
        image = Image.open(image_path).convert('RGB')
        width, height = image.size

        # Load and parse label
        with open(label_path, 'r') as f:
            line = f.readline().strip()
            class_id, cx, cy, bw, bh = map(float, line.split())
            class_id = int(class_id)

        # Convert normalized bbox to pixel coordinates
        cx *= width
        cy *= height
        bw *= width
        bh *= height

        x1 = int(cx - bw / 2)
        y1 = int(cy - bh / 2)
        x2 = int(cx + bw / 2)
        y2 = int(cy + bh / 2)

        # Crop face
        img = image.crop((x1, y1, x2, y2))

        if self.transform:
            img = self.transform(img)

        return img, class_id




def build_loader(config):
    logger = create_logger(output_dir=config.OUTPUT, dist_rank=0 if torch.cuda.device_count() == 1 else dist.get_rank(), name=f"{config.MODEL.ARCH}")
    config.defrost()
    dataset_train, config.MODEL.NUM_CLASSES = build_dataset(is_train=True, config=config)
    config.freeze()
    logger.info(f"local rank {config.LOCAL_RANK} / global rank {dist.get_rank()} successfully build train dataset")
    dataset_val, _ = build_dataset(is_train=False, config=config)
    logger.info(f"local rank {config.LOCAL_RANK} / global rank {dist.get_rank()} successfully build val dataset")

    num_tasks = dist.get_world_size()
    logger.info(f"num task:{num_tasks}")
    global_rank = dist.get_rank()
    logger.info(f"global rank:{global_rank}")
    if config.DATA.ZIP_MODE and config.DATA.CACHE_MODE == 'part':
        indices = np.arange(dist.get_rank(), len(dataset_train), dist.get_world_size())
        sampler_train = SubsetRandomSampler(indices)
    else:
        sampler_train = torch.utils.data.DistributedSampler(
            dataset_train, num_replicas=num_tasks, rank=global_rank, shuffle=True
        )
        logger.info(f"sampler:{sampler_train}")
    if dataset_val is None:
        sampler_val = None
    else:
        indices = np.arange(dist.get_rank(), len(dataset_val), dist.get_world_size())   #TODO
        logger.info(f"indices:{indices}")
        sampler_val = SubsetRandomSampler(indices)
        logger.info(f"sampler val:{sampler_val}")

    data_loader_train = torch.utils.data.DataLoader(
        dataset_train, sampler=sampler_train,
        batch_size=config.DATA.BATCH_SIZE,
        num_workers=config.DATA.NUM_WORKERS,
        pin_memory=config.DATA.PIN_MEMORY,
        drop_last=True,
    )

    if dataset_val is None:
        data_loader_val = None
    else:
        data_loader_val = torch.utils.data.DataLoader(
            dataset_val, sampler=sampler_val,
            batch_size=config.DATA.TEST_BATCH_SIZE,
            shuffle=False,
            num_workers=config.DATA.NUM_WORKERS,
            pin_memory=config.DATA.PIN_MEMORY,
            drop_last=False
        )
        logger.info(f"data_loader val:{len(data_loader_val)}")
    # setup mixup / cutmix
    mixup_fn = None
    mixup_active = config.AUG.MIXUP > 0 or config.AUG.CUTMIX > 0. or config.AUG.CUTMIX_MINMAX is not None
    if mixup_active:
        mixup_fn = Mixup(
            mixup_alpha=config.AUG.MIXUP, cutmix_alpha=config.AUG.CUTMIX, cutmix_minmax=config.AUG.CUTMIX_MINMAX,
            prob=config.AUG.MIXUP_PROB, switch_prob=config.AUG.MIXUP_SWITCH_PROB, mode=config.AUG.MIXUP_MODE,
            label_smoothing=config.MODEL.LABEL_SMOOTHING, num_classes=config.MODEL.NUM_CLASSES)

    return dataset_train, dataset_val, data_loader_train, data_loader_val, mixup_fn


def build_dataset(is_train, config):
    logger = create_logger(output_dir=config.OUTPUT, dist_rank=0 if torch.cuda.device_count() == 1 else dist.get_rank(), name=f"{config.MODEL.ARCH}")
    if config.DATA.DATASET == 'imagenet':
        transform = build_transform(is_train, config)
        prefix = 'train' if is_train else 'val'
        if config.DATA.ZIP_MODE:
            ann_file = prefix + "_map.txt"
            prefix = prefix + ".zip@/"
            dataset = CachedImageFolder(config.DATA.DATA_PATH, ann_file, prefix, transform,
                                        cache_mode=config.DATA.CACHE_MODE if is_train else 'part')
        else:
            import torchvision
            print('use raw ImageNet data')
            dataset = torchvision.datasets.ImageNet(root=config.DATA.DATA_PATH, split='train' if is_train else 'val', transform=transform)
        nb_classes = 1000

    elif config.DATA.DATASET == 'cf100':
        mean = [0.5070751592371323, 0.48654887331495095, 0.4409178433670343]
        std = [0.2673342858792401, 0.2564384629170883, 0.27615047132568404]
        if is_train:
            transform = transforms.Compose([
                transforms.RandomCrop(32, padding=4),
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor(),
                transforms.Normalize(mean, std)
            ])
            dataset = datasets.CIFAR100(root=config.DATA.DATA_PATH, train=True, download=True, transform=transform)
        else:
            transform = transforms.Compose(
                [transforms.ToTensor(),
                 transforms.Normalize(mean, std)])
            dataset = datasets.CIFAR100(root=config.DATA.DATA_PATH, train=False, download=True, transform=transform)
        nb_classes = 100
    elif config.DATA.DATASET == 'custom':
        mean=[0.5441, 0.4334, 0.3817]
        std=[0.2558, 0.2304, 0.2223]
        transform = transform = transforms.Compose([
                transforms.Resize(96),
                transforms.CenterCrop(96),
                transforms.ToTensor(),
                transforms.Normalize(mean=mean,
                                    std=std),
                ])
        if is_train:
            train_path = os.path.join(config.DATA.DATA_PATH, 'train')
            dir_list = []
            for dir in os.listdir(train_path):
                if dir == 'images':
                    dir_list.append(os.path.join(train_path,dir))
                if dir == 'labels':
                    dir_list.append(os.path.join(train_path, dir))

            dataset = CustomDataset(image_dir=dir_list[1],
                                    label_dir=dir_list[0],
                                    transform=transform)
            logger.info(f"train data len:{len(dataset)}")
        else:
            
            valid_path = os.path.join(config.DATA.DATA_PATH, 'valid')
            
            dir_list = []
            for dir in os.listdir(valid_path):
                if dir == 'images':
                    dir_list.append(os.path.join(valid_path, dir))
                if dir == 'labels':
                    dir_list.append(os.path.join(valid_path, dir))
            
            dataset = CustomDataset(image_dir=dir_list[1],
                                    label_dir=dir_list[0],
                                    transform=transform)
            logger.info(f"vald data len:{len(dataset)}")

        nb_classes = 8
    else:
        raise NotImplementedError("We only support ImageNet and CIFAR-100 now.")

    return dataset, nb_classes


def build_transform(is_train, config):
    resize_im = config.DATA.IMG_SIZE > 32
    if is_train:
        # this should always dispatch to transforms_imagenet_train

        if config.AUG.PRESET is None:
            transform = create_transform(
                input_size=config.DATA.IMG_SIZE,
                is_training=True,
                color_jitter=config.AUG.COLOR_JITTER if config.AUG.COLOR_JITTER > 0 else None,
                auto_augment=config.AUG.AUTO_AUGMENT if config.AUG.AUTO_AUGMENT != 'none' else None,
                re_prob=config.AUG.REPROB,
                re_mode=config.AUG.REMODE,
                re_count=config.AUG.RECOUNT,
                interpolation=config.DATA.INTERPOLATION,
            )
            print('=============================== original AUG! ', config.AUG.AUTO_AUGMENT)
            if not resize_im:
                # replace RandomResizedCropAndInterpolation with
                # RandomCrop
                transform.transforms[0] = transforms.RandomCrop(config.DATA.IMG_SIZE, padding=4)

        elif config.AUG.PRESET.strip() == 'raug15':
            from train.randaug import RandAugPolicy
            transform = transforms.Compose([
                transforms.RandomResizedCrop(config.DATA.IMG_SIZE),
                transforms.RandomHorizontalFlip(),
                RandAugPolicy(magnitude=15),
                transforms.ToTensor(),
                transforms.Normalize(IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD),
            ])
            print('---------------------- RAND AUG 15 distortion!')

        elif config.AUG.PRESET.strip() == 'weak':
            transform = transforms.Compose([
                transforms.RandomResizedCrop(config.DATA.IMG_SIZE),
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor(),
                transforms.Normalize(IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD),
            ])
        elif config.AUG.PRESET.strip() == 'none':
            transform = transforms.Compose([
                transforms.Resize(config.DATA.IMG_SIZE, interpolation=_pil_interp(config.DATA.INTERPOLATION)),
                transforms.CenterCrop(config.DATA.IMG_SIZE),
                transforms.ToTensor(),
                transforms.Normalize(IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD),
            ])
        else:
            raise ValueError('???' + config.AUG.PRESET)
        print(transform)
        return transform

    t = []
    if resize_im:
        if config.TEST.CROP:
            size = int((256 / 224) * config.DATA.TEST_SIZE)
            t.append(transforms.Resize(size, interpolation=_pil_interp(config.DATA.INTERPOLATION)),
                # to maintain same ratio w.r.t. 224 images
            )
            t.append(transforms.CenterCrop(config.DATA.TEST_SIZE))
        else:
            #   default for testing
            t.append(transforms.Resize(config.DATA.TEST_SIZE, interpolation=_pil_interp(config.DATA.INTERPOLATION)))
            t.append(transforms.CenterCrop(config.DATA.TEST_SIZE))
    t.append(transforms.ToTensor())
    t.append(transforms.Normalize(IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD))
    trans = transforms.Compose(t)
    return trans
