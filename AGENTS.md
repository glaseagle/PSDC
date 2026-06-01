# Agent Notes

This repository is a ComfyUI custom node fork. The main behavior to protect is native Photoshop layer-mask export from `D2 Save PSD`.

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

The smoke test queues a three-layer PSD export. Each layer uses `D2 Apply Alpha Channel` to attach a mask before `D2 Save PSD` writes one PSD in `single_file` mode.

The same graph is included as an importable ComfyUI workflow:

```text
workflows/d2_native_masks_3_layer_smoke.json
```

Requirements:

- ComfyUI is running on `http://127.0.0.1:8188/`.
- `example.png` exists in `ComfyUI/input`.
- This custom node is installed and visible in `/object_info`.

Run from the ComfyUI folder:

```powershell
$clientId = [guid]::NewGuid().ToString()
$prefix = "D2_SavePSD/layers_masks_native_smoke"

$prompt = [ordered]@{
  "1"  = [ordered]@{ class_type = "LoadImage"; inputs = [ordered]@{ image = "example.png" } }
  "2"  = [ordered]@{ class_type = "LoadImageMask"; inputs = [ordered]@{ image = "example.png"; channel = "red" } }
  "3"  = [ordered]@{ class_type = "D2 Apply Alpha Channel"; inputs = [ordered]@{ image = @("1", 0); mask = @("2", 0); invert_mask = $false } }
  "4"  = [ordered]@{ class_type = "LoadImage"; inputs = [ordered]@{ image = "example.png" } }
  "5"  = [ordered]@{ class_type = "LoadImageMask"; inputs = [ordered]@{ image = "example.png"; channel = "red" } }
  "6"  = [ordered]@{ class_type = "D2 Apply Alpha Channel"; inputs = [ordered]@{ image = @("4", 0); mask = @("5", 0); invert_mask = $false } }
  "7"  = [ordered]@{ class_type = "LoadImage"; inputs = [ordered]@{ image = "example.png" } }
  "8"  = [ordered]@{ class_type = "LoadImageMask"; inputs = [ordered]@{ image = "example.png"; channel = "red" } }
  "9"  = [ordered]@{ class_type = "D2 Apply Alpha Channel"; inputs = [ordered]@{ image = @("7", 0); mask = @("8", 0); invert_mask = $false } }
  "10" = [ordered]@{ class_type = "ImageBatch"; inputs = [ordered]@{ image1 = @("3", 0); image2 = @("6", 0) } }
  "11" = [ordered]@{ class_type = "ImageBatch"; inputs = [ordered]@{ image1 = @("10", 0); image2 = @("9", 0) } }
  "12" = [ordered]@{ class_type = "D2 Save PSD"; inputs = [ordered]@{ images = @("11", 0); filename_prefix = $prefix; file_mode = "single_file"; alpha_name = "_mask_"; alpha_name_mode = "suffix" } }
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

$psd = Get-ChildItem ".\output\D2_SavePSD" -Filter "layers_masks_native_smoke*.psd" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1

if (-not $psd) {
  throw "Smoke PSD was not written."
}

$psd.FullName
```

Then verify the PSD has three pixel layers with native masks:

```powershell
@'
from psd_tools import PSDImage
from pathlib import Path

psd_path = sorted(Path("output/D2_SavePSD").glob("layers_masks_native_smoke*.psd"))[-1]
psd = PSDImage.open(psd_path)

print("file", psd_path)
print("layer_count", len(psd))
for layer in psd:
    print(layer.name, layer.kind, "visible=" + str(layer.visible), "has_mask=" + str(layer.has_mask()))

assert len(psd) == 3
assert all(layer.kind == "pixel" for layer in psd)
assert all(layer.has_mask() for layer in psd)
'@ | .\.venv\Scripts\python.exe -
```

Expected verification:

```text
layer_count 3
Layer 1 pixel visible=True has_mask=True
Layer 2 pixel visible=True has_mask=True
Layer 3 pixel visible=True has_mask=True
```

## Development Notes

- Keep `alpha_name` and `alpha_name_mode` in the node signature so older workflows still load.
- Do not reintroduce standalone hidden mask layers as the default behavior.
- Do not commit generated PSDs, ComfyUI logs, or `__pycache__`.
- Run `python -m py_compile nodes.py` with the ComfyUI venv after code edits.
