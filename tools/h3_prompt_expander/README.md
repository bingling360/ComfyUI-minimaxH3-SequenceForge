# h3_prompt_expander · H3 prompt understanding + translation (standalone tool)

Spoken Chinese → intent IR → **official H3 format** (the `integrated_multimodal_description` / `overall_soundscape` / `non_diegetic_music` three-field structure, or the Ref2VA six-section form). It exists to cure "made-up details / reading the prompt aloud / burned-in subtitles / unrequested score".

The format baseline is MiniMax's official `MiniMax-AI/MiniMax-H3` → `skills/h3-prompt-writing` (`SKILL.md` + `references/base-en.txt` + `references/ref-en.txt`); this tool is its executable landing: **the skeleton stays verbatim English, the body can be Chinese on demand**.

- The LLM channel goes through the root `optimizer.py`: the cloud channel supports the OpenAI-compatible / Gemini / Responses protocols, and the local channel supports Transformers / GGUF. Whatever is selected in the settings box is what this tool uses.
- The deterministic validator `validate.py` needs no key; `--validate-only` costs nothing.
- The intent-confirmation card is mandatory first: nothing is compiled before the user nods; if the request is unclear, three candidate directions are offered.
- It is both a CLI and a service: `service.py` is called in-process by nodes / routes (`POST /h3chain/expand`).

> Generalization trade-off (vs. a pure case library): T8 covers styles through case breadth; this tool covers unknown input through three layers of fallback —
> the `normalize.py` rule layer (everything passes through it first, zero cost) + the `references/scene-packs.md` scene-pack layer (7 high-frequency patterns auto-routed)
> + the confirmation loop (the rest is asked, not invented). The two are complementary; T8's case mechanism can be attached via `--style-note`.

## 1. Configuration

The configuration has the same shape as the Director Console's "prompt optimization settings" (i.e. `optimizer.DEFAULT_CONFIG`). Save it as JSON and pass it with `--config`:

```json
{
  "mode": "api",
  "provider": "runninghub",
  "protocol": "openai",
  "api_key": "sk-xxxx",
  "model": "openai/gpt-5.6-sol",
  "read_media": true,
  "max_tokens": 4096
}
```

```powershell
python h3_prompt_expander/h3_expand.py "……" --config cfg.json
```

Without `--config`, defaults are used (`optimizer.DEFAULT_CONFIG`, RunningHub by default).
For a local model, set `mode` to `local` and fill in `local_model` / `local_mmproj` / `local_device`.

## 2. Usage

```powershell
# One-sentence translation; the output pastes straight into the H3 / SequenceForge master-prompt box
python h3_prompt_expander/h3_expand.py "Rainy neon market, a girl turns and smiles, says follow me" --duration 5 --config cfg.json

# Recommended: the full intent-confirmation flow (confirm, then compile; candidates when the user is vague)
python h3_prompt_expander/h3_expand.py "……" --output full --config cfg.json > /tmp/h3env.json
python h3_prompt_expander/confirm_card.py /tmp/h3env.json   # show the card to the user
python h3_prompt_expander/h3_expand.py --from /tmp/h3env.json --revise "user feedback / pick A" --output full --config cfg.json > /tmp/h3env2.json

# Style preset + scene-mechanism attachment (T8 case mechanism can be pasted into --style-note)
python h3_prompt_expander/h3_expand.py "……" --style strict --config cfg.json
python h3_prompt_expander/h3_expand.py "……" --style-note "distance reversal, gesture downgrade after the scare" --config cfg.json

# Pipe + JSON output (for agent consumption)
echo "Dusk classroom, a short-haired girl writes, looks out the window, says let's go home early today too" | python h3_prompt_expander/h3_expand.py --output json --duration 5 --config cfg.json

# Twice the cost but more robust (understand, then compile)
python h3_prompt_expander/h3_expand.py "……" --two-step --model glm-4.6 --config cfg.json

# Validate only / preprocess only, costs nothing (same as CI)
python h3_prompt_expander/h3_expand.py --validate-only h3_prompt_expander/examples/envelope_ok.json
python h3_prompt_expander/validate.py h3_prompt_expander/examples/envelope_bad.json
python h3_prompt_expander/normalize.py "blockbuster feel, no extras, coffee spills over and drowns the key"
```

Return codes: `0` = validation passed, `1` = validation-only failure, `3` = repaired but still has errors (the envelope is inspectable via stdout full), `4` = awaiting user confirmation.

### Use it directly inside the Director Console (recommended path)

The console's **concretize prompt → AI expand** button is wired to this tool; no terminal needed:

- Click `[AI expand] Chinese intent → official format` → fill in a one-sentence Chinese intent in the dialog → `generate confirmation card`
- The dialog calls `POST /h3chain/expand` (the server reuses the saved optimization settings; no key needed in the front-end)
- After checking the card, click `confirm and write back`: `integrated_multimodal_description`
  (or `detailed_description` for Ref2VA) is split on `[Shot N]` into multiple shots written to `shots`,
  while `overall_soundscape` / `non_diegetic_music` go to the same-named fields
- If the understanding is off, click `give feedback and revise` to re-enter with the previous envelope + revision (`from_envelope` + `revision`)

The corresponding service entry point is `service.py: expand_via_config()`, which returns
`intent / pe / envelope / h3_text / card_md / validation / ok / meta`.

## 3. What the output looks like

The default `--output h3` prints the official format, with a blank line between fields:

```
integrated_multimodal_description: [Shot 1] ...

overall_soundscape: ...

non_diegetic_music: N/A
```

I2VA / FL2VA / L2VA prepend a **keyframe alignment instruction** block (its own block, blank line, then the three fields):

```
How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 2) aligns with the 8.00-second mark of the target video.

integrated_multimodal_description: [Shot 1] ...
```

Ref2VA outputs six sections in a fixed order:

```
subject_definitions: ...

summary: ...

retention_analysis: ...

detailed_description: ...

overall_soundscape: ...

non_diegetic_music: N/A
```

**When pasting into SequenceForge**: each prompt box holds **one segment** (the back-end's official passthrough does not re-wrap). Multiple segments are assembled with the master-prompt format (segment header `[Segment N]` + the `Duration` / `Standalone` / `Prompt` tags, **all English**); each segment's `Prompt:` body is the official-format text above (including blank lines); the I2VA/FL2VA/L2VA alignment instruction goes at the front of the body, a blank line, then the three fields.

## 4. Bad-case regression

See `bad_cases.md` (B1–B10: gibberish / burned subtitles / unrequested score / brief drift / rushed pacing / random cuts / colliding liquid / 9-beat frozen face / local Chinese silence / identity drift).
`examples/envelope_ok.json` should pass; `envelope_bad.json` should raise E_D_* / E_FREEZE / E_SOUND_DIALOG / E_NO_MUSIC / E_QUOTE_LOST.

## 5. Files

| File | Purpose |
|---|---|
| `SKILL.md` | Agent entry point (read by opencode; contains the mandatory confirmation loop) |
| `h3_expand.py` | Main logic (scene routing + repair/revise re-entry + rendering + CLI) |
| `service.py` | Service entry point (`expand_via_config` / `validate_only`, called in-process by routes) |
| `validate.py` | Deterministic validation (no LLM; includes official-structure checks: alignment sentence vs. N/S.SS consistency, Ref2VA six sections present and ordered, summary task prefix, retention marker placement) |
| `normalize.py` | Deterministic preprocessing (dialogue / quotes / abstract words / negation / high-risk patterns, free) |
| `confirm_card.py` | Intent-card rendering (for the user to nod / shake / pick one of three) |
| `references/h3-dialect.md` | H3 official-format constraints (an executable summary of base-en / ref-en) |
| `references/intent-schema.json` | Envelope schema |
| `prompts/system_intent.md` | Understanding hop |
| `prompts/system_compile.md` | Compilation hop |
| `bad_cases.md` | Failure-mode library |
| `optimized/` | B1–B10 optimization comparison: `before_after.md` pastes directly; `B*.json` are complete intent-envelope regression sets |
