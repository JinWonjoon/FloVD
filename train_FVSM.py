import omegaconf.listconfig
import os
import math
import time
import inspect
import argparse
import datetime
import subprocess

from pathlib import Path
from omegaconf import OmegaConf
from typing import Dict, Tuple
from datetime import timedelta

import torch
import torchvision
import torch.distributed as dist
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel as DDP

from einops import rearrange
from transformers import CLIPImageProcessor, CLIPVisionModelWithProjection
from diffusers import AutoencoderKLTemporalDecoder, EulerDiscreteScheduler
from diffusers.optimization import get_scheduler
from diffusers.utils import check_min_version
from diffusers.utils.import_utils import is_xformers_available
from diffusers.pipelines.stable_video_diffusion.pipeline_stable_video_diffusion import _resize_with_antialiasing

from flovd.utils.util import setup_logger, format_time, save_videos_grid
from flovd.pipelines.pipeline_animation_FVSM import StableVideoDiffusionPipelineFVSM
from flovd.models.unet_EncPoseCond import UNetSpatioTemporalConditionModelEncPoseCond
from flovd.models.pose_adaptor import FlowEncoder, FVSM_Adaptor

from flovd.models.flow_generator import FlowGenerator, get_flow_generator_input
from flovd.utils.util import instantiate_from_config

import pdb

os.environ["TOKENIZERS_PARALLELISM"] = "false"

def init_dist(launcher="slurm", backend='nccl', port=29500, **kwargs):
    """Initializes distributed environment."""
    if launcher == 'pytorch':
        rank = int(os.environ['RANK'])
        num_gpus = torch.cuda.device_count()
        local_rank = rank % num_gpus
        torch.cuda.set_device(local_rank)
        dist.init_process_group(backend=backend, timeout=timedelta(minutes=30), **kwargs)

    elif launcher == 'slurm':
        proc_id = int(os.environ['SLURM_PROCID'])
        ntasks = int(os.environ['SLURM_NTASKS'])
        node_list = os.environ['SLURM_NODELIST']
        num_gpus = torch.cuda.device_count()
        local_rank = proc_id % num_gpus
        torch.cuda.set_device(local_rank)
        addr = subprocess.getoutput(
            f'scontrol show hostname {node_list} | head -n1')
        os.environ['MASTER_ADDR'] = addr
        os.environ['WORLD_SIZE'] = str(ntasks)
        os.environ['RANK'] = str(proc_id)
        port = os.environ.get('PORT', port)
        os.environ['MASTER_PORT'] = str(port)
        dist.init_process_group(backend=backend, timeout=timedelta(minutes=30))

    else:
        raise NotImplementedError(f'Not implemented launcher type: `{launcher}`!')
    # https://github.com/pytorch/pytorch/issues/98763
    # torch.cuda.set_device(local_rank)

    return local_rank


def save_dict(batch, conditioning_images, sample, log_dict, output_dir, global_step, idx, r_type):
    save_path_root = f"{output_dir}/samples/sample-{global_step}_{r_type}"
    gif_save_path_root = os.path.join(save_path_root, "gif")
    grid_save_path_root = os.path.join(save_path_root, "image")
    os.makedirs(gif_save_path_root, exist_ok=True)
    os.makedirs(grid_save_path_root, exist_ok=True)
    
    sample_gt = rearrange(torch.cat([sample.cpu(), (batch['pixel_values'].cpu()[0] + 1) / 2], dim=2),
                                    'f c h w -> c f h w')  # [3, f 2h, w]
    conditioning_image = conditioning_images.cpu()[0] / 2. + 0.5  # 3 h w
    
    if 'clip_name' in batch:
        gif_path = os.path.join(gif_save_path_root, f"batch-{idx:04d}_{'_'.join(batch['video_caption'][0].split(' ')[:10])}_{batch['clip_name'][0]}_Sample_GT.gif")
        grid_path = os.path.join(grid_save_path_root, f"batch-{idx:04d}_{'_'.join(batch['video_caption'][0].split(' ')[:10])}_{batch['clip_name'][0]}_cond_image.png")
    else:
        gif_path = os.path.join(gif_save_path_root, f"batch-{idx:04d}_{'_'.join(batch['video_caption'][0].split(' ')[:10])}_Sample_GT.gif")
        grid_path = os.path.join(grid_save_path_root, f"batch-{idx:04d}_{'_'.join(batch['video_caption'][0].split(' ')[:10])}_cond_image.png")
        
    save_videos_grid(sample_gt[None, ...], gif_path, rescale=False)
    torchvision.utils.save_image(conditioning_image, grid_path)
    
    
    # save depth_ctxt
    if log_dict['depth_ctxt'] is not None:
        depth_ctxt = log_dict['depth_ctxt'].cpu() # [b 1 1 h w]
        depth_min, depth_max = torch.amin(depth_ctxt[0], dim=[1, 2, 3], keepdim=True), \
                                torch.amax(depth_ctxt[0], dim=[1, 2, 3], keepdim=True)
        depth_ctxt = ((depth_ctxt[0] - depth_min) / (depth_max - depth_min + 1e-7)).clip(0.,1.)
        if 'clip_name' in batch:
            grid_path = os.path.join(grid_save_path_root, f"batch-{idx:04d}_{'_'.join(batch['video_caption'][0].split(' ')[:10])}_{batch['clip_name'][0]}_depth_ctxt.png")
        else:
            grid_path = os.path.join(grid_save_path_root, f"batch-{idx:04d}_{'_'.join(batch['video_caption'][0].split(' ')[:10])}_depth_ctxt.png")
        torchvision.utils.save_image(depth_ctxt, grid_path)
    
    # Save log_dict
    for key, value in log_dict.items():
        if value is None or key in ['original_guidance_3D', 'depth_ctxt']:
            continue
        
        if key in ['observed_mask']:
            image_gif = (value[0].cpu()).clip(0.,1.)
            image_grid = torchvision.utils.make_grid(image_gif, nrow=14)
        elif key in ['depth']:
            depth_min, depth_max = torch.amin(value[0].cpu(), dim=[1, 2, 3], keepdim=True), \
                                    torch.amax(value[0].cpu(), dim=[1, 2, 3], keepdim=True)
            image_gif = ((value[0].cpu() - depth_min) / (depth_max - depth_min + 1e-7)).clip(0.,1.)
            image_grid = torchvision.utils.make_grid(image_gif, nrow=14)
        else:
            image_gif = (value[0].cpu() / 2. + 0.5).clip(0.,1.)
            image_grid = torchvision.utils.make_grid(image_gif, nrow=14)
        
        if 'clip_name' in batch:
            gif_path = os.path.join(gif_save_path_root, f"batch-{idx:04d}_{'_'.join(batch['video_caption'][0].split(' ')[:10])}_{batch['clip_name'][0]}_{key}.gif")
            grid_path = os.path.join(grid_save_path_root, f"batch-{idx:04d}_{'_'.join(batch['video_caption'][0].split(' ')[:10])}_{batch['clip_name'][0]}_{key}.png")
        else:
            gif_path = os.path.join(gif_save_path_root, f"batch-{idx:04d}_{'_'.join(batch['video_caption'][0].split(' ')[:10])}_{key}.gif")
            grid_path = os.path.join(grid_save_path_root, f"batch-{idx:04d}_{'_'.join(batch['video_caption'][0].split(' ')[:10])}_{key}.png")

        save_videos_grid(rearrange(image_gif, "f c h w -> c f h w")[None, ...], gif_path, rescale=False)
        torchvision.utils.save_image(image_grid, grid_path)
    
    return save_path_root


def main(name: str,
         launcher: str,
         port: int,

         output_dir: str,
         pretrained_model_path: str,
         unet_subfolder: str,
         down_block_types: Tuple[str],
         up_block_types: Tuple[str],

         train_data: Dict,
         validation_data: Dict,
         random_null_image_ratio: float = 0.15,

         flow_encoder_kwargs: Dict = None,
         attention_processor_kwargs: Dict = None,
         flow_generator_kwargs: Dict = None,

         do_sanity_check: bool = True,
         sample_before_training: bool = False,
         video_length: int = 14,

         max_train_epoch: int = -1,
         max_train_steps: int = 100,
         validation_steps: int = 100,
         validation_steps_tuple: Tuple = (-1,),

         learning_rate: float = 3e-5,
         lr_warmup_steps: int = 0,
         lr_scheduler: str = "constant",

         P_mean: float = 0.7,
         P_std: float = 1.6,
         condition_image_noise_mean: float = -3,
         condition_image_noise_std: float = 1.6,
         sample_latent: bool = True,
         first_image_cond: bool = True,

         fps: int = 7,
         motion_bucket_id: int = 127,

         num_inference_steps: int = 25,
         min_guidance_scale: float = 1.0,
         max_guidance_scale: float = 3.0,

         num_workers: int = 32,
         train_batch_size: int = 1,
         adam_beta1: float = 0.9,
         adam_beta2: float = 0.999,
         adam_weight_decay: float = 1e-2,
         adam_epsilon: float = 1e-08,
         max_grad_norm: float = 1.0,
         gradient_accumulation_steps: int = 1,
         checkpointing_epochs: int = 5,
         checkpointing_steps: int = -1,

         mixed_precision_training: bool = True,
         enable_xformers_memory_efficient_attention: bool = True,

         global_seed: int = 42,
         logger_interval: int = 10,
         resume_from: str = None,
         
         use_cubic_sampling: bool = False,
         use_quadratic_sampling: bool = False,
         decode_chunk_size: int=14,
         unet_training_strategy: str = None,
         ):
    check_min_version("0.10.0.dev0")
    
    assert gradient_accumulation_steps > 0 and isinstance(gradient_accumulation_steps, int), "Set gradient_accumulation_steps as integer larger than 0"

    assert not (use_quadratic_sampling == True and use_cubic_sampling == True), "only choose one or do not choose anything."

    # Initialize distributed training
    local_rank = init_dist(launcher=launcher, port=port)
    global_rank = dist.get_rank()
    num_processes = dist.get_world_size()
    is_main_process = global_rank == 0

    seed = global_seed + global_rank
    torch.manual_seed(seed)

    # Logging folder
    folder_name = name + datetime.datetime.now().strftime("-%Y-%m-%dT%H-%M-%S")
    output_dir = os.path.join(output_dir, folder_name)

    *_, config = inspect.getargvalues(inspect.currentframe())

    logger = setup_logger(output_dir, global_rank)

    # Handle the output folder creation
    if is_main_process:
        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(f"{output_dir}/samples", exist_ok=True)
        os.makedirs(f"{output_dir}/sanity_check", exist_ok=True)
        os.makedirs(f"{output_dir}/checkpoints", exist_ok=True)
        OmegaConf.save(config, os.path.join(output_dir, 'config.yaml'))

    # Load scheduler, tokenizer and models.
    image_encoder = CLIPVisionModelWithProjection.from_pretrained(pretrained_model_path, subfolder="image_encoder")
    vae = AutoencoderKLTemporalDecoder.from_pretrained(pretrained_model_path, subfolder="vae")
    # unet = UNetSpatioTemporalConditionModelPoseCond.from_pretrained(pretrained_model_path,
    #                                                                 subfolder=unet_subfolder,
    #                                                                 down_block_types=down_block_types,
    #                                                                 up_block_types=up_block_types)
    unet = UNetSpatioTemporalConditionModelEncPoseCond.from_pretrained(pretrained_model_path,
                                                                       subfolder=unet_subfolder,
                                                                       down_block_types=down_block_types,
                                                                       up_block_types=up_block_types)
    
    noise_scheduler = EulerDiscreteScheduler.from_pretrained(pretrained_model_path, subfolder="scheduler")
    feature_extractor = CLIPImageProcessor.from_pretrained(pretrained_model_path, subfolder="feature_extractor")

    # init flow generator
    flow_generator = FlowGenerator(**flow_generator_kwargs)

    # init pose encoder
    flow_encoder = FlowEncoder(**flow_encoder_kwargs)
    
    
    # Set Training Strategy
    runtime_type = ['training', 'inference']
    training_runtime_type = 'training'

    # init attention processor
    logger.info(f"Setting the attention processors")
    unet.set_pose_cond_attn_processor(enable_xformers=(enable_xformers_memory_efficient_attention and is_xformers_available()), **attention_processor_kwargs)

    # Freeze vae, and text_encoder
    vae.requires_grad_(False)
    image_encoder.requires_grad_(False)
    unet.requires_grad_(False)
    flow_generator.requires_grad_(False)

    # pose_cond_attn_proc = torch.nn.ModuleList([v for v in unet.attn_processors.values() if
    #                                            isinstance(v, PoseAdaptorAttnProcessor)])
    # pose_cond_attn_proc.requires_grad_(True)
    
    flow_encoder.requires_grad_(True)

    pose_adaptor = FVSM_Adaptor(unet, flow_encoder, flow_generator)

    encoder_trainable_params = list(filter(lambda p: p.requires_grad, flow_encoder.parameters()))
    encoder_trainable_param_names = [p[0] for p in
                                     filter(lambda p: p[1].requires_grad, flow_encoder.named_parameters())]
    
    #---------------------------------------------------------------------------------------------------------------------------
    # Denoising U-Net training strategy
    if unet_training_strategy=='all':
        unet.requires_grad_(True)
        unet_trainable_params = [v for k, v in unet.named_parameters() if v.requires_grad]
        unet_trainable_param_names = [k for k, v in unet.named_parameters() if v.requires_grad]    
    elif unet_training_strategy=='temporal':
        # 1. modulelist -> requires_grad_ True
        # 2. 
        raise NotImplementedError("training only for temporal layer will be implemented soon.")
    else:
        assert unet_training_strategy is None
        # No trainable parameters
        unet_trainable_params = []
        unet_trainable_param_names = []
    
    # unet_trainable_params = [v for k, v in unet.named_parameters() if v.requires_grad and 'merge' in k and 'lora' not in k]
    # unet_trainable_param_names = [k for k, v in unet.named_parameters() if v.requires_grad and 'merge' in k and 'lora' not in k]
    #---------------------------------------------------------------------------------------------------------------------------
    

    trainable_params = encoder_trainable_params + unet_trainable_params
    trainable_param_names = encoder_trainable_param_names + unet_trainable_param_names
        
    trainable_params = encoder_trainable_params 
    trainable_param_names = encoder_trainable_param_names

    if is_main_process:
        logger.info(f"trainable parameter number: {len(trainable_params)}")
        logger.info(f"trainable parameter names: {trainable_param_names}")
        logger.info(f"trainable parameter scale: {sum(p.numel() for p in trainable_params) / 1e6:.3f} M")

    optimizer = torch.optim.AdamW(
        trainable_params,
        lr=learning_rate,
        betas=(adam_beta1, adam_beta2),
        weight_decay=adam_weight_decay,
        eps=adam_epsilon,
    )
    # Move models to GPU
    vae.to(local_rank)
    image_encoder.to(local_rank)

    # Get the training dataset
    logger.info(f'Building training datasets')
    train_dataset = instantiate_from_config(train_data)
    # train_dataset = RealEstate10KPose(**train_data)
    distributed_sampler = DistributedSampler(
        train_dataset,
        num_replicas=num_processes,
        rank=global_rank,
        shuffle=True,
        seed=global_seed,
    )

    # DataLoaders creation:
    train_dataloader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=train_batch_size,
        shuffle=False,
        sampler=distributed_sampler,
        num_workers=num_workers,
        pin_memory=True,
        drop_last=True,
    )

    # Get the validation dataset
    logger.info(f'Building validation datasets')
    validation_dataset = instantiate_from_config(validation_data)
    # validation_dataset = RealEstate10KPose(**validation_data)
    validation_dataloader = torch.utils.data.DataLoader(
        validation_dataset,
        batch_size=1,
        shuffle=False,
        num_workers=num_workers,
        pin_memory=True,
        drop_last=False
    )

    # Get the training iteration
    if max_train_steps == -1:
        assert max_train_epoch != -1
        max_train_steps = max_train_epoch * len(train_dataloader)

    if checkpointing_steps == -1:
        assert checkpointing_epochs != -1
        checkpointing_steps = checkpointing_epochs * len(train_dataloader)

    # Scheduler
    lr_scheduler = get_scheduler(
        lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=lr_warmup_steps * gradient_accumulation_steps,
        num_training_steps=max_train_steps * gradient_accumulation_steps,
    )

    # Validation pipeline
    validation_pipeline = StableVideoDiffusionPipelineFVSM(
        vae=vae,
        image_encoder=image_encoder,
        unet=unet,
        scheduler=noise_scheduler,
        feature_extractor=feature_extractor,
        flow_encoder=flow_encoder,
        flow_generator=flow_generator)
    # validation_pipeline.enable_model_cpu_offload()

    # DDP wrapper
    logger.info(f'Building DDP')
    pose_adaptor.to(local_rank)
    pose_adaptor = DDP(pose_adaptor, device_ids=[local_rank], output_device=local_rank)

    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / gradient_accumulation_steps)
    # Afterwards we recalculate our number of training epochs
    num_train_epochs = math.ceil(max_train_steps / num_update_steps_per_epoch)

    # Train!
    total_batch_size = train_batch_size * num_processes * gradient_accumulation_steps

    # For validation and checkpointing intervals
    checkpointing_steps = checkpointing_steps * gradient_accumulation_steps
    validation_steps = validation_steps * gradient_accumulation_steps
    validation_steps_tuple = [x*gradient_accumulation_steps for x in list(validation_steps_tuple)]
    logger_interval = logger_interval * gradient_accumulation_steps

    if is_main_process:
        logger.info("***** Running training *****")
        logger.info(f"  Num examples = {len(train_dataset)}")
        logger.info(f"  Num Epochs = {num_train_epochs}")
        logger.info(f"  Instantaneous batch size per device = {train_batch_size}")
        logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
        logger.info(f"  Gradient Accumulation steps = {gradient_accumulation_steps}")
        logger.info(f"  Total optimization steps (max_train_step X gradient_accumulation_steps) = {max_train_steps * gradient_accumulation_steps}")
    global_step = 0
    first_epoch = 0

    if resume_from is not None:
        logger.info(f"Resuming the training from the checkpoint: {resume_from}")
        ckpt = torch.load(resume_from, map_location=pose_adaptor.device)
        global_step = ckpt['global_step']
        trained_iterations = (global_step % len(train_dataloader))
        first_epoch = int(global_step // len(train_dataloader))
        # optimizer.load_state_dict(ckpt['optimizer_state_dict'])
        flow_encoder_state_dict = ckpt['flow_encoder_state_dict']
        attention_processor_state_dict = ckpt['attention_processor_state_dict']
        pose_enc_m, pose_enc_u = pose_adaptor.module.flow_encoder.load_state_dict(flow_encoder_state_dict, strict=False)
        assert len(pose_enc_m) == 0 and len(pose_enc_u) == 0
        _, attention_processor_u = pose_adaptor.module.unet.load_state_dict(attention_processor_state_dict, strict=False)
        assert len(attention_processor_u) == 0
        logger.info(f"Loading the pose encoder and attention processor weights done.")
        logger.info(f"Loading done, resuming training from the {global_step + 1}th iteration")
        lr_scheduler.last_epoch = first_epoch
    else:
        trained_iterations = 0

    # Support mixed-precision training
    scaler = torch.cuda.amp.GradScaler() if mixed_precision_training else None

    if is_main_process and do_sanity_check and sample_before_training:

        generator = torch.Generator(device=unet.device)
        generator.manual_seed(global_seed)

        if isinstance(train_data, omegaconf.listconfig.ListConfig):
            height = train_data[0].sample_size[0] if not isinstance(train_data[0].sample_size, int) else \
                train_data[0].sample_size
            width = train_data[0].sample_size[1] if not isinstance(train_data[0].sample_size, int) else \
                train_data[0].sample_size
        else:
            height = train_data.kwargs.sample_size[0] if not isinstance(train_data.kwargs.sample_size,
                                                                 int) else train_data.kwargs.sample_size
            width = train_data.kwargs.sample_size[1] if not isinstance(train_data.kwargs.sample_size,
                                                                int) else train_data.kwargs.sample_size

        validation_data_iter = iter(validation_dataloader)

        for idx, validation_batch in enumerate(validation_data_iter):
            if validation_dataset.use_sampled_validation == True and idx >= 16:
                break
            
            if first_image_cond:
                conditioning_images = validation_batch['pixel_values'][:, 0].to(local_rank)
            else:
                conditioning_images = validation_batch['condition_image'][:, 0].to(local_rank)  # [b, c, h, w] -1 - 1
            pixel_values = rearrange(validation_batch['pixel_values'].to(local_rank), "b f c h w -> (b f) c h w")
            
            for r_type in runtime_type:
                print(r_type)
                flow_generator_input = get_flow_generator_input(condition_image=conditioning_images,
                                                        pixel_values=None if r_type=='inference' else pixel_values,
                                                        intrinsics=validation_batch["intrinsics"].to(device=local_rank),
                                                        c2w=validation_batch["c2w"].to(device=local_rank),
                                                        runtime_type=r_type)
            
                with torch.no_grad():
                    with torch.cuda.amp.autocast(enabled=mixed_precision_training, dtype=torch.bfloat16):
                        sample, flow_log_dict = validation_pipeline(
                            image=conditioning_images,
                            flow_generator_input=flow_generator_input,
                            height=height,
                            width=width,
                            num_frames=video_length,
                            num_inference_steps=num_inference_steps,
                            min_guidance_scale=min_guidance_scale,
                            max_guidance_scale=max_guidance_scale,
                            generator=generator,
                            output_type='pt',
                            decode_chunk_size=decode_chunk_size,
                        ) 
                    sample = sample.frames[0].cpu() # [f 3 h w] 0-1

                    save_path = save_dict(validation_batch, conditioning_images, sample, flow_log_dict, output_dir, global_step, idx, r_type)
                
            logger.info(f"Saved samples to {save_path}")
    dist.barrier()

    for epoch in range(first_epoch, num_train_epochs):
        train_dataloader.sampler.set_epoch(epoch)
        pose_adaptor.train()

        data_iter = iter(train_dataloader)
        for step in range(trained_iterations, len(train_dataloader)):

            iter_start_time = time.time()
            batch = next(data_iter)
            data_end_time = time.time()

            # Data batch sanity check
            if epoch == first_epoch and step == 0 and do_sanity_check:
                pixel_values, condition_images, video_captions = batch['pixel_values'].cpu(), batch['condition_image'], batch['video_caption']
                pixel_values = rearrange(pixel_values, "b f c h w -> b c f h w")
                condition_images = rearrange(condition_images, "b f c h w -> b c f h w")
                for idx, (pixel_value, condition_image, video_caption) in enumerate(zip(pixel_values, condition_images, video_captions)):
                    pixel_value = pixel_value[None, ...]
                    save_videos_grid(pixel_value,
                                     f"{output_dir}/sanity_check/{'_'.join(video_caption.split(' ')[:10])}.gif",
                                     rescale=True)
                    condition_image = pixel_value[0, :, 0] if first_image_cond else condition_image[:, 0]  # [3, h, w]
                    condition_image = condition_image / 2. + 0.5
                    torchvision.utils.save_image(condition_image,
                                                 f"{output_dir}/sanity_check/{'_'.join(video_caption.split(' ')[:10])}.png")

            ### >>>> Training >>>> ###

            # Convert videos to latent space
            pixel_values = batch["pixel_values"].to(local_rank)     # [b, f, c, h, w]
            bsz, video_length = pixel_values.shape[:2]
            with torch.no_grad():
                pixel_values = rearrange(pixel_values, "b f c h w -> (b f) c h w")
                if sample_latent:
                    latents = vae.encode(pixel_values).latent_dist.sample()
                else:
                    latents = vae.encode(pixel_values).latent_dist.mode()
                latents = rearrange(latents, "(b f) c h w -> b f c h w", f=video_length)
                latents = latents * vae.config.scaling_factor

            # Sample noise that we'll add to the latents
            # 1. get the sigma of each noise
            """
            Original CameraCtrl uses normal distribution following EDM
            Inspired by Non-uniform time step sampling of T2I-Adapter, we use skewed Normal distribution for more training on the deeper timesteps..
            - If alpha_skew_norm is zero, it is the same with sampling from Normal distribution
            - If alpha_skew_norm > 0, samples from Negatively skewed distribution, i.e., mode is larger than 0 (mean < median < mode)
            
            Or, just using cubic sampling following T2I-Adapter..
            In this case, you should consider scale differences in sampling range.
            """
            # rnd_normal = torch.randn([bsz, 1, 1, 1, 1], device=pixel_values.device)
            # sigma = (rnd_normal * P_std + P_mean).exp()
            if use_cubic_sampling:
                rnd_normal = ((1 - torch.rand([bsz, 1, 1, 1, 1], device=pixel_values.device)**3)-0.5) * 3.66 * 2
                sigma = (rnd_normal * P_std + P_mean).exp()
            elif use_quadratic_sampling:
                rnd_normal = ((1 - torch.rand([bsz, 1, 1, 1, 1], device=pixel_values.device)**2)-0.5) * 3.66 * 2
                sigma = (rnd_normal * P_std + P_mean).exp()
            else:
                rnd_normal = torch.randn([bsz, 1, 1, 1, 1], device=pixel_values.device)
                sigma = (rnd_normal * P_std + P_mean).exp()
            
            
            # 2. sample the noise
            noise = torch.randn_like(latents) * sigma
            # 3. add noise to the latent
            noisy_latents = latents + noise

            # Get the preconditioning parameters
            c_skip = 1 / (sigma ** 2 + 1)
            c_out = -sigma / (sigma ** 2 + 1) ** 0.5
            c_in = 1 / (sigma ** 2 + 1) ** 0.5
            c_noise = (sigma.log() / 4).reshape([bsz])
            # Get the loss weight
            loss_weight = (sigma ** 2 + 1) / sigma ** 2     # [bsz, 1, 1, 1, 1]

            # encode conditioning image latent
            if first_image_cond:
                conditioning_pixel_value = batch["pixel_values"][:, 0].to(local_rank)    # [b, c, h, w]
            else:
                conditioning_pixel_value = batch['condition_image'][:, 0].to(local_rank)     # [b, c, h, w]
            conditioning_rnd_normal = torch.randn([bsz, 1, 1, 1], device=pixel_values.device)
            conditioning_sigma = (conditioning_rnd_normal * condition_image_noise_std + condition_image_noise_mean).exp()
            conditioning_pixel_value = torch.randn_like(conditioning_pixel_value) * conditioning_sigma + conditioning_pixel_value
            with torch.no_grad():
                conditioning_latents = vae.encode(conditioning_pixel_value).latent_dist.mode()
            conditioning_latents = conditioning_latents.unsqueeze(1).repeat(1, video_length, 1, 1, 1)   # [b f c h w]

            input_latents = torch.cat([c_in * noisy_latents, conditioning_latents], dim=2)      # [b, f, c, h, w]

            # encode image latent using the clip image encoder
            if first_image_cond:
                conditioning_images = batch["pixel_values"][:, 0].to(local_rank)    # [b, c, h, w]
            else:
                conditioning_images = batch['condition_image'][:, 0].to(local_rank)     # [b, c, h, w]
            conditioning_images_orig = conditioning_images.detach().clone()
            
            conditioning_images = _resize_with_antialiasing(conditioning_images, (224, 224))
            conditioning_images = (conditioning_images + 1.0) / 2.0
            conditioning_images = feature_extractor(images=conditioning_images,
                                                    do_normalize=True,
                                                    do_center_crop=False,
                                                    do_resize=False,
                                                    do_rescale=False,
                                                    return_tensors="pt").pixel_values.to(local_rank)
            encoder_hidden_states = image_encoder(conditioning_images).image_embeds.unsqueeze(1)      # [bsz, 1, c]
            random_p = torch.rand(bsz, device=pixel_values.device)
            uncond_mask = random_p < random_null_image_ratio
            uncond_mask = uncond_mask.unsqueeze(-1).unsqueeze(-1)
            null_conditioning = torch.zeros_like(encoder_hidden_states)
            encoder_hidden_states = torch.where(uncond_mask, null_conditioning, encoder_hidden_states)

            # get additional time ids
            noise_aug_strength = conditioning_sigma[:, 0, 0, 0]       # [bsz, ]
            add_time_ids = [[fps, motion_bucket_id, strength] for strength in noise_aug_strength]
            add_time_ids = torch.tensor(add_time_ids, device=local_rank)        # [bsz, 3]

            # Predict the noise residual and compute loss
            # Mixed-precision training
            flow_generator_input = get_flow_generator_input(condition_image=conditioning_images_orig,
                                                            pixel_values=pixel_values,
                                                            intrinsics=batch["intrinsics"].to(device=local_rank),
                                                            c2w=batch["c2w"].to(device=local_rank),
                                                            runtime_type=training_runtime_type)
            
            # https://arxiv.org/abs/2211.09800
            with torch.cuda.amp.autocast(enabled=mixed_precision_training, dtype=torch.bfloat16):
                model_pred = pose_adaptor(input_latents,
                                          c_noise,
                                          flow_generator_input,
                                          encoder_hidden_states=encoder_hidden_states,
                                          added_time_ids=add_time_ids,
                                          )  # [b f c h w]
                predicted_latents = c_out * model_pred + c_skip * noisy_latents
                loss = torch.mean(loss_weight * (predicted_latents.float() - latents.float()) ** 2)
                loss = loss / gradient_accumulation_steps

            # Backpropagate
            if mixed_precision_training:
                scaler.scale(loss).backward()
                
                # Gradient accumulation
                if (step+1) % gradient_accumulation_steps == 0:
                    """ >>> gradient clipping >>> """
                    scaler.unscale_(optimizer)
                    torch.nn.utils.clip_grad_norm_(filter(lambda p: p.requires_grad, pose_adaptor.parameters()),
                                                max_grad_norm)
                    """ <<< gradient clipping <<< """
                    scaler.step(optimizer)
                    scaler.update()
            else:
                loss.backward()
                if (step+1) % gradient_accumulation_steps == 0:
                    """ >>> gradient clipping >>> """
                    torch.nn.utils.clip_grad_norm_(filter(lambda p: p.requires_grad, pose_adaptor.parameters()),
                                                max_grad_norm)
                    """ <<< gradient clipping <<< """
                    optimizer.step()

            dist.all_reduce(loss, op=dist.ReduceOp.SUM)
            average_loss = loss / dist.get_world_size()

            lr_scheduler.step()
            if (step+1) % gradient_accumulation_steps == 0:
                optimizer.zero_grad(set_to_none=True)
            global_step += 1
            iter_end_time = time.time()

            if (global_step % logger_interval) == 0 or global_step == 0:
                gpu_memory = torch.cuda.max_memory_allocated() / (1024 ** 3)
                msg = f"Iter: {global_step}/{max_train_steps*gradient_accumulation_steps}, Loss: {(average_loss.item()*gradient_accumulation_steps): .4f}, " \
                      f"lr: {lr_scheduler.get_last_lr()}, Data time: {format_time(data_end_time - iter_start_time)}, " \
                      f"Iter time: {format_time(iter_end_time - data_end_time)}, " \
                      f"ETA: {format_time((iter_end_time - iter_start_time) * (max_train_steps*gradient_accumulation_steps - global_step))}, " \
                      f"GPU memory: {gpu_memory: .2f} G"
                logger.info(msg)

            # Save checkpoint
            if is_main_process and (global_step % checkpointing_steps == 0):
                save_path = os.path.join(output_dir, f"checkpoints")
                state_dict = {
                    "epoch": epoch,
                    "global_step": global_step,
                    "flow_encoder_state_dict": pose_adaptor.module.flow_encoder.state_dict(),
                    "attention_processor_state_dict": {k: v for k, v in unet.state_dict().items()
                                                       if k in unet_trainable_param_names},
                    "optimizer_state_dict": optimizer.state_dict()
                }
                torch.save(state_dict, os.path.join(save_path, f"checkpoint-step-{global_step}.ckpt"))
                logger.info(f"Saved state to {save_path} (global_step: {global_step})")

            # Periodically validation
            if is_main_process and (
                    (global_step + 1) % validation_steps == 0 or (global_step + 1) in validation_steps_tuple):

                generator = torch.Generator(device=latents.device)
                generator.manual_seed(global_seed)

                if isinstance(train_data, omegaconf.listconfig.ListConfig):
                    height = train_data[0].sample_size[0] if not isinstance(train_data[0].sample_size, int) else \
                    train_data[0].sample_size
                    width = train_data[0].sample_size[1] if not isinstance(train_data[0].sample_size, int) else \
                    train_data[0].sample_size
                else:
                    height = train_data.kwargs.sample_size[0] if not isinstance(train_data.kwargs.sample_size,
                                                                        int) else train_data.kwargs.sample_size
                    width = train_data.kwargs.sample_size[1] if not isinstance(train_data.kwargs.sample_size,
                                                                        int) else train_data.kwargs.sample_size

                validation_data_iter = iter(validation_dataloader)

                for idx, validation_batch in enumerate(validation_data_iter):
                    if validation_dataset.use_sampled_validation == True and idx >= 16:
                        break
                    # if idx == 11:
                    #     break
                    if first_image_cond:
                        conditioning_images = validation_batch['pixel_values'][:, 0].to(local_rank)
                    else:
                        conditioning_images = validation_batch['condition_image'][:, 0].to(local_rank)      # [b, c, h, w] -1 - 1
                    pixel_values = rearrange(validation_batch['pixel_values'].to(local_rank), "b f c h w -> (b f) c h w")
                    
                    for r_type in runtime_type:
                        
                        flow_generator_input = get_flow_generator_input(condition_image=conditioning_images,
                                                                        pixel_values=None if r_type=='inference' else pixel_values,
                                                                        intrinsics=validation_batch["intrinsics"].to(device=local_rank),
                                                                        c2w=validation_batch["c2w"].to(device=local_rank),
                                                                        runtime_type=r_type)
                        
                        with torch.no_grad():
                            with torch.cuda.amp.autocast(enabled=mixed_precision_training, dtype=torch.bfloat16):
                                sample, flow_log_dict = validation_pipeline(
                                    image=conditioning_images,
                                    flow_generator_input=flow_generator_input,
                                    height=height,
                                    width=width,
                                    num_frames=video_length,
                                    num_inference_steps=num_inference_steps,
                                    min_guidance_scale=min_guidance_scale,
                                    max_guidance_scale=max_guidance_scale,
                                    generator=generator,
                                    output_type='pt',
                                    decode_chunk_size=decode_chunk_size,
                                ) 
                        sample = sample.frames[0].cpu() # [f 3 h w] 0-1

                        save_path = save_dict(validation_batch, conditioning_images, sample, flow_log_dict, output_dir, global_step, idx, r_type)
                    
                    logger.info(f"Saved samples to {save_path}")
            dist.barrier()

            if global_step >= max_train_steps * gradient_accumulation_steps:
                break

    dist.destroy_process_group()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, required=True)
    parser.add_argument("--launcher", type=str, choices=["pytorch", "slurm"], default="pytorch")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()

    name = Path(args.config).stem
    config = OmegaConf.load(args.config)

    main(name=name, launcher=args.launcher, port=args.port, **config)
