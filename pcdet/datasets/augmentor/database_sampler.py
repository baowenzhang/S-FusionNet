import pickle
from re import L
import cv2

import os
import copy
import numpy as np
from skimage import io
import torch
import SharedArray
import torch.distributed as dist

from ...ops.iou3d_nms import iou3d_nms_utils
from pcdet.utils import box_utils, box2d_utils, calibration_kitti, common_utils
from pcdet.datasets.kitti.kitti_object_eval_python import kitti_common

class DataBaseSampler(object):
    def __init__(self, root_path, sampler_cfg, class_names, logger=None):
        self.root_path = root_path
        self.class_names = class_names
        self.sampler_cfg = sampler_cfg
        self.aug_with_img = sampler_cfg.get('AUG_WITH_IMAGE', False)
        self.joint_sample = sampler_cfg.get('JOINT_SAMPLE', False)
        self.keep_raw =  sampler_cfg.get('KEEP_RAW', False)
        self.box_iou_thres = sampler_cfg.get('BOX_IOU_THRES', 1.0)
        self.aug_use_type = sampler_cfg.get('AUG_USE_TYPE', 'annotation')
        self.point_refine = sampler_cfg.get('POINT_REFINE', False)

        self.logger = logger
        self.db_infos = {}
        for class_name in class_names:
            self.db_infos[class_name] = []
            
        self.use_shared_memory = sampler_cfg.get('USE_SHARED_MEMORY', False)
        
        for db_info_path in sampler_cfg.DB_INFO_PATH:
            db_info_path = self.root_path.resolve() / db_info_path
            with open(str(db_info_path), 'rb') as f:
                infos = pickle.load(f)
                [self.db_infos[cur_class].extend(infos[cur_class]) for cur_class in class_names]

        for func_name, val in sampler_cfg.PREPARE.items():
            self.db_infos = getattr(self, func_name)(self.db_infos, val)
        
        self.gt_database_data_key = self.load_db_to_shared_memory() if self.use_shared_memory else None

        self.sample_groups = {}
        self.sample_class_num = {}
        self.limit_whole_scene = sampler_cfg.get('LIMIT_WHOLE_SCENE', False)
        for x in sampler_cfg.SAMPLE_GROUPS:
            class_name, sample_num = x.split(':')
            if class_name not in class_names:
                continue
            self.sample_class_num[class_name] = sample_num
            self.sample_groups[class_name] = {
                'sample_num': sample_num,
                'pointer': len(self.db_infos[class_name]),
                'indices': np.arange(len(self.db_infos[class_name]))
            }

    def __getstate__(self):
        d = dict(self.__dict__)
        del d['logger']
        return d

    def __setstate__(self, d):
        self.__dict__.update(d)

    def __del__(self):
        if self.use_shared_memory:
            self.logger.info('Deleting GT database from shared memory')
            cur_rank, num_gpus = common_utils.get_dist_info()
            sa_key = self.sampler_cfg.DB_DATA_PATH[0]
            if cur_rank % num_gpus == 0 and os.path.exists(f"/dev/shm/{sa_key}"):
                SharedArray.delete(f"shm://{sa_key}")

            if num_gpus > 1:
                dist.barrier()
            self.logger.info('GT database has been removed from shared memory')

    def load_db_to_shared_memory(self):
        self.logger.info('Loading GT database to shared memory')
        cur_rank, world_size, num_gpus = common_utils.get_dist_info(return_gpu_per_machine=True)

        assert self.sampler_cfg.DB_DATA_PATH.__len__() == 1, 'Current only support single DB_DATA'
        db_data_path = self.root_path.resolve() / self.sampler_cfg.DB_DATA_PATH[0]
        sa_key = self.sampler_cfg.DB_DATA_PATH[0]

        if cur_rank % num_gpus == 0 and not os.path.exists(f"/dev/shm/{sa_key}"):
            gt_database_data = np.load(db_data_path)
            common_utils.sa_create(f"shm://{sa_key}", gt_database_data)
            
        if num_gpus > 1:
            dist.barrier()
        self.logger.info('GT database has been saved to shared memory')
        return sa_key

    def filter_by_difficulty(self, db_infos, removed_difficulty):
        new_db_infos = {}
        for key, dinfos in db_infos.items():
            pre_len = len(dinfos)
            new_db_infos[key] = [
                info for info in dinfos
                if info['difficulty'] not in removed_difficulty
            ]
            if self.logger is not None:
                self.logger.info('Database filter by difficulty %s: %d => %d' % (key, pre_len, len(new_db_infos[key])))
        return new_db_infos

    def filter_by_min_points(self, db_infos, min_gt_points_list):
        for name_num in min_gt_points_list:
            name, min_num = name_num.split(':')
            min_num = int(min_num)
            if min_num > 0 and name in db_infos.keys():
                filtered_infos = []
                for info in db_infos[name]:
                    if info['num_points_in_gt'] >= min_num:
                        filtered_infos.append(info)

                if self.logger is not None:
                    self.logger.info('Database filter by min points %s: %d => %d' %
                                     (name, len(db_infos[name]), len(filtered_infos)))
                db_infos[name] = filtered_infos

        return db_infos

    def sample_with_fixed_number(self, class_name, sample_group):
        """
        Args:
            class_name:
            sample_group:
        Returns:

        """
        sample_num, pointer, indices = int(sample_group['sample_num']), sample_group['pointer'], sample_group['indices']
        if pointer >= len(self.db_infos[class_name]):
            indices = np.random.permutation(len(self.db_infos[class_name]))
            pointer = 0

        sampled_dict = [self.db_infos[class_name][idx] for idx in indices[pointer: pointer + sample_num]]
        pointer += sample_num
        sample_group['pointer'] = pointer
        sample_group['indices'] = indices
        return sampled_dict

    @staticmethod
    def put_boxes_on_road_planes(gt_boxes, road_planes, calib):
        """
        Only validate in KITTIDataset
        Args:
            gt_boxes: (N, 7 + C) [x, y, z, dx, dy, dz, heading, ...]
            road_planes: [a, b, c, d]
            calib:

        Returns:
        """
        a, b, c, d = road_planes
        center_cam = calib.lidar_to_rect(gt_boxes[:, 0:3])
        cur_height_cam = (-d - a * center_cam[:, 0] - c * center_cam[:, 2]) / b
        center_cam[:, 1] = cur_height_cam
        cur_lidar_height = calib.rect_to_lidar(center_cam)[:, 2]
        mv_height = gt_boxes[:, 2] - gt_boxes[:, 5] / 2 - cur_lidar_height
        gt_boxes[:, 2] -= mv_height  # lidar view
        return gt_boxes, mv_height

    def copy_paste_to_image(self, data_dict, crop_feat, gt_number, point_idxes=None):
        image = data_dict['images']
        boxes3d = data_dict['gt_boxes']
        boxes2d = data_dict['gt_boxes2d']
        corners_lidar = box_utils.boxes_to_corners_3d(boxes3d)
        img_aug_type = self.sampler_cfg.IMG_AUG_TYPE
        if 'depth' in img_aug_type:
            paste_order = boxes3d[:,0].argsort()
            paste_order = paste_order[::-1]
        else:
            paste_order = np.arange(len(boxes3d),dtype=np.int)

        if 'reverse' in img_aug_type:
            paste_order = paste_order[::-1]

        paste_mask = -255 * np.ones(image.shape[:2], dtype=np.int)
        fg_mask = np.zeros(image.shape[:2], dtype=np.int)
        overlap_mask = np.zeros(image.shape[:2], dtype=np.int)
        depth_mask = np.zeros((*image.shape[:2], 2), dtype=np.float)
        points_2d, depth_2d = data_dict['calib'].lidar_to_img(data_dict['points'][:,:3])
        points_2d[:,0] = np.clip(points_2d[:,0], a_min=0, a_max=image.shape[1]-1)
        points_2d[:,1] = np.clip(points_2d[:,1], a_min=0, a_max=image.shape[0]-1)
        points_2d = points_2d.astype(np.int)
        for _order in paste_order:
            _box2d = boxes2d[_order]
            image[_box2d[1]:_box2d[3],_box2d[0]:_box2d[2]] = crop_feat[_order]
            overlap_mask[_box2d[1]:_box2d[3],_box2d[0]:_box2d[2]] += \
                (paste_mask[_box2d[1]:_box2d[3],_box2d[0]:_box2d[2]] > 0).astype(np.int)
            paste_mask[_box2d[1]:_box2d[3],_box2d[0]:_box2d[2]] = _order

            if 'cover' in self.aug_use_type:
                # HxWx2 for min and max depth of each box region
                depth_mask[_box2d[1]:_box2d[3],_box2d[0]:_box2d[2],0] = corners_lidar[_order,:,0].min()
                depth_mask[_box2d[1]:_box2d[3],_box2d[0]:_box2d[2],1] = corners_lidar[_order,:,0].max()

            # foreground area of original point cloud in image plane
            if _order < gt_number:
                fg_mask[_box2d[1]:_box2d[3],_box2d[0]:_box2d[2]] = 1
        
        data_dict['images'] = image

        if not self.joint_sample:
            return data_dict
        
        new_mask = paste_mask[points_2d[:,1], points_2d[:,0]]==(point_idxes+gt_number)
        if self.keep_raw:
            raw_mask = point_idxes==-1
        else:
            raw_fg = (fg_mask == 1) & (paste_mask >= 0) & (paste_mask < gt_number)
            raw_bg = (fg_mask == 0) & (paste_mask < 0)
            raw_mask = raw_fg[points_2d[:,1], points_2d[:,0]] | raw_bg[points_2d[:,1], points_2d[:,0]]
        keep_mask = new_mask | raw_mask
        data_dict['points_2d'] = points_2d

        if 'annotation' in self.aug_use_type:
            data_dict['points'] = data_dict['points'][keep_mask]
            data_dict['points_2d'] = data_dict['points_2d'][keep_mask]
        elif 'projection' in self.aug_use_type:
            overlap_mask[overlap_mask>=1] = 1
            data_dict['overlap_mask'] = overlap_mask
            if 'cover' in self.aug_use_type:
                data_dict['depth_mask'] = depth_mask
        
        return data_dict

    def add_sampled_boxes_to_scene(self, data_dict, sampled_gt_boxes, mv_height, sampled_gt_boxes2d, total_valid_sampled_dict):
        gt_boxes_mask = data_dict['gt_boxes_mask']
        gt_boxes = data_dict['gt_boxes'][gt_boxes_mask]
        gt_names = data_dict['gt_names'][gt_boxes_mask]
        gt_number = gt_boxes_mask.sum().astype(np.int)
        points = data_dict['points']
        if self.sampler_cfg.get('USE_ROAD_PLANE', False) and not self.aug_with_img:
            sampled_gt_boxes, mv_height = self.put_boxes_on_road_planes(
                sampled_gt_boxes, data_dict['road_plane'], data_dict['calib']
            )
            data_dict.pop('calib')
            data_dict.pop('road_plane')

        obj_points_list, obj_index_list, crop_boxes2d = [], [], []
        # convert sampled 3D boxes to image plane
        if self.aug_with_img:
            gt_boxes2d = data_dict['gt_boxes2d'][gt_boxes_mask].astype(np.int)
            gt_crops2d = [data_dict['images'][_x[1]:_x[3],_x[0]:_x[2]] for _x in gt_boxes2d]
        if self.use_shared_memory:
            gt_database_data = SharedArray.attach(f"shm://{self.gt_database_data_key}")
            gt_database_data.setflags(write=0)
        else:
            gt_database_data = None 

        for idx, info in enumerate(total_valid_sampled_dict):
            if self.use_shared_memory:
                start_offset, end_offset = info['global_data_offset']
                obj_points = copy.deepcopy(gt_database_data[start_offset:end_offset])
            else:
                file_path = self.root_path / info['path']

                obj_points = np.fromfile(str(file_path), dtype=np.float32).reshape(
                    [-1, self.sampler_cfg.NUM_POINT_FEATURES])

            obj_points[:, :3] += info['box3d_lidar'][:3]

            if self.sampler_cfg.get('USE_ROAD_PLANE', False):
                # mv height
                obj_points[:, 2] -= mv_height[idx]

            if self.aug_with_img:
                calib_file = kitti_common.get_calib_path(int(info['image_idx']), self.root_path, relative_path=False)
                sampled_calib = calibration_kitti.Calibration(calib_file)
                points_2d, depth_2d = sampled_calib.lidar_to_img(obj_points[:,:3])

            if self.point_refine:
                # align calibration metrics for points
                points_ract = data_dict['calib'].img_to_rect(points_2d[:,0], points_2d[:,1], depth_2d)
                points_lidar = data_dict['calib'].rect_to_lidar(points_ract)
                obj_points[:, :3] = points_lidar
                # align calibration metrics for boxes
                box3d_raw = sampled_gt_boxes[idx].reshape(1,-1)
                box3d_coords = box_utils.boxes_to_corners_3d(box3d_raw)[0]
                box3d_box, box3d_depth = sampled_calib.lidar_to_img(box3d_coords)
                box3d_coord_rect = data_dict['calib'].img_to_rect(box3d_box[:,0], box3d_box[:,1], box3d_depth)
                box3d_rect = box_utils.corners_rect_to_camera(box3d_coord_rect).reshape(1,-1)
                box3d_lidar = box_utils.boxes3d_kitti_camera_to_lidar(box3d_rect, data_dict['calib'])
                box2d = box_utils.boxes3d_kitti_camera_to_imageboxes(box3d_rect, data_dict['calib'], 
                                                                     data_dict['images'].shape[:2])
                sampled_gt_boxes[idx] = box3d_lidar[0]
                sampled_gt_boxes2d[idx] = box2d[0]


            obj_idx = idx * np.ones(len(obj_points), dtype=np.int)
            obj_points_list.append(obj_points)
            obj_index_list.append(obj_idx)

            # copy crops from images
            if self.aug_with_img:
                img_path = self.root_path / self.sampler_cfg.IMG_ROOT_PATH / (info['image_idx']+'.png')
                raw_image = io.imread(img_path)
                raw_image = raw_image.astype(np.float32)
                raw_center = info['bbox'].reshape(2,2).mean(0)
                new_box = sampled_gt_boxes2d[idx].astype(np.int)
                new_shape = np.array([new_box[2]-new_box[0], new_box[3]-new_box[1]])
                raw_box = np.concatenate([raw_center-new_shape/2, raw_center+new_shape/2]).astype(np.int)
                raw_box[0::2] = np.clip(raw_box[0::2], a_min=0, a_max=raw_image.shape[1])
                raw_box[1::2] = np.clip(raw_box[1::2], a_min=0, a_max=raw_image.shape[0])
                if (raw_box[2]-raw_box[0])!=new_shape[0] or (raw_box[3]-raw_box[1])!=new_shape[1]:
                    new_center = new_box.reshape(2,2).mean(0)
                    new_shape = np.array([raw_box[2]-raw_box[0], raw_box[3]-raw_box[1]])
                    new_box = np.concatenate([new_center-new_shape/2, new_center+new_shape/2]).astype(np.int)

                img_crop2d = raw_image[raw_box[1]:raw_box[3],raw_box[0]:raw_box[2]] / 255

                crop_boxes2d.append(new_box)
                gt_crops2d.append(img_crop2d) 


        obj_points = np.concatenate(obj_points_list, axis=0)
        obj_points_idx = np.concatenate(obj_index_list, axis=0)
        sampled_gt_names = np.array([x['name'] for x in total_valid_sampled_dict])

        large_sampled_gt_boxes = box_utils.enlarge_box3d(
            sampled_gt_boxes[:, 0:7], extra_width=self.sampler_cfg.REMOVE_EXTRA_WIDTH
        )
        points = box_utils.remove_points_in_boxes3d(points, large_sampled_gt_boxes)
        point_idxes = -1 * np.ones(len(points), dtype=np.int)
        points = np.concatenate([points, obj_points], axis=0)
        point_idxes = np.concatenate([point_idxes, obj_points_idx], axis=0)
        gt_names = np.concatenate([gt_names, sampled_gt_names], axis=0)
        gt_boxes = np.concatenate([gt_boxes, sampled_gt_boxes], axis=0)
        data_dict['gt_boxes'] = gt_boxes
        data_dict['gt_names'] = gt_names
        data_dict['points'] = points
        if self.aug_with_img:
            data_dict['gt_boxes2d'] = np.concatenate([gt_boxes2d, np.array(crop_boxes2d)], axis=0)
            data_dict = self.copy_paste_to_image(data_dict, gt_crops2d, gt_number, point_idxes)

        if self.sampler_cfg.get('USE_ROAD_PLANE', False) and self.aug_with_img:
            # data_dict.pop('calib')
            data_dict.pop('road_plane')
        return data_dict

    def __call__(self, data_dict):
        """
        Args:
            data_dict:
                gt_boxes: (N, 7 + C) [x, y, z, dx, dy, dz, heading, ...]

        Returns:

        """
        gt_boxes = data_dict['gt_boxes']
        gt_names = data_dict['gt_names'].astype(str)
        existed_boxes = gt_boxes
        total_valid_sampled_dict = []
        sampled_mv_height = []
        sampled_gt_boxes2d = []
        for class_name, sample_group in self.sample_groups.items():
            if self.limit_whole_scene:
                num_gt = np.sum(class_name == gt_names)
                sample_group['sample_num'] = str(int(self.sample_class_num[class_name]) - num_gt)
            if int(sample_group['sample_num']) > 0:
                sampled_dict = self.sample_with_fixed_number(class_name, sample_group)

                sampled_boxes = np.stack([x['box3d_lidar'] for x in sampled_dict], axis=0).astype(np.float32)

                if self.sampler_cfg.get('DATABASE_WITH_FAKELIDAR', False):
                    sampled_boxes = box_utils.boxes3d_kitti_fakelidar_to_lidar(sampled_boxes)

                iou1 = iou3d_nms_utils.boxes_bev_iou_cpu(sampled_boxes[:, 0:7], existed_boxes[:, 0:7])
                iou2 = iou3d_nms_utils.boxes_bev_iou_cpu(sampled_boxes[:, 0:7], sampled_boxes[:, 0:7])
                iou2[range(sampled_boxes.shape[0]), range(sampled_boxes.shape[0])] = 0
                iou1 = iou1 if iou1.shape[1] > 0 else iou2
                valid_mask = ((iou1.max(axis=1) + iou2.max(axis=1)) == 0).nonzero()[0]
                # filter out box2d iou > thres
                if self.sampler_cfg.get('USE_ROAD_PLANE', False):
                    sampled_boxes, mv_height = self.put_boxes_on_road_planes(
                        sampled_boxes, data_dict['road_plane'], data_dict['calib']
                    )
                    
                if self.aug_with_img:
                    # sampled_boxes2d = np.stack([x['bbox'] for x in sampled_dict], axis=0).astype(np.float32)
                    boxes3d_camera = box_utils.boxes3d_lidar_to_kitti_camera(sampled_boxes, data_dict['calib'])
                    sampled_boxes2d = box_utils.boxes3d_kitti_camera_to_imageboxes(boxes3d_camera, data_dict['calib'], 
                                                                                   data_dict['images'].shape[:2])
                    sampled_boxes2d = torch.Tensor(sampled_boxes2d)
                    existed_boxes2d = torch.Tensor(data_dict['gt_boxes2d'])
                    iou2d1 = box2d_utils.pairwise_iou(sampled_boxes2d, existed_boxes2d).cpu().numpy()
                    iou2d2 = box2d_utils.pairwise_iou(sampled_boxes2d, sampled_boxes2d).cpu().numpy()
                    iou2d2[range(sampled_boxes2d.shape[0]), range(sampled_boxes2d.shape[0])] = 0
                    iou2d1 = iou2d1 if iou2d1.shape[1] > 0 else iou2d2
                    
                    valid_mask = ((iou2d1.max(axis=1)<self.box_iou_thres) & 
                                 (iou2d2.max(axis=1)<self.box_iou_thres) & 
                                 ((iou1.max(axis=1) + iou2.max(axis=1)) == 0)).nonzero()[0]

                    sampled_boxes2d = sampled_boxes2d[valid_mask].cpu().numpy()
                    sampled_gt_boxes2d.append(sampled_boxes2d)
                valid_sampled_dict = [sampled_dict[x] for x in valid_mask]
                valid_sampled_boxes = sampled_boxes[valid_mask]
                
                if self.sampler_cfg.get('USE_ROAD_PLANE', False):
                    mv_height = mv_height[valid_mask]
                    sampled_mv_height = np.concatenate((sampled_mv_height, mv_height), axis=0)

                existed_boxes = np.concatenate((existed_boxes, valid_sampled_boxes), axis=0)
                #sampled_mv_height = np.concatenate((sampled_mv_height, mv_height), axis=0)
                total_valid_sampled_dict.extend(valid_sampled_dict)

        sampled_gt_boxes = existed_boxes[gt_boxes.shape[0]:, :]
        if len(sampled_gt_boxes2d) > 0:
            sampled_gt_boxes2d = np.concatenate(sampled_gt_boxes2d, axis=0)

        if total_valid_sampled_dict.__len__() > 0:
            data_dict = self.add_sampled_boxes_to_scene(data_dict, 
                                                        sampled_gt_boxes, 
                                                        sampled_mv_height, 
                                                        sampled_gt_boxes2d,
                                                        total_valid_sampled_dict)

        data_dict.pop('gt_boxes_mask')
        return data_dict
