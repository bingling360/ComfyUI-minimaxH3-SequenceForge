/* H3 长片导演台 · 配套默认工作流模板
 *
 * 由「长视频接续二采导演台工作流.json」导出生成（D:/Downloads）：
 * - 模型加载器（ref2va UNET / Qwen3-VL CLIP / 视频+音频 VAE）+ 主节点（导演台模式）
 * - 提示词与素材全走导演台状态 + H3AssetBundle 单线，画布无任何外联
 *   （提示词 1–64 段不限，全在导演台管理）
 * - 每段视频由主节点「自动保存=分段」存进项目文件夹 output/h3_projects/<项目名>/，
 *   主节点「自动成片=开启」时另编码完整成片（final_*.mp4）落同一文件夹；
 *   不再依赖 H3ChainSaver 节点（成片保存由主节点一体化完成）
 * - 资产走 H3AssetBundle 单线：`H3 Asset Bundle` 节点填项目名，一条线连主节点
 *   「资产包」，连一次终身不动；增删素材全在导演台三库面板，画布不再拉线；
 *   旧 autogrow 拉线与 H3AssetHub 串口仍兼容保留
 * - 注意：本文件由导出 JSON 直接转换，widget 顺序须与 nodes.py define_schema 严格一致
 *   （已校验 29 项）。如需改默认参数，改导出 JSON 后重新生成，勿手工编辑此数组。
 */
window.H3_DEFAULT_WORKFLOW = {
  "id": "h3-chain-director-default",
  "revision": 2,
  "last_node_id": 59,
  "last_link_id": 44,
  "nodes": [
    {
      "id": 3,
      "type": "VAELoader",
      "pos": [
        -720,
        370
      ],
      "size": [
        640,
        70
      ],
      "flags": {},
      "order": 0,
      "mode": 0,
      "inputs": [],
      "outputs": [
        {
          "name": "VAE",
          "type": "VAE",
          "links": [
            3,
            41
          ]
        }
      ],
      "title": "视频 VAE",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "VAELoader"
      },
      "widgets_values": [
        "minimax_h3_video_vae_fp16.safetensors"
      ]
    },
    {
      "id": 4,
      "type": "VAELoader",
      "pos": [
        -720,
        480
      ],
      "size": [
        640,
        70
      ],
      "flags": {},
      "order": 1,
      "mode": 0,
      "inputs": [],
      "outputs": [
        {
          "name": "VAE",
          "type": "VAE",
          "links": [
            4,
            42
          ]
        }
      ],
      "title": "音频 VAE",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "VAELoader"
      },
      "widgets_values": [
        "minimax_h3_audio_vae_fp32.safetensors"
      ]
    },
    {
      "id": 40,
      "type": "MarkdownNote",
      "pos": [
        40,
        2820
      ],
      "size": [
        620,
        380
      ],
      "flags": {},
      "order": 23,
      "mode": 0,
      "inputs": [],
      "outputs": [],
      "title": "导演台使用说明",
      "properties": {},
      "widgets_values": [
        "# H3 长片导演台 · 配套工作流\n\n- 生成控制在左侧「长片导演台」侧栏：提示词/素材/参数一体化，无需手动连点节点\n- 提示词走导演台状态（JSON 优先），**1–64 段不限**：「＋ 添加一段」加段，提示词只走导演台状态（**1–64 段不限**，画布无任何提示词/素材外联）\n- 资产走 H3AssetBundle 单线：`H3 Asset Bundle` 节点填项目名，一条线连主节点「资产包」，连一次终身不动；增删素材全在导演台三库面板（瓦片拖放/@引用），画布不再拉线；旧 autogrow 拉线与 H3AssetHub 串口仍兼容保留\n- 每段结果自动存进项目文件夹 output/h3_projects/<项目名>/（seg_NNN.mp4 + 缩略图 + 成片），导演台段卡片直接预览播放；需要另行导出可手动连「分段图像/分段音频」输出\n- 链路自动推导（无模式选择）：有段引用素材即走 ref conditioning；UNET 请按引用情况接 ref2va（或混用权重），纯文生链用 fl2va 也可\n- 每段时长/宽高比/百万像素（0.1–2.0MP 步进0.1）/种子/步数在导演台右栏「链参数」；其余参数收在「⚙ 高级设置」\n- 工具节点已预连线并旁路休眠（零成本）：latent 库行点「⚡放大」一键放大；「现抽视频源」选视频后取消 H3LatentExtract 旁路即跑现抽。平时不占队列。"
      ],
      "color": "#432",
      "bgcolor": "#653"
    },
    {
      "id": 53,
      "type": "ModelAttentionBackend",
      "pos": [
        -197.16773635195113,
        -184.82842218805052
      ],
      "size": [
        270,
        58
      ],
      "flags": {},
      "order": 29,
      "mode": 0,
      "inputs": [
        {
          "name": "model",
          "type": "MODEL",
          "link": 34
        }
      ],
      "outputs": [
        {
          "name": "MODEL",
          "type": "MODEL",
          "links": [
            35
          ]
        }
      ],
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "ModelAttentionBackend"
      },
      "widgets_values": [
        "pytorch attention"
      ]
    },
    {
      "id": 54,
      "type": "PreviewAny",
      "pos": [
        922.7541434875644,
        -132.0233707446445
      ],
      "size": [
        210,
        122
      ],
      "flags": {},
      "order": 31,
      "mode": 0,
      "inputs": [
        {
          "name": "source",
          "type": "*",
          "link": 36
        }
      ],
      "outputs": [
        {
          "name": "STRING",
          "type": "STRING",
          "links": null
        }
      ],
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "PreviewAny"
      },
      "widgets_values": []
    },
    {
      "id": 10,
      "type": "H3SeamlessChainSampler",
      "pos": [
        40,
        40
      ],
      "size": [
        720,
        1380
      ],
      "flags": {},
      "order": 30,
      "mode": 0,
      "inputs": [
        {
          "name": "模型",
          "type": "MODEL",
          "link": 35
        },
        {
          "name": "文本编码器",
          "type": "CLIP",
          "link": 2
        },
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
          "name": "起始视频",
          "shape": 7,
          "type": "IMAGE",
          "link": null
        },
        {
          "name": "起始视频音轨",
          "shape": 7,
          "type": "AUDIO",
          "link": null
        },
        {
          "name": "资产包",
          "type": "STRING",
          "link": 37
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
          "links": []
        },
        {
          "name": "帧率",
          "type": "INT"
        },
        {
          "name": "报告",
          "type": "STRING",
          "links": [
            36
          ]
        },
        {
          "name": "分段图像",
          "shape": 6,
          "type": "IMAGE"
        },
        {
          "name": "分段音频",
          "shape": 6,
          "type": "AUDIO"
        }
      ],
      "title": "H3 Seamless Chain · 导演台主节点",
      "properties": {
        "aux_id": "bingling360/ComfyUI_H3_SeamlessChain",
        "ver": "731bde31a74ff438381a07c8d647795aed63952c",
        "Node name for S&R": "H3SeamlessChainSampler"
      },
      "widgets_values": [
        "16:9",
        0.4,
        864,
        480,
        5,
        "22",
        89596547198180,
        "fixed",
        20,
        1,
        "res_multistep",
        "simple",
        "关闭",
        "",
        "自动回退",
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
        "{\"mode\":\"文生视频\",\"prompts\":[\"\"],\"first_frame\":\"\",\"end_frame\":\"\",\"last_frame\":\"\",\"ref_images\":[],\"ref_assets\":[],\"segments\":[{\"scene_prompt\":\"\",\"character_prompt\":\"\",\"soundscape\":\"\",\"music\":\"\",\"seconds\":null,\"refs\":[],\"unlink\":false,\"disabled\":false,\"frame_refs\":null}],\"inserts\":[],\"redo_segs\":[],\"upscale\":{\"schema\":2,\"on\":true,\"mode\":\"跟随生成\",\"model\":\"minimax_h3_latent_upscaler_3d_fp16.safetensors\",\"arch\":\"3D\",\"scale\":1.5,\"denoise\":0.35,\"steps\":3,\"cfg\":1,\"precision\":\"fp16\",\"time_bias\":0.03,\"mix\":0,\"adaptive\":false,\"shift\":6,\"stg\":0,\"stg_block\":25,\"passes\":1,\"decay\":0.5,\"sharpen\":0,\"pixel_sharpen\":0,\"encode\":\"高清\",\"sampler\":\"\",\"scheduler\":\"\",\"retry\":false,\"retry_target\":0.15,\"include\":[]},\"experiments\":{\"params\":{\"e1_bridge_shard\":{\"滑窗token\":6,\"子片帧数\":0,\"重叠token\":2},\"e2_memory_anchor\":{\"记忆帧数\":2,\"注入位置\":\"段首\"},\"e3_motion_gate\":{\"运动z阈值\":2,\"触发动作\":\"重摇\"},\"e4_transition_res\":{\"过渡窗帧数\":17,\"重生成步数\":20,\"双锚强度\":1}}}}",
        "高清"
      ]
    },
    {
      "id": 2,
      "type": "CLIPLoader",
      "pos": [
        -712.2928048458093,
        197.97505939231482
      ],
      "size": [
        640,
        120
      ],
      "flags": {},
      "order": 24,
      "mode": 0,
      "inputs": [],
      "outputs": [
        {
          "name": "CLIP",
          "type": "CLIP",
          "links": [
            2,
            44
          ]
        }
      ],
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "CLIPLoader"
      },
      "widgets_values": [
        "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "minimax",
        "default"
      ]
    },
    {
      "id": 1,
      "type": "UNETLoader",
      "pos": [
        -713.4749642985317,
        58.29051812722833
      ],
      "size": [
        640,
        90
      ],
      "flags": {},
      "order": 25,
      "mode": 0,
      "inputs": [],
      "outputs": [
        {
          "name": "MODEL",
          "type": "MODEL",
          "links": [
            34,
            43
          ]
        }
      ],
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "UNETLoader"
      },
      "widgets_values": [
        "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "default"
      ]
    },
    {
      "id": 55,
      "type": "H3AssetBundle",
      "title": "H3 Asset Bundle (资产包)",
      "pos": [
        380,
        1660
      ],
      "size": [
        360,
        150
      ],
      "flags": {},
      "order": 31,
      "mode": 0,
      "inputs": [],
      "outputs": [
        {
          "name": "资产包",
          "type": "STRING",
          "links": [
            37
          ]
        },
        {
          "name": "报告",
          "type": "STRING",
          "links": null
        }
      ],
      "widgets_values": [
        ""
      ],
      "properties": {
        "Node name for S&R": "H3AssetBundle"
      }
    },
    {
      "id": 56,
      "type": "H3LatentExtract",
      "title": "H3 Latent Extract (现抽存档)",
      "pos": [
        880,
        40
      ],
      "size": [
        360,
        220
      ],
      "flags": {},
      "order": 32,
      "mode": 2,
      "inputs": [
        {
          "name": "视频帧",
          "type": "IMAGE",
          "link": 39
        },
        {
          "name": "视频VAE",
          "type": "VAE",
          "link": 41
        },
        {
          "name": "音轨",
          "type": "AUDIO",
          "link": 40
        },
        {
          "name": "音频VAE",
          "type": "VAE",
          "link": 42
        }
      ],
      "outputs": [
        {
          "name": "报告",
          "type": "STRING",
          "links": null
        }
      ],
      "widgets_values": [
        0,
        0,
        "",
        ""
      ],
      "properties": {
        "Node name for S&R": "H3LatentExtract"
      }
    },
    {
      "id": 57,
      "type": "H3LatentUpscale",
      "title": "H3 Latent Upscale (库内放大)",
      "pos": [
        880,
        300
      ],
      "size": [
        360,
        320
      ],
      "flags": {},
      "order": 33,
      "mode": 2,
      "inputs": [
        {
          "name": "模型",
          "type": "MODEL",
          "link": 43
        },
        {
          "name": "文本编码器",
          "type": "CLIP",
          "link": 44
        }
      ],
      "outputs": [
        {
          "name": "报告",
          "type": "STRING",
          "links": null
        }
      ],
      "widgets_values": [
        "",
        "",
        "",
        "auto",
        2.0,
        "fp16",
        "关闭",
        "",
        0,
        6,
        0.35,
        1.0,
        ""
      ],
      "properties": {
        "Node name for S&R": "H3LatentUpscale"
      }
    },
    {
      "id": 58,
      "type": "LoadVideo",
      "title": "现抽视频源",
      "pos": [
        880,
        660
      ],
      "size": [
        320,
        120
      ],
      "flags": {},
      "order": 34,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "VIDEO",
          "type": "VIDEO",
          "links": [
            38
          ]
        }
      ],
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "LoadVideo"
      },
      "widgets_values": [
        "",
        "image"
      ]
    },
    {
      "id": 59,
      "type": "GetVideoComponents",
      "title": "现抽拆分",
      "pos": [
        880,
        800
      ],
      "size": [
        240,
        140
      ],
      "flags": {},
      "order": 35,
      "mode": 2,
      "inputs": [
        {
          "name": "video",
          "type": "VIDEO",
          "link": 38
        }
      ],
      "outputs": [
        {
          "name": "images",
          "type": "IMAGE",
          "links": [
            39
          ]
        },
        {
          "name": "audio",
          "type": "AUDIO",
          "links": [
            40
          ]
        },
        {
          "name": "fps",
          "type": "FLOAT",
          "links": null
        },
        {
          "name": "bit_depth",
          "type": "INT",
          "links": null
        }
      ],
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "GetVideoComponents"
      }
    }
  ],
  "links": [
    [
      2,
      2,
      0,
      10,
      1,
      "CLIP"
    ],
    [
      3,
      3,
      0,
      10,
      2,
      "VAE"
    ],
    [
      4,
      4,
      0,
      10,
      3,
      "VAE"
    ],
    [
      34,
      1,
      0,
      53,
      0,
      "MODEL"
    ],
    [
      35,
      53,
      0,
      10,
      0,
      "MODEL"
    ],
    [
      36,
      10,
      3,
      54,
      0,
      "STRING"
    ],
    [
      37,
      55,
      0,
      10,
      6,
      "STRING"
    ],
    [
      38,
      58,
      0,
      59,
      0,
      "VIDEO"
    ],
    [
      39,
      59,
      0,
      56,
      0,
      "IMAGE"
    ],
    [
      40,
      59,
      1,
      56,
      2,
      "AUDIO"
    ],
    [
      41,
      3,
      0,
      56,
      1,
      "VAE"
    ],
    [
      42,
      4,
      0,
      56,
      3,
      "VAE"
    ],
    [
      43,
      1,
      0,
      57,
      0,
      "MODEL"
    ],
    [
      44,
      2,
      0,
      57,
      1,
      "CLIP"
    ]
  ],
  "groups": [
    {
      "id": 1,
      "title": "模型加载",
      "bounding": [
        -760,
        0,
        720,
        620
      ],
      "color": "#3f789e",
      "flags": {}
    },
    {
      "id": 2,
      "title": "导演台主链",
      "bounding": [
        0,
        0,
        1260,
        1100
      ],
      "color": "#88A",
      "flags": {}
    },
    {
      "id": 3,
      "title": "素材池与提示词 · 自动管理（导演台控制，勿删；隐藏=未使用）",
      "bounding": [
        0,
        1290,
        2360,
        1450
      ],
      "color": "#b58b2a",
      "flags": {}
    }
  ],
  "config": {},
  "extra": {
    "ds": {
      "scale": 0.6512906823746598,
      "offset": [
        1868.9135086855777,
        765.6611648228783
      ]
    },
    "frontendVersion": "1.48.7"
  },
  "version": 0.4
};
