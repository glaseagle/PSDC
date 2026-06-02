# Agent Notes

This repository is a ComfyUI custom node fork. The main behavior to protect is native Photoshop layer-mask export from `D2 Save PSD`, including positioned masks created by `D2 Apply Alpha Channel`.

## Expected Layout

Install the repo under:

```text
ComfyUI/custom_nodes/D2-SavePSD-ComfyUI-NativeMasks
```

During local development in an existing upstream checkout, the folder name may still be:

```text
ComfyUI/custom_nodes/D2-SavePSD-ComfyUI
```

Both layouts work as long as ComfyUI imports the custom node.

## Install

From the ComfyUI folder:

```powershell
cd custom_nodes
git clone https://github.com/glaseagle/D2-SavePSD-ComfyUI-NativeMasks.git D2-SavePSD-ComfyUI-NativeMasks
cd D2-SavePSD-ComfyUI-NativeMasks
..\..\.venv\Scripts\python.exe install.py
```

If ComfyUI uses a different Python environment, run `install.py` with that Python instead.

The install script installs:

- `psd-tools`
- `scikit-image`

## Restart ComfyUI

ComfyUI must be restarted after installing or editing this node.

The expected local URL is:

```text
http://127.0.0.1:8188/
```

Before a smoke test, confirm the API responds:

```powershell
Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8188/" -TimeoutSec 15
```

## Smoke Test

The smoke test queues a three-layer PSD export. Each layer uses `D2 Apply Alpha Channel` to place a smaller image and same-size mask onto a larger 768x768 destination canvas before `D2 Save PSD` writes one PSD in `single_file` mode.

The same graph is included as an importable ComfyUI workflow:

```text
workflows/d2_native_masks_3_layer_smoke.json
```

Requirements:

- ComfyUI is running on `http://127.0.0.1:8188/`.
- This custom node is installed and visible in `/object_info`.

Run from the ComfyUI folder:

```powershell
$clientId = [guid]::NewGuid().ToString()
$prefix = "D2_SavePSD/positioned_masks_smoke"

$prompt = [ordered]@{
  "1" = [ordered]@{ class_type = "EmptyImage"; inputs = [ordered]@{ width = 768; height = 768; batch_size = 1; color = 0 } }
  "2" = [ordered]@{ class_type = "EmptyImage"; inputs = [ordered]@{ width = 220; height = 160; batch_size = 1; color = 14236194 } }
  "3" = [ordered]@{ class_type = "SolidMask"; inputs = [ordered]@{ value = 1.0; width = 220; height = 160 } }
  "4" = [ordered]@{ class_type = "D2 Apply Alpha Channel"; inputs = [ordered]@{ image = @("2", 0); mask = @("3", 0); invert_mask = $false; x = 64; y = 96; offset_x = 0; offset_y = 0; destination = @("1", 0) } }
  "5" = [ordered]@{ class_type = "EmptyImage"; inputs = [ordered]@{ width = 280; height = 180; batch_size = 1; color = 2879348 } }
  "6" = [ordered]@{ class_type = "SolidMask"; inputs = [ordered]@{ value = 1.0; width = 280; height = 180 } }
  "7" = [ordered]@{ class_type = "D2 Apply Alpha Channel"; inputs = [ordered]@{ image = @("5", 0); mask = @("6", 0); invert_mask = $false; x = 330; y = 240; offset_x = 0; offset_y = 0; destination = @("1", 0) } }
  "8" = [ordered]@{ class_type = "EmptyImage"; inputs = [ordered]@{ width = 180; height = 260; batch_size = 1; color = 3235182 } }
  "9" = [ordered]@{ class_type = "SolidMask"; inputs = [ordered]@{ value = 1.0; width = 180; height = 260 } }
  "10" = [ordered]@{ class_type = "D2 Apply Alpha Channel"; inputs = [ordered]@{ image = @("8", 0); mask = @("9", 0); invert_mask = $false; x = 520; y = 430; offset_x = 0; offset_y = 0; destination = @("1", 0) } }
  "11" = [ordered]@{ class_type = "ImageBatch"; inputs = [ordered]@{ image1 = @("4", 0); image2 = @("7", 0) } }
  "12" = [ordered]@{ class_type = "ImageBatch"; inputs = [ordered]@{ image1 = @("11", 0); image2 = @("10", 0) } }
  "13" = [ordered]@{ class_type = "D2 Save PSD"; inputs = [ordered]@{ images = @("12", 0); filename_prefix = $prefix; file_mode = "single_file"; alpha_name = "_mask_"; alpha_name_mode = "suffix" } }
}

$body = @{ prompt = $prompt; client_id = $clientId } | ConvertTo-Json -Depth 30
$response = Invoke-RestMethod -Uri "http://127.0.0.1:8188/prompt" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 20
$promptId = $response.prompt_id

$deadline = (Get-Date).AddSeconds(60)
do {
  Start-Sleep -Seconds 2
  $history = Invoke-RestMethod -Uri "http://127.0.0.1:8188/history/$promptId" -TimeoutSec 10
  $entry = $history.$promptId
} until ($entry -or (Get-Date) -gt $deadline)

if (-not $entry -or $entry.status.status_str -ne "success") {
  throw "Smoke prompt failed or timed out."
}

$psd = Get-ChildItem ".\output\D2_SavePSD" -Filter "positioned_masks_smoke*.psd" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1

if (-not $psd) {
  throw "Smoke PSD was not written."
}

$psd.FullName
```

Then verify the PSD has three pixel layers with native masks at the expected positions:

```powershell
@'
from psd_tools import PSDImage
from pathlib import Path

psd_path = sorted(Path("output/D2_SavePSD").glob("positioned_masks_smoke*.psd"))[-1]
psd = PSDImage.open(psd_path)
expected = [
    (520, 430, 700, 690),
    (330, 240, 610, 420),
    (64, 96, 284, 256),
]

print("file", psd_path)
print("layer_count", len(psd))
for layer, bbox in zip(psd, expected):
    mask_bbox = layer.mask.topil().getbbox() if layer.has_mask() else None
    print(layer.name, layer.kind, "visible=" + str(layer.visible), "has_mask=" + str(layer.has_mask()), "mask_bbox=" + str(mask_bbox))
    assert mask_bbox == bbox

assert len(psd) == 3
assert all(layer.kind == "pixel" for layer in psd)
assert all(layer.has_mask() for layer in psd)
'@ | .\.venv\Scripts\python.exe -
```

Expected verification:

```text
layer_count 3
Layer 1 pixel visible=True has_mask=True mask_bbox=(520, 430, 700, 690)
Layer 2 pixel visible=True has_mask=True mask_bbox=(330, 240, 610, 420)
Layer 3 pixel visible=True has_mask=True mask_bbox=(64, 96, 284, 256)
```

## Development Notes

- Keep `alpha_name` and `alpha_name_mode` in the save node signature so older workflows still load.
- `D2 Apply Alpha Channel` should continue to work without `destination`; in that mode it returns an RGBA image the same size as `image`.
- When `destination` is connected, masks smaller than the destination should be placed at `x`, `y` and the remaining alpha area should stay black.
- Do not reintroduce standalone hidden mask layers as the default behavior.
- Do not commit generated PSDs, ComfyUI logs, or `__pycache__`.
- Run `python -m py_compile nodes.py` with the ComfyUI venv after code edits.
