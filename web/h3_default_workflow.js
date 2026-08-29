/* H3 长片导演台 · 配套默认工作流模板
 *
 * 由「长视频接续二采导演台工作流.json」导出生成（D:/Downloads）：
 * - 模型加载器（ref2va UNET / Qwen3-VL CLIP / 视频+音频 VAE）+ 主节点（导演台模式）
 * - 提示词×3（PrimitiveStringMultiline 隐藏预连线到「提示词组」）：导演台 JSON 优先，
 *   画布节点作为镜像/兜底——没有导演台状态也能直接跑（导演台段数 1–64 不限）
 * - 每段视频由主节点「自动保存=分段」存进项目文件夹 output/h3_projects/<项目名>/，
 *   主节点「自动成片=开启」时另编码完整成片（final_*.mp4）落同一文件夹；
 *   不再依赖 H3ChainSaver 节点（成片保存由主节点一体化完成）
 * - 「素材池 · 自动管理」节点组：首帧图 + 目标尾帧图 + 尾帧图 + 参考图×9
 *   + 参考视频×3 + 参考音频×3，全部预连线到主节点对应输入槽；
 *   不用时 mode=2(Never)+折叠 = 隐藏但连线常驻，导演台上传/删除素材时点亮/隐藏
 * - 注意：本文件由导出 JSON 直接转换，widget 顺序须与 nodes.py define_schema 严格一致
 *   （已校验 29 项）。如需改默认参数，改导出 JSON 后重新生成，勿手工编辑此数组。
 */
window.H3_DEFAULT_WORKFLOW = {
  "id": "h3-chain-director-default",
  "revision": 2,
  "last_node_id": 54,
  "last_link_id": 36,
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
            3
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
            4
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
      "id": 50,
      "type": "PrimitiveStringMultiline",
      "pos": [
        40,
        1330
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 2,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "STRING",
          "type": "STRING",
          "links": [
            7
          ]
        }
      ],
      "title": "提示词·1",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "PrimitiveStringMultiline"
      },
      "widgets_values": [
        "示例段落：黄昏的海边小镇，海浪轻拍礁石，镜头缓缓推近灯塔"
      ]
    },
    {
      "id": 51,
      "type": "PrimitiveStringMultiline",
      "pos": [
        370,
        1330
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 3,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "STRING",
          "type": "STRING",
          "links": [
            8
          ]
        }
      ],
      "title": "提示词·2",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "PrimitiveStringMultiline"
      },
      "widgets_values": [
        ""
      ]
    },
    {
      "id": 52,
      "type": "PrimitiveStringMultiline",
      "pos": [
        700,
        1330
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 4,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "STRING",
          "type": "STRING",
          "links": [
            9
          ]
        }
      ],
      "title": "提示词·3",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "PrimitiveStringMultiline"
      },
      "widgets_values": [
        ""
      ]
    },
    {
      "id": 20,
      "type": "LoadImage",
      "pos": [
        40,
        1660
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 5,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "IMAGE",
          "type": "IMAGE",
          "links": [
            10
          ]
        },
        {
          "name": "MASK",
          "type": "MASK"
        }
      ],
      "title": "首帧图",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "LoadImage"
      },
      "widgets_values": [
        "",
        "image"
      ]
    },
    {
      "id": 6,
      "type": "LoadImage",
      "pos": [
        40,
        2000
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 6,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "IMAGE",
          "type": "IMAGE",
          "links": [
            11
          ]
        },
        {
          "name": "MASK",
          "type": "MASK"
        }
      ],
      "title": "目标尾帧图",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "LoadImage"
      },
      "widgets_values": [
        "",
        "image"
      ]
    },
    {
      "id": 5,
      "type": "LoadImage",
      "pos": [
        380,
        1660
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 7,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "IMAGE",
          "type": "IMAGE",
          "links": [
            12
          ]
        },
        {
          "name": "MASK",
          "type": "MASK"
        }
      ],
      "title": "尾帧图",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "LoadImage"
      },
      "widgets_values": [
        "",
        "image"
      ]
    },
    {
      "id": 21,
      "type": "LoadImage",
      "pos": [
        720,
        1660
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 8,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "IMAGE",
          "type": "IMAGE",
          "links": [
            13
          ]
        },
        {
          "name": "MASK",
          "type": "MASK"
        }
      ],
      "title": "参考图·1",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "LoadImage"
      },
      "widgets_values": [
        "",
        "image"
      ]
    },
    {
      "id": 22,
      "type": "LoadImage",
      "pos": [
        1040,
        1660
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 9,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "IMAGE",
          "type": "IMAGE",
          "links": [
            14
          ]
        },
        {
          "name": "MASK",
          "type": "MASK"
        }
      ],
      "title": "参考图·2",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "LoadImage"
      },
      "widgets_values": [
        "",
        "image"
      ]
    },
    {
      "id": 23,
      "type": "LoadImage",
      "pos": [
        1360,
        1660
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 10,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "IMAGE",
          "type": "IMAGE",
          "links": [
            15
          ]
        },
        {
          "name": "MASK",
          "type": "MASK"
        }
      ],
      "title": "参考图·3",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "LoadImage"
      },
      "widgets_values": [
        "",
        "image"
      ]
    },
    {
      "id": 24,
      "type": "LoadImage",
      "pos": [
        720,
        2000
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 11,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "IMAGE",
          "type": "IMAGE",
          "links": [
            16
          ]
        },
        {
          "name": "MASK",
          "type": "MASK"
        }
      ],
      "title": "参考图·4",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "LoadImage"
      },
      "widgets_values": [
        "",
        "image"
      ]
    },
    {
      "id": 25,
      "type": "LoadImage",
      "pos": [
        1040,
        2000
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 12,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "IMAGE",
          "type": "IMAGE",
          "links": [
            17
          ]
        },
        {
          "name": "MASK",
          "type": "MASK"
        }
      ],
      "title": "参考图·5",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "LoadImage"
      },
      "widgets_values": [
        "",
        "image"
      ]
    },
    {
      "id": 26,
      "type": "LoadImage",
      "pos": [
        1360,
        2000
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 13,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "IMAGE",
          "type": "IMAGE",
          "links": [
            18
          ]
        },
        {
          "name": "MASK",
          "type": "MASK"
        }
      ],
      "title": "参考图·6",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "LoadImage"
      },
      "widgets_values": [
        "",
        "image"
      ]
    },
    {
      "id": 27,
      "type": "LoadImage",
      "pos": [
        720,
        2340
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 14,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "IMAGE",
          "type": "IMAGE",
          "links": [
            19
          ]
        },
        {
          "name": "MASK",
          "type": "MASK"
        }
      ],
      "title": "参考图·7",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "LoadImage"
      },
      "widgets_values": [
        "",
        "image"
      ]
    },
    {
      "id": 28,
      "type": "LoadImage",
      "pos": [
        1040,
        2340
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 15,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "IMAGE",
          "type": "IMAGE",
          "links": [
            20
          ]
        },
        {
          "name": "MASK",
          "type": "MASK"
        }
      ],
      "title": "参考图·8",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "LoadImage"
      },
      "widgets_values": [
        "",
        "image"
      ]
    },
    {
      "id": 29,
      "type": "LoadImage",
      "pos": [
        1360,
        2340
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 16,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "IMAGE",
          "type": "IMAGE",
          "links": [
            21
          ]
        },
        {
          "name": "MASK",
          "type": "MASK"
        }
      ],
      "title": "参考图·9",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "LoadImage"
      },
      "widgets_values": [
        "",
        "image"
      ]
    },
    {
      "id": 30,
      "type": "LoadVideo",
      "pos": [
        1380,
        1660
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 17,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "VIDEO",
          "type": "VIDEO",
          "links": [
            24
          ]
        }
      ],
      "title": "参考视频·1",
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
      "id": 33,
      "type": "GetVideoComponents",
      "pos": [
        1700,
        1660
      ],
      "size": [
        280,
        150
      ],
      "flags": {
        "collapsed": true
      },
      "order": 26,
      "mode": 2,
      "inputs": [
        {
          "name": "video",
          "type": "VIDEO",
          "link": 24
        }
      ],
      "outputs": [
        {
          "name": "images",
          "type": "IMAGE",
          "links": [
            22
          ]
        },
        {
          "name": "audio",
          "type": "AUDIO",
          "links": [
            23
          ]
        },
        {
          "name": "fps",
          "type": "FLOAT"
        },
        {
          "name": "bit_depth",
          "type": "INT"
        }
      ],
      "title": "拆分视频·1",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "GetVideoComponents"
      },
      "widgets_values": []
    },
    {
      "id": 31,
      "type": "LoadVideo",
      "pos": [
        1380,
        1900
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 18,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "VIDEO",
          "type": "VIDEO",
          "links": [
            27
          ]
        }
      ],
      "title": "参考视频·2",
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
      "id": 34,
      "type": "GetVideoComponents",
      "pos": [
        1700,
        1900
      ],
      "size": [
        280,
        150
      ],
      "flags": {
        "collapsed": true
      },
      "order": 27,
      "mode": 2,
      "inputs": [
        {
          "name": "video",
          "type": "VIDEO",
          "link": 27
        }
      ],
      "outputs": [
        {
          "name": "images",
          "type": "IMAGE",
          "links": [
            25
          ]
        },
        {
          "name": "audio",
          "type": "AUDIO",
          "links": [
            26
          ]
        },
        {
          "name": "fps",
          "type": "FLOAT"
        },
        {
          "name": "bit_depth",
          "type": "INT"
        }
      ],
      "title": "拆分视频·2",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "GetVideoComponents"
      },
      "widgets_values": []
    },
    {
      "id": 32,
      "type": "LoadVideo",
      "pos": [
        1380,
        2140
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 19,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "VIDEO",
          "type": "VIDEO",
          "links": [
            30
          ]
        }
      ],
      "title": "参考视频·3",
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
      "id": 35,
      "type": "GetVideoComponents",
      "pos": [
        1700,
        2140
      ],
      "size": [
        280,
        150
      ],
      "flags": {
        "collapsed": true
      },
      "order": 28,
      "mode": 2,
      "inputs": [
        {
          "name": "video",
          "type": "VIDEO",
          "link": 30
        }
      ],
      "outputs": [
        {
          "name": "images",
          "type": "IMAGE",
          "links": [
            28
          ]
        },
        {
          "name": "audio",
          "type": "AUDIO",
          "links": [
            29
          ]
        },
        {
          "name": "fps",
          "type": "FLOAT"
        },
        {
          "name": "bit_depth",
          "type": "INT"
        }
      ],
      "title": "拆分视频·3",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "GetVideoComponents"
      },
      "widgets_values": []
    },
    {
      "id": 36,
      "type": "LoadAudio",
      "pos": [
        2040,
        1660
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 20,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "AUDIO",
          "type": "AUDIO",
          "links": [
            31
          ]
        },
        {
          "name": "name",
          "type": "STRING"
        }
      ],
      "title": "参考音频·1",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "LoadAudio"
      },
      "widgets_values": [
        "",
        null,
        null
      ]
    },
    {
      "id": 37,
      "type": "LoadAudio",
      "pos": [
        2040,
        1900
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 21,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "AUDIO",
          "type": "AUDIO",
          "links": [
            32
          ]
        },
        {
          "name": "name",
          "type": "STRING"
        }
      ],
      "title": "参考音频·2",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "LoadAudio"
      },
      "widgets_values": [
        "",
        null,
        null
      ]
    },
    {
      "id": 38,
      "type": "LoadAudio",
      "pos": [
        2040,
        2140
      ],
      "size": [
        280,
        300
      ],
      "flags": {
        "collapsed": true
      },
      "order": 22,
      "mode": 2,
      "inputs": [],
      "outputs": [
        {
          "name": "AUDIO",
          "type": "AUDIO",
          "links": [
            33
          ]
        },
        {
          "name": "name",
          "type": "STRING"
        }
      ],
      "title": "参考音频·3",
      "properties": {
        "cnr_id": "comfy-core",
        "ver": "0.33.1",
        "Node name for S&R": "LoadAudio"
      },
      "widgets_values": [
        "",
        null,
        null
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
        "# H3 长片导演台 · 配套工作流\n\n- 生成控制在左侧「长片导演台」侧栏：提示词/素材/参数一体化，无需手动连点节点\n- 提示词走导演台状态（JSON 优先），**1–64 段不限**：「＋ 添加一段」加段，「提示词·1..3」隐藏节点只是画布镜像兼兜底，超过 3 段全在导演台管理\n- 「素材池 · 自动管理」组的节点由导演台自动点亮/隐藏：**连线常驻，不用时只是隐藏**，请勿删除；「目标尾帧图」= 尾帧图片（FL2VA 首尾帧剧情终点，仅首帧模式）；「尾帧图」= 每段尾帧锚定（任意模式可用，防主体漂移）\n- 每段结果自动存进项目文件夹 output/h3_projects/<项目名>/（seg_NNN.mp4 + 缩略图 + 成片），导演台段卡片直接预览播放；需要另行导出可手动连「分段图像/分段音频」输出\n- 默认 ref2va UNET（多参模式）；纯文生/首帧模式请在导演台切换，或把 UNETLoader 换成 fl2va 权重\n- 每段时长/宽高比/百万像素（0.1–2.0MP 步进0.1）/种子/步数在导演台右栏「链参数」；其余参数收在「⚙ 高级设置」"
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
          "name": "首帧图片",
          "shape": 7,
          "type": "IMAGE",
          "link": 10
        },
        {
          "name": "尾帧图片",
          "shape": 7,
          "type": "IMAGE",
          "link": 11
        },
        {
          "name": "每段尾帧锚定",
          "shape": 7,
          "type": "IMAGE",
          "link": 12
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
          "label": "提示词_0",
          "name": "提示词组.提示词_0",
          "type": "STRING",
          "link": 7
        },
        {
          "label": "提示词_1",
          "name": "提示词组.提示词_1",
          "shape": 7,
          "type": "STRING",
          "link": 8
        },
        {
          "label": "提示词_2",
          "name": "提示词组.提示词_2",
          "shape": 7,
          "type": "STRING",
          "link": 9
        },
        {
          "label": "提示词_3",
          "name": "提示词组.提示词_3",
          "shape": 7,
          "type": "STRING",
          "link": null
        },
        {
          "label": "参考图片_0",
          "name": "参考图片组.参考图片_0",
          "shape": 7,
          "type": "IMAGE",
          "link": 13
        },
        {
          "label": "参考图片_1",
          "name": "参考图片组.参考图片_1",
          "shape": 7,
          "type": "IMAGE",
          "link": 14
        },
        {
          "label": "参考图片_2",
          "name": "参考图片组.参考图片_2",
          "shape": 7,
          "type": "IMAGE",
          "link": 15
        },
        {
          "label": "参考图片_3",
          "name": "参考图片组.参考图片_3",
          "shape": 7,
          "type": "IMAGE",
          "link": 16
        },
        {
          "label": "参考图片_4",
          "name": "参考图片组.参考图片_4",
          "shape": 7,
          "type": "IMAGE",
          "link": 17
        },
        {
          "label": "参考图片_5",
          "name": "参考图片组.参考图片_5",
          "shape": 7,
          "type": "IMAGE",
          "link": 18
        },
        {
          "label": "参考图片_6",
          "name": "参考图片组.参考图片_6",
          "shape": 7,
          "type": "IMAGE",
          "link": 19
        },
        {
          "label": "参考图片_7",
          "name": "参考图片组.参考图片_7",
          "shape": 7,
          "type": "IMAGE",
          "link": 20
        },
        {
          "label": "参考图片_8",
          "name": "参考图片组.参考图片_8",
          "shape": 7,
          "type": "IMAGE",
          "link": 21
        },
        {
          "label": "参考视频_0",
          "name": "参考视频组.参考视频_0",
          "shape": 7,
          "type": "IMAGE",
          "link": 22
        },
        {
          "label": "参考视频_1",
          "name": "参考视频组.参考视频_1",
          "shape": 7,
          "type": "IMAGE",
          "link": 25
        },
        {
          "label": "参考视频_2",
          "name": "参考视频组.参考视频_2",
          "shape": 7,
          "type": "IMAGE",
          "link": 28
        },
        {
          "label": "参考视频音轨_0",
          "name": "参考视频音轨组.参考视频音轨_0",
          "shape": 7,
          "type": "AUDIO",
          "link": 23
        },
        {
          "label": "参考视频音轨_1",
          "name": "参考视频音轨组.参考视频音轨_1",
          "shape": 7,
          "type": "AUDIO",
          "link": 26
        },
        {
          "label": "参考视频音轨_2",
          "name": "参考视频音轨组.参考视频音轨_2",
          "shape": 7,
          "type": "AUDIO",
          "link": 29
        },
        {
          "label": "参考音频_0",
          "name": "参考音频组.参考音频_0",
          "shape": 7,
          "type": "AUDIO",
          "link": 31
        },
        {
          "label": "参考音频_1",
          "name": "参考音频组.参考音频_1",
          "shape": 7,
          "type": "AUDIO",
          "link": 32
        },
        {
          "label": "参考音频_2",
          "name": "参考音频组.参考音频_2",
          "shape": 7,
          "type": "AUDIO",
          "link": 33
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
            2
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
            34
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
      7,
      50,
      0,
      10,
      9,
      "STRING"
    ],
    [
      8,
      51,
      0,
      10,
      10,
      "STRING"
    ],
    [
      9,
      52,
      0,
      10,
      11,
      "STRING"
    ],
    [
      10,
      20,
      0,
      10,
      4,
      "IMAGE"
    ],
    [
      11,
      6,
      0,
      10,
      5,
      "IMAGE"
    ],
    [
      12,
      5,
      0,
      10,
      6,
      "IMAGE"
    ],
    [
      13,
      21,
      0,
      10,
      13,
      "IMAGE"
    ],
    [
      14,
      22,
      0,
      10,
      14,
      "IMAGE"
    ],
    [
      15,
      23,
      0,
      10,
      15,
      "IMAGE"
    ],
    [
      16,
      24,
      0,
      10,
      16,
      "IMAGE"
    ],
    [
      17,
      25,
      0,
      10,
      17,
      "IMAGE"
    ],
    [
      18,
      26,
      0,
      10,
      18,
      "IMAGE"
    ],
    [
      19,
      27,
      0,
      10,
      19,
      "IMAGE"
    ],
    [
      20,
      28,
      0,
      10,
      20,
      "IMAGE"
    ],
    [
      21,
      29,
      0,
      10,
      21,
      "IMAGE"
    ],
    [
      22,
      33,
      0,
      10,
      22,
      "IMAGE"
    ],
    [
      23,
      33,
      1,
      10,
      25,
      "AUDIO"
    ],
    [
      24,
      30,
      0,
      33,
      0,
      "VIDEO"
    ],
    [
      25,
      34,
      0,
      10,
      23,
      "IMAGE"
    ],
    [
      26,
      34,
      1,
      10,
      26,
      "AUDIO"
    ],
    [
      27,
      31,
      0,
      34,
      0,
      "VIDEO"
    ],
    [
      28,
      35,
      0,
      10,
      24,
      "IMAGE"
    ],
    [
      29,
      35,
      1,
      10,
      27,
      "AUDIO"
    ],
    [
      30,
      32,
      0,
      35,
      0,
      "VIDEO"
    ],
    [
      31,
      36,
      0,
      10,
      28,
      "AUDIO"
    ],
    [
      32,
      37,
      0,
      10,
      29,
      "AUDIO"
    ],
    [
      33,
      38,
      0,
      10,
      30,
      "AUDIO"
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
