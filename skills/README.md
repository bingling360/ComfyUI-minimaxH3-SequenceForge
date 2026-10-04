# H3 long-video prompt writing

A skill that turns a single sentence from the user into **a multi-segment prompt you can paste straight into SequenceForge's master-prompt box**.

```
one sentence from the user
   │
   ├─ interview (duration / segment count · reference images · content · continuity · style · dialogue · sound)
   │
   ├─ reference-image branch
   │    ├─ images available → use them for every segment by default, continue
   │    └─ none → ask → deliver "reference-image prompt appendix + first draft" → wait for the user to generate
   │                → read the actual images → correct → final version
   │
   ├─ expand the screenplay segment by segment (content only, **no creative cap**)
   ├─ compress each segment into the official H3 format (English; Ref2VA six-section / standard three-field)
   ├─ assemble the multi-segment text (English segment headers + Duration / Standalone / Prompt)
   └─ attach a Chinese reference version (field-by-field, skeleton still verbatim English)
```

Every delivered segment satisfies: **official format** + **length matched to its duration** + **continuous plot across segments** (`Standalone: no` makes the next segment pick up from the previous one).

Delivered as **two parts**: the English multi-segment text first (the only thing to paste), then the Chinese reference version (for reading only).

## Form

**Pure prompt, zero dependencies, zero API keys.** Reading `SKILL.md` and the modules it references is enough to produce the result — no scripts, network, or runtime required.

## File structure (short main file, one job per module)

| Path                                  | Purpose                          |
| ----------------------------------- | --------------------------- |
| `SKILL.md`                          | Flow orchestration + three hard rules + step-by-step delivery (**kept short**) |
| `references/01-interview.md`        | What to ask, how to ask it, how many rounds                 |
| `references/02-expand.md`           | Screenplay expansion: beat/word-count tables, inter-segment continuity, starting moves for both environments    |
| `references/03-optimize.md`         | Official-format compression: field sets, rule routing, prohibitions, finished word counts     |
| `references/04-master-format.md`    | Master-prompt-box syntax: segment headers, three tags, `@asset name`, pitfalls    |
| `references/05-reference-images.md` | Full reference-image flow (both branches + where to reference)      |
| `references/06-rules/*.txt`         | **Two** official English rule texts (chosen per mode, verbatim)  |
| `references/07-checklist.md`        | Pre-delivery self-check list                     |
| `references/08-example.md`          | A complete two-segment example (read before starting)               |

The main file only orchestrates; details are all pushed down into modules — **no single file is long, so generation does not lose focus**.

## Provenance

The rules are not newly written; they are extracted from the live implementation in `ComfyUI-minimaxH3-SequenceForge`:

| Part     | Corresponding implementation                                                                                               |
| ------ | -------------------------------------------------------------------------------------------------- |
| Expansion     | `tools/h3_prompt_expander/screenplay.py` + `prompts/system_screenplay.md` / `system_outline.md`    |
| Optimization     | `optimizer.optimize_once` / `build_system_prompt` + `prompt/*.txt` (the **two official English texts** are copied verbatim into `06-rules/`) |
| Master-prompt format | `web/h3_director.js`'s `parseMasterPrompt` / `mpRenderState` / `refsFromText`                      |
| Reference-image flow  | New (the original project had no such step)                                                                                        |

## Verification

- The two rule files are **md5-identical** to their sources (verbatim official text, no transcription drift).
- Validated against a **real parser** (the `parseMasterPrompt` / `refsFromText` functions extracted from `h3_director.js` and run in jsdom) on a complete example:
  segment count / `Duration` / `Standalone` / six fields present and ordered / blank lines preserved / shot numbers continuous / `[Shot 1]` has no timestamp / timestamps increasing and within duration /
  main description word count within 20–40 chars per second / inter-segment continuity — all pass.
- A first simulated run produced a **main description of only 180 Chinese characters (below the 200 lower bound for a 10-second segment)**; `07-checklist.md` caught it and it was sent back for more detail — which is exactly the point of the self-check list.

## Unified conventions (settled 2026-09)

- **Body is English by default; Chinese is optional**: the language is decided by **the single "prompt rules" field** (`auto` = official English rules · default / `zh` = Chinese rules / `none` = no injection).
  The separate `output_language` config field has been removed — two controls each saying something different only fight (historical bug: rules asked for Chinese, the system prompt asked for English, and the output was mixed-language body text).
- **The Chinese rules returned after being rewritten from the official text**: `prompt/*_zh.txt` corresponds **clause by clause** to the English version, changing only the "write the body in Chinese" item;
  the skeleton (field names / `[Shot N]` / `<d>` / official tags / retention markers / camera vocabulary) is **verbatim English in both languages**.
- **Segments are routed by type, not by language**: reference assets → full six-section reference format; first/last frames → FL2VA/I2VA/L2VA; neither → the standard three fields.
- **This skill produces English bodies only** (plus a **Chinese reference version** for reading), so `06-rules/` holds only the two official English texts and does not reference the plugin's Chinese rules.
- **The home-grown four-field convention is fully removed**: `<@name>` / `<#name:dialogue>` were invented syntax the official tokenizer never understood;
  the Chinese-to-English rule file `prompt_translate_to_en.txt` is likewise deleted.
- **Ref2VA has exactly one convention**: six sections + `@asset name` only in the `subject_definitions` definition lines; the body refers to them with `<Subject N>`.
  (Historical bug: the body contained `@`, because the model was told "reference assets are written directly as @asset name".)
- **AI expansion has no cap**: `02-expand.md` keeps only the one red line "do not change what the user explicitly asked to keep";
  the online agent asks for requirements first, the local one just improvises.
