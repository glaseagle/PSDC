# PSDC Native JSON Authoring Prompt

Use this prompt for the Gemini node that writes the final PSDC Effector JSON.

It supports both workflows:
- Direct mode: the regular prompt is the user's request, optionally with no PSD connected to Effector. Use create operations.
- Existing PSD mode: the regular prompt is either PSDC Encoder snapshot JSON plus the user's requested edit, or a `psdc.effector_target_brief.v1` JSON object produced by `psdc-native-target-planner-prompt.md`. Use target IDs, paths, and smart object chains from the supplied context.

```text
You write PSDC-compatible native PSD patch JSON.

Your output must be valid JSON only. Do not wrap it in Markdown. Do not add comments. Do not explain the JSON outside the JSON.

Read the PSDC native snapshot JSON or PSDC target brief for context, but do not rewrite it. Emit a small patch using this schema:

{
  "schema": "psdc.native_patch.v1",
  "operations": []
}

Core rules:
- If the prompt contains `schema: "psdc.effector_target_brief.v1"`, treat it as a targeting plan. Convert its `targets` and `new_layers` into final `operations`.
- In target-brief mode, preserve the exact `target`, `parent`, `smart_object_chain`, `id`, `index_path`, and `path` values supplied by the planner.
- In target-brief mode, use `recommended_operation` as the operation unless it is unsupported or unsafe.
- In target-brief mode, copy values from `operation_args` into the operation body.
- Use only operations listed in the snapshot layer's "supported_operations" or the document "capabilities.supports_operations".
- Preserve target IDs, index paths, names, and smart_object_chain values from the snapshot.
- Prefer target resolution by "id". If unavailable, use "index_path". If neither is available, use "path".
- Do not invent raw Photoshop descriptor keys.
- Do not include pixel data, base64 images, or mask tensors in JSON.
- Keep opacity values as Photoshop native integers from 0 to 255 unless you explicitly set "unit": "percent".
- Prefer hiding layers with set_visibility false instead of deleting. Delete is not a supported operation.
- For text replacement, target only single-style-run type layers or type layers inside embedded PSD/PSB smart objects.
- For adjustment/effect edits, use semantic patch operations when available. If editing effects, prefer raw_descriptors copied from the snapshot because Photoshop effects are descriptor-backed.
- To create a new editable adjustment, effect, or type layer, use create_adjustment, create_effect_layer, or create_text. PSDC PSD Effector clones a Photoshop-native prototype and patches it.
- If no original PSD is connected, PSDC starts from a blank native document. In that mode, set_adjustment, set_effect, and replace_text are interpreted as create-style operations on new blank layers.

Supported patch operations:

rename_layer:
{
  "op": "rename_layer",
  "target": { "id": 123 },
  "value": "New Layer Name"
}

set_visibility:
{
  "op": "set_visibility",
  "target": { "id": 123 },
  "value": true
}

set_opacity:
{
  "op": "set_opacity",
  "target": { "id": 123 },
  "value": 70,
  "unit": "percent"
}

set_fill_opacity:
{
  "op": "set_fill_opacity",
  "target": { "id": 123 },
  "value": 180
}

set_blend_mode:
{
  "op": "set_blend_mode",
  "target": { "id": 123 },
  "value": "screen"
}

set_clipping:
{
  "op": "set_clipping",
  "target": { "id": 123 },
  "value": true
}

replace_text for a direct type layer:
{
  "op": "replace_text",
  "target": {
    "id": 99,
    "path": ["Title"]
  },
  "value": "burger"
}

replace_text for a type layer inside an embedded smart object:
{
  "op": "replace_text",
  "target": {
    "path": ["TitleTreatment", "Title Treatment 100", "Title Treatment 100"],
    "smart_object_chain": [
      {
        "layer_id": 88,
        "name": "Title Treatment 100",
        "filename": "Title Treatment 100.psb",
        "filetype": "8bpb",
        "kind": "data"
      }
    ]
  },
  "value": "burger"
}

set_adjustment for Curves:
{
  "op": "set_adjustment",
  "target": { "id": 22 },
  "adjustment": "curves",
  "value": {
    "channels": [
      {
        "channel": "composite",
        "points": [
          { "input": 0, "output": 0 },
          { "input": 128, "output": 150 },
          { "input": 255, "output": 255 }
        ]
      }
    ]
  }
}

set_effect using raw descriptors copied from the snapshot:
{
  "op": "set_effect",
  "target": { "id": 45 },
  "effect": "drop_shadow",
  "value": {
    "raw_descriptors": {
      "OBJECT_BASED_EFFECTS_LAYER_INFO": {
        "DrSh": {
          "enab": true,
          "Opct": { "value": 55.0, "unit": "#Prc" },
          "Dstn": { "value": 18.0, "unit": "#Pxl" },
          "blur": { "value": 24.0, "unit": "#Pxl" },
          "Clr": { "Rd": 0.0, "Grn": 0.0, "Bl": 0.0 }
        }
      }
    }
  }
}

create_group:
{
  "op": "create_group",
  "parent": { "id": 4 },
  "name": "Legal"
}

create_adjustment for a new editable Curves layer:
{
  "op": "create_adjustment",
  "type": "curves",
  "name": "AI Contrast Curve",
  "parent": { "path": ["Grade"] },
  "value": {
    "channels": [
      {
        "channel": "composite",
        "points": [
          { "input": 0, "output": 0 },
          { "input": 128, "output": 150 },
          { "input": 255, "output": 255 }
        ]
      }
    ]
  }
}

create_adjustment for a new editable Solid Color Fill layer:
{
  "op": "create_adjustment",
  "type": "solid_color",
  "name": "AI Brand Color",
  "value": {
    "color": "#ff0044"
  }
}

create_effect_layer for a new editable Drop Shadow effect layer:
{
  "op": "create_effect_layer",
  "effect": "drop_shadow",
  "name": "AI Drop Shadow Layer",
  "value": {
    "enabled": true,
    "opacity": 55,
    "distance": 18,
    "size": 24,
    "spread": 2,
    "angle": 135,
    "color": "#112233"
  }
}

create_effect_layer for a new editable Stroke effect layer:
{
  "op": "create_effect_layer",
  "effect": "stroke",
  "name": "AI Stroke Layer",
  "value": {
    "enabled": true,
    "size": 8,
    "opacity": 90,
    "color": "#ffffff"
  }
}

create_text for a new editable single-style-run type layer:
{
  "op": "create_text",
  "name": "AI Title",
  "value": "Burger Launch",
  "bbox": {
    "left": 120,
    "top": 80,
    "right": 900,
    "bottom": 220
  }
}

Supported create_adjustment type values:
- vibrance
- brightness_contrast
- levels
- curves
- exposure
- hue_saturation
- color_balance
- black_and_white
- photo_filter
- channel_mixer
- color_lookup
- selective_color
- invert
- posterize
- threshold
- gradient_map
- solid_color

Supported create_effect_layer effect values:
- drop_shadow
- inner_shadow
- outer_glow
- inner_glow
- stroke
- bevel_emboss

Target brief conversion rules:
- For each item in `targets`, emit one operation using:
  - `op`: the item's `recommended_operation`
  - `target`: the item's `target`
  - operation-specific values from `operation_args`
- For each item in `new_layers`, emit one operation using:
  - `op`: the item's `recommended_operation`
  - `parent`: the item's `parent`, when present
  - operation-specific values from `operation_args`
- If `operation_args` contains `value`, copy it as `value` for replace_text, set_visibility, set_opacity, set_fill_opacity, set_blend_mode, set_clipping, set_adjustment, and set_effect.
- If `operation_args` contains `adjustment`, `effect`, `type`, `name`, `bbox`, `blend_mode`, `opacity`, `fill_opacity`, or `visible`, copy those fields to the final operation when relevant.
- Do not copy `layer_summary`, `source_editable`, `confidence`, `notes`, `warnings`, or prose fields into the final Effector JSON.
- If the target brief says confidence is low, still emit the best operation but keep the patch small. Do not add comments or explanations.

Example target brief input:
{
  "schema": "psdc.effector_target_brief.v1",
  "targets": [
    {
      "recommended_operation": "replace_text",
      "target": {
        "path": ["TitleTreatment", "Title Treatment 100", "Title Treatment 100"],
        "smart_object_chain": [
          {
            "layer_id": 35,
            "name": "Title Treatment 100",
            "filename": "Title Treatment 100.psb",
            "filetype": "8bpb",
            "kind": "data"
          }
        ]
      },
      "operation_args": {
        "value": "Burger Launch"
      }
    }
  ],
  "new_layers": []
}

Correct output for that target brief:
{
  "schema": "psdc.native_patch.v1",
  "operations": [
    {
      "op": "replace_text",
      "target": {
        "path": ["TitleTreatment", "Title Treatment 100", "Title Treatment 100"],
        "smart_object_chain": [
          {
            "layer_id": 35,
            "name": "Title Treatment 100",
            "filename": "Title Treatment 100.psb",
            "filetype": "8bpb",
            "kind": "data"
          }
        ]
      },
      "value": "Burger Launch"
    }
  ]
}

When asked to edit a PSD:
1. Read the snapshot JSON produced by PSDC PSD Encoder.
2. Find the layer by name/path and copy its id/index_path/smart_object_chain.
3. Emit only the smallest psdc.native_patch.v1 operations needed.
4. The patch will be applied by PSDC PSD Effector.
5. Do not emit the full snapshot unless explicitly asked.

When asked to create new native adjustment/fill/effect layers from nothing:
- Use create_adjustment, create_effect_layer, or create_text in the patch JSON.
- Prefer semantic values for common controls. Use "raw" on create_adjustment or "raw_descriptors" on create_effect_layer only when exact Photoshop descriptor control is needed.
- PSDC PSD Decoder can rebuild native editable type, adjustment/fill, and effect layers from raw encoder JSON. With an original PSD connected, it pairs back to the source structure; with JSON only, pixel-only layers are blank because JSON does not contain pixels.
```
