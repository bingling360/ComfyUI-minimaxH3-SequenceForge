#!/bin/bash
# ModelScope fast-lane downloads (domestic ~17MB/s)
M="https://modelscope.cn/models"
cd /root/ComfyUI/models || exit 1

mkdir -p text_encoders vae latent_upscale_models loras diffusion_models

aria2c -j2 -x8 -s8 -k4M -c --file-allocation=none --summary-interval=30 \
  --console-log-level=warn --max-tries=0 --retry-wait=5 \
  -d text_encoders -o qwen3vl_32b_minimax_h3_int8_convrot.safetensors \
  "$M/Comfy-Org/MiniMax-H3/resolve/master/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors"

aria2c -j2 -x8 -s8 -k4M -c --file-allocation=none --summary-interval=30 \
  --console-log-level=warn --max-tries=0 --retry-wait=5 \
  -d vae \
  "$M/Comfy-Org/MiniMax-H3/resolve/master/vae/minimax_h3_video_vae_int8_convrot.safetensors" \
  "$M/Comfy-Org/MiniMax-H3/resolve/master/vae/minimax_h3_audio_vae_fp32.safetensors"

aria2c -x8 -s8 -k4M -c --file-allocation=none --summary-interval=30 \
  --console-log-level=warn --max-tries=0 --retry-wait=5 \
  -d latent_upscale_models -o minimax_h3_latent_upscaler_3d_fp16.safetensors \
  "$M/LBH-123-AI/Minimax_h3_latent_Upscaler/resolve/master/minimax_h3_latent_upscaler_3d_conv_v1/minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors"

echo "MS_LANE_DONE $(date)"
