import argparse
import os
import matplotlib.pyplot as plt
import numpy as np
import torch

from tqdm.auto import tqdm
from transformers import CLIPTokenizer, CLIPTextModel
from dataset_mri import MRIDataset
from diffusers import (
    DDPMScheduler,
    UNet2DConditionModel,
    DDIMScheduler,
)
from pipeline_mri import MRIDiffusionPipeline
from pathlib import Path
from skimage.metrics import peak_signal_noise_ratio
from torch.utils.data import DataLoader

from mri.utils import clear, get_mask, normalize_np, real_to_nchw_comp
from mri.mri import MulticoilMRI
from sigpy.mri import poisson
from utils import calculate_ssim, calculate_lpips, batch_update_json, set_seed, count_entries_in_json

def main(args):

    set_seed(args.seed)

    args.save_dir = Path(args.save_dir) / f"{args.mask_type}" / f"acc_{args.acc_factor}" / f"cfg{args.cfg_scale}" / f"eta{args.eta}"
    args.save_dir.mkdir(exist_ok=True, parents=True)
    json_path = os.path.join(args.save_dir, "summary.json")
  
    device = torch.device("cuda" if torch.cuda.is_available else "cpu")
    tokenizer = CLIPTokenizer.from_pretrained(args.pretrained_model_name_or_path, subfolder="tokenizer")
    text_encoder = CLIPTextModel.from_pretrained(args.pretrained_model_name_or_path, subfolder="text_encoder")
    noise_scheduler = DDPMScheduler.from_pretrained(args.pretrained_model_name_or_path, subfolder="scheduler")
    if args.mri_type == "fastmri":
        unet = UNet2DConditionModel.from_pretrained(args.pretrained_model_name_or_path, subfolder="fastmri")
    elif args.mri_type == "skm-tea":
        unet = UNet2DConditionModel.from_pretrained(args.pretrained_model_name_or_path, subfolder="skm-tea")
    
    text_encoder.eval()
    unet.eval()

    pipeline = MRIDiffusionPipeline(
                text_encoder=text_encoder,
                tokenizer=tokenizer,
                unet=unet,
                scheduler=noise_scheduler,
                use_clip=args.use_clip,
                config_path=args.model_config,
            )
    pipeline = pipeline.to(device)
    pipeline.scheduler = DDIMScheduler.from_config(pipeline.scheduler.config)
    pipeline.scheduler.set_timesteps(num_inference_steps=args.num_timesteps)
    
    image_size = 512 if args.mri_type == "skm-tea" else 320

    args.load_dir_meta_brain = args.load_dir_meta_brain if args.load_dir_meta_brain != "null" else None
    mri_dataset = MRIDataset(args.load_dir_meta_knee, args.load_dir_meta_brain, stage=args.stage)
    mri_dataloader = DataLoader(
        mri_dataset,          # The dataset to load
        batch_size=args.batch_size,    # Number of samples per batch
        shuffle=False,     # Whether to shuffle data at every epoch
        drop_last=False,  # Drop the last incomplete batch
    )

    num_resume = count_entries_in_json(json_path)

    psnr_list = []
    pbar = tqdm(enumerate(mri_dataloader), total=len(mri_dataloader), desc="Inference")
    for i, batch in pbar:
        # 1. extract metadata
        if i < num_resume:
            continue

        set_seed(args.seed)

        x = batch["image"]
        mps = batch["mps"]
        filename = batch["filename"]
        slice_num = batch["slice_number"]
        prompt = batch["prompt"]
        pathology = batch["pathology"]
        anatomy = batch["anatomy"]

        B = x.shape[0]
        _, N_coil, h, w = mps.shape
        
        x = x.to(device)
        mps = mps.to(device)

         # Define forward operator
        if args.mask_type == "poisson2d":
            mask = poisson((image_size, image_size), accel=args.acc_factor, dtype=np.float32)
            mask = torch.from_numpy(mask).unsqueeze(0).unsqueeze(0)
            mask = mask.to(device)
        else:
            # Define forward operator
            mask = get_mask(
                torch.zeros([B, 1, image_size, image_size]), image_size, B, 
                type=args.mask_type,
                acc_factor=args.acc_factor, 
                center_fraction=args.center_fraction
            )
            mask = mask.to(device)

        if args.cfg_scale == 0:
            prompt = [""] * B

        # 2. load image
        A_funcs = MulticoilMRI(320, mask, mps) # mask: float32, mps: complex64 -> float32 broadcast to complex64 with equal number
        y = A_funcs.A(x)
        ATy = A_funcs.AT(y)
        
        recon = pipeline.dds(
            prompt=prompt, 
            guidance_scale=args.cfg_scale, 
            num_inference_steps=args.num_timesteps, 
            eta=args.eta,
            A_funcs=A_funcs,
            y=y,
            gamma=args.gamma,
            CG_iter=args.CG_iter,
        )
        recon = real_to_nchw_comp(recon)

        json_data_list = []

        for i in range(B):

            x_i = x[i:i+1, :, :, :]
            recon_i = recon[i:i+1, :, :, :]
            volume_i = filename[i]
            slice_num_i = slice_num[i].item()
            ATy_i = ATy[i:i+1, :, :, :]
            mask_i = mask[i:i+1, :, :, :]
            pathology_i = pathology[i]
            anatomy_i = anatomy[i]

            save_path_input_i = os.path.join(args.save_dir, "input", anatomy_i, volume_i)
            save_path_recon_i = os.path.join(args.save_dir, "recon", anatomy_i, volume_i)
            save_path_label_i = os.path.join(args.save_dir, "label", anatomy_i, volume_i)
            save_path_numpy_i = os.path.join(args.save_dir, "numpy", anatomy_i, volume_i)
            os.makedirs(save_path_input_i, exist_ok=True)
            os.makedirs(save_path_recon_i, exist_ok=True)
            os.makedirs(save_path_label_i, exist_ok=True)
            os.makedirs(save_path_numpy_i, exist_ok=True)

            np.save(os.path.join(save_path_numpy_i, f"{slice_num_i:03d}.npy"), clear(recon_i))
            plt.imsave(os.path.join(save_path_input_i,  f"{slice_num_i:03d}.png"), np.abs(clear(ATy_i)), cmap='gray')
            plt.imsave(os.path.join(save_path_input_i,  f"mask_{slice_num_i:03d}.png"), np.abs(clear(mask_i)), cmap='gray')
            plt.imsave(os.path.join(save_path_label_i,  f"{slice_num_i:03d}.png"), np.abs(clear(x_i)), cmap='gray')
            plt.imsave(os.path.join(save_path_recon_i, f"{slice_num_i:03d}.png"), np.abs(clear(recon_i)), cmap='gray')
            
            # Metric computation
            x_i = normalize_np(np.abs(clear(x_i)))
            recon_i = normalize_np(np.abs(clear(recon_i)))
            
            psnr = peak_signal_noise_ratio(x_i, recon_i)
            ssim = calculate_ssim(x_i, recon_i)
            lpips_score = calculate_lpips(x_i, recon_i, device=device)

            if pathology_i == "null":
                json_data_list.append({"anatomy": anatomy_i, "volume": volume_i, "slice_number": slice_num_i, "pathology": None, "psnr": psnr, "ssim": ssim, "lpips": lpips_score})
            else:
                json_data_list.append({"anatomy": anatomy_i, "volume": volume_i, "slice_number": slice_num_i, "pathology": pathology_i, "psnr": psnr, "ssim": ssim, "lpips": lpips_score})

            psnr_list.append(psnr)
            
            # update the tqdm bar with the PSNR value for this slice
            pbar.set_postfix({"PSNR": psnr})
        
        batch_update_json(json_path, json_data_list)

    psnr_avg = sum(psnr_list) / len(psnr_list)
    print(f"PSNR: {psnr_avg:04f}\n")

        
if __name__=='__main__':
    parser = argparse.ArgumentParser(description="MRI-Inference-Argument")
    parser.add_argument('--pretrained_model_name_or_path', type=str, default="./MRI_checkpoint", help="directory of checkpoint")
    parser.add_argument('--config_dir', type=str, default="./MRI_checkpoint")
    parser.add_argument('--num_timesteps', type=int, default=100, help="Number of timesteps for inference")
    parser.add_argument('--cfg_scale', type=float, default=1.0, help="CFG scale")
    parser.add_argument('--load_dir_meta_knee', type=str, 
                        default="../fastmri-plus/knee/meta/metadata_val.csv",)
    parser.add_argument('--load_dir_meta_brain', type=str, 
                        default="../fastmri-plus/brain/meta/metadata_val.csv",)
    parser.add_argument('--save_dir', type=str, 
                        default="./result/recon_complex_multi", help="The directory for saving generated images")
    parser.add_argument('--seed', type=int, default=42, help="Seed for reproducible outputs")
    parser.add_argument('--eta', type=float, default=0.8, help="DDIM eta")
    # MRI-related args
    parser.add_argument('--mask_type', type=str, 
                        default="uniform1d", help="masking type in the Fourier domain")
    parser.add_argument('--acc_factor', type=int, 
                        default=4, help="severity of undersampling")
    parser.add_argument('--center_fraction', type=float, 
                        default=0.08, help="severity of undersampling")
    parser.add_argument('--gamma', type=float, 
                        default=5.0, help="regularization weight inversely proportional to proximal step size")
    parser.add_argument('--CG_iter', type=int, 
                        default=5, help="Num CG iter per timestep. Default is 5")
    parser.add_argument('--batch_size', default=1, type=int, help="batch-size", help="To keep batch size = 1 if mps data is not always same shape")
    parser.add_argument('--mri_type', type=str, choices=["fastmri", "skm-tea"], default="fastmri")
    
    args = parser.parse_args()
    main(args)