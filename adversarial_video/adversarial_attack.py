import os
import sys
sys.path.append('modules/pytorch_mask_rcnn')

import numpy as np

import torch
from torch.autograd import Variable
from torch.autograd.gradcheck import zero_gradients
from model import Dataset, unmold_image, MaskRCNN, compute_losses
from visualize import display_instances
from coco import CocoDataset, CocoConfig

from matplotlib.pyplot import savefig
from tqdm import tqdm


# Root directory of the project
ROOT_DIR = os.getcwd()

# Path to trained weights file
COCO_MODEL_PATH = os.path.join(ROOT_DIR, "mask_rcnn_coco.pth")

# Directory to save logs and model checkpoints, if not provided
# through the command line argument --logs
DEFAULT_LOGS_DIR = os.path.join(ROOT_DIR, "logs")
DEFAULT_DATASET_YEAR = "2014"

class_names = ['BG', 'person', 'bicycle', 'car', 'motorcycle', 'airplane',
               'bus', 'train', 'truck', 'boat', 'traffic light',
               'fire hydrant', 'stop sign', 'parking meter', 'bench', 'bird',
               'cat', 'dog', 'horse', 'sheep', 'cow', 'elephant', 'bear',
               'zebra', 'giraffe', 'backpack', 'umbrella', 'handbag', 'tie',
               'suitcase', 'frisbee', 'skis', 'snowboard', 'sports ball',
               'kite', 'baseball bat', 'baseball glove', 'skateboard',
               'surfboard', 'tennis racket', 'bottle', 'wine glass', 'cup',
               'fork', 'knife', 'spoon', 'bowl', 'banana', 'apple',
               'sandwich', 'orange', 'broccoli', 'carrot', 'hot dog', 'pizza',
               'donut', 'cake', 'chair', 'couch', 'potted plant', 'bed',
               'dining table', 'toilet', 'tv', 'laptop', 'mouse', 'remote',
               'keyboard', 'cell phone', 'microwave', 'oven', 'toaster',
               'sink', 'refrigerator', 'book', 'clock', 'vase', 'scissors',
               'teddy bear', 'hair drier', 'toothbrush']


def img_to_np(img):
    img = img.cpu().numpy()
    img = np.copy(img)
    img = np.swapaxes(img, 0, 1)
    img = np.swapaxes(img, 1, 2)

    return img


def train_adversarial(model, train_dataset, epochs, layers, target_attack=False):
    """Train the model.
    train_dataset, val_dataset: Training and validation Dataset objects.
    learning_rate: The learning rate to train with
    epochs: Number of training epochs. Note that previous training epochs
            are considered to be done alreay, so this actually determines
            the epochs to train in total rather than in this particaular
            call.
    layers: Allows selecting wich layers to train. It can be:
        - A regular expression to match layer names to train
        - One of these predefined values:
          heaads: The RPN, classifier and mask heads of the network
          all: All the layers
          3+: Train Resnet stage 3 and up
          4+: Train Resnet stage 4 and up
          5+: Train Resnet stage 5 and up
    """

    # Pre-defined layer regular expressions
    layer_regex = {
        # all layers but the backbone
        "heads": r"(fpn.P5\_.*)|(fpn.P4\_.*)|(fpn.P3\_.*)|(fpn.P2\_.*)|(rpn.*)|(classifier.*)|(mask.*)",
        # From a specific Resnet stage and up
        "3+": r"(fpn.C3.*)|(fpn.C4.*)|(fpn.C5.*)|(fpn.P5\_.*)|(fpn.P4\_.*)|(fpn.P3\_.*)|(fpn.P2\_.*)|(rpn.*)|(classifier.*)|(mask.*)",
        "4+": r"(fpn.C4.*)|(fpn.C5.*)|(fpn.P5\_.*)|(fpn.P4\_.*)|(fpn.P3\_.*)|(fpn.P2\_.*)|(rpn.*)|(classifier.*)|(mask.*)",
        "5+": r"(fpn.C5.*)|(fpn.P5\_.*)|(fpn.P4\_.*)|(fpn.P3\_.*)|(fpn.P2\_.*)|(rpn.*)|(classifier.*)|(mask.*)",
        # All layers
        "all": ".*",
    }
    if layers in layer_regex.keys():
        layers = layer_regex[layers]

    # Data generators
    train_set = Dataset(train_dataset, model.config, augment=False)
    train_generator = torch.utils.data.DataLoader(train_set, batch_size=1, shuffle=False, num_workers=4)

    model.set_trainable(layers)

    for epoch in range(model.epoch + 1, epochs + 1):
        # Training
        train_adversarial_batch(model, train_generator, target_attack=target_attack)


def train_adversarial_batch(model, datagenerator, target_attack=False, use_mask=False):
    for i, inputs in tqdm(enumerate(datagenerator), total=len(datagenerator)):
        images = inputs[0]
        image_metas = inputs[1]
        rpn_match = inputs[2]
        rpn_bbox = inputs[3]
        gt_class_ids = inputs[4]
        gt_boxes = inputs[5]
        gt_masks = inputs[6]

        # image_metas as numpy array
        image_metas = image_metas.numpy()

        # To GPU
        if model.config.GPU_COUNT:
            images = images.cuda()
            rpn_match = rpn_match.cuda()
            rpn_bbox = rpn_bbox.cuda()
            gt_class_ids = gt_class_ids.cuda()
            gt_boxes = gt_boxes.cuda()
            gt_masks = gt_masks.cuda()

        # SETTINGS
        steps = 30
        max_perturbation = 15

        # Wrap in variables
        images_orig = images.clone()
        images = Variable(images, requires_grad=True)
        rpn_match = Variable(rpn_match)
        rpn_bbox = Variable(rpn_bbox)
        gt_class_ids = Variable(gt_class_ids)
        gt_boxes = Variable(gt_boxes)
        gt_masks = Variable(gt_masks)

        # Create mask of
        mask = create_mask(images_orig.shape, gt_boxes[0][0])

        for step in range(steps):
            model.zero_grad()
            zero_gradients(images)
            # Run object detection
            rpn_class_logits, rpn_pred_bbox, target_class_ids, mrcnn_class_logits, target_deltas, mrcnn_bbox, target_mask, mrcnn_mask = \
                model.predict([images, image_metas, gt_class_ids, gt_boxes, gt_masks], mode='training')

            # Compute losses
            rpn_class_loss, rpn_bbox_loss, mrcnn_class_loss, mrcnn_bbox_loss, mrcnn_mask_loss = compute_losses(
                rpn_match, rpn_bbox, rpn_class_logits, rpn_pred_bbox, target_class_ids, mrcnn_class_logits,
                target_deltas, mrcnn_bbox, target_mask, mrcnn_mask)
            loss = rpn_class_loss + rpn_bbox_loss + mrcnn_class_loss + mrcnn_bbox_loss + mrcnn_mask_loss
            loss.backward()

            # Calculate gradient
            # grad = images.grad * 10000
            grad = torch.sign(images.grad)

            # Change part of the image in mask only if enabled
            if use_mask:
                grad.data = grad.data * mask

            # Clamp max perturbation per step
            grad = torch.clamp(grad, -max_perturbation / steps, max_perturbation / steps)

            # Add/Subtract perturbation based on attack
            if target_attack:
                images_tmp = unmold_image_tensor(images.data - grad.data, model.config)
            else:
                images_tmp = unmold_image_tensor(images.data + grad.data, model.config)

            # Clamp to reasonable image values
            images_tmp = torch.clamp(images_tmp, 0, 255)
            images_data = mold_image_tensor(images_tmp, model.config)

            # Set adversarial image as new input
            images.data = images_data

        a = images[0].data.clone()
        """

        # Run detection
        image = unmold_image(img_to_np(images_orig[0]), model.config)
        results = model.detect([image])
        # Visualize results
        r = results[0]

        plt = display_instances(image, r['rois'], r['masks'], r['class_ids'], class_names, r['scores'])

        #plt.show()
        #plt.close()
        plt.savefig('./data/video/results/{}.png'.format(i))
        """
        # Run detection
        image = unmold_image(img_to_np(a), model.config)
        results = model.detect([image])

        # Visualize results
        r = results[0]
        plt = display_instances(image, r['rois'], r['masks'], r['class_ids'], class_names, r['scores'])
        #plt.show()
        #plt.close()
        plt.savefig('./data/video/results_adversarial/{}.png'.format(i))


def create_mask(shape, bbox):
    '''

    :param shape: mask shape
    :param bbox: (x, y, width, height)
    :return:
    '''

    bbox = bbox.data.cpu().numpy().astype(int)
    mask = torch.zeros(shape)
    for i in range(3):
        for j in range(bbox[0], bbox[2]):
            for k in range(bbox[1], bbox[3]):
                mask[0][i][j][k] = 1

    return mask.cuda()


def mold_image_tensor(images, config):
    """Takes RGB images with 0-255 values and subtraces
    the mean pixel and converts it to float. Expects image
    colors in RGB order.
    """
    return images - torch.from_numpy(config.MEAN_PIXEL).float().cuda().unsqueeze(1).unsqueeze(2).unsqueeze(0).expand_as(images)


def unmold_image_tensor(normalized_images, config):
    """Takes a image normalized with mold() and returns the original."""
    return normalized_images + torch.from_numpy(config.MEAN_PIXEL).float().cuda().unsqueeze(1).unsqueeze(2).unsqueeze(0).expand_as(normalized_images)


if __name__ == '__main__':
    config = CocoConfig()
    config.display()

    # Create model
    model = MaskRCNN(config=config, model_dir=DEFAULT_LOGS_DIR)
    if config.GPU_COUNT:
        model = model.cuda()

    # Select weights file to load
    model_path = COCO_MODEL_PATH

    # Load weights
    print("Loading weights ", model_path)
    model.load_weights(model_path)

    dataset_path = './data/video'

    target = True

    dataset_train = CocoDataset()
    if target:
        dataset_train.load_coco(dataset_path, "adversarial_video", year=2014, auto_download=False)
    else:
        dataset_train.load_coco(dataset_path, "adversarial_video", year=2014, auto_download=False)
    dataset_train.prepare()

    train_adversarial(
        model,
        dataset_train,
        epochs=1,
        layers='all',
        target_attack=target
    )
