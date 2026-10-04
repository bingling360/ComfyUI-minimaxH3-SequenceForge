#!/bin/bash
# hf-mirror lane downloads (multi-connection, resumable)
cd /root/ComfyUI/models || exit 1
mkdir -p diffusion_models loras

cat > /tmp/hfurls.txt <<'EOF'
https://hf-mirror.com/smhfacct/Minimax-H3-fl2va-ref2va-hybrid-models/resolve/main/minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors
  out=diffusion_models/minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors
https://hf-mirror.com/binglingzhimeng/minimax_h3_hybrid_fl2va_ref2va_b25-49_w6a8/resolve/main/minimax_h3_hybrid_fl2va_ref2va_b25-49_w6a8.safetensors
  out=diffusion_models/minimax_h3_hybrid_fl2va_ref2va_b25-49_w6a8.safetensors
https://hf-mirror.com/Momoking/MiniMax-H3-Turbo-Lora-ComfyUI/resolve/main/minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors
  out=loras/minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors
https://hf-mirror.com/JOKER141/MiniMax-H3-General-Motion-Continuity-Repair/resolve/main/Motion_Repair_V2.safetensors
  out=loras/Motion_Repair_V2.safetensors
EOF

aria2c -j4 -x16 -s16 -k4M -c --file-allocation=none --summary-interval=30 \
  --console-log-level=warn --max-tries=0 --retry-wait=5 -i /tmp/hfurls.txt

echo "HF_LANE_DONE $(date)"
