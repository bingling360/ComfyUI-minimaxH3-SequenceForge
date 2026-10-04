/* H3 长片导演台 · 配套默认工作流模板（rev 3.3 · 2026-10-02）
 *
 * 由用户导出「新版默认工作流.json」（frontend 1.53.6）重制；按用户截图反馈修三轮：
 * - rev3.3（2026-10-02，用户拍板）：二采 UNET 由 w6a8 换成 int8 混合（与一采同权重）；
 *   二采链新增 Motion Repair 0.25（一采保持 0.6）；二采链**整条出厂忽略**
 *   （mode=4，不进执行图、内存零占用 —— 要二采时框选 ④ 组「取消忽略」再跑）；
 *   导演台「二采放大」出厂模式改为关闭（要二采在右栏开）。
 * - rev3.2 报告线连到「帧率」（真机 1.53.6 两轮实证）：运行时输出槽按 schema 建 4 槽，
 *   但前端会把「未接线的停机场槽」从数组里裁掉 → 3/4 槽随载入时序漂移，输出连线
 *   按位次写本质不可靠。导演台因此新增输出侧按名搬线
 *   （savedOutputIntents / repairMisplacedOutputLinks），把落错的报告线接回来。
 * - rev3.4（2026-10-04）**根治**：光靠载入后搬线不够 —— 每次存档都会把 origin_slot
 *   按当时的位次再写回去，搬线一旦错过时机（前端缓存旧 JS / 载入时序），断连就复现。
 *   真正的根因是「帧率」这个停机场槽夹在「音频」和「报告」之间，把「报告」从 2 号位
 *   挤到 3 号位，而 rev3 之前的存档写的正是 2。现在 nodes.py 把「帧率」排到**末位**，
 *   「报告」钉死在 2 号位 → 隐藏/裁剪末位槽不再挪动可见端口的位次，各版本存档全自洽，
 *   搬线逻辑降级为「只救历史错位文件」的兜底。模板 outputs 随之改 4 槽制
 *   （图像/音频/报告/帧率）、报告线 origin_slot=2。
 *   输入侧由序列化数组重建：模板 6 实槽顺序被采纳，控件槽由前端追加在尾部。
 *   输入侧相反：**输入槽由序列化数组重建**（本模板 6 实槽顺序被采纳，控件槽
 *   由前端追加在尾部，起始视频/起始视频音轨不出现在画布）→ 输入连线写
 *   序列化位次（视频VAE=0/音频VAE=1/模型=2/文本编码器=3/二采模型=4）。
 * - rev3.1 报告线断开：3 输出制口径在真机上同样错位——两轮实证合并为上面的结论。
 * - rev3.1 布局：皮肤把主节点缩成「标题+端口+打开导演台按钮」（mountDeskButton 里
 *   setSize(computeSize())，尺寸运行时重算），分组 ③ 按紧凑形态收紧；
 *   分组 ④（二采链）节点距组顶/组底 ≥50，不再溢出；说明卡挪进初始视野（左列下方）。
 *
 * 链路与参数（同 rev3）：
 * - 一采：UNET int8 混合 → turbo LoRA（步数 8 配套）→ Motion Repair 0.6 →
 *   comfy kitchen 注意力 → Sol-Attn 块稀疏 → 主节点
 * - 二采：UNET int8 混合（rev3.3 起与一采同权重，替代 w6a8）→ Motion Repair 0.25 →
 *   同款注意力 → 主节点「二采模型」槽（高清精化专用）。整条链出厂 mode=4 忽略：
 *   不进执行图、内存零占用；要二采时框选 ④ 组「取消忽略」，再在右栏开「跟随生成」
 * - 链参数出厂值三处同源（nodes.py INPUT_TYPES default / h3_director.js
 *   CHAIN_DEFAULTS / 本模板）：1.0MP、每段时长 8s、步数 8、桥帧门控 关、
 *   接缝重摇 关、审片 关、参考图像尺寸 max；导演台状态 = 锚定双轨 schema
 *   （二采 关闭 · 1.4× / denoise 0.35 / steps 4 / shift 6 / euler+simple，
 *   语义桥开 alpha 0.15，AI 优化 GLM api 预设）。
 * - widget 顺序须与 nodes.py define_schema 严格一致（30 项）。改默认参数：
 *   nodes.py、CHAIN_DEFAULTS、本文件三处同改（见 docs/改动总结_新版默认工作流_2026-10-01.md）。
 */
window.H3_DEFAULT_WORKFLOW = {
  "id": "h3-chain-director-default",
  "revision": 2,
  "last_node_id": 72,
  "last_link_id": 62,
  "nodes": [
    {
      "id": 1,
      "type": "UNETLoader",
      "pos": [
        -980,
        40
      ],
      "size": [
        640,
        90
      ],
      "flags": {},
      "order": 0,
      "mode": 0,
      "inputs": [],
      "outputs": [
        {
          "name": "MODEL",
          "type": "MODEL",
          "links": [
            47
          ]
        }
      ],
      "title": "① 一采 UNET · int8 混合（fl2va+ref2va）",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "UNETLoader"
      },
      "widgets_values": [
        "minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors",
        "default"
      ],
      "widgets_values_named": {
        "unet_name": "minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors",
        "weight_dtype": "default"
      },
      "color": "#3f789e"
    },
    {
      "id": 2,
      "type": "CLIPLoader",
      "pos": [
        -980,
        170
      ],
      "size": [
        640,
        120
      ],
      "flags": {},
      "order": 1,
      "mode": 0,
      "inputs": [],
      "outputs": [
        {
          "name": "CLIP",
          "type": "CLIP",
          "links": [
            2
          ]
        }
      ],
      "title": "② 文本编码器 · Qwen3-VL 32B（nvfp4-awq）",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "CLIPLoader"
      },
      "widgets_values": [
        "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "minimax",
        "default"
      ],
      "widgets_values_named": {
        "clip_name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "type": "minimax",
        "device": "default"
      },
      "color": "#3f789e"
    },
    {
      "id": 3,
      "type": "VAELoader",
      "pos": [
        -980,
        330
      ],
      "size": [
        640,
        70
      ],
      "flags": {},
      "order": 2,
      "mode": 0,
      "inputs": [],
      "outputs": [
        {
          "name": "VAE",
          "type": "VAE",
          "links": [
            3
          ]
        }
      ],
      "title": "③ 视频 VAE · int8",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "VAELoader"
      },
      "widgets_values": [
        "minimax_h3_video_vae_int8_convrot.safetensors"
      ],
      "widgets_values_named": {
        "vae_name": "minimax_h3_video_vae_int8_convrot.safetensors"
      },
      "color": "#3f789e"
    },
    {
      "id": 4,
      "type": "VAELoader",
      "pos": [
        -980,
        440
      ],
      "size": [
        640,
        70
      ],
      "flags": {},
      "order": 3,
      "mode": 0,
      "inputs": [],
      "outputs": [
        {
          "name": "VAE",
          "type": "VAE",
          "links": [
            4
          ]
        }
      ],
      "title": "④ 音频 VAE · fp32",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "VAELoader"
      },
      "widgets_values": [
        "minimax_h3_audio_vae_fp32.safetensors"
      ],
      "widgets_values_named": {
        "vae_name": "minimax_h3_audio_vae_fp32.safetensors"
      },
      "color": "#3f789e"
    },
    {
      "id": 63,
      "type": "LoraLoaderModelOnly",
      "pos": [
        -240,
        40
      ],
      "size": [
        300,
        82
      ],
      "flags": {},
      "order": 4,
      "mode": 0,
      "inputs": [
        {
          "name": "model",
          "type": "MODEL",
          "link": 47
        }
      ],
      "outputs": [
        {
          "name": "MODEL",
          "type": "MODEL",
          "links": [
            48
          ]
        }
      ],
      "title": "⑤ turbo 加速 LoRA（配套少步采样 · 1.0）",
      "properties": {
        "Node name for S&R": "LoraLoaderModelOnly"
      },
      "widgets_values": [
        "minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors",
        1
      ],
      "widgets_values_named": {
        "lora_name": "minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors",
        "strength_model": 1
      },
      "color": "#2a8f6d"
    },
    {
      "id": 64,
      "type": "LoraLoaderModelOnly",
      "pos": [
        100,
        40
      ],
      "size": [
        300,
        82
      ],
      "flags": {},
      "order": 5,
      "mode": 0,
      "inputs": [
        {
          "name": "model",
          "type": "MODEL",
          "link": 48
        }
      ],
      "outputs": [
        {
          "name": "MODEL",
          "type": "MODEL",
          "links": [
            49
          ]
        }
      ],
      "title": "⑥ Motion Repair 运动修复（0.6）",
      "properties": {
        "Node name for S&R": "LoraLoaderModelOnly"
      },
      "widgets_values": [
        "Motion_Repair_V2.safetensors",
        0.6
      ],
      "widgets_values_named": {
        "lora_name": "Motion_Repair_V2.safetensors",
        "strength_model": 0.6
      },
      "color": "#2a8f6d"
    },
    {
      "id": 53,
      "type": "ModelAttentionBackend",
      "pos": [
        440,
        40
      ],
      "size": [
        300,
        58
      ],
      "flags": {},
      "order": 6,
      "mode": 0,
      "inputs": [
        {
          "name": "model",
          "type": "MODEL",
          "link": 49
        }
      ],
      "outputs": [
        {
          "name": "MODEL",
          "type": "MODEL",
          "links": [
            45
          ]
        }
      ],
      "title": "⑦ 注意力后端 · comfy kitchen",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "ModelAttentionBackend"
      },
      "widgets_values": [
        "comfy kitchen attention"
      ],
      "widgets_values_named": {
        "attention": "comfy kitchen attention"
      },
      "color": "#7c5cbf"
    },
    {
      "id": 60,
      "type": "BlockSparseAttention",
      "pos": [
        440,
        140
      ],
      "size": [
        300,
        250
      ],
      "flags": {},
      "order": 7,
      "mode": 0,
      "inputs": [
        {
          "name": "model",
          "type": "MODEL",
          "link": 45
        }
      ],
      "outputs": [
        {
          "name": "model",
          "type": "MODEL",
          "links": [
            46
          ]
        }
      ],
      "title": "⑧ 块稀疏注意力 · Sol-Attn（一采）",
      "properties": {
        "Node name for S&R": "BlockSparseAttention"
      },
      "widgets_values": [
        "sol-attn",
        1.3,
        0.2,
        1,
        "",
        12288,
        256,
        "exact_kv_and_rows",
        false
      ],
      "widgets_values_named": {
        "selection": "sol-attn",
        "selection.tau": 1.3,
        "start_percent": 0.2,
        "end_percent": 1,
        "dense_blocks": "",
        "min_tokens": 12288,
        "extra_tokens": 256,
        "sink_conditioning": "exact_kv_and_rows",
        "verbose": false
      },
      "color": "#7c5cbf"
    },
    {
      "id": 69,
      "type": "UNETLoader",
      "pos": [
        -980,
        610
      ],
      "size": [
        640,
        90
      ],
      "flags": {},
      "order": 8,
      "mode": 4,
      "inputs": [],
      "outputs": [
        {
          "name": "MODEL",
          "type": "MODEL",
          "links": [
            59
          ]
        }
      ],
      "title": "⑨ 二采 UNET · int8 混合（高清精化）",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "UNETLoader"
      },
      "widgets_values": [
        "minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors",
        "default"
      ],
      "widgets_values_named": {
        "unet_name": "minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors",
        "weight_dtype": "default"
      },
      "color": "#b58b2a"
    },
    {
      "id": 70,
      "type": "ModelAttentionBackend",
      "pos": [
        100,
        610
      ],
      "size": [
        300,
        58
      ],
      "flags": {},
      "order": 9,
      "mode": 4,
      "inputs": [
        {
          "name": "model",
          "type": "MODEL",
          "link": 62
        }
      ],
      "outputs": [
        {
          "name": "MODEL",
          "type": "MODEL",
          "links": [
            60
          ]
        }
      ],
      "title": "⑪ 注意力后端（二采）",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "ModelAttentionBackend"
      },
      "widgets_values": [
        "comfy kitchen attention"
      ],
      "widgets_values_named": {
        "attention": "comfy kitchen attention"
      },
      "color": "#7c5cbf"
    },
    {
      "id": 71,
      "type": "BlockSparseAttention",
      "pos": [
        440,
        610
      ],
      "size": [
        300,
        250
      ],
      "flags": {},
      "order": 10,
      "mode": 4,
      "inputs": [
        {
          "name": "model",
          "type": "MODEL",
          "link": 60
        }
      ],
      "outputs": [
        {
          "name": "model",
          "type": "MODEL",
          "links": [
            61
          ]
        }
      ],
      "title": "⑫ 块稀疏注意力 · Sol-Attn（二采）",
      "properties": {
        "Node name for S&R": "BlockSparseAttention"
      },
      "widgets_values": [
        "sol-attn",
        1.3,
        0.2,
        1,
        "",
        12288,
        256,
        "exact_kv_and_rows",
        false
      ],
      "widgets_values_named": {
        "selection": "sol-attn",
        "selection.tau": 1.3,
        "start_percent": 0.2,
        "end_percent": 1,
        "dense_blocks": "",
        "min_tokens": 12288,
        "extra_tokens": 256,
        "sink_conditioning": "exact_kv_and_rows",
        "verbose": false
      },
      "color": "#7c5cbf"
    },
    {
      "id": 10,
      "type": "H3SeamlessChainSampler",
      "pos": [
        840,
        40
      ],
      "size": [
        300,
        240
      ],
      "flags": {},
      "order": 11,
      "mode": 0,
      "inputs": [
        {
          "name": "视频VAE",
          "type": "VAE",
          "link": 3
        },
        {
          "name": "音频VAE",
          "type": "VAE",
          "link": 4
        },
        {
          "name": "模型",
          "shape": 7,
          "type": "MODEL",
          "link": 46
        },
        {
          "name": "文本编码器",
          "shape": 7,
          "type": "CLIP",
          "link": 2
        },
        {
          "name": "二采模型",
          "shape": 7,
          "type": "MODEL",
          "link": 61
        },
        {
          "name": "自定义Sigmas",
          "shape": 7,
          "type": "SIGMAS",
          "link": null
        }
      ],
      "outputs": [
        {
          "name": "图像",
          "type": "IMAGE",
          "links": []
        },
        {
          "name": "音频",
          "type": "AUDIO",
          "links": null
        },
        {
          "name": "报告",
          "type": "STRING",
          "links": [
            58
          ]
        },
        {
          "name": "帧率",
          "type": "INT",
          "links": null
        }
      ],
      "title": "H3 Seamless Chain · 导演台主节点",
      "properties": {
        "aux_id": "bingling360/ComfyUI_H3_SeamlessChain",
        "ver": "731bde31a74ff438381a07c8d647795aed63952c",
        "Node name for S&R": "H3SeamlessChainSampler",
        "__h3_out_intents": [
          {
            "name": "报告",
            "targetId": "54",
            "targetName": "source",
            "targetSlot": 0,
            "type": "STRING"
          }
        ]
      },
      "widgets_values": [
        "16:9",
        1,
        1376,
        768,
        8,
        "22",
        19389496656561,
        "fixed",
        8,
        1,
        "res_multistep",
        "simple",
        "关闭",
        "",
        "关闭",
        30,
        34,
        0,
        "关闭",
        "分段",
        0,
        "关闭",
        0.06,
        1,
        "关闭",
        "文生视频",
        "开启",
        "{\"mode\":\"文生视频\",\"prompts\":[\"\"],\"first_frame\":\"\",\"end_frame\":\"\",\"last_frame\":\"\",\"ref_images\":[],\"ref_assets\":[],\"segments\":[{\"scene_prompt\":\"\",\"character_prompt\":\"\",\"soundscape\":\"\",\"music\":\"\",\"seconds\":null,\"refs\":[],\"frame_img\":null,\"unlink\":false,\"disabled\":false,\"auto_ref\":null,\"auto_seq\":null,\"frame_refs\":null,\"latent_save\":null,\"latent_ref\":null,\"tail_src\":null,\"anchors\":[]}],\"inserts\":[],\"redo_segs\":[],\"upscale\":{\"schema\":2,\"on\":true,\"enlarge\":true,\"mode\":\"关闭\",\"model\":\"minimax_h3_latent_upscaler_3d_fp16.safetensors\",\"arch\":\"auto\",\"scale\":1.4,\"size_mode\":\"倍率\",\"target_w\":1280,\"target_h\":704,\"megapixels\":1,\"denoise\":0.35,\"steps\":4,\"cfg\":1,\"precision\":\"fp16\",\"time_bias\":0,\"mix\":0,\"adaptive\":false,\"shift\":6,\"stg\":0,\"stg_block\":25,\"passes\":1,\"decay\":0.5,\"sharpen\":0,\"pixel_sharpen\":0,\"device\":\"auto\",\"sampler\":\"euler\",\"scheduler\":\"simple\",\"retry\":false,\"retry_target\":0.15,\"include\":[]},\"bridge\":{\"enabled\":true,\"adapter\":\"BUNNY_H3_Semantic_Bridge_V2_seed22345.safetensors\",\"alpha\":0.15,\"scope\":\"all\"},\"optimizer\":{\"mode\":\"api\",\"provider\":\"glm\",\"api_url\":\"https://open.bigmodel.cn/api/paas/v4\",\"api_key\":\"\",\"api_keys\":{},\"model\":\"glm-5.3-flashx\",\"provider_models\":{},\"protocol\":\"openai\",\"read_media\":true,\"local_model\":\"\",\"local_mmproj\":\"\",\"local_device\":\"cuda\",\"max_tokens\":8192,\"timeout\":300,\"thinking\":\"disabled\",\"reasoning_effort\":\"\",\"rule_file\":\"auto\",\"cfg_ver\":3,\"expand\":{\"style\":\"balanced\"}},\"opt_hist\":null}",
        "max",
        1
      ],
      "widgets_values_named": {
        "宽高比": "16:9",
        "百万像素": 1,
        "宽度": 1376,
        "高度": 768,
        "每段时长": 8,
        "引导帧数": "22",
        "种子": 19389496656561,
        "control_after_generate": "fixed",
        "步数": 8,
        "CFG": 1,
        "采样器": "res_multistep",
        "调度器": "simple",
        "自动存档": "关闭",
        "存档目录": "1",
        "桥帧门控": "关闭",
        "清晰度阈值": 30,
        "回退上限": 34,
        "锚定加噪": 0,
        "审片模式": "关闭",
        "自动保存": "分段",
        "重跑起始段": 0,
        "接缝重摇": "关闭",
        "重摇阈值": 0.06,
        "重摇上限": 1,
        "递减锚定": "关闭",
        "生成模式": "文生视频",
        "自动成片": "开启",
        "导演台状态": "{\"mode\":\"文生视频\",\"prompts\":[\"\"],\"first_frame\":\"\",\"end_frame\":\"\",\"last_frame\":\"\",\"ref_images\":[],\"ref_assets\":[],\"segments\":[{\"scene_prompt\":\"\",\"character_prompt\":\"\",\"soundscape\":\"\",\"music\":\"\",\"seconds\":null,\"refs\":[],\"frame_img\":null,\"unlink\":false,\"disabled\":false,\"auto_ref\":null,\"auto_seq\":null,\"frame_refs\":null,\"latent_save\":null,\"latent_ref\":null,\"tail_src\":null,\"anchors\":[]}],\"inserts\":[],\"redo_segs\":[],\"upscale\":{\"schema\":2,\"on\":true,\"enlarge\":true,\"mode\":\"关闭\",\"model\":\"minimax_h3_latent_upscaler_3d_fp16.safetensors\",\"arch\":\"auto\",\"scale\":1.4,\"size_mode\":\"倍率\",\"target_w\":1280,\"target_h\":704,\"megapixels\":1,\"denoise\":0.35,\"steps\":4,\"cfg\":1,\"precision\":\"fp16\",\"time_bias\":0,\"mix\":0,\"adaptive\":false,\"shift\":6,\"stg\":0,\"stg_block\":25,\"passes\":1,\"decay\":0.5,\"sharpen\":0,\"pixel_sharpen\":0,\"device\":\"auto\",\"sampler\":\"euler\",\"scheduler\":\"simple\",\"retry\":false,\"retry_target\":0.15,\"include\":[]},\"bridge\":{\"enabled\":true,\"adapter\":\"BUNNY_H3_Semantic_Bridge_V2_seed22345.safetensors\",\"alpha\":0.15,\"scope\":\"all\"},\"optimizer\":{\"mode\":\"api\",\"provider\":\"glm\",\"api_url\":\"https://open.bigmodel.cn/api/paas/v4\",\"api_key\":\"\",\"api_keys\":{},\"model\":\"glm-5.3-flashx\",\"provider_models\":{},\"protocol\":\"openai\",\"read_media\":true,\"local_model\":\"\",\"local_mmproj\":\"\",\"local_device\":\"cuda\",\"max_tokens\":8192,\"timeout\":300,\"thinking\":\"disabled\",\"reasoning_effort\":\"\",\"rule_file\":\"auto\",\"cfg_ver\":3,\"expand\":{\"style\":\"balanced\"}},\"opt_hist\":null}",
        "参考图像尺寸": "max",
        "响度对齐强度": 1
      }
    },
    {
      "id": 54,
      "type": "PreviewAny",
      "pos": [
        1180,
        40
      ],
      "size": [
        260,
        200
      ],
      "flags": {},
      "order": 12,
      "mode": 0,
      "inputs": [
        {
          "name": "source",
          "type": "*",
          "link": 58
        }
      ],
      "outputs": [
        {
          "name": "STRING",
          "type": "STRING",
          "links": null
        }
      ],
      "title": "运行报告",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "PreviewAny"
      },
      "widgets_values": [],
      "widgets_values_named": {}
    },
    {
      "id": 40,
      "type": "MarkdownNote",
      "pos": [
        -1010,
        960
      ],
      "size": [
        780,
        680
      ],
      "flags": {},
      "order": 13,
      "mode": 0,
      "inputs": [],
      "outputs": [],
      "title": "导演台使用说明",
      "properties": {},
      "widgets_values": [
        "# H3 长片导演台 · 配套默认工作流（2026-10-02 版）\n\n生成全在左侧「长片导演台」侧栏：提示词 / 素材 / 链参数一体化，画布零连线操作。\n- 提示词走导演台状态，1–64 段不限：顶部选段条「＋」加段；素材在三库面板（项目资产 / 全局库 / 成片）拖放，或正文 @素材名 引用\n- 每段自动存 output/h3_projects/<项目名>/（seg_NNN.mp4 + 缩略图）；「自动成片=开启」另编码完整成片，段卡片直接预览播放\n- 链路自动推导（无模式选择）：段里引用素材即走 ref conditioning；纯文生链走 fl2va\n\n**一采链（上排，左→右）**：UNET int8 混合权重（fl2va+ref2va）→ turbo 加速 LoRA（配套少步采样，本模板步数 8）→ Motion Repair 运动修复 0.6 → comfy kitchen 注意力 → Sol-Attn 块稀疏注意力（≥12288 token 生效、0.2 起生效）→ 主节点\n\n**二采链（下排）**：UNET int8 混合（与一采同权重）→ Motion Repair 0.25 → 同款注意力 → 主节点「二采模型」槽，专做高清精化二采（换权重不影响一采与接缝重摇）。**出厂整条为「忽略」态（mode=4）**：不进执行图、内存零占用；要二采时框选 ④ 组「取消忽略」，并在右栏把二采拨回「跟随生成」\n\n**出厂链参数**（= 右栏「↺ 恢复默认」基准，与 nodes.py 默认值同源）：\n- 16:9 · 1.0MP（1376×768）｜每段时长 8s｜步数 8｜CFG 1｜res_multistep / simple\n- 审片模式 关｜自动保存 分段｜自动成片 开｜参考图像尺寸 max（身份保真优先，参考管线 2048 短边，较慢）\n- 检测重摇默认全关（桥帧门控 / 接缝重摇）——要自动排坏段，到「视频延续 · 检测重摇」里打开\n- 右栏「二采放大」预置：关闭（要用时在右栏开「跟随生成」）· 1.4× · 去噪 0.35 · 精化 4 步 · shift 6 · euler/simple\n- 语义桥预置：开 · BUNNY V2 · alpha 0.15 · 全量过桥；AI 优化：GLM 预设（open.bigmodel.cn · glm-5.3-flashx，Key 在「AI 优化设置」自填）\n\n小字：「宽度/高度」是旧版兼容位（画布由 宽高比×百万像素 换算，改它们不生效）；自定义Sigmas 槽默认空置（接少步 sigma 表覆盖「步数/调度器」）；序章（上传视频当第 1 段）走「起始视频」端口，导演台默认把它收起，需要时载入含序章连线的旧工作流即可恢复；顶栏「↺ 全局重置」一键回出厂（链参数 + 语义桥 + 二采；性能优化与 AI 优化设置各有自己的恢复默认）。"
      ],
      "widgets_values_named": {
        "text": "# H3 长片导演台 · 配套默认工作流（2026-10-02 版）\n\n生成全在左侧「长片导演台」侧栏：提示词 / 素材 / 链参数一体化，画布零连线操作。\n- 提示词走导演台状态，1–64 段不限：顶部选段条「＋」加段；素材在三库面板（项目资产 / 全局库 / 成片）拖放，或正文 @素材名 引用\n- 每段自动存 output/h3_projects/<项目名>/（seg_NNN.mp4 + 缩略图）；「自动成片=开启」另编码完整成片，段卡片直接预览播放\n- 链路自动推导（无模式选择）：段里引用素材即走 ref conditioning；纯文生链走 fl2va\n\n**一采链（上排，左→右）**：UNET int8 混合权重（fl2va+ref2va）→ turbo 加速 LoRA（配套少步采样，本模板步数 8）→ Motion Repair 运动修复 0.6 → comfy kitchen 注意力 → Sol-Attn 块稀疏注意力（≥12288 token 生效、0.2 起生效）→ 主节点\n\n**二采链（下排）**：UNET int8 混合（与一采同权重）→ Motion Repair 0.25 → 同款注意力 → 主节点「二采模型」槽，专做高清精化二采（换权重不影响一采与接缝重摇）。**出厂整条为「忽略」态（mode=4）**：不进执行图、内存零占用；要二采时框选 ④ 组「取消忽略」，并在右栏把二采拨回「跟随生成」\n\n**出厂链参数**（= 右栏「↺ 恢复默认」基准，与 nodes.py 默认值同源）：\n- 16:9 · 1.0MP（1376×768）｜每段时长 8s｜步数 8｜CFG 1｜res_multistep / simple\n- 审片模式 关｜自动保存 分段｜自动成片 开｜参考图像尺寸 max（身份保真优先，参考管线 2048 短边，较慢）\n- 检测重摇默认全关（桥帧门控 / 接缝重摇）——要自动排坏段，到「视频延续 · 检测重摇」里打开\n- 右栏「二采放大」预置：关闭（要用时在右栏开「跟随生成」）· 1.4× · 去噪 0.35 · 精化 4 步 · shift 6 · euler/simple\n- 语义桥预置：开 · BUNNY V2 · alpha 0.15 · 全量过桥；AI 优化：GLM 预设（open.bigmodel.cn · glm-5.3-flashx，Key 在「AI 优化设置」自填）\n\n小字：「宽度/高度」是旧版兼容位（画布由 宽高比×百万像素 换算，改它们不生效）；自定义Sigmas 槽默认空置（接少步 sigma 表覆盖「步数/调度器」）；序章（上传视频当第 1 段）走「起始视频」端口，导演台默认把它收起，需要时载入含序章连线的旧工作流即可恢复；顶栏「↺ 全局重置」一键回出厂（链参数 + 语义桥 + 二采；性能优化与 AI 优化设置各有自己的恢复默认）。"
      },
      "color": "#432",
      "bgcolor": "#653"
    },
    {
      "id": 72,
      "type": "LoraLoaderModelOnly",
      "pos": [
        -300,
        610
      ],
      "size": [
        300,
        82
      ],
      "flags": {},
      "order": 9,
      "mode": 4,
      "inputs": [
        {
          "name": "model",
          "type": "MODEL",
          "link": 59
        }
      ],
      "outputs": [
        {
          "name": "MODEL",
          "type": "MODEL",
          "links": [
            62
          ]
        }
      ],
      "title": "⑩ Motion Repair · 运动修复（二采 0.25）",
      "properties": {
        "Node name for S&R": "LoraLoaderModelOnly"
      },
      "widgets_values": [
        "Motion_Repair_V2.safetensors",
        0.25
      ],
      "widgets_values_named": {
        "lora_name": "Motion_Repair_V2.safetensors",
        "strength_model": 0.25
      },
      "color": "#2a8f6d"
    }
  ],
  "links": [
    [
      2,
      2,
      0,
      10,
      3,
      "CLIP"
    ],
    [
      3,
      3,
      0,
      10,
      0,
      "VAE"
    ],
    [
      4,
      4,
      0,
      10,
      1,
      "VAE"
    ],
    [
      46,
      60,
      0,
      10,
      2,
      "MODEL"
    ],
    [
      47,
      1,
      0,
      63,
      0,
      "MODEL"
    ],
    [
      48,
      63,
      0,
      64,
      0,
      "MODEL"
    ],
    [
      49,
      64,
      0,
      53,
      0,
      "MODEL"
    ],
    [
      45,
      53,
      0,
      60,
      0,
      "MODEL"
    ],
    [
      58,
      10,
      2,
      54,
      0,
      "STRING"
    ],
    [
      59,
      69,
      0,
      72,
      0,
      "MODEL"
    ],
    [
      60,
      70,
      0,
      71,
      0,
      "MODEL"
    ],
    [
      61,
      71,
      0,
      10,
      4,
      "MODEL"
    ],
    [
      62,
      72,
      0,
      70,
      0,
      "MODEL"
    ]
  ],
  "groups": [
    {
      "id": 1,
      "title": "① 模型加载",
      "bounding": [
        -1010,
        -10,
        700,
        540
      ],
      "color": "#3f789e",
      "flags": {}
    },
    {
      "id": 2,
      "title": "② 一采加速链（LoRA × 注意力）",
      "bounding": [
        -270,
        -10,
        1060,
        420
      ],
      "color": "#2a8f6d",
      "flags": {}
    },
    {
      "id": 3,
      "title": "③ 导演台主节点",
      "bounding": [
        810,
        0,
        690,
        300
      ],
      "color": "#a1309b",
      "flags": {}
    },
    {
      "id": 4,
      "title": "④ 二采模型链（高清精化）",
      "bounding": [
        -1010,
        560,
        1780,
        350
      ],
      "color": "#b58b2a",
      "flags": {}
    }
  ],
  "config": {},
  "extra": {
    "ds": {
      "scale": 0.5,
      "offset": [
        570,
        10
      ]
    },
    "frontendVersion": "1.53.6"
  },
  "version": 0.4
};
