# ComfyUI-minimaxH3-SequenceForge

**Seamless long-form video production for MiniMax H3 inside ComfyUI.**

SequenceForge turns a stack of per-shot prompts into one continuous, arbitrarily long video — chaining each new segment onto the tail of the previous one through the model's own conditioning protocol, archiving every segment to disk so you can resume, re-shoot, or re-render at any point. Everything is driven from a full-screen **Director Console** that ships with the plugin.

No monkey-patching. No extra Python dependencies. Official MiniMax H3 nodes only.

---

## Table of contents

1. [What's in the box](#1-whats-in-the-box)
2. [How the seam works](#2-how-the-seam-works)
3. [Highlights](#3-highlights)
4. [Requirements](#4-requirements)
5. [Installation](#5-installation)
6. [Quick start](#6-quick-start)
7. [The Director Console](#7-the-director-console)
8. [Prompt authoring](#8-prompt-authoring)
9. [Key parameters](#9-key-parameters)
10. [Project archiving](#10-project-archiving)
11. [Per-segment review](#11-per-segment-review)
12. [Re-running and rerolling segments](#12-re-running-and-rerolling-segments)
13. [Selective redo](#13-selective-redo)
14. [Segment ordering and disabling](#14-segment-ordering-and-disabling)
15. [Prologue: continue from an uploaded video](#15-prologue-continue-from-an-uploaded-video)
16. [Standalone shots (hard cut)](#16-standalone-shots-hard-cut)
17. [Manual anchors](#17-manual-anchors)
18. [Merge export](#18-merge-export)
19. [Latent-upscale second pass](#19-latent-upscale-second-pass)
20. [Bridge-frame gating](#20-bridge-frame-gating)
21. [Seam Doctor](#21-seam-doctor)
22. [Run report (wire-free)](#22-run-report-wire-free)
23. [Semantic bridge](#23-semantic-bridge)
24. [Enhance-A-Video / FETA](#24-enhance-a-video--feta)
25. [Prompt optimizer and expander](#25-prompt-optimizer-and-expander)
26. [Custom sigmas (distilled LoRAs)](#26-custom-sigmas-distilled-loras)
27. [Asset library](#27-asset-library)
28. [Performance settings](#28-performance-settings)
29. [Seam metrics](#29-seam-metrics)
30. [Repository layout](#30-repository-layout)
31. [HTTP API](#31-http-api)
32. [Upstream tracking](#32-upstream-tracking)
33. [Removed in 1.0](#33-removed-in-10)
34. [Credits and licensing](#34-credits-and-licensing)

---

## 1. What's in the box

| Node | Purpose |
|---|---|
| **`H3SeamlessChainSampler`** | The workhorse. Per-segment prompts → segment-by-segment sampling → seamless audio+video chaining → tail-trim → optional latent-upscale second pass → per-segment save + final encode. Also hosts the Director Console state. |
| **`H3SeamDoctor`** | Seam diagnostics — measures but never modifies. Quantifies **time gaps / motion teleports / color drift / content switches / sharpness drops / audio pops** per seam, with a residual false-color comparison image. |
| **`H3RunReport`** | Wire-free run report. **Zero input sockets**, so the "the report wire broke again after restart" failure mode cannot exist. The text is fetched by the front-end straight from the plugin API. |
| **`H3EAVFetaPatch`** | Optional Enhance-A-Video / FETA temporal-attention enhancement, adapted to H3. Report-only by default. |
| **`H3EAVFetaReport`** | Reads the CFI / g statistics from the last EAV/FETA run. |

> **Note.** `H3StoryboardChain` (storyboard chaining) and `H3ChainSaver` (final-save gallery) have been **removed**. Storyboard mode is not part of the Director Console mainline, and final-save is now folded into the main node's *Auto finalize* switch. See [§33 Removed in 1.0](#33-removed-in-10).

---

## 2. How the seam works

When generating segment *N+1*, SequenceForge slices the last `guide frames` frames **directly out of segment *N*'s sampled latent** (no decode, no re-encode → zero color drift) and pins them to the head of segment *N+1* through the official conditioning protocol (`minimax_keyframes`, anchored at `resolved_frame_index=0`). The anchor is re-injected on every sampling step, so the model keeps drawing along the motion trajectory of the previous tail. After decoding, the overlapping bridge at the head is trimmed and the segments are concatenated — the seam becomes visually continuous.

The tail is also snapped to the H3 token grid before chaining, so the guide anchor and the actual output tail coincide exactly (otherwise the continuation point lands on padding frames the segment never emitted — the classic "mystery jump").

---

## 3. Highlights

- **Official protocol, zero monkey-patching.** Conditioning / latent construction calls the official `MiniMaxH3ImageToVideo` / `MiniMaxH3ReferenceToVideo`; sampling goes through the official `common_ksampler`. Upgrades survive.
- **Zero extra dependencies.** `requirements.txt` is empty — only ComfyUI's bundled torch and `comfy` core. No opencv / scenedetect / ffmpeg installs.
- **No cache architecture.** The loop runs inside a single execution; inter-segment handoff is all local variables — the "stale cache replay" class of bugs simply cannot occur.
- **Dual-stream continuation.** The guide pins the previous tail's **audio window** as well as its video frames.
- **Custom sigmas.** The `custom sigmas` socket accepts an external sigma table (HyperFlow 8-step, H3 Turbo, …). Connected, it overrides `steps` and `scheduler` for the base pass only; disconnected, behavior is byte-identical to before.
- **Per-segment review.** With *review mode = confirm each segment*, one run produces exactly one new segment and returns — no need to run a whole chain to discover a bad segment.
- **Re-shoot any segment.** Edit segment *N*'s prompt → only segment *N* is rebuilt (double-anchored against its archived neighbors; downstream untouched). *Selective redo* lets you flag any number of finished segments and re-shoot them in one batch without cascading.
- **Segment reordering.** Drag segment cards to reorder; reorder only rebuilds from the change point. **Pause** any prompt segment (`⏸`) to skip it without renumbering slots — zero redo cost, instantly reversible.
- **Auto-save (on by default).** With *auto-save = segments*, each finished segment lands in the project folder with no downstream wiring. *Auto finalize* (on by default) then concatenates the finished chain into `final_<timestamp>.mp4`.
- **Game-style project archiving.** One project = one folder under `output/h3_projects/`. Per-segment latent archives (~5 MB each) let an interrupted run resume with **frame-identical** results, skipping already-completed segments.
- **Prologue continuation.** Feed a `LoadVideo` into *start video* and it becomes segment 0: the final cut starts with it and generation continues seamlessly from its tail.
- **Standalone shots.** Mark a segment `🔗 standalone` to hard-cut it from the previous one — no bridge injection, no head trim, no post-processing.
- **Manual anchors.** Pin an external clip / image / previous tail / finished segment / latent-library entry to any segment's head / mid / tail as a guide anchor. Five sources, 17k+5 window widths, image+audio / image-only / audio-only modes. The only hard constraint is resolution.
- **Merge export.** Re-concatenate existing segments, finals, and external videos in click order into `finals/merged_*.mp4` — no re-run.
- **Latent-upscale second pass.** After each segment is finalized (and before decode) the latent is **neurally upscaled → lightly re-sampled** to recover high-frequency detail. The saved segment video and final cut are the high-res result directly. Audio is carried over untouched.
- **Bridge-frame gating.** Score the tail frames that will become the bridge (Laplacian sharpness + exposure); on a bad tail, roll back 17/34 frames to a good one.
- **Seam reroll.** When the seam frame-difference exceeds a threshold, re-sample that segment with a new seed and keep the smallest-difference attempt.
- **Tail-trim alignment + loudness alignment.** Segment output tails are snapped to the token grid; per-segment head loudness is matched to the previous tail (±6 dB clamp, 1 s fade-out, never accumulates along the chain).

---

## 4. Requirements

- **ComfyUI ≥ v0.34.0 recommended.** Hard floor is v0.30.0 (must include the official MiniMax H3 nodes and `comfy_api.latest`); below that the plugin does not load and says so in the console.
- **A GPU that can hold the H3 UNET.** The reference cloud setup is a 24 GB RTX 3090. The plugin is developed on a 6 GB GTX 1660 SUPER (sm75) but that is a dev box, not a target configuration.
- **sm75 (Turing) note.** Do **not** enable `sol_attn` on sm75 — it bypasses the sm80 guard, does not error, and returns garbage. The node's eligibility check is the only protection.

---

## 5. Installation

### 5.1 The plugin

**Option A — git clone (recommended):**

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/bingling360/ComfyUI-minimaxH3-SequenceForge.git
```

**Option B — manual copy:**

Copy the whole `ComfyUI-minimaxH3-SequenceForge` folder (it **must** include `web/` and `example_workflows/`) into `ComfyUI/custom_nodes/`. `web/` is the Director Console front-end; without it the nodes still run but the console will not appear.

**Dependencies:** none. `requirements.txt` is empty — only ComfyUI's bundled torch / `comfy` core and the official MiniMax H3 nodes are used. `pip install -r requirements.txt` can be skipped.

Restart ComfyUI. When the console prints `[ComfyUI_H3_SeamlessChain] 路由已注册…` the plugin loaded. AutoDL environments (`/root/miniconda3`) follow the same steps.

### 5.2 Latent-upscale weights (optional)

Only needed if you want the second-pass high-res render. Download from HuggingFace [`LBH-123-AI/Minimax_h3_latent_Upscaler`](https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler) into `ComfyUI/models/latent_upscale_models/` (create the folder if needed). **The weights are not committed to git** (a single 691 MB file exceeds GitHub's 100 MB limit).

| File | Size | Notes |
|---|---|---|
| `minimax_h3_latent_upscaler_3d_fp16.safetensors` | 691 MB | **Recommended** — half precision, low VRAM |
| `minimax_h3_latent_upscaler_3d_bf16.safetensors` | 691 MB | bf16 variant (pick one of the two) |
| `minimax_h3_latent_upscaler_3d_fp32.pth` | 1.38 GB | Full precision, best quality, double VRAM |

```bash
# Option 1 — download the file from the browser:
#   https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler/tree/main

# Option 2 — huggingface-cli
pip install -U "huggingface_hub[cli]"
hf download LBH-123-AI/Minimax_h3_latent_Upscaler \
   minimax_h3_latent_upscaler_3d_fp16.safetensors \
   --local-dir ComfyUI/models/latent_upscale_models

# Option 3 — hf-mirror.com (no proxy needed in mainland China)
HF_ENDPOINT=https://hf-mirror.com hf download LBH-123-AI/Minimax_h3_latent_Upscaler \
   minimax_h3_latent_upscaler_3d_fp16.safetensors \
   --local-dir ComfyUI/models/latent_upscale_models
```

- The **network architecture** dropdown must match the weights (2D residual backbone / pure 3D convolution). All three files above are **3D backbones** — pick "3D".
- Without the weights the main chain still works; only the *latent upscale second pass* panel is unavailable (empty dropdown).

### 5.3 Models and LoRAs for the bundled workflow

The plugin itself needs no weights, but the bundled `example_workflows/备用初始化导演台工作流.json` is wired for a specific, known-good stack. Download the three files below (plus the official CLIP and VAEs) or the loaders will report a missing file.

| Role | File | Source | Size | Destination |
|---|---|---|---|---|
| Base-pass UNET **and** second-pass UNET | `minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors` | [`smhfacct/Minimax-H3-fl2va-ref2va-hybrid-models`](https://huggingface.co/smhfacct/Minimax-H3-fl2va-ref2va-hybrid-models) | ~21 GB | `ComfyUI/models/diffusion_models/` |
| Turbo (few-step) LoRA | `minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors` | [`drbaph/MiniMax-H3-Turbo-Lora-ComfyUI`](https://huggingface.co/drbaph/MiniMax-H3-Turbo-Lora-ComfyUI) | 620 MB | `ComfyUI/models/loras/` |
| Motion Repair LoRA (V2) | `Motion_Repair_V2.safetensors` | [`JOKER141/MiniMax-H3-General-Motion-Continuity-Repair`](https://huggingface.co/JOKER141/MiniMax-H3-General-Motion-Continuity-Repair) | 155 MB | `ComfyUI/models/loras/` |

```bash
pip install -U "huggingface_hub[cli]"

hf download smhfacct/Minimax-H3-fl2va-ref2va-hybrid-models \
  minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors \
  --local-dir ComfyUI/models/diffusion_models

hf download drbaph/MiniMax-H3-Turbo-Lora-ComfyUI \
  minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors \
  --local-dir ComfyUI/models/loras

hf download JOKER141/MiniMax-H3-General-Motion-Continuity-Repair \
  Motion_Repair_V2.safetensors \
  --local-dir ComfyUI/models/loras

# mainland China: prepend HF_ENDPOINT=https://hf-mirror.com to any command above
```

#### Main model — MiniMax H3 hybrid (`fl2va` + `ref2va`)

MiniMax shipped two H3 checkpoints with identical architecture and weight layout but different training regimes:

- **`fl2va`** — trained on first/last-keyframe conditioning only; clearly higher visual and audio quality.
- **`ref2va`** — additionally trained on multimodal reference conditioning (image / video / audio), but a known training-quality issue makes its raw output noticeably weaker even on tasks that use no references at all.

That creates an awkward trade-off: `ref2va` is the only checkpoint that supports reference conditioning, but it costs real output quality. A tensor-by-tensor comparison shows the two are bit-identical or near-identical (cosine ≥ 0.9997) almost everywhere — attention QKV/output projections, MLPs, RMSNorms, patch projections, RoPE, the token refiner — with the meaningful differences concentrated in the per-block **`adaln_proj`** weights, the AdaLN modulation projections that route text / audio / video / **reference** signals into the residual stream. The hybrid therefore keeps `fl2va` as the base and swaps in `ref2va`'s `adaln_proj` for a chosen tail of blocks.

Four variants are published, differing only in how many of the last blocks come from `ref2va`:

| Variant | Blocks from `ref2va` | Character |
|---|---|---|
| `b30-49` | last 20 of 50 | closest to `fl2va` — highest visual/audio quality, least reference capability |
| **`b25-49`** | last 25 of 50 | **used by the bundled workflow** — close to `fl2va`, slightly reduced reference capability |
| `b20-49` | last 30 of 50 | closer to `ref2va` |
| `b15-49` | last 35 of 50 | closest to `ref2va` — best reference fidelity, lowest visual/audio quality |

All variants are built from the pruned **int8-convrot** base models. If your work leans heavily on reference assets (r2v chains), try `b20-49` or `b15-49`; if you mostly do t2v / i2v, stay on `b25-49` or `b30-49`. See the model card for the license.

#### Turbo LoRA — few-step acceleration

[`drbaph/MiniMax-H3-Turbo-Lora-ComfyUI`](https://huggingface.co/drbaph/MiniMax-H3-Turbo-Lora-ComfyUI) (**Apache-2.0**) hosts MiniMax-H3 Turbo and few-step LoRAs converted and optimized for ComfyUI. They accelerate the joint video + synchronized-audio generation by cutting the number of sampling steps — the bundled workflow pairs the Turbo LoRA at strength **1.0** with **8 steps**, which is why the factory `steps` default is 8 rather than 25. The same repo also ships HyperFlow 8-step conversions; see [§26 Custom sigmas](#26-custom-sigmas-distilled-loras) for the matching sigma table.

> The repo carries 4-step and 8-step, FL2V and Ref2V, full and pruned variants. The file the bundled workflow expects is `minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors`; if you pick a different variant, update the LoRA node's file name and check its recommended step count.

#### Motion Repair LoRA — motion continuity repair (V2)

[`JOKER141/MiniMax-H3-General-Motion-Continuity-Repair`](https://huggingface.co/JOKER141/MiniMax-H3-General-Motion-Continuity-Repair) is a **general-purpose** motion-continuity LoRA, not a combat-only one: running, sports, dance, acrobatics, character interaction, combat, weapon motion. V2 concentrates on H3's hardest failure zones — flips, spins, rolls, inversions, and orientation recovery — and on the full sequence from takeoff through inversion and reorientation to landing, so limbs stop reconnecting wrong and bodies stop twisting into unreadable masses. It learns structure, orientation sense and momentum recovery rather than named tricks, so it generalizes. It is still a *repair* tool — extreme transition frames can occasionally show small artifacts — but it turns many discarded clips into usable shots.

The author's recommended weights:

- **standalone, ~0.9** — a relatively high weight is needed to visibly affect motion continuity, the slow-motion tendency, broken transitions and prompt following;
- **alongside a Combat LoRA, stage-1 ~0.5–0.7** — Combat already supplies motion speed and impact, so this LoRA only needs a medium weight to correct continuity / coordination / action logic;
- very high weights can begin to reshape the whole motion logic, or affect visual style and audio characteristics.

The bundled workflow uses `Motion_Repair_V2.safetensors` at **0.6** on the base pass and **0.25** on the second pass. The V1 file (`Motion_Repair.safetensors`) is in the same repo.

#### Also required — official MiniMax H3 files

The bundled workflow's CLIP and VAE loaders point at the official [`Comfy-Org/MiniMax-H3`](https://huggingface.co/Comfy-Org/MiniMax-H3) weights: `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` (CLIP, type `minimax`), `minimax_h3_video_vae_int8_convrot.safetensors` and `minimax_h3_audio_vae_fp32.safetensors` (VAEs). Download them into `ComfyUI/models/text_encoders/` and `ComfyUI/models/vae/` (ModelScope carries the same files if HuggingFace is slow).

> The second-pass chain (second UNET + Motion Repair 0.25) ships in the **ignored** state (`mode = 4`) — it does not enter the execution graph and uses no memory until you un-bypass that group and switch the console's second-pass panel back to *follow generation*.

### 5.4 Verify

1. After restart, the node menu should find `H3SeamlessChainSampler`, `H3SeamDoctor`, `H3RunReport`, and the two EAV/FETA nodes.
2. Visit `http://127.0.0.1:8188/h3chain/ping` (adjust the port). A JSON response means the project-archive routes are mounted (the Director Console depends on them; if they are missing, the console shows a diagnostic banner).
3. Load `example_workflows/备用初始化导演台工作流.json`, adjust the model paths to your environment, and run.

---

## 6. Quick start

1. Load the bundled example workflow and download the weights it expects ([§5.3](#53-models-and-loras-for-the-bundled-workflow)).
2. Open the **Director Console** (full-screen, from the node or the sidebar mini-entry).
3. Write a prompt per segment (1–64 segments), or paste a multi-segment master prompt and let the console split it.
4. Queue. The first segment is generated; with auto-save on, `finals/seg_001.mp4` appears in the project folder, and when the chain finishes, `finals/final_<timestamp>.mp4` as well.

How the bundled workflow is wired (all official ComfyUI nodes; weights in [§5.3](#53-models-and-loras-for-the-bundled-workflow)):

- **Base pass (top row):** `UNETLoader` (hybrid int8) → `LoraLoaderModelOnly` (Turbo, 1.0) → `LoraLoaderModelOnly` (Motion Repair, 0.6) → `ModelAttentionBackend` → `BlockSparseAttention` → the node's *model* input.
- **Second pass (bottom row, shipped bypassed):** `UNETLoader` (same hybrid) → `LoraLoaderModelOnly` (Motion Repair, 0.25) → `ModelAttentionBackend` → `BlockSparseAttention` → the node's *second-pass model* input.
- **CLIP** — `CLIPLoader` with `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`, type **`minimax`** (Qwen3-VL).
- **VAEs** — video `minimax_h3_video_vae_int8_convrot.safetensors`, audio `minimax_h3_audio_vae_fp32.safetensors`.
- The bundled template uses one **hybrid** UNET for both t2v/i2v and r2v conditioning instead of separate `fl2va` / `ref2va` checkpoints. If you follow the official MiniMax H3 workflow instead, insert the official **ModelSamplingMiniMaxH3** (shift video 12 / audio 3) between the UNET and the node, and use `minimax_h3_fl2va_*` for t2v / i2v and `minimax_h3_ref2va_*` for r2v.

---

## 7. The Director Console

The console is a full-screen, state-driven control surface. Mode, prompts, first-frame, reference assets, and per-segment settings all live in the node's *director state* JSON — nothing is stored in canvas wiring.

Key areas:

- **Left rail — project archive.** Live disk scan of every project (cover / progress / updated time). Click to **load** (switch the archive directory and load that project's prompts); **🗑 delete** removes the whole folder; **＋ new project** creates the folder and a 0-segment manifest on disk immediately.
- **Center — segment cards.** One card per segment with its prompt, scene/character/soundscape/music fields, per-segment duration, reference chips, and controls: `🎲 reroll`, `⏸ pause`, `🔗 standalone`, `↺ reset second pass`, drag handle for reordering.
- **Right rail — second pass, semantic bridge, performance, prompt optimization settings.**
- **Asset library** — open from the left rail; also hosts **merge export** (see §18).

The console's prompts are persisted **in the project folder** (debounced write-back to the manifest ~1.5 s after editing, plus on project switch/create). Cut the power, restart — nothing is lost.

---

## 8. Prompt authoring

The plugin assembles the official three-field structure for you at runtime; **you only write the body text**.

```
integrated_multimodal_description: [Shot 1] {scene}. {characters}. {main prompt: picture and action timeline}
overall_soundscape: {ambient + action sound, 1-4 sentences}
non_diegetic_music: {score: instruments + tempo + dynamics, 1-3 sentences}
```

- **Assembly sources:** the segment card's scene / character / soundscape / music fields plus the main prompt box; canvas-direct mode wraps the main prompt the same way.
- **Empty = omitted.** Leaving soundscape / music blank drops the field entirely (= unconstrained, not "silent"). The official `N/A` value explicitly requests silence — do not use it casually.
- **Dialogue auto-conversion.** Chinese dialogue `「…」` in the main prompt is converted to the official `<d>[中文] …</d>`; hand-written `<d>` tags are left alone. **Give speakers stable IDs** (`短发女主 (S1) 轻声说：「……」`), reusing the same number for the same character across segments; silent characters get no number.
- **Official passthrough.** If the main prompt already contains any of `integrated_multimodal_description:` / `overall_soundscape:` / `detailed_description:`, the whole segment is sent verbatim (r2v users may hand-write the official six-section structure `subject_definitions` / `summary` / `retention_analysis` / `detailed_description` / `overall_soundscape` / `non_diegetic_music`).
- **i2v first segment.** When the first frame comes from the console's `first_frame` (or an asset tagged "first frame"), the first segment's prompt is prefixed with the official I2VA instruction line.
- **Per-segment frame references.** In first-frame video mode, each segment card can toggle whether it references the first / last frame image (`segments[i].frame_refs`; default = first segment references the first frame, last segment references the last frame). Toggling is part of that segment's hash, so changes re-run from that segment.
- **r2v reference mapping.** In multi-reference mode, a segment that does not explicitly write `<Picture k>` gets only a minimal mapping block (`[References]` + one line per resource); segments that do write tags are passed through.

### Official writing notes

| Element | Official convention |
|---|---|
| Shot notation | `[Shot 1]` opens with no timestamp; a cut is `[Shot 2] At 00:03.500, the camera cuts to...`, timestamps strictly increasing and inside the segment duration |
| Camera vocabulary | `Push In / Pull Out / Pan Left / Pan Right / Truck Left / Truck Right / Tilt Up / Tilt Down / Pedestal Up / Pedestal Down / Arc Shot / Tracking Shot / Static Shot / POV`, with `with small/large amplitude` and `at slow/fast speed` written as a natural clause |
| Dialogue | `<d>[English] line</d>` (language tag as `[English]` / `[中文]` etc.); off-screen voiceover is `says in an off-screen voiceover`; truncated lines use `<cutoff>`, cross-shot continuation `<scenetrans>` |
| Visible text | On-screen text (signs / subtitles / neon) goes in English double quotes and is **not translated**: `A red neon sign reading "营业中" glows above the doorway.` |
| Soundscape field | Only ambience + action sound + non-verbal vocalization; dialogue / a character singing / music the character can hear do **not** belong here |
| Music field | Only score the characters cannot hear; a radio or street performance they *can* hear is a scene event in the description line |
| Don'ts | Avoid abstract words (cinematic / beautiful), avoid plot-summary style, match description density to segment length (4–15 s), define every reference tag and keep it consistent, do not duplicate audio content across the two fields |

**Multiple shots in one segment:** write `[Shot 2] At 00:03.500, ...` directly in the main prompt. Chaining is already seamless — no need to force cuts between segments.

### Task-chain auto-detection (no `task_type` selection)

| Wiring | Chain | UNET |
|---|---|---|
| Nothing connected | pure t2v | fl2va |
| Console `first_frame` / asset tagged "first frame" | first segment i2v + continuation t2v | fl2va |
| *start video* (+ optional audio) | prologue (uploaded video) + continuation t2v | fl2va |
| Any reference group | every segment r2v (`<Picture i>` / `<Video k>` / `<Audio j>`), can coexist with the prologue | ref2va |

---

## 9. Key parameters

All controls are on the node (labels are Chinese in the UI).

- **`宽高比` (aspect ratio) + `百万像素` (megapixels).** The canvas is the only source: aspect ratio (21:9 / 16:9 / 9:16 / 4:3 / 3:4 / 1:1) × megapixels (0.1–2.0), 32-aligned. Presets: 0.2 draft (608×352) / 0.5 fast preview (960×544) / 0.98 H3 native (1344×768) / 1.0 (1376×768) / 2.0 oversample (1920×1088). `宽度` / `高度` remain only as legacy compatibility slots and are overridden.
- **`每段时长` (segment duration).** Visible seconds per segment @24 fps (0.5–15.0, default 8.0), auto-snapped to the 17k+5 frame grid (8.0 s → 192 frames, 5.0 s → 124 frames). Per-segment overrides live in the console.
- **`引导帧数` (guide frames).** The overlap bridge: how many of the previous tail's frames are pinned to the next head. Options 关闭 / 1 / 5 / 22 / 39 / 56 (grid points; default 22). `1` = single-frame anchor (official style, cheap); `关闭` = no automatic reference to the previous segment (hard cut, independent sampling), loudness alignment still applies.
- **`种子` (seed).** Segment *i* uses `seed + i`.
- **`步数` (steps).** Default **8** (distilled models). `CFG` default 1.0; sampler `res_multistep`; scheduler `simple`.
- **`自动存档` (auto archive).** Legacy compatibility switch, folded into *auto-save*; old workflows with `自动存档=自动存档` map to `自动保存=分段`.
- **`存档目录` (archive directory).** Project name: one folder per project under `output/h3_projects/`. Empty = auto-name from a parameter fingerprint; a fixed name pins all runs to that folder.
- **`桥帧门控` (bridge-frame gating).** Default **关闭**. `标注` = score and report only; `自动回退` = on a below-threshold tail, roll back 17/34 frames to a good one.
- **`清晰度阈值` (sharpness threshold)** default 30.0, **`回退上限` (rollback cap)** default 34 (multiples of 17).
- **`锚定加噪` (anchor noise)** default 0.0, **`递减锚定` (decaying anchor)** default 关闭. Both are *next-stage infrastructure*, off by default because measurement showed a net quality cost; kept as technical reserves.
- **`审片模式` (review mode).** Default 关闭; `逐段确认` = one new segment per run.
- **`自动保存` (auto-save).** Default `分段`; **`自动成片` (auto finalize)** default `开启`.
- **`重跑起始段` (rerun from segment).** 0 = auto (follow archive progress, re-do changed prompts); N = discard the archive from segment N and regenerate.
- **`接缝重摇` (seam reroll).** Default **关闭**. `自动` = re-sample a segment when its seam difference exceeds the threshold, keeping the smallest-difference attempt.
- **`重摇阈值` (reroll threshold)** default 0.06 (good seams ~0.02–0.03, bad 0.08+), **`重摇上限` (reroll cap)** default 1.
- **`生成模式` (generation mode).** Deprecated placeholder, folded and hidden — the chain is derived from actual references.
- **`导演台状态` (director state).** JSON written by the console; takes precedence over canvas wiring when present. Stores only relative input filenames — no media, keys, or absolute paths.
- **`起始视频` / `起始视频音轨`.** Prologue input (§15).
- **`二采模型` (second-pass model).** Optional dedicated UNET for the high-res second pass (§19).
- **`参考图像尺寸` (reference image size).** `match` (scale each reference to the generation canvas area, shrink-only) or `max` (2048 short side via the reference pipeline; best identity fidelity, potentially several times slower). Default `max`.
- **`响度对齐强度` (loudness alignment strength)** default 1.0.
- **`自定义Sigmas` (custom sigmas).** Optional sigma table for distilled LoRAs (§26).

**Outputs (order matters — see §22):** `图像 / 音频 / 报告 / 帧率`.

**Note on the `图像` output.** By default it is a **single placeholder frame**. Final rendering
goes through streaming concatenation of the per-segment mp4s, so the full-chain frame tensor is
never materialised in RAM — that tensor is **36.9GB** for 544×960 × 5895 frames (~4 min), and it
was what blew up a 64GB machine with `DefaultCPUAllocator: not enough memory`. Set the perf
setting `final_mode = memory` (强制内存帧编码) if you genuinely need the full-chain IMAGE output;
in that mode `frames_dtype = uint8` cuts the footprint to a quarter.

---

## 10. Project archiving

**One project = one folder under `output/h3_projects/`**, laid out like a game save:

```
output/h3_projects/
  h3chain_state.json              # current chain pointer (report text lives here)
  <project name>/
    manifest.json                 # prompts / params / progress / finals (atomic write)
    finals/
      seg_001.mp4, seg_002.mp4 …  # per-segment videos
      thumb_001.png …             # segment thumbnails
      final_<timestamp>.mp4       # final cut
      merged_<timestamp>.mp4      # merge-export output (may be several)
    latent/
      seg_001.pt …                # continuation latents (~5 MB each)
    assets/                       # project-linked assets
    texts/                        # prompt text files
```

- With `自动存档=自动存档` (or auto-save = segments), each finished segment writes its AV latent and the manifest atomically (per-segment seed / prompt hash / trim amount / title / `updated_at` / finals). After a crash at segment *k*, re-running with the same parameters loads segments 1…k from the archive (seconds) and resumes from *k+1*.
- The project name is auto-derived from the **shared parameter** fingerprint (resolution / duration / guide frames / sampling params — prompts excluded). **Changing a shared parameter refuses to resume** (start a new chain). Changing one segment's prompt re-does just that segment.
- The seed sequence is authoritative in the manifest; a multi-run resume is frame-identical to a single run.
- **The console is the archive manager** — list, load, delete, and create projects against the live disk.

### Archive HTTP routes

Registered in `routes.py` with a runtime fallback so 404/405 cannot happen. Every route is registered **twice** — once at the root path and once under `/api` — because ComfyUI 0.33.x only generates `/api` copies for routes known at startup, while newer front-ends force the `/api` prefix. See [§31](#31-http-api) for the full list.

- **Migration note.** Since v3 the archive root is `output/h3_projects/` (schema `h3seamless/ckpt-v3`); the old `output/checkpoints/` and `output/h3_auto/` are neither read nor written and can be deleted manually.

---

## 11. Per-segment review

`审片模式=逐段确认`: one queue = one new segment. Perfect for a "generate → watch → confirm" rhythm with no front-end interaction:

1. Run → only segment 1 is generated, then it returns. Watch `finals/seg_001.mp4` in the project folder (or the segment card preview). The `图像` / `音频` outputs are the accumulated cut of finished segments so far.
2. Satisfied → **just run again**; finished segments load from the archive (seconds) and segment 2 is generated. Repeat to the end.
3. Not satisfied → either edit segment *N*'s prompt and run (auto re-does from *N*), or set `重跑起始段 = N` and change the seed (reroll from *N*).
4. When all segments are done, run once more to assemble the final cut.

Review mode auto-enables archiving (otherwise cross-run continuation is impossible).

---

## 12. Re-running and rerolling segments

- **Prompt-change auto re-run** (`重跑起始段=0`): per-segment prompt hashing. Changing segment *N* rebuilds **only segment *N*** — double-anchored against segment *N-1*'s archived tail and segment *N+1*'s archived head, then slotted back. No cascade. Changing sampling params (steps / CFG / sampler / scheduler / model) triggers **no** redo; it only affects newly generated segments. Changing resolution is the one hard constraint → whole-chain redo or a new chain.
- **Targeted reroll** (`重跑起始段=N`): discard the archive from segment *N*, regenerate *N* and onward with `seed + segment number`. Without changing the seed it equals a plain redo; change the seed for new randomness.

---

## 13. Selective redo

`▶ continue from here` cascades through all following segments. To swap out only a few segments and keep the rest:

1. On a finished segment, click `🎲 reroll` → choose an **anchor mode** (default double) → *mark*. Mark as many segments as you like; cards show a `🔁 pending reroll · mode` badge.
2. Submit with the footer's `🎲 reroll N marked segments`. A random seed is derived and review mode is temporarily enabled; the queue snapshot goes into `manifest.redo_queue`. Pending marks can be cancelled.
3. Review mode advances **one segment per run**; when done, one more run reassembles the full cut (or use merge export).
4. **Anchor modes** (temporary strategy for this reroll, independent of the segment's *standalone* attribute): double (seamless replacement) / previous only / next only / none (free, hard cut at both ends). Explicit identity anchors (last-frame image, per-segment tail anchor) take priority.
5. **Seed bump:** if a rerolled segment's seed equals the archive's, it is auto-incremented → the segment fingerprint changes → its second-pass record is invalidated and re-rendered, **without** truncating later segments' second-pass records. This is where the time savings come from.
6. Reroll marks and `⏸ pause` are mutually exclusive (enforced on both ends).

---

## 14. Segment ordering and disabling

- **Drag to reorder:** the `⠿` handle on a card reorders execution (with `⬆`/`⬇` fallbacks). Zero back-end changes — reordering changes the per-segment hash sequence, so it re-runs from the reorder point; reroll marks and second-pass selections inside the moved range are cleared.
- **Disable a segment (`⏸ pause`):** keeps the segment in the chain but skips execution and excludes it from the final cut (translucent dashed outline). Slots stay stable, latents are kept, and toggling is **zero-cost**. The bridge across a disabled segment reuses the last executed tail. Both base-resolution and high-res final assembly exclude disabled segments.

---

## 15. Prologue: continue from an uploaded video

1. Wire `LoadVideo`'s `frames` → *start video*, and its `audio` → *start video audio track* (unwired = silent prologue).
2. Run: the uploaded video is encoded as **segment 0 (prologue)** into the archive; the final cut starts with it and generation continues from its tail (segment numbers shift by one).
3. Conventions: processed at **24 fps**; frames are floored to the 17k+5 grid; if longer than the segment duration, only the first part is taken. The prologue goes through one VAE re-encode (noted in the report).
4. Changing the video = changing the chain (the prologue fingerprint is hashed); the prologue itself cannot be "rerolled".
5. Mutually exclusive with a first-frame image (both are segment 1's visual origin); it **can** coexist with reference assets (r2v).

---

## 16. Standalone shots (hard cut)

For a shot genuinely unrelated to the previous one (scene change, flashback, parallel narrative), forcing a continuation is harmful. Tick `🔗 standalone` on the segment card to disconnect everything:

- **No bridge injection** (video and audio both cut) — the segment starts from pure noise.
- **No head trim** — the full segment is kept.
- **No seam post-processing** and no head loudness alignment; seam metrics record `N/A`.
- The previous segment does not construct a tail bridge for it either.
- The card shows a *standalone* badge and the segment rail marks it in orange.

The flag is part of that segment's prompt hash (`unlink|` prefix), so toggling it re-runs from that segment. It only affects the seam between *this* segment and the previous one; two adjacent standalone segments are also hard-cut between themselves.

---

## 17. Manual anchors

Pinning an external clip / image / previous tail / finished segment / latent-library entry to a specific position in a segment as a guide keyframe. Anchors act during **generation only** — they do **not** enter the final cut (to mix an external clip into the cut, use merge export).

Usage (the console's dual-track timeline):

1. **Source track:** pick the material — ① previous tail `prev_tail` (default bridge) ② finished segment `segment` ③ latent library `library` ④ external video `video` ⑤ image `image`. Drag a window on the frame strip to select the source frame range.
2. **Target track:** pick where it lands — `head` / `mid` (any frame index, negative counts from the tail) / `tail` (single-frame identity anchor). Any number of anchors, fully free placement.
3. **Window width:** locked to 17k+5 steps (5 / 22 / 39 / 56 / 73 …; cap 362 for a single encode); single-frame identity anchors use 1.
4. **Branch:** image+audio / image-only / audio-only.

Key semantics:

- **The only hard constraint is resolution** (C/H/W must match, else the bridge cannot be assembled). With a source, the window is decoded and center-covered to the project canvas before VAE encoding; with **no usable source it errors out** (no silent fallback).
- **Triggers redo:** changing the anchor / prompt / material / segment duration rebuilds that segment only (double-anchored; no cascade).
- **Does not trigger redo:** sampling params (steps / CFG / sampler / scheduler / model / fade_ratio / gate) affect only newly generated segments.
- **Old archives** whose manifest has a non-empty `inserts` (used the old "insert video" feature) error out and ask for a new project — no migration, no silent degradation.

Unlike the prologue, an anchor can land at any segment's head/mid/tail and never enters the cut.

---

## 18. Merge export

Concatenate existing segments / finals / external videos in a chosen order into one new file — no re-run. The entry point is in the **asset library** (left rail → open library → the **`⧉ merge export`** button at the far right of the title bar):

1. Click `⧉ merge export` to enter selection mode; the same slot becomes `⧉ start merge` + `✕ exit merge`. The list shows videos only; **clicking a tile adds it to the list, and click order is concatenation order** (numbered 1/2/3/4 badges; click again to remove; double-click to preview).
2. To mix in an external video, use the library's own upload, then click its tile.
3. Click `⧉ start merge` → the back-end streams the concatenation (PyAV, H.264 + AAC, unified 24 fps) into `finals/merged_<timestamp>.mp4`; the console jumps to the final-cut area when done.
4. Merge records are written to `manifest.merges` (never cleared, traceable). Exiting selection mode (or a successful merge) clears the list immediately.

Rules: the list is memory-only (no archive writes, no chain changes, no redos); the canvas follows project parameters (mismatched sources are scaled to fit; missing params fall back to the first source's actual size); silent sources get silence padding (output always has an audio track); sources may be library entries, segments, or any mp4 in the project / `input` directory (with directory-traversal checks). Only videos can be selected. While a merge runs, the console's generate buttons are paused.

---

## 19. Latent-upscale second pass

Integrates the latent-upscale network from [LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler](https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler) and the second-sampling paradigm from [wjluoxiao/ComfyUI-JZL-MiniMax-H3](https://github.com/wjluoxiao/ComfyUI-JZL-MiniMax-H3). The second pass is a **render channel inside the main loop**: after a segment is finalized (reroll / gating / cut decisions all done, latent archived, before the segment is written) it immediately runs **neural upscale H×W** (time dimension unchanged, 24-channel H3 VAE latent, LATENTS_MEAN/STD normalized) **→ low-strength re-sample** (official `common_ksampler`, denoise < 1) **→ decode**. The saved segment video and the final cut are the high-res result directly.

1. **Install weights** (§5.2).
2. Open the right rail's **latent upscale second pass** panel and pick a mode: *follow generation* (run the second pass as each segment is finalized), *manual* (tick segments and run), or *off*.
3. Base params: upscale model (scanned from the directory), architecture (must match the weights), scale 1.0–4.0 (latent even-aligned = pixel multiple of 32), second-pass strength 0.05–1.0 (0.35–0.55 typical), second-pass steps / CFG (1.0 typical for H3), precision fp32 / fp16 / bf16.
4. Anti-blur extras: STG skip-block guidance, multi-round decaying refinement, latent / pixel dual-domain sharpening, standard / high / extreme encode tiers, independent second-pass sampler and scheduler, detail-gain retry. All new options default to off / standard and do not change old workflows; only enabled options enter the second-pass fingerprint.
5. When records are complete and sizes match, the final cut **streams directly from the high-res segments** (`final_<timestamp>.mp4`, using the selected CRF / preset / AQ tier). Incomplete records / mixed sizes / a failed concat fall back to base-resolution encoding; the high-res segments are unaffected. Segment cards show a `second pass W×H` badge; `↺ reset second pass` clears the record for a re-render.

Key semantics:

- **Audio is carried over untouched** — the re-sample's audio output is discarded; the segment and final tracks are the original (zero audio regression).
- **Redo rules:** a second-pass param change (fingerprint change) re-renders all in-range segments' high-res versions; the base chain is untouched. A base-chain redo from segment *k* clears second-pass records ≥ *k* and re-renders on the next run. A prompt / seed change invalidates only that segment. A single-segment second pass depends only on that segment's latent + bridge-anchor latent + second-pass params.
- **True second pass (upscale then re-sample):** the network super-resolves the latent to scale×, the cond / bridge / tail anchors are built at the **high-res target resolution** (CondSync upscale bridge), and the low-strength re-sample runs on the **high-res latent** — the upscale output is genuinely consumed, no "wasted upscale".
- **Preemptive VRAM management:** H3 UNET fp16 is ~26 GB resident; 2× high-res activation is about 4× base. Before re-sampling, `unload_all_models()` moves CLIP / video VAE / audio VAE to CPU (only the UNET is reloaded); the upscale network is also moved back to CPU after use. An actual OOM first auto-unloads resident models and retries once (zero param change, zero degradation); if that still fails, or the pre-check judges it insufficient, the whole chain stops with a prompt to lower the scale / steps / σ — rather than silently mixing high-res and base-resolution segments.
- **Failure handling:** a missing / mismatched upscale model is reported before the run and the second pass is skipped for that round; canvas / VRAM pre-check failures and sampling OOM stop the chain; other per-segment exceptions record and fall back to that segment's base save with a forced re-encode.
- **Dedicated second-pass model / LoRA:** the optional *second-pass model* socket (`UNETLoader → LoraLoader → this socket`) supplies an independent chain for the high-res pass; unwired = reuse the base *model*. Its 8-char structural signature is recorded in the report and `manifest.upscale.segs[N].model` but does **not** enter the fingerprint — swapping it does not auto-redo existing high-res segments (the report warns; use `重跑起始段` or `↺ reset second pass` to force it).

---

## 20. Bridge-frame gating

- `桥帧门控=标注` (default **关闭**): score each segment tail into the report; no behavior change — run a few chains to see the score distribution before setting a threshold.
- `桥帧门控=自动回退`: when the tail score is below `清晰度阈值`, roll back 17/34 frames (limited by `回退上限`, snapped to the 17k+5 grid) to a passing frame for the bridge; that segment's visible output shortens accordingly, and the timeline stays continuous. If no tier passes, the original tail is kept with a report warning.

---

## 21. Seam Doctor

Attach `H3SeamDoctor` next to the sampler (measure-only, it does not touch generation). Wire `图像` / `音频` / `帧率` to the sampler's matching outputs. Per seam:

- `[curve]` per-frame difference around the seam (`▮` marks the seam), `[strength]` seam difference as a multiple of the chain median
- `[color]` RGB-mean ΔE and histogram difference; `[sharp]` Laplacian sharpness change
- `[structure]` NCC (same picture continuing vs content actually changed)
- `[shift]` whole-frame translation search — residual collapse after a small shift = **motion teleport**
- `[gap]` triangular test for a **time gap**: seam diff ≈ N frames of evolution + shift can't fix it + same scene continuing
- `[repeat]` post-seam replay detection, `[audio]` RMS / centroid / pops, `[assembly]` consistency check
- Summary: seam-strength ranking, type distribution, worst-seam location (frame + timecode)

**Three ways to read the report:**

1. **Saved file (recommended):** every run writes `ComfyUI/output/h3_seam_doctor/report_<timestamp>.txt`.
2. **Front-end hover:** hover the node's `报告` output dot to see the content, or pin the value to the Node Feed sidebar.
3. **Show Text node:** if ComfyUI-Custom-Scripts (pysssss) is installed, `报告` → Show Text (optional).

The `对比图` output is a per-seam `before | after | residual false-color (×4)` strip — wire it to `PreviewImage`.

---

## 22. Run report (wire-free)

**The problem it solves.** The console's `报告` is a *wire output*, and ComfyUI stores wires **by slot index** (the workflow JSON's `links` records `origin_slot`). Change the output order once and an old archive's wire silently lands on another slot — the canvas still looks connected, but the downstream node receives nothing. This is exactly what happened when `帧率` was inserted before `报告`: the report wire fell onto the hidden `帧率` slot, so **every ComfyUI restart disconnected the console and the `PreviewAny` report node**.

**Two fixes:**

1. **Structural (root cause, nothing for you to do).** The main node's output order is now `图像 / 音频 / 报告 / 帧率` — the `帧率` **parking slot** (never wired, never drawn) is moved to the end and `报告` is pinned back to **slot 2**. Archives written before rev3 used `origin_slot=2` too, so old and new archives are self-consistent, and hiding / trimming the trailing slot can never shift a visible port again. The old "realign wires by name after load" logic is kept as a fallback for historical misaligned files only.
2. **Wire-free node (optional fallback).** `H3RunReport` has **zero input sockets**, so "the wire broke" cannot structurally occur. The report text is read by the front-end straight from `GET /h3chain/projects → state.report` (the report is already persisted per segment in `output/h3_projects/h3chain_state.json`), shown in a scrollable area with a `🔄 refresh` button and auto-refresh after each run.

**How to use it:** to drop wiring entirely, delete the `PreviewAny` attached to `报告` and drop in `H3RunReport` instead — **wire nothing**, place it anywhere. The original `PreviewAny` can also stay (the structural fix means it connects correctly again).

> **Maintainer rule.** New outputs are always appended at the end; hidden / parking slots only ever go last. A visible port's index is a wire coordinate — moving it silently rewires every saved workflow. Changing `nodes.py`'s `outputs` order changes archive semantics. Guards: `tests/test_workflows_frozen.py`, `tests/js/desk_node_skin_check.js`, `tests/js/desk_slot_geometry_check.js`, `tests/test_run_report.py`.

---

## 23. Semantic bridge

`semantic_bridge.py` inlines the BUNNY H3 conditioning bridge (from FourBunny / JOKER141). Instead of a standalone node on a CONDITIONING line, the same computation runs inside the render chain: the cond tensor `[B, T, 5120]` is passed token-wise through a 5120→512→512→5120 bottleneck MLP and blended with the original by `alpha`:

```
out = t + alpha * (mlp(rms(t)) - t)
```

It helps H3 hold relationships like "who is doing what / prop ownership / attacker vs target / identity and state after occlusion". It is **not** a motion-repair tool and does not rewrite prompts. The author's own note: roughly 60% of cases improve, 20% are neutral, 10% introduce new errors — more strength is not better; compare at the same seed.

The weight ships in `models/BUNNY_H3_Semantic_Bridge_V2_seed22345.safetensors` (≈22 MB, committed). The console's semantic-bridge panel scans `models/` for the dropdown. The scope switch exists because the cond tensor mixes text and vision tokens (H3 does not use a chat template; reference / first-frame images are spliced into the sequence as vision blocks) — scope lets you apply the bridge to text tokens only, vision tokens only, or all.

---

## 24. Enhance-A-Video / FETA

`H3EAVFetaPatch` adapts Enhance-A-Video / FETA to H3: it measures and scales only the target-video attention rows of the packed sequence; text / conditioning / reference / audio rows are untouched. Wire it at the chain tail, after LoRA and accelerator nodes, before the sampler's *model* input. It needs no sigmas input (progress is read from the sampler's written sigma).

- Default mode is **report-only** — check the CFI / g values in the console log before enabling *apply*.
- The reference project measured g averaging only ~1.00034 on H3, i.e. a very small gain — measure before enabling.
- `H3EAVFetaReport` (optional) outputs the last run's CFI / g statistics as JSON and passes the image through; not wiring it changes nothing.

Source and license details are in [`docs/EAV_FETA.md`](docs/EAV_FETA.md).

---

## 25. Prompt optimizer and expander

`optimizer.py` is a self-written prompt optimizer (cloud: OpenAI-compatible / Gemini / Responses; local: Transformers / GGUF). The console's **prompt optimize** and **AI expand + optimize** buttons call it. `tools/h3_prompt_expander/` is the standalone, executable version of the official MiniMax H3 prompt format (see its own [README](tools/h3_prompt_expander/README.md)).

Two details worth knowing:

- **Language has a single source.** The *prompt rules* field decides the language (`auto` = official English rules, default / `zh` = Chinese rules / `none` = no injection). There is no separate `output_language` field — two controls saying different things only fight.
- **Reference marks have two numbering systems.** The mark shown to the LLM (`图片1`) and the official tag written into the body (`<Picture 1>`) are numbered differently and are not interchangeable. On the way back both are translated to `@<asset name>`; anything untranslatable (out-of-range index, asset no longer in the pool) is left as-is with a warning. `<Subject N>` is intentionally not translated (it is an abstract reusable-content label, not an asset reference).

> **API keys** live only in `optimizer.local.json` (gitignored) and are never written into source. The settings panel's *Save* writes **both** the current workflow's chain state **and** that file (`POST /h3chain/optimizer-config`), so a key saved once is reused by every new workflow (new / imported / default template) instead of having to be re-entered each time — and the panel never echoes the key back (it only reports *which* providers have one). Guard: `tests/test_optimizer_glm.py::test_no_api_key_committed_in_source` — run it after touching any tool script.

---

## 26. Custom sigmas (distilled LoRAs)

Distilled LoRAs (HyperFlow 8-step, H3 Turbo, …) are trained with their own sigma table; recomputing one from "steps + scheduler" does not match the distillation trajectory. The optional `自定义Sigmas` socket takes an external sigma table (e.g. from `ApplyHyperFlow` / `ManualSigmas` / `BasicScheduler`) and overrides `steps` and `scheduler` for the base pass (actual steps = number of sigma points − 1). Unwired, behavior is byte-identical to before. The HyperFlow official 8-step table is `1.0, 0.931506, 0.839236, 0.703462, 0.5, 0.296538, 0.160764, 0.068494, 0.0`. See [`docs/HyperFlow适配与自定义Sigmas.md`](docs/HyperFlow适配与自定义Sigmas.md).

---

## 27. Asset library

`library.py` provides a browser over four scopes (assets / finals / latents / texts), modelled on Majoor Assets Manager. `asset_store.py` keeps the global-library + project-link two-layer model with the manifest as the single source of truth; `asset_hub.py` retains only the pure validation / tag-normalization functions. Reference assets reach the model **only** through the console's three panels (tile drag-drop / `@` references) — the console writes the asset list into the chain state (`ds.ref_assets`), so **no canvas wiring is needed**. Per-segment caps match the official node:

- **9 images** (`<Picture i>`), **3 videos** (`<Video k>`, 24 fps, 2–15 s), **3 standalone audio** (`<Audio j>`). A reference video's own audio is auto-paired to the same-numbered `<Audio j>`.
- Tag numbers count **from 1 in the order referenced within the segment** (official semantics). Any reference asset makes the whole chain r2v — switch to a `ref2va` UNET.

---

## 28. Performance settings

`perf.py` holds hardware judgments and scenario strategy as pure functions. The console exposes machine-related settings that persist globally (across projects): OOM auto-retry, activation-peak probe, frame dtype, final-mode, tile splitting, block swap knobs, etc.

> **VRAM note.** On a DynamicVRAM build, aimdo deliberately reserves `model_size × 10` of virtual address space as a weight page cache, so "free VRAM ≈ 0" is by design, not a leak. The real switch is `comfy.memory_management.aimdo_enabled` — do not inspect `sys.modules` for it (it is always `True`).

> **Block swap.** Do not write your own block-swap logic — the official `comfy/ldm/minimax/model.py` prefetch queue runs unconditionally and will conflict with the official `partially_load` / vbar, break LoRAs, and hit aimdo's external-pin ban. Use only the three supported knobs: `blocks_swap_on` / `blocks_to_swap` / `blocks_prefetch`.

---

## 29. Seam metrics

"Good seam?" is not just one frame difference. The sampler measures a **five-dimensional z-score** per seam (seam value vs the segment's robust median / 1.4826×MAD baseline), written to `manifest.seam_metrics`; the Seam Doctor report adds a `[baseline]` line.

| Metric | Implementation | Attribution |
|---|---|---|
| boundary optical flow `flow_z` / acceleration `flow_accel_z` | OpenCV Farneback + second-order velocity difference | motion discontinuity |
| `lpips_z` | `lpips` (AlexNet); falls back to gradient-structure difference if missing | overall temporal jump |
| embedding drift `emb_z` | `open_clip` cosine distance; falls back to RGB histogram if missing | appearance drift |
| camera `cam_z` | ORB + affine decomposition (translation / rotation / scale rates) | camera jump |
| pose `pose_z` | DWPose (controlnet_aux), off by default | geometry discontinuity |

- **Acceptance line: `|z| < 2.0` is a good seam** (the seam metric lies inside the segment's normal distribution).
- All dependencies are optional (`cv2` ships with ComfyUI; `lpips` / `open_clip` degrade gracefully) — never a hard dependency.
- Attribution cheat-sheet: high CLIP / embedding → appearance, high flow → motion, high camera → camera, high LPIPS → overall temporal, high pose → geometry.

---

## 30. Repository layout

```
ComfyUI-minimaxH3-SequenceForge/
├─ __init__.py               # Entry point: registers nodes, declares WEB_DIRECTORY=./web,
│                            #   mounts HTTP routes (extension hook + PromptServer fallback)
├─ nodes.py                  # H3SeamlessChainSampler — the main orchestration loop (largest file)
├─ seam_doctor.py            # H3SeamDoctor — seam root-cause diagnostics (measure only)
├─ run_report.py             # H3RunReport — wire-free run report node
├─ eav_feta.py               # H3EAVFetaPatch / H3EAVFetaReport — Enhance-A-Video / FETA
│
├─ checkpoint.py             # Archive engine: shared-parameter fingerprint, per-segment prompt hash,
│                            #   atomic manifest read/write, segment AV latent persistence
├─ projects.py               # Game-style project archive + merge-export implementation
├─ anchors.py                # Manual-anchor data structures (normalize / validate / migrate)
├─ guides.py                 # Official MiniMaxH3AddGuide anchor-semantics alignment (pure functions)
├─ grid.py                   # H3 frame-grid math: token↔pixel-frame mapping, audio-window conversion
├─ qc.py                     # Bridge-frame scoring (Laplacian sharpness + exposure)
├─ metrics.py                # Five-dimensional seam-quality metrics (z-scores)
├─ cond_cache.py             # CLIP text-encode LRU cache (skip repeated TE forwards)
├─ media.py                  # Shared PyAV encode/decode (segment / final / merge / upload decode)
├─ latent_tools.py           # In-library latent transcode (extract latent from a video/final)
├─ transcode_queue.py        # Background transcode job table + progress + cancel
│
├─ upscale.py                # Latent-upscale second pass (render channel inside the main loop)
├─ upscale_net.py            # Second-pass upscale networks (2D residual / pure 3D) + weight loading
├─ semantic_bridge.py        # BUNNY H3 semantic conditioning bridge (inlined)
├─ sigmas_adapter.py         # External sigma-table adapter for distilled LoRAs
├─ perf.py                   # Hardware profile & scenario strategy (pure functions)
│
├─ routes.py                 # HTTP routes (/h3chain/*) — the console's data API
├─ optimizer.py              # Prompt optimizer back-end (cloud + local channels)
├─ prompts.py                # Official H3 format contract (frame math / alignment / parsing)
├─ library.py                # Asset-library index layer (four scopes)
├─ asset_store.py            # Global library + project link, two-layer asset storage
├─ asset_hub.py              # Asset-manifest validation & tag normalization (pure functions)
├─ launch_ref.py             # ComfyUI launch-command reference (single source of truth)
│
├─ web/                      # Front-end (shipped via WEB_DIRECTORY, auto-loaded by ComfyUI)
│   ├─ h3_director.js        # Director Console (full-screen) + sidebar mini-entry
│   ├─ h3_default_workflow.js# Default workflow template
│   ├─ h3_assets.js          # Asset panel
│   ├─ h3_library.js         # Asset library UI
│   ├─ h3_latent.js          # Latent library UI
│   ├─ h3_prompts.js         # Mark codec + prompt rules (single source)
│   ├─ h3_api.js             # API helpers
│   ├─ h3_report.js          # Run-report node skin (scrollable + refresh)
│   └─ h3d_anchor.js         # Manual-anchor dual-track timeline
│
├─ example_workflows/
│   └─ 备用初始化导演台工作流.json   # Director-Console init workflow (models + main node + 3 prompts pre-wired)
│
├─ skills/                   # Agent skills shipped with the plugin
│   ├─ README.md
│   └─ h3-longvideo-prompt/  # "Write a multi-segment long-video prompt" skill (SKILL.md + references/)
│
├─ prompt/                   # Official prompt-writing rules (base + ref2v, EN + ZH)
│   ├─ minimaxh3_base_prompt_writing{,_zh}.txt
│   └─ minimaxh3_official_ref2v_prompt_writing{,_zh}.txt
│
├─ tools/                    # Dev / verification tools (not runtime)
│   ├─ h3_prompt_expander/   # Standalone prompt expander (CLI + service) — has its own README
│   ├─ anchor_ui_smoke.cjs   # Front-end smoke test (must be 0 errors before UI delivery)
│   ├─ make_anchor_preview.py# Generates the anchor preview from web/ source
│   └─ …                     # Benchmarks, probes, simulators
│
├─ tests/                    # 55 Python test modules + 30 jsdom front-end checks
│
├─ models/                   # Bundled semantic-bridge weight (~22 MB, committed)
├─ docs/                     # Engineering documentation (not runtime)
│   ├─ UPSTREAM.md           # Upstream tracking ledger (upscale network) — read before editing upscale_net.py
│   ├─ EAV_FETA.md           # Enhance-A-Video / FETA sources & licensing
│   ├─ HyperFlow适配与自定义Sigmas.md
│   ├─ upstream_snapshot/    # Upstream baseline snapshots for diffing
│   └─ …
│
├─ .deploy/                  # Reference deployment scripts for the cloud 3090 box
│                            #   (model downloads, watchdog, safetensors verification)
├─ pyproject.toml            # ComfyUI plugin metadata
├─ requirements.txt          # Empty dependency declaration (comment only)
└─ README.md                 # This document
```

Runtime output (not in the repo): cuts / segments / archives live in `ComfyUI/output/h3_projects/<project>/`; Seam Doctor reports in `ComfyUI/output/h3_seam_doctor/`.

---

## 31. HTTP API

All routes are registered under both the root path and `/api`. Groups:

- **Diagnostics / status:** `ping`, `busy`, `perf`, `launch_ref`, `vram_cleanup`, `grid_spec`
- **Projects:** `projects`, `project`, `create_project`, `save_prompts`, `delete_project`, `delete_file`, `merge`, `upscale_reset`, `redo_cancel`, `latent_slice`, `latent_delete`, `trim`, `probe`, `move_media`, `split_av`
- **Models / config:** `upscale_models`, `bridge_models`, `vae_files`
- **Prompt optimization:** `optimize`, `optimize_stream`, `optimize_multi`, `optimize_multi_stream`, `expand`, `expand_multi`, `expand_validate`, `expand_optimize`, `expand_optimize_stream`
- **Assets / library:** `assets`, `asset_check`, `asset_links`, `asset_link`, `asset_mark`, `asset_unlink`, `asset_mirror`, `assets_repair`, `import_asset`, `compile_refs`, `library_upload`, `library_file`, `lib_list`, `lib_item`, `lib_thumb`, `lib_raw`, `lib_status`, `lib_scan`, `lib_rate`, `lib_tag`, `lib_alias`, `lib_mirror`, `lib_archive`, `lib_stage`, `lib_zip`, `lib_zip_file`, `lib_delete`, `lib_ref`, `lib_role`, `lib_collections`, `lib_collection_save`, `lib_collection_delete`
- **Anchors:** `anchor_sources`, `anchor_sheet`, `anchor_sheet_build`
- **Transcoding:** `transcode_submit`, `transcode_jobs`, `transcode_job`, `transcode_cancel`

---

## 32. Upstream tracking

The second-pass upscale network is ported line-by-line from [LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler](https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler) (only the **network structure and weight loading**, not its node layer or inference interface). The tracking ledger, the difference list vs. upstream, and the sync SOP are all in **[`docs/UPSTREAM.md`](docs/UPSTREAM.md)** — **read it before editing `upscale_net.py`** so you do not "fix" a deliberate local enhancement back into a bug or re-research something upstream already implements.

---

## 33. Removed in 1.0

The following were removed from the plugin. Historical notes are available in `docs/` and the git history:

- **`H3StoryboardChain`** (storyboard chaining) — not part of the Director Console mainline.
- **`H3ChainSaver`** (final-save gallery) — folded into the main node's *auto finalize* switch.
- **`H3AssetHub` / `H3AssetBundle`** — assets now flow only through the console state.
- **`H3MediaToLatent` / `H3LatentExtract` / `H3LatentUpscale`** — latent transcode / in-library upscale moved into the automatic path and `latent_tools.py`.
- **Seam post-processing profiles** (latent refine / smoothstep pixel blend / smart cut) and their controls — removed; only *seam reroll*, *bridge-frame gating*, and *anchor noise / decaying anchor* remain.
- **Old example workflows** (`h3_chain_t2v.json`, `h3_chain_r2v_official_base.json`, `h3_storyboard_base.json`, `h3_chain_storyboard_review.json`) — recoverable from git history.

---

## 34. Credits and licensing

This plugin is released under **Apache-2.0**.

It builds on and adapts several third-party projects. Their licenses are noted here for accuracy:

- **MiniMax H3 prompt format** — official [`MiniMax-AI/MiniMax-H3`](https://github.com/MiniMax-AI/MiniMax-H3) `skills/h3-prompt-writing` (`SKILL.md` + `references/base-en.txt` + `references/ref-en.txt`). The skeleton is copied verbatim; the body is authored by the user.
- **Latent-upscale network** — [LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler](https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler).
- **Second-sampling paradigm** — [wjluoxiao/ComfyUI-JZL-MiniMax-H3](https://github.com/wjluoxiao/ComfyUI-JZL-MiniMax-H3).
- **Enhance-A-Video / FETA** — three layers: the paper ([arXiv:2502.07508v3](https://arxiv.org/abs/2502.07508)); the official implementation [NUS-HPC-AI-Lab/Enhance-A-Video](https://github.com/NUS-HPC-AI-Lab/Enhance-A-Video) (**Apache-2.0**); and the H3 adaptation [T8mars/comfyui-minimax-h3-audio-T8](https://github.com/T8mars/comfyui-minimax-h3-audio-T8) (**GPL-3.0-or-later**). "FETA" is **not** a paper term — it comes from the T8 source header. Details in [`docs/EAV_FETA.md`](docs/EAV_FETA.md).
- **Semantic bridge** — FourBunny / JOKER141's BUNNY H3 Conditioning Bridge.
- **Asset library** — interaction model modelled on Majoor Assets Manager (see `docs/三库重构方案_基于Majoor资产管理器.md`).
- **Default-workflow weights** — the bundled template's UNET and LoRAs are third-party, not part of this plugin: [`smhfacct/Minimax-H3-fl2va-ref2va-hybrid-models`](https://huggingface.co/smhfacct/Minimax-H3-fl2va-ref2va-hybrid-models), [`drbaph/MiniMax-H3-Turbo-Lora-ComfyUI`](https://huggingface.co/drbaph/MiniMax-H3-Turbo-Lora-ComfyUI) (**Apache-2.0**), [`JOKER141/MiniMax-H3-General-Motion-Continuity-Repair`](https://huggingface.co/JOKER141/MiniMax-H3-General-Motion-Continuity-Repair). Download links and notes in [§5.3](#53-models-and-loras-for-the-bundled-workflow).
