/* H3 长片导演台 · 配套默认工作流模板（rev 3.6 · 2026-10-12）
 *
 * 由用户导出「初始化导演台工作流.json」（frontend 1.53.6）重制：
 * - 一采链新增两件：turbo LoRA 换成 ref2v 8step 版（0.75）→ 追加 H3_Combat_V2 战斗 LoRA（0.6）；
 *   Motion Repair 一采降到 0.3；「一采自定义Sigmas」默认接一张 10 步手工 sigma 表（ManualSigmas，
 *   11 个点）—— 接了它一采的「步数 / 调度器」由表接管（表就是 10 步），拔线即回落到节点控件。
 * - rev3.6：一采「块稀疏注意力 · Sol-Attn」出厂改为**忽略**（mode=4，旁路）—— 一采只走
 *   comfy kitchen 注意力，Sol-Attn 不进执行图（参数与二采那份同款：sol-attn / tau 1.3 /
 *   0.2 起生效 / ≥12288 token）；要启用右键该节点「取消忽略」。
 * - 链参数出厂值三处同源（nodes.py INPUT_TYPES default / h3_director.js CHAIN_DEFAULTS / 本模板）：
 *   1.0MP、每段时长 8s、**步数 10**、**采样器 er_sde**、桥帧门控 关、接缝重摇 关、审片 关、
 *   参考图像尺寸 match；性能优化编码质量档 crf 默认 19（perf.py DEFAULT_PERF["x264_crf"]）。
 * - 导演台状态 = 锚定双轨 schema（二采 关闭 · 1.4× / denoise 0.35 / steps 4 / shift 6 /
 *   euler+simple，语义桥关 alpha 0.1，AI 优化 GLM api 预设）。
 *
 * 端口口径（rev3.4 起，真机 1.53.6 两轮实证后钉死，勿动）：
 * - 输入侧由序列化数组重建：7 实槽顺序 视频VAE=0/音频VAE=1/模型=2/文本编码器=3/
 *   二采模型=4/一采自定义Sigmas=5/二采自定义Sigmas=6；控件槽由前端追加在尾部；
 *   起始视频/起始视频音轨不出现在画布。
 * - 输出侧按 schema 建 4 槽（图像/音频/报告/帧率）；「报告」钉死在 2 号位，
 *   停机场槽「帧率」排末位 —— 位次=连线坐标，报告线 origin_slot=2，与历史存档自洽。
 *
 * 布局：四分组 ① 模型加载（蓝）② 一采加速链（青绿）③ 导演台主节点（品红）
 *   ④ 二采模型链（琥珀）；节点按链路着色；标题 ①–⑭ 标链路顺序；
 *   说明卡在左列下方（-1010, 960），初始视野 scale 0.5 / offset [570,10]。
 *
 * widget 顺序须与 nodes.py define_schema 严格一致（30 项）。改默认参数：
 *   nodes.py、CHAIN_DEFAULTS、本文件三处同改（见 docs/改动总结_新版默认工作流_2026-10-01.md）。
 */
window.H3_DEFAULT_WORKFLOW = {
  "id": "h3-chain-director-default",
  "revision": 2,
  "last_node_id": 74,
  "last_link_id": 70,
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
      "title": "⑤ turbo 加速 LoRA（少步配套 · 0.75）",
      "properties": {
        "Node name for S&R": "LoraLoaderModelOnly"
      },
      "widgets_values": [
        "minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors",
        0.75
      ],
      "widgets_values_named": {
        "lora_name": "minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors",
        "strength_model": 0.75
      },
      "color": "#2a8f6d"
    },
    {
      "id": 73,
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
      "title": "⑥ 战斗 LoRA · H3_Combat_V2（0.6）",
      "properties": {
        "Node name for S&R": "LoraLoaderModelOnly"
      },
      "widgets_values": [
        "H3_Combat_V2.safetensors",
        0.6
      ],
      "widgets_values_named": {
        "lora_name": "H3_Combat_V2.safetensors",
        "strength_model": 0.6
      },
      "color": "#2a8f6d"
    },
    {
      "id": 64,
      "type": "LoraLoaderModelOnly",
      "pos": [
        440,
        40
      ],
      "size": [
        300,
        82
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
            50
          ]
        }
      ],
      "title": "⑦ Motion Repair 运动修复（一采 0.3）",
      "properties": {
        "Node name for S&R": "LoraLoaderModelOnly"
      },
      "widgets_values": [
        "Motion_Repair_V2.safetensors",
        0.3
      ],
      "widgets_values_named": {
        "lora_name": "Motion_Repair_V2.safetensors",
        "strength_model": 0.3
      },
      "color": "#2a8f6d"
    },
    {
      "id": 53,
      "type": "ModelAttentionBackend",
      "pos": [
        780,
        40
      ],
      "size": [
        300,
        58
      ],
      "flags": {},
      "order": 7,
      "mode": 0,
      "inputs": [
        {
          "name": "model",
          "type": "MODEL",
          "link": 50
        }
      ],
      "outputs": [
        {
          "name": "model",
          "type": "MODEL",
          "links": [
            45
          ]
        }
      ],
      "title": "⑧ 注意力后端 · comfy kitchen",
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
        780,
        140
      ],
      "size": [
        300,
        250
      ],
      "flags": {},
      "order": 8,
      "mode": 4,
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
      "title": "⑨ 块稀疏注意力 · Sol-Attn（一采 · 出厂忽略）",
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
      "id": 74,
      "type": "ManualSigmas",
      "pos": [
        780,
        440
      ],
      "size": [
        270,
        58
      ],
      "flags": {},
      "order": 9,
      "mode": 0,
      "inputs": [],
      "outputs": [
        {
          "name": "SIGMAS",
          "type": "SIGMAS",
          "links": [
            70
          ]
        }
      ],
      "title": "⑩ 一采少步 Sigma 表（10 步）",
      "properties": {
        "Node name for S&R": "ManualSigmas"
      },
      "widgets_values": [
        "1, 0.9908, 0.9765, 0.959, 0.937, 0.91, 0.86, 0.78, 0.55, 0.24, 0"
      ],
      "widgets_values_named": {
        "sigmas": "1, 0.9908, 0.9765, 0.959, 0.937, 0.91, 0.86, 0.78, 0.55, 0.24, 0"
      }
    },
    {
      "id": 10,
      "type": "H3SeamlessChainSampler",
      "pos": [
        1270,
        40
      ],
      "size": [
        300,
        240
      ],
      "flags": {},
      "order": 14,
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
          "link": 65
        },
        {
          "name": "一采自定义Sigmas",
          "shape": 7,
          "type": "SIGMAS",
          "link": 70
        },
        {
          "name": "二采自定义Sigmas",
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
        10,
        1,
        "er_sde",
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
        "{\"mode\":\"文生视频\",\"prompts\":[\"\"],\"first_frame\":\"\",\"end_frame\":\"\",\"last_frame\":\"\",\"ref_images\":[],\"ref_assets\":[],\"segments\":[{\"scene_prompt\":\"\",\"character_prompt\":\"\",\"soundscape\":\"\",\"music\":\"\",\"seconds\":null,\"refs\":[],\"frame_img\":null,\"unlink\":false,\"disabled\":false,\"auto_ref\":null,\"auto_seq\":null,\"frame_refs\":null,\"latent_save\":null,\"latent_ref\":null,\"tail_src\":null,\"anchors\":[]}],\"inserts\":[],\"redo_segs\":[],\"upscale\":{\"schema\":2,\"on\":true,\"enlarge\":true,\"mode\":\"关闭\",\"model\":\"minimax_h3_latent_upscaler_3d_fp16.safetensors\",\"arch\":\"auto\",\"scale\":1.4,\"size_mode\":\"倍率\",\"target_w\":1280,\"target_h\":704,\"megapixels\":1,\"denoise\":0.35,\"steps\":4,\"cfg\":1,\"precision\":\"fp16\",\"mix\":0,\"adaptive\":false,\"shift\":6,\"stg\":0,\"stg_block\":25,\"passes\":1,\"decay\":0.5,\"sharpen\":0,\"pixel_sharpen\":0,\"device\":\"auto\",\"sampler\":\"euler\",\"scheduler\":\"simple\",\"retry\":false,\"retry_target\":0.15,\"include\":[]},\"bridge\":{\"enabled\":false,\"adapter\":\"BUNNY_H3_Semantic_Bridge_V2_seed22345.safetensors\",\"alpha\":0.1,\"scope\":\"all\"},\"optimizer\":{\"mode\":\"api\",\"provider\":\"glm\",\"api_url\":\"https://open.bigmodel.cn/api/paas/v4\",\"api_key\":\"\",\"api_keys\":{},\"model\":\"glm-5.3-flashx\",\"provider_models\":{},\"protocol\":\"openai\",\"read_media\":true,\"local_model\":\"\",\"local_mmproj\":\"\",\"local_device\":\"cuda\",\"max_tokens\":8192,\"timeout\":300,\"thinking\":\"disabled\",\"reasoning_effort\":\"\",\"rule_file\":\"auto\",\"cfg_ver\":3,\"expand\":{\"style\":\"balanced\"}},\"opt_hist\":null}",
        "match",
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
        "步数": 10,
        "CFG": 1,
        "采样器": "er_sde",
        "调度器": "simple",
        "自动存档": "关闭",
        "存档目录": "",
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
        "导演台状态": "{\"mode\":\"文生视频\",\"prompts\":[\"\"],\"first_frame\":\"\",\"end_frame\":\"\",\"last_frame\":\"\",\"ref_images\":[],\"ref_assets\":[],\"segments\":[{\"scene_prompt\":\"\",\"character_prompt\":\"\",\"soundscape\":\"\",\"music\":\"\",\"seconds\":null,\"refs\":[],\"frame_img\":null,\"unlink\":false,\"disabled\":false,\"auto_ref\":null,\"auto_seq\":null,\"frame_refs\":null,\"latent_save\":null,\"latent_ref\":null,\"tail_src\":null,\"anchors\":[]}],\"inserts\":[],\"redo_segs\":[],\"upscale\":{\"schema\":2,\"on\":true,\"enlarge\":true,\"mode\":\"关闭\",\"model\":\"minimax_h3_latent_upscaler_3d_fp16.safetensors\",\"arch\":\"auto\",\"scale\":1.4,\"size_mode\":\"倍率\",\"target_w\":1280,\"target_h\":704,\"megapixels\":1,\"denoise\":0.35,\"steps\":4,\"cfg\":1,\"precision\":\"fp16\",\"mix\":0,\"adaptive\":false,\"shift\":6,\"stg\":0,\"stg_block\":25,\"passes\":1,\"decay\":0.5,\"sharpen\":0,\"pixel_sharpen\":0,\"device\":\"auto\",\"sampler\":\"euler\",\"scheduler\":\"simple\",\"retry\":false,\"retry_target\":0.15,\"include\":[]},\"bridge\":{\"enabled\":false,\"adapter\":\"BUNNY_H3_Semantic_Bridge_V2_seed22345.safetensors\",\"alpha\":0.1,\"scope\":\"all\"},\"optimizer\":{\"mode\":\"api\",\"provider\":\"glm\",\"api_url\":\"https://open.bigmodel.cn/api/paas/v4\",\"api_key\":\"\",\"api_keys\":{},\"model\":\"glm-5.3-flashx\",\"provider_models\":{},\"protocol\":\"openai\",\"read_media\":true,\"local_model\":\"\",\"local_mmproj\":\"\",\"local_device\":\"cuda\",\"max_tokens\":8192,\"timeout\":300,\"thinking\":\"disabled\",\"reasoning_effort\":\"\",\"rule_file\":\"auto\",\"cfg_ver\":3,\"expand\":{\"style\":\"balanced\"}},\"opt_hist\":null}",
        "参考图像尺寸": "match",
        "响度对齐强度": 1
      }
    },
    {
      "id": 54,
      "type": "PreviewAny",
      "pos": [
        1610,
        40
      ],
      "size": [
        260,
        200
      ],
      "flags": {},
      "order": 15,
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
      "order": 10,
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
      "title": "⑪ 二采 UNET · int8 混合（高清精化）",
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
      "order": 11,
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
      "title": "⑫ Motion Repair · 运动修复（二采 0.25）",
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
      "order": 12,
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
          "name": "model",
          "type": "MODEL",
          "links": [
            60
          ]
        }
      ],
      "title": "⑬ 注意力后端（二采）",
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
      "order": 13,
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
            65
          ]
        }
      ],
      "title": "⑭ 块稀疏注意力 · Sol-Attn（二采）",
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
      "order": 16,
      "mode": 0,
      "inputs": [],
      "outputs": [],
      "title": "导演台使用说明",
      "properties": {},
      "widgets_values": [
        "# H3 长片导演台 · 配套默认工作流（2026-10-12 版）\n\n生成全在左侧「长片导演台」侧栏：提示词 / 素材 / 链参数一体化，画布零连线操作。\n- 提示词走导演台状态，1–64 段不限：顶部选段条「＋」加段；素材在三库面板（项目资产 / 全局库 / 成片）拖放，或正文 @素材名 引用\n- 每段自动存 output/h3_projects/<项目名>/（seg_NNN.mp4 + 缩略图）；「自动成片=开启」另编码完整成片，段卡片直接预览播放\n- 链路自动推导（无模式选择）：段里引用素材即走 ref conditioning；纯文生链走 fl2va\n\n**一采链（上排，左→右）**：UNET int8 混合权重（fl2va+ref2va）→ turbo 加速 LoRA（少步配套 · 0.75）→ H3_Combat_V2 战斗 LoRA（0.6）→ Motion Repair 运动修复（0.3）→ comfy kitchen 注意力 → Sol-Attn 块稀疏注意力 → 主节点\n\n**Sol-Attn 出厂「忽略」**：一采的块稀疏注意力节点默认 mode=4（旁路）—— 不进执行图，一采只走 comfy kitchen 注意力。要启用：右键该节点「取消忽略」（或框选后按 Ctrl+B），参数仍是 sol-attn / tau 1.3 / 0.2 起生效 / ≥12288 token\n\n**一采 Sigma 表**：主节点「一采自定义Sigmas」默认接一张 10 步手工 sigma 表（ManualSigmas，11 个点）。接了它，一采的「步数 / 调度器」由表接管（表本身就是 10 步）；拔掉这条线即回落到节点上的「步数 / 采样器」\n\n**二采链（下排）**：UNET int8 混合（与一采同权重）→ Motion Repair 0.25 → 同款注意力 → 主节点「二采模型」槽，专做高清精化二采（换权重不影响一采与接缝重摇）。**出厂整条为「忽略」态（mode=4）**：不进执行图、内存零占用；要二采时框选 ④ 组「取消忽略」，并在右栏把二采拨回「跟随生成」\n\n**出厂链参数**（= 右栏「↺ 恢复默认」基准，与 nodes.py 默认值同源）：\n- 16:9 · 1.0MP（1376×768）｜每段时长 8s｜步数 10｜CFG 1｜er_sde / simple\n- 审片模式 关｜每段自动落盘（自动保存=分段）｜自动成片 开｜参考图像尺寸 match（按画幅像素面积等比缩小，省显存与时间）\n- 检测重摇默认全关（桥帧门控 / 接缝重摇）——要自动排坏段，到「视频延续 · 检测重摇」里打开\n- 右栏「二采放大」预置：关闭（要用时在右栏开「跟随生成」）· 1.4× · 去噪 0.35 · 精化 4 步 · shift 6 · euler/simple\n- 语义桥预置：关 · BUNNY V2 · alpha 0.1 · 全量过桥（要用时在右栏「语义桥」打开）；AI 优化：GLM 预设（open.bigmodel.cn · glm-5.3-flashx，Key 在「AI 优化设置」自填）\n- 性能优化：编码 libx264 · 质量档 crf 19 · 高清编码档 开（medium preset + 暗部 aq + 抗条纹抖动）\n\n小字：「宽度/高度」是旧版兼容位（画布由 宽高比×百万像素 换算，改它们不生效）；「二采自定义Sigmas」槽默认空置（接表即覆盖二采「去噪/精化步数/调度器」）；序章（上传视频当第 1 段）走「起始视频」端口，导演台默认把它收起，需要时载入含序章连线的旧工作流即可恢复；顶栏「↺ 全局重置」一键回出厂（链参数 + 语义桥 + 二采 + 性能优化；AI 优化设置有自己的恢复默认）。"
      ],
      "widgets_values_named": {
        "text": "# H3 长片导演台 · 配套默认工作流（2026-10-12 版）\n\n生成全在左侧「长片导演台」侧栏：提示词 / 素材 / 链参数一体化，画布零连线操作。\n- 提示词走导演台状态，1–64 段不限：顶部选段条「＋」加段；素材在三库面板（项目资产 / 全局库 / 成片）拖放，或正文 @素材名 引用\n- 每段自动存 output/h3_projects/<项目名>/（seg_NNN.mp4 + 缩略图）；「自动成片=开启」另编码完整成片，段卡片直接预览播放\n- 链路自动推导（无模式选择）：段里引用素材即走 ref conditioning；纯文生链走 fl2va\n\n**一采链（上排，左→右）**：UNET int8 混合权重（fl2va+ref2va）→ turbo 加速 LoRA（少步配套 · 0.75）→ H3_Combat_V2 战斗 LoRA（0.6）→ Motion Repair 运动修复（0.3）→ comfy kitchen 注意力 → Sol-Attn 块稀疏注意力 → 主节点\n\n**Sol-Attn 出厂「忽略」**：一采的块稀疏注意力节点默认 mode=4（旁路）—— 不进执行图，一采只走 comfy kitchen 注意力。要启用：右键该节点「取消忽略」（或框选后按 Ctrl+B），参数仍是 sol-attn / tau 1.3 / 0.2 起生效 / ≥12288 token\n\n**一采 Sigma 表**：主节点「一采自定义Sigmas」默认接一张 10 步手工 sigma 表（ManualSigmas，11 个点）。接了它，一采的「步数 / 调度器」由表接管（表本身就是 10 步）；拔掉这条线即回落到节点上的「步数 / 采样器」\n\n**二采链（下排）**：UNET int8 混合（与一采同权重）→ Motion Repair 0.25 → 同款注意力 → 主节点「二采模型」槽，专做高清精化二采（换权重不影响一采与接缝重摇）。**出厂整条为「忽略」态（mode=4）**：不进执行图、内存零占用；要二采时框选 ④ 组「取消忽略」，并在右栏把二采拨回「跟随生成」\n\n**出厂链参数**（= 右栏「↺ 恢复默认」基准，与 nodes.py 默认值同源）：\n- 16:9 · 1.0MP（1376×768）｜每段时长 8s｜步数 10｜CFG 1｜er_sde / simple\n- 审片模式 关｜每段自动落盘（自动保存=分段）｜自动成片 开｜参考图像尺寸 match（按画幅像素面积等比缩小，省显存与时间）\n- 检测重摇默认全关（桥帧门控 / 接缝重摇）——要自动排坏段，到「视频延续 · 检测重摇」里打开\n- 右栏「二采放大」预置：关闭（要用时在右栏开「跟随生成」）· 1.4× · 去噪 0.35 · 精化 4 步 · shift 6 · euler/simple\n- 语义桥预置：关 · BUNNY V2 · alpha 0.1 · 全量过桥（要用时在右栏「语义桥」打开）；AI 优化：GLM 预设（open.bigmodel.cn · glm-5.3-flashx，Key 在「AI 优化设置」自填）\n- 性能优化：编码 libx264 · 质量档 crf 19 · 高清编码档 开（medium preset + 暗部 aq + 抗条纹抖动）\n\n小字：「宽度/高度」是旧版兼容位（画布由 宽高比×百万像素 换算，改它们不生效）；「二采自定义Sigmas」槽默认空置（接表即覆盖二采「去噪/精化步数/调度器」）；序章（上传视频当第 1 段）走「起始视频」端口，导演台默认把它收起，需要时载入含序章连线的旧工作流即可恢复；顶栏「↺ 全局重置」一键回出厂（链参数 + 语义桥 + 二采 + 性能优化；AI 优化设置有自己的恢复默认）。"
      },
      "color": "#432",
      "bgcolor": "#653"
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
      73,
      0,
      "MODEL"
    ],
    [
      49,
      73,
      0,
      64,
      0,
      "MODEL"
    ],
    [
      50,
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
      62,
      72,
      0,
      70,
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
      65,
      71,
      0,
      10,
      4,
      "MODEL"
    ],
    [
      70,
      74,
      0,
      10,
      5,
      "SIGMAS"
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
      "title": "② 一采加速链（LoRA × 注意力 × Sigma 表）",
      "bounding": [
        -270,
        -10,
        1440,
        520
      ],
      "color": "#2a8f6d",
      "flags": {}
    },
    {
      "id": 3,
      "title": "③ 导演台主节点",
      "bounding": [
        1210,
        0,
        700,
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
