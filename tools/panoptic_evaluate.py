#!/usr/bin/env python
'''
The script uses a simple procedure to combine semantic segmentation and instance
segmentation predictions. The procedure is described in section 7 of the
panoptic segmentation paper https://arxiv.org/pdf/1801.00868.pdf.

On top of the procedure described in the paper. This script remove from
prediction small segments of stuff semantic classes. This addition allows to
decrease number of false positives.
'''
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals
from panopticapi.utils import IdGenerator, id2rgb, get_traceback, rgb2id
from collections import defaultdict
import multiprocessing
import os, sys
import numpy as np
import json
import time
import argparse
import copy
import logging
import shutil


DEFAULT_CONFIDENCE_THR = 0.5
DEFAULT_OVERLAP_THR = 0.5
DEFAULT_STUFF_AREA_LIMIT = 64 * 64


# def parse_args():
#     parser = argparse.ArgumentParser(
#         description="This script uses a simple procedure to combine semantic \
#                segmentation and instance segmentation predictions. See this \
#                file's head for more information."
#     )
#     parser.add_argument('--logger_file', type=str,
#                         help="logger file with resulting COCO panoptic format prediction")
#     parser.add_argument('--gt_json_file', type=str,
#                         help="JSON file with ground truth data")
#
#     parser.add_argument('--semseg_json_file', type=str,
#                         help="JSON file with semantic segmentation predictions")
#     parser.add_argument('--instseg_json_file', type=str,
#                         help="JSON file with instance segmentation predictions")
#
#     parser.add_argument('--categories_json_file', type=str,
#                         help="JSON file with Panoptic COCO categories information"
#                         )
#     parser.add_argument('--panoptic_json_file', type=str,
#                         help="JSON file with resulting COCO panoptic format prediction")
#     parser.add_argument(
#         '--segmentations_folder', type=str, help="Folder with ,\
#             panoptic COCO format segmentations. Default: X if panoptic_json_file is \
#             X.json"
#     )
#     parser.add_argument('--confidence_thr', type=float, default=DEFAULT_CONFIDENCE_THR,
#                         help="Predicted segments with smaller confidences than the threshold are filtered out")
#     parser.add_argument('--overlap_thr', type=float, default=DEFAULT_OVERLAP_THR,
#                         help="Segments that have higher that the threshold ratio of \
#                            their area being overlapped by segments with higher confidence are filtered out")
#     parser.add_argument('--stuff_area_limit', type=float, default=DEFAULT_STUFF_AREA_LIMIT,
#                         help="Stuff segments with area smaller that the limit are filtered out")
#
#     parser.add_argument('--gt_folder', type=str,
#                         help="Folder with ground turth COCO format segmentations. \
#                                    Default: X if the corresponding json file is X.json")
#
#     args = parser.parse_args()
#     return args


try:
    import PIL.Image as Image
except Exception:
    print("Failed to import the image processing packages.")
    sys.exit(-1)

try:
    from pycocotools import mask as COCOmask
except Exception:
    raise Exception("Please install pycocotools module from https://github.com/cocodataset/cocoapi")


OFFSET = 256 * 256 * 256
VOID = 0


class PQStatCat():
        def __init__(self):
            self.iou = 0.0
            self.tp = 0
            self.fp = 0
            self.fn = 0

        def __iadd__(self, pq_stat_cat):
            self.iou += pq_stat_cat.iou
            self.tp += pq_stat_cat.tp
            self.fp += pq_stat_cat.fp
            self.fn += pq_stat_cat.fn
            return self


class PQStat():
    def __init__(self):
        self.pq_per_cat = defaultdict(PQStatCat)

    def __getitem__(self, i):
        return self.pq_per_cat[i]

    def __iadd__(self, pq_stat):
        for label, pq_stat_cat in pq_stat.pq_per_cat.items():
            self.pq_per_cat[label] += pq_stat_cat
        return self

    def pq_average(self, categories, isthing):
        pq, sq, rq, n = 0, 0, 0, 0
        per_class_results = {}
        for label, label_info in categories.items():
            if isthing is not None:
                cat_isthing = label_info['isthing'] == 1
                if isthing != cat_isthing:
                    continue
            iou = self.pq_per_cat[label].iou
            tp = self.pq_per_cat[label].tp
            fp = self.pq_per_cat[label].fp
            fn = self.pq_per_cat[label].fn
            if tp + fp + fn == 0:
                per_class_results[label] = {'pq': 0.0, 'sq': 0.0, 'rq': 0.0}
                continue
            n += 1
            pq_class = iou / (tp + 0.5 * fp + 0.5 * fn)
            sq_class = iou / tp if tp != 0 else 0
            rq_class = tp / (tp + 0.5 * fp + 0.5 * fn)
            per_class_results[label] = {'pq': pq_class, 'sq': sq_class, 'rq': rq_class}
            pq += pq_class
            sq += sq_class
            rq += rq_class

        return {'pq': pq / n, 'sq': sq / n, 'rq': rq / n, 'n': n}, per_class_results


@get_traceback
def pq_compute_single_core(proc_id, annotation_set, gt_folder, pred_folder, categories):
    pq_stat = PQStat()

    idx = 0
    for gt_ann, pred_ann in annotation_set:
        if idx % 100 == 0:
            print('Core: {}, {} from {} images processed'.format(proc_id, idx, len(annotation_set)))
        idx += 1

        pan_gt = np.array(Image.open(os.path.join(gt_folder, gt_ann['file_name'])), dtype=np.uint32)
        pan_gt = rgb2id(pan_gt)
        pan_pred = np.array(Image.open(os.path.join(pred_folder, pred_ann['file_name'])), dtype=np.uint32)
        pan_pred = rgb2id(pan_pred)

        gt_segms = {el['id']: el for el in gt_ann['segments_info']}
        pred_segms = {el['id']: el for el in pred_ann['segments_info']}

        # predicted segments area calculation + prediction sanity checks
        pred_labels_set = set(el['id'] for el in pred_ann['segments_info'])
        labels, labels_cnt = np.unique(pan_pred, return_counts=True)
        for label, label_cnt in zip(labels, labels_cnt):
            if label not in pred_segms:
                if label == VOID:
                    continue
                raise KeyError('In the image with ID {} segment with ID {} is presented in PNG and not presented in JSON.'.format(gt_ann['image_id'], label))
            pred_segms[label]['area'] = label_cnt
            pred_labels_set.remove(label)
            if pred_segms[label]['category_id'] not in categories:
                raise KeyError('In the image with ID {} segment with ID {} has unknown category_id {}.'.format(gt_ann['image_id'], label, pred_segms[label]['category_id']))
        if len(pred_labels_set) != 0:
            raise KeyError('In the image with ID {} the following segment IDs {} are presented in JSON and not presented in PNG.'.format(gt_ann['image_id'], list(pred_labels_set)))

        # confusion matrix calculation
        pan_gt_pred = pan_gt.astype(np.uint64) * OFFSET + pan_pred.astype(np.uint64)
        gt_pred_map = {}
        labels, labels_cnt = np.unique(pan_gt_pred, return_counts=True)
        for label, intersection in zip(labels, labels_cnt):
            gt_id = label // OFFSET
            pred_id = label % OFFSET
            gt_pred_map[(gt_id, pred_id)] = intersection

        # count all matched pairs
        gt_matched = set()
        pred_matched = set()
        for label_tuple, intersection in gt_pred_map.items():
            gt_label, pred_label = label_tuple
            if gt_label not in gt_segms:
                continue
            if pred_label not in pred_segms:
                continue
            if gt_segms[gt_label]['iscrowd'] == 1:
                continue
            if gt_segms[gt_label]['category_id'] != pred_segms[pred_label]['category_id']:
                continue

            union = pred_segms[pred_label]['area'] + gt_segms[gt_label]['area'] - intersection - gt_pred_map.get((VOID, pred_label), 0)
            iou = intersection / union
            if iou > 0.5:
                pq_stat[gt_segms[gt_label]['category_id']].tp += 1
                pq_stat[gt_segms[gt_label]['category_id']].iou += iou
                gt_matched.add(gt_label)
                pred_matched.add(pred_label)

        # count false positives
        crowd_labels_dict = {}
        for gt_label, gt_info in gt_segms.items():
            if gt_label in gt_matched:
                continue
            # crowd segments are ignored
            if gt_info['iscrowd'] == 1:
                crowd_labels_dict[gt_info['category_id']] = gt_label
                continue
            pq_stat[gt_info['category_id']].fn += 1

        # count false positives
        for pred_label, pred_info in pred_segms.items():
            if pred_label in pred_matched:
                continue
            # intersection of the segment with VOID
            intersection = gt_pred_map.get((VOID, pred_label), 0)
            # plus intersection with corresponding CROWD region if it exists
            if pred_info['category_id'] in crowd_labels_dict:
                intersection += gt_pred_map.get((crowd_labels_dict[pred_info['category_id']], pred_label), 0)
            # predicted segment is ignored if more than half of the segment correspond to VOID and CROWD regions
            if intersection / pred_info['area'] > 0.5:
                continue
            pq_stat[pred_info['category_id']].fp += 1
    print('Core: {}, all {} images processed'.format(proc_id, len(annotation_set)))
    return pq_stat


def pq_compute_multi_core(matched_annotations_list, gt_folder, pred_folder, categories):
    cpu_num = multiprocessing.cpu_count()
    annotations_split = np.array_split(matched_annotations_list, cpu_num)
    print("Number of cores: {}, images per core: {}".format(cpu_num, len(annotations_split[0])))
    workers = multiprocessing.Pool(processes=cpu_num)
    processes = []
    for proc_id, annotation_set in enumerate(annotations_split):
        p = workers.apply_async(pq_compute_single_core,
                                (proc_id, annotation_set, gt_folder, pred_folder, categories))
        processes.append(p)
    pq_stat = PQStat()
    for p in processes:
        pq_stat += p.get()
    return pq_stat


def pq_compute(gt_json_file, pred_json, gt_folder=None, pred_folder=None, logger=None):

    start_time = time.time()
    with open(gt_json_file, 'r') as f:
        gt_json = json.load(f)
    # with open(pred_json_file, 'r') as f:
    #     pred_json = json.load(f)

    if gt_folder is None:
        gt_folder = gt_json_file.replace('.json', '')
    # if pred_folder is None:
    #     pred_folder = pred_json_file.replace('.json', '')
    categories = {el['id']: el for el in gt_json['categories']}

    print("Evaluation panoptic segmentation metrics:")
    print("Ground truth:")
    print("\tSegmentation folder: {}".format(gt_folder))
    print("\tJSON file: {}".format(gt_json_file))
    print("Prediction:")
    print("\tSegmentation folder: {}".format(pred_folder))
    # print("\tJSON file: {}".format(pred_json_file))

    if not os.path.isdir(gt_folder):
        raise Exception("Folder {} with ground truth segmentations doesn't exist".format(gt_folder))
    if not os.path.isdir(pred_folder):
        raise Exception("Folder {} with predicted segmentations doesn't exist".format(pred_folder))

    pred_annotations = {el['image_id']: el for el in pred_json['annotations']}
    matched_annotations_list = []
    for gt_ann in gt_json['annotations']:
        image_id = gt_ann['image_id']
        if image_id not in pred_annotations:
            raise Exception('no prediction for the image with id: {}'.format(image_id))
        matched_annotations_list.append((gt_ann, pred_annotations[image_id]))

    pq_stat = pq_compute_multi_core(matched_annotations_list, gt_folder, pred_folder, categories)

    metrics = [("All", None), ("Things", True), ("Stuff", False)]
    results = {}
    for name, isthing in metrics:
        results[name], per_class_results = pq_stat.pq_average(categories, isthing=isthing)
        if name == 'All':
            results['per_class'] = per_class_results

    logger.info(
        "{:4s}| {:>5s} {:>5s} {:>5s}".format("IDX", "PQ", "SQ", "RQ"))
    for idx, result in results['per_class'].items():
        logger.info("{:4d} | {:5.1f} {:5.1f} {:5.1f}".format(idx, 100 * result['pq'],
                                                                                       100 * result['sq'],
                                                                                       100 * result['rq']))
    logger.info("{:10s}| {:>5s}  {:>5s}  {:>5s} {:>5s}".format("", "PQ", "SQ", "RQ", "N"))
    logger.info("-" * (10 + 7 * 4))

    for name, _isthing in metrics:
        logger.info("{:10s}| {:5.1f}  {:5.1f}  {:5.1f} {:5d}".format(
            name,
            100 * results[name]['pq'],
            100 * results[name]['sq'],
            100 * results[name]['rq'],
            results[name]['n'])
        )

    t_delta = time.time() - start_time
    logger.info("Time elapsed: {:0.2f} seconds".format(t_delta))

    return results


def combine_to_panoptic_single_core(proc_id, img_ids, img_id2img, inst_by_image, imgid2_occ,
                                    sem_by_image, segmentations_folder, overlap_thr, confidence_thr,
                                    stuff_area_limit, categories):
    if imgid2_occ is not None:
        return combine_to_panoptic_single_core_occ(
                                    proc_id, img_ids, img_id2img, inst_by_image, imgid2_occ,
                                    sem_by_image, segmentations_folder, overlap_thr, confidence_thr,
                                    stuff_area_limit, categories)

    # cases without occ
    panoptic_json = []
    id_generator = IdGenerator(categories)

    for idx, img_id in enumerate(img_ids):
        img = img_id2img[img_id]

        if idx % 100 == 0:
            print('Core: {}, {} from {} images processed.'.format(proc_id, idx,
                                                                  len(img_ids)))

        pan_segm_id = np.zeros((img['height'],
                                img['width']), dtype=np.uint32)
        used = None
        annotation = {}
        annotation['image_id'] = int(img_id)
        annotation['file_name'] = img['file_name'].replace('.jpg', '.png')

        segments_info = []
        for ann in inst_by_image[img_id]:
            if ann['score'] < confidence_thr:
                continue
            area = COCOmask.area(ann['segmentation'])
            if area == 0:
                continue
            if used is None:
                intersect = 0
                used = copy.deepcopy(ann['segmentation'])
            else:
                intersect = COCOmask.area(
                    COCOmask.merge([used, ann['segmentation']], intersect=True)
                )
            if intersect / area > overlap_thr:
                continue
            used = COCOmask.merge([used, ann['segmentation']], intersect=False)

            mask = COCOmask.decode(ann['segmentation']) == 1
            if intersect != 0:
                mask = np.logical_and(pan_segm_id == 0, mask)
            segment_id = id_generator.get_id(ann['category_id'])
            panoptic_ann = {}
            panoptic_ann['id'] = segment_id  # id for each instance
            panoptic_ann['category_id'] = ann['category_id']  # discrete categories
            pan_segm_id[mask] = segment_id
            segments_info.append(panoptic_ann)

        for ann in sem_by_image[img_id]:
            # loop through each stuff class (thing categories have been filtered out previously)
            # ann['segmentation'] is the mask for this stuff class
            mask = COCOmask.decode(ann['segmentation']) == 1
            mask_left = np.logical_and(pan_segm_id == 0, mask)
            if mask_left.sum() < stuff_area_limit:
                continue
            segment_id = id_generator.get_id(ann['category_id'])
            panoptic_ann = {}
            panoptic_ann['id'] = segment_id  # same id for the whole stuff class
            panoptic_ann['category_id'] = ann['category_id']  # compact categories
            pan_segm_id[mask_left] = segment_id
            segments_info.append(panoptic_ann)

        annotation['segments_info'] = segments_info
        panoptic_json.append(annotation)

        Image.fromarray(id2rgb(pan_segm_id)).save(
            os.path.join(segmentations_folder, annotation['file_name'])
        )

    return panoptic_json


def combine_to_panoptic_single_core_occ(proc_id, img_ids, img_id2img, inst_by_image, imgid2_occ,
                                    sem_by_image, segmentations_folder, overlap_thr, confidence_thr,
                                    stuff_area_limit, categories):
    panoptic_json = []
    id_generator = IdGenerator(categories)

    for idx, img_id in enumerate(img_ids):
        img = img_id2img[img_id]

        if idx % 100 == 0:
            print('Core: {}, {} from {} images processed.'.format(proc_id, idx,
                                                                  len(img_ids)))

        pan_segm_id = np.zeros((img['height'],
                                img['width']), dtype=np.uint32)
        annotation = {}
        annotation['image_id'] = int(img_id)
        annotation['file_name'] = img['file_name'].replace('.jpg', '.png')

        anns = inst_by_image[img_id]
        masks = []
        for ann in anns:
            masks.append(COCOmask.decode(ann['segmentation']) == 1)

        inst_segments_info = []

        current_valid = np.full((len(anns), img['height'], img['width']), False)
        occ_matrix = imgid2_occ[str(img_id)]['matrix']
        occ_ind_map = imgid2_occ[str(img_id)]['ind_map']
        for rank_i in range(len(anns)):
            i = occ_ind_map[str(rank_i)]
            ann = anns[i]
            if ann['score'] < confidence_thr:
                continue
            area = masks[i].sum()
            if area == 0:
                continue

            current_valid[i] = np.logical_and(masks[i], pan_segm_id == 0)
            for rank_j in range(rank_i):
                j = occ_ind_map[str(rank_j)]
                if occ_matrix[rank_i][rank_j]:
                    intersection_ij = np.logical_and(masks[i], masks[j])
                    current_valid[i] = np.logical_or(current_valid[i], np.logical_and(current_valid[j], intersection_ij))
                    current_valid[j] = np.logical_and(current_valid[j], ~intersection_ij)
            if current_valid[i].sum() / area <= overlap_thr:
                continue

            segment_id = id_generator.get_id(ann['category_id'])
            pan_segm_id[current_valid[i]] = segment_id
            inst_segments_info.append(dict(
                id=segment_id,                    # id for each instance
                category_id=ann['category_id']))  # discrete categories

        valid_ids = np.unique(pan_segm_id)
        segments_info = [info for info in inst_segments_info if info['id'] in valid_ids]

        for ann in sem_by_image[img_id]:
            # loop through each stuff class (thing categories have been filtered out previously)
            # ann['segmentation'] is the mask for this stuff class
            mask = COCOmask.decode(ann['segmentation']) == 1
            mask_left = np.logical_and(pan_segm_id == 0, mask)
            if mask_left.sum() < stuff_area_limit:
                continue
            segment_id = id_generator.get_id(ann['category_id'])
            panoptic_ann = {}
            panoptic_ann['id'] = segment_id  # same id for the whole stuff class
            panoptic_ann['category_id'] = ann['category_id']  # compact categories
            pan_segm_id[mask_left] = segment_id
            segments_info.append(panoptic_ann)

        annotation['segments_info'] = segments_info
        panoptic_json.append(annotation)

        Image.fromarray(id2rgb(pan_segm_id)).save(
            os.path.join(segmentations_folder, annotation['file_name'])
        )
    return panoptic_json


def combine_to_panoptic_multi_core(img_id2img, inst_by_image, imgid2_occ,
                                   sem_by_image, segmentations_folder, overlap_thr, confidence_thr,
                                   stuff_area_limit, categories):
    cpu_num = multiprocessing.cpu_count()
    img_ids_split = np.array_split(list(img_id2img), cpu_num)
    print("Number of cores: {}, images per core: {}".format(cpu_num, len(img_ids_split[0])))
    workers = multiprocessing.Pool(processes=cpu_num)
    processes = []
    for proc_id, img_ids in enumerate(img_ids_split):
        p = workers.apply_async(combine_to_panoptic_single_core,
                                (proc_id, img_ids, img_id2img, inst_by_image, imgid2_occ,
                                 sem_by_image, segmentations_folder, overlap_thr, confidence_thr,
                                 stuff_area_limit, categories))
        processes.append(p)
    panoptic_json = []
    for p in processes:
        panoptic_json.extend(p.get())
    return panoptic_json


def combine_predictions(semseg_json_file,        # input
                        instseg_json_file,       # input
                        aux_json_file,           # input
                        images_json_file,        # input original "panoptic_val2017.json"
                        categories_json_file,    # input original discrete category ids
                        gt_folder,               # input original "panoptic_val2017"
                        work_dir,
                        mode,                    # either "val" or "test", or "submit"
                        logger,
                        confidence_thr=DEFAULT_CONFIDENCE_THR,
                        overlap_thr=DEFAULT_OVERLAP_THR,
                        stuff_area_limit=DEFAULT_STUFF_AREA_LIMIT):

    assert mode in ['val', 'test', 'submit']

    start_time = time.time()

    with open(semseg_json_file, 'r') as f:
        sem_results = json.load(f)
    with open(instseg_json_file, 'r') as f:
        inst_results = json.load(f)
    with open(aux_json_file, 'r') as f:
        aux_results = json.load(f)
        inst_idx2_srm_scores = aux_results.get('inst_idx2_srm_scores')
        imgid2_occ = aux_results.get("imgid2_occ")
    with open(images_json_file, 'r') as f:
        images_d = json.load(f)
    img_id2img = {img['id']: img for img in images_d['images']}

    with open(categories_json_file, 'r') as f:
        categories_list = json.load(f)
    categories = {el['id']: el for el in categories_list}

    panoptic_json_file = os.path.join(work_dir, 'combination_result.json')  # will be created as intermediate result
    segmentations_folder = os.path.join(work_dir, 'combination_result')  # will be created as intermediate result
    if not os.path.isdir(segmentations_folder):
        print("Creating folder {} for panoptic segmentation PNGs".format(segmentations_folder))
        os.mkdir(segmentations_folder)

    print("Combining:")
    print("Semantic segmentation:")
    print("\tJSON file: {}".format(semseg_json_file))
    print("and")
    print("Instance segmentations:")
    print("\tJSON file: {}".format(instseg_json_file))
    print("into")
    print("Panoptic segmentations:")
    print("\tSegmentation folder: {}".format(segmentations_folder))
    if mode in ["test", "submit"]:
        print("\tJSON file: {}".format(panoptic_json_file))
    print("List of images to combine is takes from {}".format(images_json_file))
    print('\n')

    inst_by_image = defaultdict(list)
    for inst in inst_results:
        inst_by_image[inst['image_id']].append(inst)
    print("Both SRM & OCC supported")
    for img_id in inst_by_image.keys():
        if imgid2_occ is not None:  # for occ & occ+srm
            pass
        elif inst_idx2_srm_scores is not None:  # for srm
            inst_by_image[img_id] = sorted(inst_by_image[img_id],
                                           key=lambda el: -inst_idx2_srm_scores[str(el['inst_idx'])])
        else:
            inst_by_image[img_id] = sorted(inst_by_image[img_id], key=lambda el: -el['score'])

    if imgid2_occ is not None:
        for img_id, instances in inst_by_image.items():
            assert len(instances) == len(imgid2_occ[str(img_id)]['matrix'])

    sem_by_image = defaultdict(list)
    for sem in sem_results:
        if categories[sem['category_id']]['isthing'] == 1:  # ignore thing categories in semantic results
            continue
        sem_by_image[sem['image_id']].append(sem)

    panoptic_json = combine_to_panoptic_multi_core(
        img_id2img,
        inst_by_image,
        imgid2_occ,
        sem_by_image,
        segmentations_folder,
        overlap_thr,
        confidence_thr,
        stuff_area_limit,
        categories
    )

    with open(images_json_file, 'r') as f:
        coco_d = json.load(f)
    coco_d['annotations'] = list(panoptic_json)
    coco_d['categories'] = list(categories.values())

    if mode in ["test", "submit"]:
        # save json file for submission
        save_file = open(panoptic_json_file, 'w')
        json.dump(coco_d, save_file)

    t_delta = time.time() - start_time
    print("Time elapsed: {:0.2f} seconds".format(t_delta))

    # evaluate
    if mode != "submit":
        pq_compute(images_json_file, coco_d, gt_folder, segmentations_folder, logger)

    # if in test mode, result_folder_panoptic will be kept for submission
    if mode == "val":
        shutil.rmtree(segmentations_folder)  # remove the generated png folder


def logger_init_pq(path):
    logger = logging.getLogger(__name__)
    logger.setLevel(level=logging.INFO)
    handler = logging.FileHandler(path + "/panoptic_evaluate.log")
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.addHandler(console)
    return logger


if __name__ == "__main__":
    coco_dir = "data/coco/annotations/"
    base = "work_dirs/your-work-dir/"
    logger = logger_init_pq(base + "log")
    combine_predictions(base + "temp_0_semantic.json",
                        base + "temp_0_instance.json",
                        base + "temp_0_aux.json",
                        coco_dir + "panoptic_val2017.json",
                        "panopticapi/panoptic_coco_categories.json",
                        coco_dir + "panoptic_val2017",
                        work_dir=base,
                        mode="test",
                        logger=logger)
