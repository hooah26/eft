
"""
This script can be used to evaluate a trained model on 3D pose/shape and masks/part segmentation. You first need to download the datasets and preprocess them.
Example usage:
```
python3 eval.py --checkpoint=data/model_checkpoint.pt --dataset=h36m-p1 --log_freq=20
```
Running the above command will compute the MPJPE and Reconstruction Error on the Human3.6M dataset (Protocol I). The ```--dataset``` option can take different values based on the type of evaluation you want to perform:
1. Human3.6M Protocol 1 ```--dataset=h36m-p1```
2. Human3.6M Protocol 2 ```--dataset=h36m-p2```
3. 3DPW ```--dataset=3dpw```
4. LSP ```--dataset=lsp```
5. MPI-INF-3DHP ```--dataset=mpi-inf-3dhp```
"""


import torch
from torch.utils.data import DataLoader
import sys
import numpy as np
import cv2
import os
import argparse
import json
from collections import namedtuple
from tqdm import tqdm
import torchgeometry as tgm

import pickle as pkl
from fairmocap.core import config 
from fairmocap.core import constants 
from fairmocap.models import hmr, SMPL
from fairmocap.datasets import BaseDataset
from fairmocap.utils.imutils import uncrop
from fairmocap.utils.pose_utils import reconstruction_error
# from utils.part_utils import PartRenderer

# Define command-line arguments
parser = argparse.ArgumentParser()
parser.add_argument('--checkpoint', default=None, help='Path to network checkpoint')
parser.add_argument('--dataset', default='h36m-p1', choices=['h36m-p1', 'h36m-p2', 'lsp', '3dpw', '3dpw-vibe', 'mpi-inf-3dhp'], help='Choose evaluation dataset')
parser.add_argument('--log_freq', default=50, type=int, help='Frequency of printing intermediate results')
parser.add_argument('--batch_size', default=1, help='Batch size for testing')
parser.add_argument('--shuffle', default=False, action='store_true', help='Shuffle data')
parser.add_argument('--num_workers', default=4, type=int, help='Number of processes for data loading')
parser.add_argument('--result_file', default=None, help='If set, save detections to a .npz file')


g_smpl_neutral = None
g_smpl_male = None
g_smpl_female = None


def run_evaluation(model, dataset_name, dataset, result_file,
                   batch_size=1, img_res=224, 
                   num_workers=32, shuffle=False, log_freq=50, bVerbose= True):
    """Run evaluation on the datasets and metrics we report in the paper. """

    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
    # # Transfer model to the GPU
    # model.to(device)

    # Load SMPL model
    global g_smpl_neutral, g_smpl_male, g_smpl_female
    if g_smpl_neutral is None:
        g_smpl_neutral = SMPL(config.SMPL_MODEL_DIR,
                            create_transl=False).to(device)
        g_smpl_male = SMPL(config.SMPL_MODEL_DIR,
                        gender='male',
                        create_transl=False).to(device)
        g_smpl_female = SMPL(config.SMPL_MODEL_DIR,
                        gender='female',
                        create_transl=False).to(device)

        smpl_neutral = g_smpl_neutral
        smpl_male = g_smpl_male
        smpl_female = g_smpl_female
    else:
        smpl_neutral = g_smpl_neutral
        smpl_male = g_smpl_male
        smpl_female = g_smpl_female

    
    # renderer = PartRenderer()
    
    # Regressor for H36m joints
    J_regressor = torch.from_numpy(np.load(config.JOINT_REGRESSOR_H36M)).float()
    
    save_results = result_file is not None
    # Disable shuffling if you want to save the results
    if save_results:
        shuffle=False
    # Create dataloader for the dataset
    data_loader = DataLoader(dataset, batch_size=batch_size, shuffle=shuffle, num_workers=num_workers)
    
    # Pose metrics
    # MPJPE and Reconstruction error for the non-parametric and parametric shapes
    # mpjpe = np.zeros(len(dataset))
    # recon_err = np.zeros(len(dataset))
    quant_mpjpe = {}#np.zeros(len(dataset))
    quant_recon_err = {}#np.zeros(len(dataset))
    mpjpe = np.zeros(len(dataset))
    recon_err = np.zeros(len(dataset))

    mpjpe_smpl = np.zeros(len(dataset))
    recon_err_smpl = np.zeros(len(dataset))

    # Shape metrics
    # Mean per-vertex error
    shape_err = np.zeros(len(dataset))
    shape_err_smpl = np.zeros(len(dataset))

    # Mask and part metrics
    # Accuracy
    accuracy = 0.
    parts_accuracy = 0.
    # True positive, false positive and false negative
    tp = np.zeros((2,1))
    fp = np.zeros((2,1))
    fn = np.zeros((2,1))
    parts_tp = np.zeros((7,1))
    parts_fp = np.zeros((7,1))
    parts_fn = np.zeros((7,1))
    # Pixel count accumulators
    pixel_count = 0
    parts_pixel_count = 0

    # Store SMPL parameters
    smpl_pose = np.zeros((len(dataset), 72))
    smpl_betas = np.zeros((len(dataset), 10))
    smpl_camera = np.zeros((len(dataset), 3))
    pred_joints = np.zeros((len(dataset), 17, 3))

    eval_pose = False
    eval_masks = False
    eval_parts = False
    # Choose appropriate evaluation for each dataset
    if dataset_name == 'h36m-p1' or dataset_name == 'h36m-p2' or dataset_name == '3dpw' or dataset_name == '3dpw-vibe' or dataset_name == 'mpi-inf-3dhp':
        eval_pose = True
    elif dataset_name == 'lsp':
        eval_masks = True
        eval_parts = True
        annot_path = config.DATASET_FOLDERS['upi-s1h']

    joint_mapper_h36m = constants.H36M_TO_J17 if dataset_name == 'mpi-inf-3dhp' else constants.H36M_TO_J14
    joint_mapper_gt = constants.J24_TO_J17 if dataset_name == 'mpi-inf-3dhp' else constants.J24_TO_J14
    # Iterate over the entire dataset
    # cnt =0
    for step, batch in enumerate(tqdm(data_loader, desc='Eval', total=len(data_loader))):
        # Get ground truth annotations from the batch

        # imgName = batch['imgname'][0]
        # seqName = os.path.basename ( os.path.dirname(imgName) )

        gt_pose = batch['pose'].to(device)
        gt_betas = batch['betas'].to(device)
        gt_vertices = smpl_neutral(betas=gt_betas, body_pose=gt_pose[:, 3:], global_orient=gt_pose[:, :3]).vertices
        images = batch['img'].to(device)
        gender = batch['gender'].to(device)
        curr_batch_size = images.shape[0]
        

        bLoadFromFile = True
        missingPkl = False
        if bLoadFromFile:
            # pklDir= '/run/media/hjoo/disk/data/cvpr2020_eft_researchoutput/0_SPIN/0_exemplarOutput/04-22_3dpw_test_with8143_iter10'
            pklDir= '/private/home/hjoo/spinOut/05-11_3dpw_test_with1336_iter5'
            pklDir= '/private/home/hjoo/spinOut/05-11_3dpw_test_with1039_iter5'
            pklDir= '/private/home/hjoo/spinOut/05-11_3dpw_test_with1336_iter10'
            pklDir= '/private/home/hjoo/spinOut/05-11_3dpw_test_with1336_iter3'
            pklDir= '/run/media/hjoo/disk/data/cvpr2020_eft_researchoutput/0_SPIN/0_exemplarOutput/05-24_3dpw_test_with1336_iterUpto20' 
            pklDir= '/private/home/hjoo/spinOut/05-25_3dpw_test_with1336_iterUpto50_thr2e4'
            
            

            
            
            pklDir= '/private/home/hjoo/spinOut/05-25_3dpw_test_smplify_3dpwtest_from1336'
            pklDir= '/private/home/hjoo/spinOut/05-25_3dpw_test_smplify_3dpwtest_from7640'
            pklDir= '/private/home/hjoo/spinOut/05-25_3dpw_test_smplify_3dpwtest_from5992'
            pklDir= '/private/home/hjoo/spinOut/05-25_3dpw_test_with1644_h36m_thr2e4'
            
            #New test with LSP Init
            pklDir= '/private/home/hjoo/spinOut/05-27_3dpw_test_smplify_3dpwtest_from732_lsp'
            pklDir= '/private/home/hjoo/spinOut/05-27_3dpw_test_with732_lsp_withHips'
            pklDir= '/private/home/hjoo/spinOut/05-27_3dpw_test_with732_lsp_noHips'

            #New test with MPII start
                        

            #SMPLify
            pklDir= '/private/home/hjoo/spinOut/05-28_3dpw_test_smplify_3dpwtest_from3097_best'
            pklDir= '/private/home/hjoo/spinOut/05-31_3dpw_test_smplify_3dpwTest_bestW3DPW_from8653'
            pklDir= '/private/home/hjoo/spinOut/05-27_3dpw_test_smplify_3dpwtest_from35_mpii'

            #EFT
            pklDir= '/private/home/hjoo/spinOut/05-28_3dpw_test_with35_mpii_noHips'
            pklDir= '/private/home/hjoo/spinOut/05-31_3dpw_test_3dpwTest_bestW3DPW_from8653'
            pklDir= '/private/home/hjoo/spinOut/05-27_3dpw_test_with35_mpii_withHips'
            pklDir= '/private/home/hjoo/spinOut/05-27_3dpw_test_with35_mpii_noHips'
            pklDir= '/private/home/hjoo/spinOut/05-25_3dpw_test_with7640_iterUpto50_thr2e4'
            pklDir= '/private/home/hjoo/spinOut/05-25_3dpw_test_with5992_iterUpto50_thr2e4'
            pklDir= '/private/home/hjoo/spinOut/05-28_3dpw_test_byeft_with3097_best_noHips'
            


            #Rebuttal Additional Ablation
            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_3dpwTest_from8653_layer4only'       #Layer 4 only
            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_3dpwTest_from8653_afterResOnly'     #HMR FC part only
            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_3dpwTest_from8653_allResLayers'     #Res Layers
            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_3dpwTest_from8653_layer4andLayer'       #layer4 + HMR FC part


            #Rebuttal Additional Ablation (more)
            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_3dpwTest_from8653_ablation_layerteset_onlyRes_withconv1'       #All resnet
            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_3dpwTest_from8653_ablation_layerteset_all'       #no freezing
            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_3dpwTest_from8653_ablation_layerteset_decOnly'       #The last regression layer
            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_3dpwTest_from8653_ablation_layerteset_fc2Later'       #The last regression layer

            

            #Restart Some verification
            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_3dpwTest_from8653_ablation_ablation_noFreez'       #The last regression layer
            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_3dpwTest_from8653_again_noFreez'       #Original. No freeze. For debug
            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_from8653_ablation_layerteset_all'       #Original. No freeze. For debug


            #Rebuttal: Real Ablation
            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_from8653_ablation_layerteset_onlyRes_withconv1'       #Optimizing Res50. Freeze HMR FC
            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_from8653_ablation_layerteset_onlyAfterRes'       #Optimizing HMR FC part. Freeze Res50

            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_from8653_ablation_layerteset_decOnly'       #Free all except the last layer of HMR 
            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_from8653_ablation_layerteset_onlyLayer4'       #Optimzing only Layer4 of ResNet

            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_from8653_ablation_layerteset_fc2Later'          #HMR FC2 and layer
            pklDir= '/private/home/hjoo/spinOut/08-08_3dpw_test_abl_from8653_ablation_layerteset_onlyRes50LastConv'             #Last Conv of Res50
           
            #Ablation: SMPLify
            pklDir= '/private/home/hjoo/spinOut/08-10_3dpw_test_smplify_abl_3dpwTest_from8653_noPrior'            
            pklDir= '/private/home/hjoo/spinOut/08-10_3dpw_test_smplify_abl_3dpwTest_from8653_noCamFirst'             
            pklDir= '/private/home/hjoo/spinOut/08-10_3dpw_test_smplify_abl_3dpwTest_from8653_noCamFirst_noPrior'      
            pklDir= '/private/home/hjoo/spinOut/08-10_3dpw_test_smplify_abl_3dpwTest_from8653_noAnglePrior'            
            pklDir= '/private/home/hjoo/spinOut/08-10_3dpw_test_smplify_abl_3dpwTest_from8653_noPosePrior'             


            #CVPR 2021. New Start (old 3DPW)
            pklDir= '/private/home/hjoo/spinOut/10-31_3dpw_test_with7640_coco3d'
            pklDir= '/private/home/hjoo/spinOut/10-31_3dpw_test_with8377_cocoAl_h36_inf_3dpw'
            pklDir= '/private/home/hjoo/spinOut/10-31_3dpw_test_with6814_cocoAl_h36_inf'
            pklDir= '/private/home/hjoo/spinOut/10-31_3dpw_test_with5992_cocoAl'
            pklDir= '/private/home/hjoo/spinOut/10-31_3dpw_test_35_mpii'
            pklDir= '/private/home/hjoo/spinOut/10-31_3dpw_test_1644_h36m'


            #CVPR 2021. New Start (old 3DPW)
            pklDir= '/private/home/hjoo/spinOut/11-01_3dpw_test__vibe_with8377_cocoAl_h36_inf_3dpw'
            pklDir= '/private/home/hjoo/spinOut/11-01_3dpw_test__vibe_with6814_cocoAl_h36_inf'
            pklDir= '/private/home/hjoo/spinOut/11-01_3dpw_test__vibe_with7640_coco3d'
            pklDir= '/private/home/hjoo/spinOut/11-01_3dpw_test__vibe_732_lsp'
            pklDir= '/private/home/hjoo/spinOut/11-01_3dpw_test__vibe_35_mpii'
            pklDir= '/private/home/hjoo/spinOut/11-01_3dpw_test__vibe_with5992_cocoAl'
            pklDir= '/private/home/hjoo/spinOut/11-01_3dpw_test__vibe_1644_h36m'

            

            #New Ablation Study for CVPR 2021
            pklDir= '/private/home/hjoo/spinOut/11-09_3dpw_test_abl_3dpwTest_from8377_deco_all'
            pklDir= '/private/home/hjoo/spinOut/11-09_3dpw_test_abl_3dpwTest_from8377_res_all'
            pklDir= '/private/home/hjoo/spinOut/11-09_3dpw_test_abl_3dpwTest_from8377_dec_lastlayer'
            pklDir= '/private/home/hjoo/spinOut/11-09_3dpw_test_abl_3dpwTest_from8377_res_last'
         

            #SMPLify CVPR 2021
            pklDir= '/private/home/hjoo/spinOut/11-02_3dpw_test_smplify_with7640_cocopart_iter100'
            pklDir= '/private/home/hjoo/spinOut/11-02_3dpw_test_smplify_with5992_cocoall3d_iter100'
            pklDir= '/private/home/hjoo/spinOut/11-02_3dpw_test_smplify_with8377_cocoall3d_h36m_inf_3dpw_iter100'
            pklDir= '/private/home/hjoo/spinOut/11-02_3dpw_test_smplify_with6814_cocoall3d_h36m_inf_iter100'
            pklDir= '/private/home/hjoo/spinOut/11-02_3dpw_test_smplify_with35_mpii3d_iter100'
            pklDir= '/private/home/hjoo/spinOut/11-02_3dpw_test_smplify_with1644_h36m_iter100'
            pklDir= '/private/home/hjoo/spinOut/11-02_3dpw_test_smplify_with732_lspet_iter100'
            pklDir= '/private/home/hjoo/spinOut/11-02_3dpw_test_smplify_with7640_cocopart_iter50'

            #SMPLify. Iteration 50 with 2d kp threshold
            pklDir= '/private/home/hjoo/spinOut/11-09_3dpw_test_smplify_with732_lspet_iter50_th'
            pklDir= '/private/home/hjoo/spinOut/11-09_3dpw_test_smplify_with6814_cocoall3d_h36m_inf_iter50_th'
            pklDir= '/private/home/hjoo/spinOut/11-09_3dpw_test_smplify_with1644_h36m_iter50_th'
            pklDir= '/private/home/hjoo/spinOut/11-09_3dpw_test_smplify_with35_mpii3d_iter50_th'
            pklDir= '/private/home/hjoo/spinOut/11-09_3dpw_test_smplify_with5992_cocoall3d_iter50_th'
            pklDir= '/private/home/hjoo/spinOut/11-09_3dpw_test_smplify_with8377_cocoall3d_h36m_inf_3dpw_iter50_th'

            pklDir= '/private/home/hjoo/spinOut/11-09_3dpw_test_smplify_abl_3dpwTest_from8377_noCamFirst'
            pklDir= '/private/home/hjoo/spinOut/11-09_3dpw_test_smplify_abl_3dpwTest_from8377_noPrior'
            pklDir= '/private/home/hjoo/spinOut/11-10_3dpw_test_smplify_abl_3dpwTest_from8377_noCamFirst_noPrior'
            pklDir= '/private/home/hjoo/spinOut/11-10_3dpw_test_smplify_abl_3dpwTest_from8377_noPosePrior'

            pklDir= '/private/home/hjoo/spinOut/11-12_3dpw_test_smplify_abl_3dpwTest_from8377_noPoseAnglePrior'
            pklDir= '/private/home/hjoo/spinOut/11-12_3dpw_test_smplify_abl_3dpwTest_from8377_noCamFirst_noPoseAnglePrior'
            pklDir= '/private/home/hjoo/spinOut/11-12_3dpw_test_smplify_abl_3dpwTest_from8377_noAnglePrior'

            # python -m fairmocap.apps.evalfrompkl

            # 08-08_3dpw_test_3dpwTest_bestW3DPW_from8653
            # 
            # 
            # 08-08_3dpw_test_abl_3dpwTest_from8653_layer4only
         
            pred_rotmat_list, pred_betas_list, pred_camera_list = [],[],[]
            for sample_idx, name in zip(batch['sample_index'], batch['imgname']):
                
                seqName = os.path.basename ( os.path.dirname(name) )

                sample_idx = sample_idx.item()
                imgNameOnly = os.path.basename(name)[:-4]
                pklfilename = f'{seqName}_{imgNameOnly}_{sample_idx}.pkl'
                pklfilepath = os.path.join( pklDir,pklfilename)
                # cnt+=1

                # assert os.path.exists(pklfilepath)
                if os.path.exists(pklfilepath) == False:
                    missingPkl = True
                    print(f"Missing file: {pklfilepath}")
                    continue
                    # break
                else:
                    with open(pklfilepath,'rb') as f:
                        data = pkl.load(f)
                        pred_rotmat_list.append(data['pred_pose_rotmat'])
                        pred_betas_list.append(data['pred_shape'])
                        pred_camera_list.append(data['pred_camera'])
                
                pred_rotmat = torch.from_numpy( np.concatenate(pred_rotmat_list, axis=0)).cuda()
                pred_betas = torch.from_numpy(np.concatenate(pred_betas_list, axis=0)).cuda()
                pred_camera = torch.from_numpy(np.concatenate(pred_camera_list, axis=0)).cuda()


        if missingPkl:            
            with torch.no_grad():
                assert False
                pred_rotmat, pred_betas, pred_camera = model(images)


        pred_output = smpl_neutral(betas=pred_betas, body_pose=pred_rotmat[:,1:], global_orient=pred_rotmat[:,0].unsqueeze(1), pose2rot=False)
        pred_vertices = pred_output.vertices

        if save_results:
            rot_pad = torch.tensor([0,0,1], dtype=torch.float32, device=device).view(1,3,1)
            rotmat = torch.cat((pred_rotmat.view(-1, 3, 3), rot_pad.expand(curr_batch_size * 24, -1, -1)), dim=-1)
            pred_pose = tgm.rotation_matrix_to_angle_axis(rotmat).contiguous().view(-1, 72)
            smpl_pose[step * batch_size:step * batch_size + curr_batch_size, :] = pred_pose.cpu().numpy()
            smpl_betas[step * batch_size:step * batch_size + curr_batch_size, :]  = pred_betas.cpu().numpy()
            smpl_camera[step * batch_size:step * batch_size + curr_batch_size, :]  = pred_camera.cpu().numpy()

    
        # 3D pose evaluation
        if eval_pose:
            # Regressor broadcasting
            J_regressor_batch = J_regressor[None, :].expand(pred_vertices.shape[0], -1, -1).to(device)
            # Get 14 ground truth joints
            if 'h36m' in dataset_name or 'mpi-inf' in dataset_name:
                gt_keypoints_3d = batch['pose_3d'].cuda()
                gt_keypoints_3d = gt_keypoints_3d[:, joint_mapper_gt, :-1]
            # For 3DPW get the 14 common joints from the rendered shape
            else:
                gt_vertices = smpl_male(global_orient=gt_pose[:,:3], body_pose=gt_pose[:,3:], betas=gt_betas).vertices 
                gt_vertices_female = smpl_female(global_orient=gt_pose[:,:3], body_pose=gt_pose[:,3:], betas=gt_betas).vertices 
                gt_vertices[gender==1, :, :] = gt_vertices_female[gender==1, :, :]
                gt_keypoints_3d = torch.matmul(J_regressor_batch, gt_vertices)
                gt_pelvis = gt_keypoints_3d[:, [0],:].clone()
                gt_keypoints_3d = gt_keypoints_3d[:, joint_mapper_h36m, :]
                gt_keypoints_3d = gt_keypoints_3d - gt_pelvis             

                if False:
                    from renderer import viewer2D
                    from renderer import glViewer
                    import humanModelViewer
                    batchNum = gt_pose.shape[0]
                    for i in range(batchNum):
                        smpl_face = humanModelViewer.GetSMPLFace()
                        meshes_gt = {'ver': gt_vertices[i].cpu().numpy()*100, 'f': smpl_face}
                        meshes_pred = {'ver': pred_vertices[i].cpu().numpy()*100, 'f': smpl_face}

                        glViewer.setMeshData([meshes_gt, meshes_pred], bComputeNormal= True)
                        glViewer.show(5)

            # Get 14 predicted joints from the mesh
            pred_keypoints_3d = torch.matmul(J_regressor_batch, pred_vertices)
            if save_results:
                pred_joints[step * batch_size:step * batch_size + curr_batch_size, :, :]  = pred_keypoints_3d.cpu().numpy()
            pred_pelvis = pred_keypoints_3d[:, [0],:].clone()
            pred_keypoints_3d = pred_keypoints_3d[:, joint_mapper_h36m, :]
            pred_keypoints_3d = pred_keypoints_3d - pred_pelvis 

            #Visualize GT mesh and SPIN output mesh
            if False:
                from renderer import viewer2D
                from renderer import glViewer
                import humanModelViewer

                gt_keypoints_3d_vis = gt_keypoints_3d.cpu().numpy()
                gt_keypoints_3d_vis = np.reshape(gt_keypoints_3d_vis, (gt_keypoints_3d_vis.shape[0],-1))        #N,14x3
                gt_keypoints_3d_vis = np.swapaxes(gt_keypoints_3d_vis, 0,1) *100

                pred_keypoints_3d_vis = pred_keypoints_3d.cpu().numpy()
                pred_keypoints_3d_vis = np.reshape(pred_keypoints_3d_vis, (pred_keypoints_3d_vis.shape[0],-1))        #N,14x3
                pred_keypoints_3d_vis = np.swapaxes(pred_keypoints_3d_vis, 0,1) *100
                # output_sample = output_sample[ : , np.newaxis]*0.1
                # gt_sample = gt_sample[: , np.newaxis]*0.1
                # (skelNum, dim, frames)
                glViewer.setSkeleton( [gt_keypoints_3d_vis, pred_keypoints_3d_vis] ,jointType='smplcoc  o')#(skelNum, dim, frames)
                glViewer.show()
                

            # Absolute error (MPJPE)
            error = torch.sqrt(((pred_keypoints_3d - gt_keypoints_3d) ** 2).sum(dim=-1)).mean(dim=-1).cpu().numpy()

            error_upper = torch.sqrt(((pred_keypoints_3d - gt_keypoints_3d) ** 2).sum(dim=-1)).mean(dim=-1).cpu().numpy()
            # mpjpe[step * batch_size:step * batch_size + curr_batch_size] = error

            # Reconstuction_error
            r_error = reconstruction_error(pred_keypoints_3d.cpu().numpy(), gt_keypoints_3d.cpu().numpy(), reduction=None)

            r_error_upper = reconstruction_error(pred_keypoints_3d.cpu().numpy(), gt_keypoints_3d.cpu().numpy(), reduction=None)
            # recon_err[step * batch_size:step * batch_size + curr_batch_size] = r_error

            for ii, p in enumerate(batch['imgname'][:len(r_error)]):
                seqName = os.path.basename( os.path.dirname(p))
                # quant_mpjpe[step * batch_size:step * batch_size + curr_batch_size] = error
                if seqName not in quant_mpjpe.keys():
                    quant_mpjpe[seqName] = []
                    quant_recon_err[seqName] = []
                
                quant_mpjpe[seqName].append(error[ii]) 
                quant_recon_err[seqName].append(r_error[ii])

            # Reconstuction_error
            # quant_recon_err[step * batch_size:step * batch_size + curr_batch_size] = r_error

            list_mpjpe = np.hstack([ quant_mpjpe[k] for k in quant_mpjpe])
            list_reconError = np.hstack([ quant_recon_err[k] for k in quant_recon_err])
            if bVerbose:
                print(">>> {} : MPJPE {:.02f} mm, error: {:.02f} mm | Total MPJPE {:.02f} mm, error {:.02f} mm".format(seqName, np.mean(error)*1000, np.mean(r_error)*1000, np.hstack(list_mpjpe).mean()*1000, np.hstack(list_reconError).mean()*1000) )

            # print("MPJPE {}, error: {}".format(np.mean(error)*100, np.mean(r_error)*100))

        # If mask or part evaluation, render the mask and part images
        # if eval_masks or eval_parts:
        #     mask, parts = renderer(pred_vertices, pred_camera)

        # Mask evaluation (for LSP)
        if eval_masks:
            center = batch['center'].cpu().numpy()
            scale = batch['scale'].cpu().numpy()
            # Dimensions of original image
            orig_shape = batch['orig_shape'].cpu().numpy()
            for i in range(curr_batch_size):
                # After rendering, convert imate back to original resolution
                pred_mask = uncrop(mask[i].cpu().numpy(), center[i], scale[i], orig_shape[i]) > 0
                # Load gt mask
                gt_mask = cv2.imread(os.path.join(annot_path, batch['maskname'][i]), 0) > 0
                # Evaluation consistent with the original UP-3D code
                accuracy += (gt_mask == pred_mask).sum()
                pixel_count += np.prod(np.array(gt_mask.shape))
                for c in range(2):
                    cgt = gt_mask == c
                    cpred = pred_mask == c
                    tp[c] += (cgt & cpred).sum()
                    fp[c] +=  (~cgt & cpred).sum()
                    fn[c] +=  (cgt & ~cpred).sum()
                f1 = 2 * tp / (2 * tp + fp + fn)

        # Part evaluation (for LSP)
        if eval_parts:
            center = batch['center'].cpu().numpy()
            scale = batch['scale'].cpu().numpy()
            orig_shape = batch['orig_shape'].cpu().numpy()
            for i in range(curr_batch_size):
                pred_parts = uncrop(parts[i].cpu().numpy().astype(np.uint8), center[i], scale[i], orig_shape[i])
                # Load gt part segmentation
                gt_parts = cv2.imread(os.path.join(annot_path, batch['partname'][i]), 0)
                # Evaluation consistent with the original UP-3D code
                # 6 parts + background
                for c in range(7):
                   cgt = gt_parts == c
                   cpred = pred_parts == c
                   cpred[gt_parts == 255] = 0
                   parts_tp[c] += (cgt & cpred).sum()
                   parts_fp[c] +=  (~cgt & cpred).sum()
                   parts_fn[c] +=  (cgt & ~cpred).sum()
                gt_parts[gt_parts == 255] = 0
                pred_parts[pred_parts == 255] = 0
                parts_f1 = 2 * parts_tp / (2 * parts_tp + parts_fp + parts_fn)
                parts_accuracy += (gt_parts == pred_parts).sum()
                parts_pixel_count += np.prod(np.array(gt_parts.shape))

        # Print intermediate results during evaluation
        if bVerbose:
            if step % log_freq == log_freq - 1:
                if eval_pose:
                    print('MPJPE: ' + str(1000 * mpjpe[:step * batch_size].mean()))
                    print('Reconstruction Error: ' + str(1000 * recon_err[:step * batch_size].mean()))
                    print()
                if eval_masks:
                    print('Accuracy: ', accuracy / pixel_count)
                    print('F1: ', f1.mean())
                    print()
                if eval_parts:
                    print('Parts Accuracy: ', parts_accuracy / parts_pixel_count)
                    print('Parts F1 (BG): ', parts_f1[[0,1,2,3,4,5,6]].mean())
                    print()

        # if step==3:     #Debug
        #     break
    # Save reconstructions to a file for further processing
    if save_results:
        np.savez(result_file, pred_joints=pred_joints, pose=smpl_pose, betas=smpl_betas, camera=smpl_camera)
    # Print final results during evaluation

    if bVerbose:
        print('*** Final Results ***')
        print()
    if eval_pose:
        # if bVerbose:
        #     print('MPJPE: ' + str(1000 * mpjpe.mean()))
        #     print('Reconstruction Error: ' + str(1000 * recon_err.mean()))
        #     print()
        list_mpjpe = np.hstack([ quant_mpjpe[k] for k in quant_mpjpe])
        list_reconError = np.hstack([ quant_recon_err[k] for k in quant_recon_err])

        output_str ='SeqNames; '
        for seq in quant_mpjpe:
            output_str += seq + ';'
        output_str +='\n MPJPE; '
        quant_mpjpe_avg_mm = np.hstack(list_mpjpe).mean()*1000
        output_str += "Avg {:.02f} mm; ".format( quant_mpjpe_avg_mm)
        for seq in quant_mpjpe:
            output_str += '{:.02f}; '.format(1000 * np.hstack(quant_mpjpe[seq]).mean())


        output_str +='\n Recon Error; '
        quant_recon_error_avg_mm = np.hstack(list_reconError).mean()*1000
        output_str +="Avg {:.02f}mm; ".format( quant_recon_error_avg_mm )
        for seq in quant_recon_err:
            output_str += '{:.02f}; '.format(1000 * np.hstack(quant_recon_err[seq]).mean())
        if bVerbose:
            print(output_str)
        else:
            print(">>>  Test on 3DPW: MPJPE: {} | quant_recon_error_avg_mm: {}".format(quant_mpjpe_avg_mm, quant_recon_error_avg_mm) )

    
        #Export log to json
        if os.path.exists("/private/home/hjoo/codes/fairmocap/benchmarks_eval"):
            targetFile = "evalFromPkl_" + os.path.basename(pklDir)  + ".txt"
            targetFile = os.path.join('/private/home/hjoo/codes/fairmocap/benchmarks_eval',targetFile)
            print(f"\n Writing output to:{targetFile}")
            text_file = open(targetFile, "w")
            text_file.write(output_str)
            text_file.write("\n Input Pkl Dir:{}".format(pklDir))
            text_file.close()

        return quant_mpjpe_avg_mm, quant_recon_error_avg_mm

    if bVerbose:
        if eval_masks:
            print('Accuracy: ', accuracy / pixel_count)
            print('F1: ', f1.mean())
            print()
        if eval_parts:
            print('Parts Accuracy: ', parts_accuracy / parts_pixel_count)
            print('Parts F1 (BG): ', parts_f1[[0,1,2,3,4,5,6]].mean())
            print()

    return -1       #Should return something

if __name__ == '__main__':
    
    if len(sys.argv)>1:
        args = parser.parse_args()
    else:
        params =['--checkpoint','/home/hjoo/Dropbox (Facebook)/spinouput/2019-10-29-00:48-out/test1/checkpoints/2019_10_29-01_11_29.pt','--dataset','3dpw', '--log_freq','20']
        params =['--checkpoint','/home/hjoo/Dropbox (Facebook)/spinouput/10-30-40560-coco3d_first/checkpoints/2019_10_30-13_57_53.pt','--dataset','h36m-p1', '--log_freq','20']
        # params =['--checkpoint','data/model_checkpoint.pt','--dataset','h36m-p1', '--log_freq','20','--num_workers',0]
        params =['--checkpoint','/home/hjoo/Dropbox (Facebook)/spinouput/10-31-5896-ours_coco3d_all/checkpoints/2019_10_31-11_37_20.pt']      #wCOCO3D only early   first try
        params =['--checkpoint','/home/hjoo/Dropbox (Facebook)/spinouput/10-31-50173-spin_all/checkpoints/2019_11_01-22_18_03.pt']      #wCOCO3D only early   first try
        params =['--checkpoint','logs/11-07-55557-w_upper0_2_spin_all-5372/checkpoints/2019_11_07-21_13_54-best-58.088939636945724.pt']
        params =['--checkpoint','/home/hjoo/Dropbox (Facebook)/spinouput/11-14-84106-fromInit_coco3d_cocoplus3d-2878/checkpoints/2019_11_15-23_18_29-best-61.0622800886631.pt']        #Ours 3D  (no Aug!)

        #Semi-final
        params =['--checkpoint','logs/11-13-78679-bab_spin_mlc3d_fter60_ag-9589/checkpoints/2019_11_14-02_14_28-best-55.79321086406708.pt']     #Ours 3D + augmentation
        params =['--checkpoint','logs/11-13-78681-bab_spin_mlc3d_fter65-1249/checkpoints/2019_11_14-05_36_41-best-56.23840540647507.pt']        #Ours 3D  .... this is also with Aug!!

        params =['--checkpoint','spinmodel_shared/11-13-78679-bab_spin_mlc3d_fter60_ag-9589/checkpoints/2019_11_14-02_14_28-best-55.79321086406708.pt']     #Ours 3D + augmentation
        params =['--checkpoint','logs/11-13-78679-bab_spin_mlc3d_fter60-7183/checkpoints/2019_11_14-08_12_35-best-56.12510070204735.pt']        #Ours 3D  (no Aug!)
        params =['--checkpoint','data/model_checkpoint.pt']     #Original

        params +=['--dataset','3dpw-vibe']        
        # params +=['--num_workers',0]

        args = parser.parse_args(params)
        args.batch_size =64
        args.num_workers =4

    model = hmr(config.SMPL_MEAN_PARAMS)
    checkpoint = torch.load(args.checkpoint)
    model.load_state_dict(checkpoint['model'], strict=False)
    model.cuda()
    model.eval()

    # # Setup evaluation dataset
    # # dataset = BaseDataset(None, '3dpw', is_train=False, bMiniTest=False)
    # dataset = BaseDataset(None, '3dpw', is_train=False, bMiniTest=False, bEnforceUpperOnly=False)
    # # dataset = BaseDataset(None, '3dpw-crop', is_train=False, bMiniTest=False, bEnforceUpperOnly=True)
    # # # Run evaluation
    # run_evaluation(model, '3dpw',dataset , args.result_file,
    #                batch_size=args.batch_size,
    #                shuffle=args.shuffle,
    #                log_freq=args.log_freq, num_workers=args.num_workers)


    # # Setup evaluation dataset
    # dataset = BaseDataset(None, 'h36m-p1', is_train=False, bMiniTest=False, bEnforceUpperOnly=False)
    
    # # Run evaluation
    # run_evaluation(model, 'h36m-p1', dataset, args.result_file,
    #                batch_size=args.batch_size,
    #                shuffle=args.shuffle,
    #                log_freq=args.log_freq, num_workers=args.num_workers)

    # Setup evaluation dataset
    dataset = BaseDataset(None, args.dataset, is_train=False, bMiniTest=False, bEnforceUpperOnly=False)
    
    # Run evaluation
    run_evaluation(model, args.dataset, dataset, args.result_file,
                   batch_size=args.batch_size,
                   shuffle=args.shuffle,
                   log_freq=args.log_freq, num_workers=args.num_workers)
