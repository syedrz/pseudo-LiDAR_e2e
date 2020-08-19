import torch as torch
import numpy as np
from lib.config import cfg
import lib.utils.kitti_utils as kitti_utils
import kitti_util


def pointrcnn_transform(pts_lidar, calib, PRCNN_npoints, sample_id, mode='TRAIN', random_select=True):

    pts_rect = calib.lidar_to_rect(pts_lidar[:, 0:3])
    pts_intensity = pts_lidar[:, 3]

    if cfg.PC_REDUCE_BY_RANGE:
        with torch.no_grad():
            x_range, y_range, z_range = cfg.PC_AREA_SCOPE
            pts_x, pts_y, pts_z = pts_rect[:, 0], pts_rect[:, 1], pts_rect[:, 2]
            range_flag = (pts_x >= x_range[0]) & (pts_x <= x_range[1]) \
                            & (pts_y >= y_range[0]) & (pts_y <= y_range[1]) \
                            & (pts_z >= z_range[0]) & (pts_z <= z_range[1])
            pts_valid_flag = range_flag

    pts_rect = pts_rect[pts_valid_flag][:, 0:3]
    pts_intensity = pts_intensity[pts_valid_flag]

    # print('Length of sparse generated points: {}'.format(len(pts_rect)))

    # generate inputs
    if mode == 'TRAIN' or random_select:
        # Sample the number of points to a fixed value `PRCNN_npoints` for PointNet
        if PRCNN_npoints < len(pts_rect):
            pts_depth = pts_rect[:, 2]
            pts_near_flag = pts_depth < 40.0
            far_idxs_choice = torch.nonzero(pts_near_flag == 0).flatten()
            near_idxs = torch.nonzero(pts_near_flag == 1).flatten()
            
            idx = torch.randperm(near_idxs.size(0))[
                :PRCNN_npoints - len(far_idxs_choice)]
            near_idxs_choice = near_idxs[idx]

            choice = torch.cat((near_idxs_choice, far_idxs_choice), dim=0) \
                if len(far_idxs_choice) > 0 else near_idxs_choice
            choice = choice[torch.randperm(choice.shape[0])]
        else:
            choice = torch.arange(0, len(pts_rect), dtype=torch.long)

            if PRCNN_npoints > len(pts_rect):
                times = PRCNN_npoints // len(pts_rect)
                rem = PRCNN_npoints % len(pts_rect)

                idx = torch.randperm(choice.size(0))[:rem]
                extra_choice = choice[idx]

                if times == 1:
                    choice = torch.cat((choice, extra_choice), dim=0)
                else:
                    if len(extra_choice) == 0:
                        choice = choice.repeat(times)
                    else:
                        choice = torch.cat((choice.repeat(times), extra_choice), dim=0)
                    #print("Sample id: {}".format(sample_id))
                    #print("Original pts size: {}".format(len(pts_rect)))

            choice = choice[torch.randperm(choice.shape[0])]

        ret_pts_rect = pts_rect[choice, :]
        if len(ret_pts_rect) != PRCNN_npoints:
            #print("Original pts size: {}".format(len(pts_rect)))
            #print(len(ret_pts_rect))
            pass

        # translate intensity to [-0.5, 0.5]
        ret_pts_intensity = pts_intensity[choice] - 0.5
    else:
        ret_pts_rect = pts_rect
        ret_pts_intensity = pts_intensity - 0.5

    pts_features = [ret_pts_intensity.reshape(-1, 1)]
    ret_pts_features = torch.cat(
        pts_features, dim=1) if pts_features.__len__() > 1 else pts_features[0]

    if cfg.RPN.USE_INTENSITY:
        pts_input = torch.cat(
            (ret_pts_rect, ret_pts_features), dim=1)  # (N, C)
    else:
        pts_input = ret_pts_rect

    return pts_input, ret_pts_rect, ret_pts_features


def generate_rpn_sample(pts_lidar, sample_info, idx, PRCNN_npoints, mode='TRAIN'):
    calib = kitti_util.Calib(sample_info['calib'][idx])

    pts_input, ret_pts_rect, ret_pts_features = pointrcnn_transform(
        pts_lidar, calib, PRCNN_npoints, sample_id=sample_info['sample_id'][idx], mode=mode, random_select=sample_info['random_select'][idx])

    sample = {}
    if cfg.RPN.FIXED:
        sample['pts_input'] = pts_input
        sample['pts_rect'] = ret_pts_rect
        sample['pts_features'] = ret_pts_features
        return sample

    gt_boxes3d = sample_info['gt_boxes3d'][idx]
    # Filter zero rows added by batching
    gt_boxes3d = gt_boxes3d[~np.all(gt_boxes3d == 0, axis=1)]

    # generate training labels
    # Not doing backprop here for now
    rpn_cls_label, rpn_reg_label = generate_rpn_training_labels(
        ret_pts_rect.cpu().detach().numpy(), gt_boxes3d)

    sample['pts_input'] = pts_input
    sample['pts_rect'] = ret_pts_rect
    sample['pts_features'] = ret_pts_features
    sample['rpn_cls_label'] = rpn_cls_label
    sample['rpn_reg_label'] = rpn_reg_label

    return sample


def get_detector_batch(points, batch, mode='TRAIN'):
    """
    Input:
         points: Pointcloud generated by Depth Network (In lidar coordinates) [Batch, N]
         batch: Data batch containing left and right image features

    Returns: Updated batch with features for the pointcloud
             Has three new fields: pts_input, pts_rect, pts_features
    """
    batch_size = len(batch['sample_id'])
    assert len(points) == batch_size

    samples = [generate_rpn_sample(pts_lidar, batch, idx, cfg.RPN.NUM_POINTS, mode)
               for idx, pts_lidar in enumerate(points)]

    for key in samples[0].keys():
        if isinstance(samples[0][key], np.ndarray):
            if batch_size == 1:
                batch[key] = samples[0][key][np.newaxis, ...]
            else:
                batch[key] = np.concatenate(
                    [samples[k][key][np.newaxis, ...] for k in range(batch_size)], axis=0)

        elif isinstance(samples[0][key], torch.Tensor):
            if batch_size == 1:
                batch[key] = torch.unsqueeze(samples[0][key], dim=0)
            else:
                batch[key] = torch.cat(
                    [samples[k][key].unsqueeze_(0) for k in range(batch_size)], dim=0)
    return batch


def generate_rpn_training_labels(pts_rect, gt_boxes3d):
    cls_label = np.zeros((pts_rect.shape[0]), dtype=np.int32)
    # dx, dy, dz, ry, h, w, l
    reg_label = np.zeros((pts_rect.shape[0], 7), dtype=np.float32)
    gt_corners = kitti_utils.boxes3d_to_corners3d(gt_boxes3d, rotate=True)
    extend_gt_boxes3d = kitti_utils.enlarge_box3d(
        gt_boxes3d, extra_width=0.2)
    extend_gt_corners = kitti_utils.boxes3d_to_corners3d(
        extend_gt_boxes3d, rotate=True)
    for k in range(gt_boxes3d.shape[0]):
        box_corners = gt_corners[k]
        fg_pt_flag = kitti_utils.in_hull(pts_rect, box_corners)
        fg_pts_rect = pts_rect[fg_pt_flag]
        cls_label[fg_pt_flag] = 1

        # enlarge the bbox3d, ignore nearby points
        extend_box_corners = extend_gt_corners[k]
        fg_enlarge_flag = kitti_utils.in_hull(pts_rect, extend_box_corners)
        ignore_flag = np.logical_xor(fg_pt_flag, fg_enlarge_flag)
        cls_label[ignore_flag] = -1

        # pixel offset of object center
        center3d = gt_boxes3d[k][0:3].copy()  # (x, y, z)
        center3d[1] -= gt_boxes3d[k][3] / 2
        # Now y is the true center of 3d box 20180928
        reg_label[fg_pt_flag, 0:3] = center3d - fg_pts_rect

        # size and angle encoding
        reg_label[fg_pt_flag, 3] = gt_boxes3d[k][3]  # h
        reg_label[fg_pt_flag, 4] = gt_boxes3d[k][4]  # w
        reg_label[fg_pt_flag, 5] = gt_boxes3d[k][5]  # l
        reg_label[fg_pt_flag, 6] = gt_boxes3d[k][6]  # ry

    return cls_label, reg_label
