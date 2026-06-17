import copy
import hashlib
import json
import logging
import os
import random
import shutil
from enum import Enum as PyEnum

import folder_paths
import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFont
from psd_tools import PSDImage
from psd_tools.constants import BlendMode, Tag

try:
    # The v3 node API (and its Autogrow dynamic inputs) only exists on newer
    # ComfyUI builds. Older versions fall back to a fixed-socket combine node
    # instead of failing to import the whole pack.
    from comfy_api.latest import io
except ImportError:
    io = None

HAS_AUTOGROW = io is not None and hasattr(io, "Autogrow")

MAX_RESOLUTION = 16384
PSD_STACK_TYPE = "PSDC_PSD_STACK"
NODE_DIR = os.path.dirname(os.path.abspath(__file__))
NATIVE_PROTOTYPE_LIBRARY = os.path.join(NODE_DIR, "assets", "psdc_adjustment_prototypes.psd")


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tensor_to_uint8_array(tensor):
    if torch.is_tensor(tensor):
        array = tensor.detach().cpu().numpy()
    else:
        array = tensor

    if array.dtype in (np.float32, np.float64) or np.issubdtype(array.dtype, np.floating):
        array = np.clip(array, 0.0, 1.0)
        array = (array * 255.0).round().astype(np.uint8)
    elif array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)

    return array


def clone_for_psd(tensor):
    return tensor.detach().cpu().clone()


def create_pil_from_tensor(img_tensor, with_alpha=True):
    array = tensor_to_uint8_array(img_tensor)

    if with_alpha:
        if array.shape[2] == 3:
            alpha = np.full((array.shape[0], array.shape[1], 1), 255, dtype=np.uint8)
            array = np.concatenate((array, alpha), axis=2)
        return Image.fromarray(array[:, :, :4], "RGBA")

    if array.shape[2] == 4:
        array = array[:, :, :3]
    return Image.fromarray(array, "RGB")


def extract_alpha_mask(img_tensor):
    array = tensor_to_uint8_array(img_tensor)

    if array.shape[2] == 4:
        alpha_mask = Image.fromarray(array[:, :, 3], "L")
        rgb_image = Image.fromarray(array[:, :, :3], "RGB")
        return alpha_mask, rgb_image

    height, width = array.shape[:2]
    alpha_mask = Image.new("L", (width, height), 255)
    rgb_image = Image.fromarray(array[:, :, :3], "RGB")
    return alpha_mask, rgb_image


def append_pixel_layer_with_mask(psd, rgb_image, alpha_mask, layer_name, has_alpha, opacity=255):
    rgb_layer = psd.create_pixel_layer(rgb_image, name=layer_name)
    if has_alpha:
        rgb_layer.create_mask(alpha_mask)
    rgb_layer.opacity = int(opacity)
    return rgb_layer


def match_batch_size(tensor, batch_size):
    if tensor.shape[0] > batch_size:
        return tensor[:batch_size]
    if tensor.shape[0] < batch_size:
        repeat_shape = (batch_size - tensor.shape[0],) + (1,) * (tensor.dim() - 1)
        return torch.cat((tensor, tensor[-1:].repeat(repeat_shape)), dim=0)
    return tensor


def normalize_mask_batch(mask, batch_size):
    if len(mask.shape) == 2:
        mask = mask.unsqueeze(0)
    elif len(mask.shape) == 4:
        if mask.shape[-1] == 1:
            mask = mask.squeeze(-1)
        elif mask.shape[1] == 1:
            mask = mask.squeeze(1)
    elif len(mask.shape) == 5:
        mask = mask.squeeze(1).squeeze(1)

    return match_batch_size(mask, batch_size)


def resize_mask_to_image(mask, image):
    if mask.shape[1:3] != image.shape[1:3]:
        mask = torch.nn.functional.interpolate(
            mask.unsqueeze(1).float(),
            size=(image.shape[1], image.shape[2]),
            mode="bicubic",
            align_corners=False,
        ).squeeze(1)

    return mask.clamp(0.0, 1.0)


def resize_mask_to_size(mask, height, width):
    if mask.shape[1:3] != (height, width):
        mask = torch.nn.functional.interpolate(
            mask.unsqueeze(1).float(),
            size=(height, width),
            mode="bicubic",
            align_corners=False,
        ).squeeze(1)

    return mask.clamp(0.0, 1.0)


def get_mask_batch_size(mask):
    if len(mask.shape) == 2:
        return 1
    return int(mask.shape[0])


def get_mask_size(mask):
    if len(mask.shape) == 2:
        return int(mask.shape[0]), int(mask.shape[1])
    if len(mask.shape) == 3:
        return int(mask.shape[1]), int(mask.shape[2])
    if len(mask.shape) == 4:
        return int(mask.shape[1]), int(mask.shape[2])
    raise ValueError(f"Unsupported mask shape: {tuple(mask.shape)}")


def normalize_positions(value, batch_size, offset):
    if isinstance(value, list):
        positions = value
    else:
        positions = [value]

    if len(positions) < batch_size:
        positions = positions + [positions[-1]] * (batch_size - len(positions))

    return [int(position + offset) for position in positions[:batch_size]]


def visible_bounds(x, y, source_width, source_height, canvas_width, canvas_height):
    source_x1 = max(0, -x)
    source_y1 = max(0, -y)
    dest_x1 = max(0, x)
    dest_y1 = max(0, y)

    visible_width = min(source_width - source_x1, canvas_width - dest_x1)
    visible_height = min(source_height - source_y1, canvas_height - dest_y1)

    if visible_width <= 0 or visible_height <= 0:
        return None

    return (
        source_x1,
        source_y1,
        source_x1 + visible_width,
        source_y1 + visible_height,
        dest_x1,
        dest_y1,
        dest_x1 + visible_width,
        dest_y1 + visible_height,
    )


def composite_tensors(destination, source, mask, x_positions, y_positions):
    output = destination[..., :3].clone()
    source_rgb = source[..., :3]
    canvas_height = output.shape[1]
    canvas_width = output.shape[2]

    for index in range(output.shape[0]):
        bounds = visible_bounds(
            x_positions[index],
            y_positions[index],
            source_rgb.shape[2],
            source_rgb.shape[1],
            canvas_width,
            canvas_height,
        )
        if bounds is None:
            continue

        source_x1, source_y1, source_x2, source_y2, dest_x1, dest_y1, dest_x2, dest_y2 = bounds
        source_pixels = source_rgb[index, source_y1:source_y2, source_x1:source_x2, :]
        mask_pixels = mask[index, source_y1:source_y2, source_x1:source_x2].unsqueeze(-1)
        destination_pixels = output[index, dest_y1:dest_y2, dest_x1:dest_x2, :]

        output[index, dest_y1:dest_y2, dest_x1:dest_x2, :] = (
            source_pixels * mask_pixels + destination_pixels * (1.0 - mask_pixels)
        )

    return output.clamp(0.0, 1.0)


def create_psd_stack_from_destination(destination):
    destination = destination[..., :3]
    batch_size = destination.shape[0]
    return {
        "type": PSD_STACK_TYPE,
        "version": 1,
        "width": int(destination.shape[2]),
        "height": int(destination.shape[1]),
        "batch_size": int(batch_size),
        "layers": [
            {
                "name": "Background",
                "image": clone_for_psd(destination),
                "mask": None,
                "x": [0] * batch_size,
                "y": [0] * batch_size,
            }
        ],
    }


def create_empty_psd_stack(width, height, batch_size):
    return {
        "type": PSD_STACK_TYPE,
        "version": 1,
        "width": int(width),
        "height": int(height),
        "batch_size": int(batch_size),
        "layers": [],
    }


def is_psd_stack(psd):
    return isinstance(psd, dict) and psd.get("type") == PSD_STACK_TYPE and "layers" in psd


def copy_psd_stack(psd):
    return {
        **psd,
        "layers": [dict(layer) for layer in psd.get("layers", [])],
    }


def clear_native_passthrough(psd):
    psd.pop("native_passthrough", None)
    for layer in psd.get("layers", []):
        layer.pop("native_source_layer", None)
        layer.pop("source_index_path", None)
    return psd


def native_passthrough_source_path(psd_stack):
    if not is_psd_stack(psd_stack):
        return None

    passthrough = psd_stack.get("native_passthrough")
    if not isinstance(passthrough, dict) or not passthrough.get("enabled"):
        return None

    source_path = passthrough.get("source_path")
    if source_path and os.path.isfile(source_path):
        return source_path

    try:
        source_path = psd_stack_source_path(psd_stack)
    except Exception:
        return None

    return source_path if os.path.isfile(source_path) else None


def psd_overlay_layers(psd_stack):
    if not is_psd_stack(psd_stack):
        return []
    return [layer for layer in psd_stack.get("layers", []) if not layer.get("native_source_layer")]


def match_position_list(positions, batch_size):
    if len(positions) >= batch_size:
        return positions[:batch_size]
    return positions + [positions[-1]] * (batch_size - len(positions))


def match_psd_stack_batch_size(psd, batch_size):
    if psd.get("batch_size") == batch_size:
        return copy_psd_stack(psd)

    psd = copy_psd_stack(psd)
    psd["batch_size"] = int(batch_size)
    layers = []

    for layer in psd["layers"]:
        matched_layer = dict(layer)
        matched_layer["image"] = match_batch_size(layer["image"], batch_size)
        if layer.get("mask") is not None:
            matched_layer["mask"] = match_batch_size(layer["mask"], batch_size)
        matched_layer["x"] = match_position_list(list(layer.get("x", [0])), batch_size)
        matched_layer["y"] = match_position_list(list(layer.get("y", [0])), batch_size)
        layers.append(matched_layer)

    psd["layers"] = layers
    return psd


def resize_image_tensor(tensor, height, width):
    if tensor.shape[1:3] == (height, width):
        return clone_for_psd(tensor)

    resized = torch.nn.functional.interpolate(
        tensor.permute(0, 3, 1, 2).float(),
        size=(height, width),
        mode="bicubic",
        align_corners=False,
    ).permute(0, 2, 3, 1)
    return resized.clamp(0.0, 1.0).to(dtype=tensor.dtype)


def resize_mask_tensor(mask, height, width):
    if mask.shape[1:3] == (height, width):
        return clone_for_psd(mask)

    resized = torch.nn.functional.interpolate(
        mask.unsqueeze(1).float(),
        size=(height, width),
        mode="bicubic",
        align_corners=False,
    ).squeeze(1)
    return resized.clamp(0.0, 1.0).to(dtype=mask.dtype)


def fit_image_batch_to_canvas(image, width, height):
    image = image[..., :3]
    old_height = int(image.shape[1])
    old_width = int(image.shape[2])
    if old_width == width and old_height == height:
        return image.clone()

    scale = min(width / max(1, old_width), height / max(1, old_height))
    scale = max(scale, 1.0)
    new_height = max(1, int(round(old_height * scale)))
    new_width = max(1, int(round(old_width * scale)))
    resized = resize_image_tensor(image, new_height, new_width)
    canvas = torch.zeros((image.shape[0], height, width, 3), dtype=image.dtype, device=image.device)
    canvas[:, : min(new_height, height), : min(new_width, width), :] = resized[
        :, : min(new_height, height), : min(new_width, width), :
    ]
    return canvas


def scale_position_list(positions, batch_size, scale):
    positions = match_position_list(list(positions), batch_size)
    return [int(round(position * scale)) for position in positions]


def resize_psd_stack_to_canvas(psd, width, height, batch_size=None):
    if not is_psd_stack(psd):
        return create_empty_psd_stack(width, height, batch_size or 1)

    if batch_size is None:
        batch_size = int(psd.get("batch_size", 1))

    psd = match_psd_stack_batch_size(psd, batch_size)
    old_width = max(1, int(psd.get("width", width)))
    old_height = max(1, int(psd.get("height", height)))

    width = max(int(width), old_width)
    height = max(int(height), old_height)

    if old_width == width and old_height == height:
        return psd

    scale = min(width / old_width, height / old_height)
    scale = max(scale, 1.0)
    resized = copy_psd_stack(psd)
    clear_native_passthrough(resized)
    resized["width"] = int(width)
    resized["height"] = int(height)
    resized["batch_size"] = int(batch_size)
    layers = []

    for layer in resized["layers"]:
        new_layer = dict(layer)
        image = layer["image"]
        new_layer_height = max(1, int(round(image.shape[1] * scale)))
        new_layer_width = max(1, int(round(image.shape[2] * scale)))
        new_layer["image"] = resize_image_tensor(image, new_layer_height, new_layer_width)
        if layer.get("mask") is not None:
            new_layer["mask"] = resize_mask_tensor(layer["mask"], new_layer_height, new_layer_width)
        new_layer["x"] = scale_position_list(layer.get("x", [0]), batch_size, scale)
        new_layer["y"] = scale_position_list(layer.get("y", [0]), batch_size, scale)
        layers.append(new_layer)

    resized["layers"] = layers
    return resized


def layer_required_canvas(width, height, layer_width, layer_height, x_positions=None, y_positions=None):
    if x_positions is None:
        x_positions = [0]
    if y_positions is None:
        y_positions = [0]

    for x_position, y_position in zip(x_positions, y_positions):
        width = max(width, int(max(0, x_position)) + int(layer_width))
        height = max(height, int(max(0, y_position)) + int(layer_height))

    return int(width), int(height)


def composite_target_canvas(base_width, base_height, destination=None, source=None, mask=None, x_positions=None, y_positions=None):
    width = int(base_width)
    height = int(base_height)

    if destination is not None:
        width = max(width, int(destination.shape[2]))
        height = max(height, int(destination.shape[1]))

    if source is not None:
        width, height = layer_required_canvas(
            width,
            height,
            int(source.shape[2]),
            int(source.shape[1]),
            x_positions,
            y_positions,
        )
    elif mask is not None:
        mask_height, mask_width = get_mask_size(mask)
        width, height = layer_required_canvas(width, height, mask_width, mask_height, x_positions, y_positions)

    return int(width), int(height)


def prepare_psd_stack(psd, width, height, batch_size, destination=None):
    if not is_psd_stack(psd) or psd.get("width") != width or psd.get("height") != height:
        if destination is not None:
            return create_psd_stack_from_destination(destination)
        return create_empty_psd_stack(width, height, batch_size)

    return match_psd_stack_batch_size(psd, batch_size)


def append_composite_layer_to_psd(psd, source, mask, x_positions, y_positions, opacity=255):
    psd = copy_psd_stack(psd)
    layer_number = sum(1 for layer in psd["layers"] if layer.get("name") != "Background") + 1
    psd["layers"].append(
        {
            "name": f"Layer {layer_number}",
            "image": clone_for_psd(source[..., :3]),
            "mask": clone_for_psd(mask) if mask is not None else None,
            "x": [int(x) for x in x_positions],
            "y": [int(y) for y in y_positions],
            "opacity": int(opacity),
        }
    )
    return psd


def select_batch_item(tensor, index):
    if tensor.shape[0] == 0:
        raise ValueError("PSD layer tensor has an empty batch.")
    return tensor[min(index, tensor.shape[0] - 1)]


def layer_to_pil(layer, batch_index, width, height):
    image_tensor = select_batch_item(layer["image"], batch_index)
    image_array = tensor_to_uint8_array(image_tensor)[..., :3]
    mask_tensor = layer.get("mask")

    if mask_tensor is None:
        x_positions = match_position_list(list(layer.get("x", [0])), batch_index + 1)
        y_positions = match_position_list(list(layer.get("y", [0])), batch_index + 1)
        x_position = int(x_positions[batch_index])
        y_position = int(y_positions[batch_index])
        if image_array.shape[0] == height and image_array.shape[1] == width and x_position == 0 and y_position == 0:
            return Image.fromarray(image_array, "RGB"), None, False, int(layer.get("opacity", 255))

        rgb_canvas = np.zeros((height, width, 3), dtype=np.uint8)
        alpha_canvas = np.zeros((height, width), dtype=np.uint8)
        bounds = visible_bounds(
            x_position,
            y_position,
            image_array.shape[1],
            image_array.shape[0],
            width,
            height,
        )
        if bounds is not None:
            source_x1, source_y1, source_x2, source_y2, dest_x1, dest_y1, dest_x2, dest_y2 = bounds
            rgb_canvas[dest_y1:dest_y2, dest_x1:dest_x2, :] = image_array[source_y1:source_y2, source_x1:source_x2, :]
            alpha_canvas[dest_y1:dest_y2, dest_x1:dest_x2] = 255
        return Image.fromarray(rgb_canvas, "RGB"), Image.fromarray(alpha_canvas, "L"), True, int(layer.get("opacity", 255))

    mask_array = tensor_to_uint8_array(select_batch_item(mask_tensor, batch_index))
    if mask_array.ndim == 3:
        mask_array = mask_array[..., 0]

    rgb_canvas = np.zeros((height, width, 3), dtype=np.uint8)
    alpha_canvas = np.zeros((height, width), dtype=np.uint8)
    x_positions = match_position_list(list(layer.get("x", [0])), batch_index + 1)
    y_positions = match_position_list(list(layer.get("y", [0])), batch_index + 1)

    bounds = visible_bounds(
        int(x_positions[batch_index]),
        int(y_positions[batch_index]),
        image_array.shape[1],
        image_array.shape[0],
        width,
        height,
    )

    if bounds is not None:
        source_x1, source_y1, source_x2, source_y2, dest_x1, dest_y1, dest_x2, dest_y2 = bounds
        rgb_canvas[dest_y1:dest_y2, dest_x1:dest_x2, :] = image_array[source_y1:source_y2, source_x1:source_x2, :]
        alpha_canvas[dest_y1:dest_y2, dest_x1:dest_x2] = mask_array[source_y1:source_y2, source_x1:source_x2]

    return Image.fromarray(rgb_canvas, "RGB"), Image.fromarray(alpha_canvas, "L"), True, int(layer.get("opacity", 255))


def flatten_psd_stack(psd_stack, dtype=torch.float32, device="cpu"):
    batch_size = int(psd_stack.get("batch_size", 1))
    height = int(psd_stack["height"])
    width = int(psd_stack["width"])
    output = torch.zeros((batch_size, height, width, 3), dtype=dtype, device=device)

    for layer in psd_stack["layers"]:
        image = match_batch_size(layer["image"].to(dtype=dtype, device=device), batch_size)[..., :3]
        mask = layer.get("mask")
        if mask is None:
            mask = torch.ones((batch_size, image.shape[1], image.shape[2]), dtype=dtype, device=device)
        else:
            mask = match_batch_size(mask.to(dtype=dtype, device=device), batch_size)
        opacity = float(layer.get("opacity", 255)) / 255.0
        if opacity <= 0:
            continue
        mask = (mask * opacity).clamp(0.0, 1.0)
        x_positions = match_position_list(list(layer.get("x", [0])), batch_size)
        y_positions = match_position_list(list(layer.get("y", [0])), batch_size)
        output = composite_tensors(output, image, mask, x_positions, y_positions)

    return output


def create_psd_image_from_stack(psd_stack, batch_index=0):
    width = int(psd_stack["width"])
    height = int(psd_stack["height"])
    psd = PSDImage.new("RGB", (width, height))

    # Stack convention (matches flatten_psd_stack and PSDC Load PSD): layers[0] is the
    # bottom layer, layers[-1] is the top. create_pixel_layer appends each new layer
    # above the previous one, so iterate bottom-to-top to preserve z-order.
    for layer in psd_stack["layers"]:
        rgb_image, alpha_mask, has_alpha, opacity = layer_to_pil(layer, batch_index, width, height)
        append_pixel_layer_with_mask(psd, rgb_image, alpha_mask, layer["name"], has_alpha, opacity)

    return psd


class PSDC_ApplyAlphaChannel:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "mask": ("MASK",),
                "invert_mask": ("BOOLEAN", {"default": False}),
                "x": ("INT", {"default": 0, "min": -MAX_RESOLUTION, "max": MAX_RESOLUTION, "step": 1}),
                "y": ("INT", {"default": 0, "min": -MAX_RESOLUTION, "max": MAX_RESOLUTION, "step": 1}),
                "offset_x": ("INT", {"default": 0, "min": -MAX_RESOLUTION, "max": MAX_RESOLUTION, "step": 1}),
                "offset_y": ("INT", {"default": 0, "min": -MAX_RESOLUTION, "max": MAX_RESOLUTION, "step": 1}),
            },
            "optional": {
                "destination": ("IMAGE",),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "apply_alpha_channel"
    CATEGORY = "PSDC/Image"

    def apply_alpha_channel(self, image, mask, invert_mask=False, x=0, y=0, offset_x=0, offset_y=0, destination=None):
        source = image
        batch_size = source.shape[0]
        mask = normalize_mask_batch(mask, batch_size)
        processed_mask = resize_mask_to_image(mask, source)

        if invert_mask:
            processed_mask = 1.0 - processed_mask

        if destination is None:
            output = torch.zeros(
                (batch_size, source.shape[1], source.shape[2], 4),
                dtype=source.dtype,
                device=source.device,
            )
            output[..., :3] = source[..., :3]
            output[..., 3] = processed_mask
            return (output,)

        destination = match_batch_size(destination, batch_size)
        x_positions = normalize_positions(x, batch_size, offset_x)
        y_positions = normalize_positions(y, batch_size, offset_y)
        canvas_width, canvas_height = composite_target_canvas(
            int(destination.shape[2]),
            int(destination.shape[1]),
            source=source,
            x_positions=x_positions,
            y_positions=y_positions,
        )

        output = torch.zeros(
            (batch_size, canvas_height, canvas_width, 4),
            dtype=source.dtype,
            device=source.device,
        )

        for index in range(batch_size):
            source_x1 = max(0, -x_positions[index])
            source_y1 = max(0, -y_positions[index])
            dest_x1 = max(0, x_positions[index])
            dest_y1 = max(0, y_positions[index])

            visible_width = min(source.shape[2] - source_x1, canvas_width - dest_x1)
            visible_height = min(source.shape[1] - source_y1, canvas_height - dest_y1)

            if visible_width <= 0 or visible_height <= 0:
                continue

            source_x2 = source_x1 + visible_width
            source_y2 = source_y1 + visible_height
            dest_x2 = dest_x1 + visible_width
            dest_y2 = dest_y1 + visible_height

            output[index, dest_y1:dest_y2, dest_x1:dest_x2, :3] = source[
                index, source_y1:source_y2, source_x1:source_x2, :3
            ]
            output[index, dest_y1:dest_y2, dest_x1:dest_x2, 3] = processed_mask[
                index, source_y1:source_y2, source_x1:source_x2
            ]

        return (output,)


class PSDC_ImageCompositePSD:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "x": ("INT", {"default": 0, "min": -MAX_RESOLUTION, "max": MAX_RESOLUTION, "step": 1}),
                "y": ("INT", {"default": 0, "min": -MAX_RESOLUTION, "max": MAX_RESOLUTION, "step": 1}),
                "offset_x": ("INT", {"default": 0, "min": -MAX_RESOLUTION, "max": MAX_RESOLUTION, "step": 1}),
                "offset_y": ("INT", {"default": 0, "min": -MAX_RESOLUTION, "max": MAX_RESOLUTION, "step": 1}),
            },
            "optional": {
                "destination": ("IMAGE",),
                "source": ("IMAGE",),
                "mask": ("MASK",),
                "psd": ("PSD",),
            },
        }

    RETURN_TYPES = ("IMAGE", "PSD")
    RETURN_NAMES = ("image", "psd")
    FUNCTION = "execute"
    CATEGORY = "PSDC/Image"

    def execute(self, x, y, offset_x, offset_y, destination=None, source=None, mask=None, psd=None):
        psd_connected = is_psd_stack(psd)
        destination_count = int(destination.shape[0]) if destination is not None else 0
        source_count = int(source.shape[0]) if source is not None else 0
        mask_count = get_mask_batch_size(mask) if mask is not None else 0
        source_layer_count = max(source_count, mask_count)
        destination_layer_count = destination_count if psd_connected and destination is not None else 0
        batch_to_layers = max(destination_layer_count, source_layer_count) > 1
        target_position_count = max(source_layer_count, 1) if batch_to_layers else max(source_count, mask_count, 1)
        target_x_positions = normalize_positions(x, target_position_count, offset_x)
        target_y_positions = normalize_positions(y, target_position_count, offset_y)

        if source is not None:
            dtype = source.dtype
            device = source.device
        elif destination is not None:
            dtype = destination.dtype
            device = destination.device
        elif mask is not None:
            dtype = torch.float32
            device = mask.device if torch.is_tensor(mask) else "cpu"
        else:
            dtype = torch.float32
            device = "cpu"

        if not any((psd_connected, destination is not None, source is not None, mask is not None)):
            empty = torch.zeros((1, 1, 1, 3), dtype=torch.float32)
            return (empty, create_empty_psd_stack(1, 1, 1))

        if psd_connected:
            base_width = int(psd["width"])
            base_height = int(psd["height"])
            batch_size = 1 if batch_to_layers else max(
                int(psd.get("batch_size", 1)),
                destination_count,
                source_count,
                mask_count,
                1,
            )
            width, height = composite_target_canvas(
                base_width,
                base_height,
                destination=destination,
                source=source,
                mask=mask,
                x_positions=target_x_positions,
                y_positions=target_y_positions,
            )
            psd_stack = resize_psd_stack_to_canvas(psd, width, height, batch_size)
            image = flatten_psd_stack(psd_stack, dtype=dtype, device=device)
        else:
            batch_size = 1 if batch_to_layers else max(destination_count, source_count, mask_count, 1)
            if destination is not None:
                base_width = int(destination.shape[2])
                base_height = int(destination.shape[1])
                width, height = composite_target_canvas(
                    base_width,
                    base_height,
                    source=source,
                    mask=mask,
                    x_positions=target_x_positions,
                    y_positions=target_y_positions,
                )
                base_image = match_batch_size(destination[..., :3], batch_size).to(dtype=dtype, device=device)
                base_image = fit_image_batch_to_canvas(base_image, width, height)
                psd_stack = create_psd_stack_from_destination(base_image)
                image = base_image.clone()
            elif source is not None:
                width, height = composite_target_canvas(
                    int(source.shape[2]),
                    int(source.shape[1]),
                    source=source,
                    x_positions=target_x_positions,
                    y_positions=target_y_positions,
                )
                psd_stack = create_empty_psd_stack(width, height, batch_size)
                image = torch.zeros((batch_size, height, width, 3), dtype=dtype, device=device)
            else:
                height, width = get_mask_size(mask)
                width, height = composite_target_canvas(
                    width,
                    height,
                    mask=mask,
                    x_positions=target_x_positions,
                    y_positions=target_y_positions,
                )
                psd_stack = create_empty_psd_stack(width, height, batch_size)
                image = torch.zeros((batch_size, height, width, 3), dtype=dtype, device=device)

        if psd_connected and destination is not None:
            if batch_to_layers:
                destination_layers = destination[..., :3].to(dtype=dtype, device=device)
                for index in range(destination_count):
                    layer = destination_layers[index : index + 1]
                    layer_x = [0]
                    layer_y = [0]
                    layer_mask = torch.ones((1, layer.shape[1], layer.shape[2]), dtype=dtype, device=device)
                    image = composite_tensors(image, layer, layer_mask, layer_x, layer_y)
                    psd_layer_mask = None
                    if layer.shape[1] != height or layer.shape[2] != width:
                        psd_layer_mask = layer_mask
                    psd_stack = append_composite_layer_to_psd(psd_stack, layer, psd_layer_mask, layer_x, layer_y)
            else:
                destination_layer = match_batch_size(destination[..., :3], batch_size).to(dtype=dtype, device=device)
                x_positions = [0] * batch_size
                y_positions = [0] * batch_size
                layer_mask = torch.ones(
                    (batch_size, destination_layer.shape[1], destination_layer.shape[2]),
                    dtype=dtype,
                    device=device,
                )
                image = composite_tensors(image, destination_layer, layer_mask, x_positions, y_positions)
                psd_layer_mask = None
                if destination_layer.shape[1] != height or destination_layer.shape[2] != width:
                    psd_layer_mask = layer_mask
                psd_stack = append_composite_layer_to_psd(
                    psd_stack,
                    destination_layer,
                    psd_layer_mask,
                    x_positions,
                    y_positions,
                )

        if source is None and mask is None:
            return (image, psd_stack)

        if batch_to_layers:
            layer_count = max(source_layer_count, 1)
            source_layers = match_batch_size(source.to(dtype=dtype, device=device), layer_count) if source is not None else None
            mask_layers = normalize_mask_batch(mask, layer_count).to(dtype=dtype, device=device) if mask is not None else None
            x_positions = normalize_positions(x, layer_count, offset_x)
            y_positions = normalize_positions(y, layer_count, offset_y)

            for index in range(layer_count):
                if source_layers is None:
                    layer_height, layer_width = get_mask_size(mask_layers[index : index + 1])
                    layer_source = torch.zeros((1, layer_height, layer_width, 3), dtype=dtype, device=device)
                    layer_mask = mask_layers[index : index + 1]
                    opacity = 0
                else:
                    layer_source_full = source_layers[index : index + 1]
                    layer_source = layer_source_full[..., :3]
                    if mask_layers is not None:
                        layer_mask = resize_mask_to_image(mask_layers[index : index + 1], layer_source)
                    elif layer_source_full.shape[-1] == 4:
                        layer_mask = layer_source_full[..., 3]
                    else:
                        layer_mask = None
                    opacity = 255

                layer_x = [x_positions[index]]
                layer_y = [y_positions[index]]
                if (
                    source_layers is not None
                    and layer_mask is None
                    and (
                        layer_source.shape[1] != height
                        or layer_source.shape[2] != width
                        or layer_x[0] != 0
                        or layer_y[0] != 0
                    )
                ):
                    layer_mask = torch.ones((1, layer_source.shape[1], layer_source.shape[2]), dtype=dtype, device=device)

                if opacity > 0:
                    composite_mask = layer_mask
                    if composite_mask is None:
                        composite_mask = torch.ones(
                            (1, layer_source.shape[1], layer_source.shape[2]),
                            dtype=dtype,
                            device=device,
                        )
                    image = composite_tensors(image, layer_source, composite_mask, layer_x, layer_y)

                psd_stack = append_composite_layer_to_psd(
                    psd_stack,
                    layer_source,
                    layer_mask,
                    layer_x,
                    layer_y,
                    opacity=opacity,
                )

            return (image, psd_stack)

        source_alpha = None
        if source is not None:
            source = match_batch_size(source, batch_size).to(dtype=dtype, device=device)
            if source.shape[-1] == 4:
                source_alpha = source[..., 3]
            source_rgb = source[..., :3]
            layer_height = int(source_rgb.shape[1])
            layer_width = int(source_rgb.shape[2])
        else:
            layer_height, layer_width = get_mask_size(mask)
            source_rgb = torch.zeros((batch_size, layer_height, layer_width, 3), dtype=dtype, device=device)

        if mask is None:
            layer_mask = source_alpha.to(dtype=dtype, device=device) if source_alpha is not None else None
        else:
            layer_mask = normalize_mask_batch(mask, batch_size).to(dtype=dtype, device=device)
            if source is not None:
                layer_mask = resize_mask_to_image(layer_mask, source_rgb)

        x_positions = normalize_positions(x, batch_size, offset_x)
        y_positions = normalize_positions(y, batch_size, offset_y)

        if source is not None and layer_mask is None:
            needs_position_mask = (
                layer_height != height
                or layer_width != width
                or any(position != 0 for position in x_positions)
                or any(position != 0 for position in y_positions)
            )
            if needs_position_mask:
                layer_mask = torch.ones((batch_size, layer_height, layer_width), dtype=dtype, device=device)

        if source is None:
            opacity = 0
        else:
            opacity = 255
            composite_mask = layer_mask
            if composite_mask is None:
                composite_mask = torch.ones((batch_size, layer_height, layer_width), dtype=dtype, device=device)
            image = composite_tensors(image, source_rgb, composite_mask, x_positions, y_positions)

        psd_stack = append_composite_layer_to_psd(
            psd_stack,
            source_rgb,
            layer_mask,
            x_positions,
            y_positions,
            opacity=opacity,
        )

        return (image, psd_stack)


def pil_rgba_to_tensors(pil_image):
    array = np.array(pil_image.convert("RGBA")).astype(np.float32) / 255.0
    rgb = torch.from_numpy(array[..., :3]).unsqueeze(0).contiguous()
    alpha = torch.from_numpy(array[..., 3]).unsqueeze(0).contiguous()
    return rgb, alpha


def psd_key_to_string(value):
    if isinstance(value, bytes):
        return value.decode("latin-1", errors="replace").replace("\x00", "").strip()
    if isinstance(value, PyEnum):
        return value.name
    return str(value)


def psd_value_to_json(value, depth=0):
    if depth > 8:
        return repr(value)

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, bytes):
        if len(value) > 128:
            return {"type": "bytes", "length": len(value)}
        return psd_key_to_string(value)

    if isinstance(value, PyEnum):
        enum_value = value.value
        return {"name": value.name, "value": psd_key_to_string(enum_value)}

    if isinstance(value, np.generic):
        return value.item()

    if isinstance(value, torch.Tensor):
        return {"type": "tensor", "shape": list(value.shape), "dtype": str(value.dtype)}

    if type(value).__name__ == "Curves":
        return serialize_curves(value)

    if hasattr(value, "items") and callable(value.items):
        result = {"_type": type(value).__name__}
        for attr in ("name", "classID", "version", "data_version", "ostype"):
            if hasattr(value, attr):
                attr_value = getattr(value, attr)
                if attr_value not in (None, "", b"\x00\x00\x00\x00"):
                    result[f"_{attr}"] = psd_value_to_json(attr_value, depth + 1)
        result.update({psd_key_to_string(key): psd_value_to_json(item, depth + 1) for key, item in value.items()})
        return result

    if isinstance(value, (list, tuple)):
        return [psd_value_to_json(item, depth + 1) for item in value]

    if hasattr(value, "typeID") and hasattr(value, "enum"):
        result = {
            "_type": type(value).__name__,
            "typeID": psd_key_to_string(value.typeID),
            "enum": psd_key_to_string(value.enum),
        }
        try:
            result["name"] = value.get_name()
        except Exception:
            pass
        return result

    if hasattr(value, "unit") and hasattr(value, "value"):
        return {
            "_type": type(value).__name__,
            "value": psd_value_to_json(value.value, depth + 1),
            "unit": psd_value_to_json(value.unit, depth + 1),
        }

    if hasattr(value, "value"):
        inner_value = getattr(value, "value")
        if isinstance(inner_value, bytes) and len(inner_value) > 128:
            return {"_type": type(value).__name__, "value": {"type": "bytes", "length": len(inner_value)}}
        return psd_value_to_json(inner_value, depth + 1)

    if hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
        try:
            return [psd_value_to_json(item, depth + 1) for item in value]
        except Exception:
            pass

    attrs = {}
    for attr in dir(value):
        if attr.startswith("_") or attr in ("data", "parent", "context"):
            continue
        try:
            attr_value = getattr(value, attr)
        except Exception:
            continue
        if callable(attr_value):
            continue
        if isinstance(attr_value, bytes) and len(attr_value) > 128:
            attrs[attr] = {"type": "bytes", "length": len(attr_value)}
        elif isinstance(attr_value, (str, int, float, bool, type(None), bytes, list, tuple)) or hasattr(attr_value, "items"):
            attrs[attr] = psd_value_to_json(attr_value, depth + 1)

    if attrs:
        return {"_type": type(value).__name__, **attrs}

    return repr(value)


def serialize_curves(curves):
    channel_names = ["composite", "red", "green", "blue", "alpha"]
    channels = []
    for index, points in enumerate(getattr(curves, "data", []) or []):
        if getattr(curves, "is_map", False):
            channels.append(
                {
                    "index": index,
                    "channel": channel_names[index] if index < len(channel_names) else f"channel_{index}",
                    "lookup": list(points),
                }
            )
        else:
            channels.append(
                {
                    "index": index,
                    "channel": channel_names[index] if index < len(channel_names) else f"channel_{index}",
                    "points": [{"output": int(point[0]), "input": int(point[1])} for point in points],
                }
            )

    return {
        "_type": "Curves",
        "version": getattr(curves, "version", None),
        "is_map": bool(getattr(curves, "is_map", False)),
        "count_map": getattr(curves, "count_map", None),
        "channels": channels,
        "extra": psd_value_to_json(getattr(curves, "extra", None)),
    }


def layer_tag_name(tag):
    return tag.name if hasattr(tag, "name") else psd_key_to_string(tag)


def layer_id_from_tags(layer):
    tagged_blocks = getattr(layer, "tagged_blocks", None)
    if not tagged_blocks:
        return None

    for tag in tagged_blocks.keys():
        if layer_tag_name(tag) == "LAYER_ID":
            try:
                return psd_value_to_json(tagged_blocks.get(tag).data)
            except Exception:
                return None
    return None


ADJUSTMENT_TAG_TERMS = (
    "CURVE",
    "LEVEL",
    "GRADIENT",
    "SOLID_COLOR",
    "PATTERN",
    "HUE",
    "SATURATION",
    "BRIGHTNESS",
    "CONTRAST",
    "EXPOSURE",
    "PHOTO_FILTER",
    "BLACK_AND_WHITE",
    "VIBRANCE",
    "COLOR_BALANCE",
    "COLOR_LOOKUP",
    "SELECTIVE_COLOR",
    "CHANNEL_MIXER",
    "POSTERIZE",
    "THRESHOLD",
    "INVERT",
)

EFFECT_TAG_TERMS = (
    "EFFECT",
    "STROKE",
    "SHADOW",
    "GLOW",
    "BEVEL",
    "OVERLAY",
)

DESCRIPTOR_TAG_TERMS = (
    "TYPE_TOOL",
    "PLACED_LAYER",
    "SMART_OBJECT",
)


def classify_layer_tag(tag_name):
    if any(term in tag_name for term in ADJUSTMENT_TAG_TERMS):
        return "adjustments"
    if any(term in tag_name for term in EFFECT_TAG_TERMS):
        return "effect_descriptors"
    if any(term in tag_name for term in DESCRIPTOR_TAG_TERMS):
        return "descriptors"
    return None


def serialize_layer_tags(layer):
    tagged_blocks = getattr(layer, "tagged_blocks", None)
    if not tagged_blocks:
        return {"adjustments": {}, "effect_descriptors": {}, "descriptors": {}}

    tags = {"adjustments": {}, "effect_descriptors": {}, "descriptors": {}}
    for tag in tagged_blocks.keys():
        tag_name = layer_tag_name(tag)
        category = classify_layer_tag(tag_name)
        if not category:
            continue
        try:
            data = tagged_blocks.get(tag).data
        except Exception as error:
            tags[category][tag_name] = {"error": str(error)}
            continue
        tags[category][tag_name] = psd_value_to_json(data)
    return tags


def serialize_layer_effects(layer):
    effects = []
    try:
        layer_effects = layer.effects
    except Exception:
        return effects

    for effect in layer_effects:
        effects.append(psd_value_to_json(effect))
    return effects


def serialize_smart_object(layer):
    smart_object = getattr(layer, "smart_object", None)
    if not smart_object:
        return None

    result = {}
    for attr in ("filename", "kind", "filetype", "filesize", "resolution", "unique_id", "transform_box", "warp"):
        try:
            value = getattr(smart_object, attr)
        except Exception:
            continue
        if attr == "data":
            continue
        result[attr] = psd_value_to_json(value)
    return result


def serialize_layer_structure(layer, index_path):
    bbox = tuple(int(value) for value in getattr(layer, "bbox", (0, 0, 0, 0)))
    layer_info = {
        "index_path": list(index_path),
        "id": layer_id_from_tags(layer),
        "name": layer.name or "",
        "kind": str(getattr(layer, "kind", "")),
        "class": type(layer).__name__,
        "visible": bool(getattr(layer, "visible", True)),
        "opacity": int(getattr(layer, "opacity", 255)),
        "fill_opacity": int(getattr(layer, "fill_opacity", 255)),
        "blend_mode": psd_value_to_json(getattr(layer, "blend_mode", None)),
        "clipping": bool(getattr(layer, "clipping", False)),
        "bbox": {"left": bbox[0], "top": bbox[1], "right": bbox[2], "bottom": bbox[3]},
        "has_mask": bool(layer.has_mask()) if hasattr(layer, "has_mask") else False,
        "has_vector_mask": bool(layer.has_vector_mask()) if hasattr(layer, "has_vector_mask") else False,
        "has_effects": bool(layer.has_effects()) if hasattr(layer, "has_effects") else False,
        "adjustments": {},
        "effects": [],
        "effect_descriptors": {},
        "descriptors": {},
        "smart_object": None,
        "children": [],
    }

    smart_object = serialize_smart_object(layer)
    if smart_object:
        layer_info["smart_object"] = smart_object

    effects = serialize_layer_effects(layer)
    if effects:
        layer_info["effects"] = effects

    tags = serialize_layer_tags(layer)
    if tags["adjustments"]:
        layer_info["adjustments"] = tags["adjustments"]
    if tags["effect_descriptors"]:
        layer_info["effect_descriptors"] = tags["effect_descriptors"]
    if tags["descriptors"]:
        layer_info["descriptors"] = tags["descriptors"]

    if layer.is_group():
        layer_info["children"] = [
            serialize_layer_structure(child, index_path + (index,)) for index, child in enumerate(layer)
        ]

    return layer_info


def serialize_psd_document(psd, source_path=None):
    return {
        "schema": "psdc.psd_structure.v1",
        "description": "Layer/effect/adjustment metadata extracted from a Photoshop PSD. Pixel tensors are not embedded.",
        "source": {"path": str(source_path) if source_path else None, "filename": os.path.basename(str(source_path)) if source_path else None},
        "document": {
            "width": int(psd.width),
            "height": int(psd.height),
            "layer_count_top_level": len(psd),
            "layer_order": "array order follows psd-tools iteration order used by PSDC; children preserve their group nesting.",
        },
        "layers": [serialize_layer_structure(layer, (index,)) for index, layer in enumerate(psd)],
    }


def synthesize_stack_structure(psd_stack):
    return {
        "schema": "psdc.psd_structure.v1",
        "description": "Synthetic structure generated from a PSDC PSD stack. Original Photoshop-only adjustment/effect descriptors are unavailable.",
        "source": {"path": None, "filename": None},
        "document": {
            "width": int(psd_stack.get("width", 1)),
            "height": int(psd_stack.get("height", 1)),
            "batch_size": int(psd_stack.get("batch_size", 1)),
            "layer_count": len(psd_stack.get("layers", [])),
            "layer_order": "bottom_to_top",
        },
        "layers": [
            {
                "index_path": [index],
                "id": None,
                "name": layer.get("name", f"Layer {index + 1}"),
                "kind": "pixel",
                "class": "PSDCStackLayer",
                "visible": int(layer.get("opacity", 255)) > 0,
                "opacity": int(layer.get("opacity", 255)),
                "fill_opacity": int(layer.get("opacity", 255)),
                "blend_mode": "normal",
                "clipping": False,
                "bbox": {
                    "left": int(match_position_list(list(layer.get("x", [0])), 1)[0]),
                    "top": int(match_position_list(list(layer.get("y", [0])), 1)[0]),
                    "right": int(match_position_list(list(layer.get("x", [0])), 1)[0]) + int(layer["image"].shape[2]),
                    "bottom": int(match_position_list(list(layer.get("y", [0])), 1)[0]) + int(layer["image"].shape[1]),
                },
                "has_mask": layer.get("mask") is not None,
                "has_vector_mask": False,
                "has_effects": False,
                "adjustments": {},
                "effect_descriptors": {},
                "descriptors": {},
                "effects": [],
                "smart_object": None,
                "children": [],
            }
            for index, layer in enumerate(psd_stack.get("layers", []))
        ],
    }


def psd_stack_structure_matches_current_layers(psd_stack):
    structure = psd_stack.get("structure")
    if not isinstance(structure, dict):
        return False

    document = structure.get("document", {})
    try:
        width_matches = int(document.get("width")) == int(psd_stack.get("width"))
        height_matches = int(document.get("height")) == int(psd_stack.get("height"))
        layer_count = document.get(
            "decoded_layer_count",
            document.get("layer_count_top_level", document.get("layer_count")),
        )
        layer_count_matches = int(layer_count) == len(psd_stack.get("layers", []))
    except (TypeError, ValueError):
        return False

    return width_matches and height_matches and layer_count_matches


def clamp_int(value, default=0, minimum=None, maximum=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = int(default)

    if minimum is not None:
        result = max(int(minimum), result)
    if maximum is not None:
        result = min(int(maximum), result)
    return result


def bool_from_json(value, default=True):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    if value is None:
        return default
    return bool(value)


def structure_layer_name(layer_info, fallback="Layer"):
    name = layer_info.get("name") if isinstance(layer_info, dict) else None
    if name is None:
        return fallback
    name = str(name).strip()
    return name if name else fallback


def structure_layer_bbox(layer_info, document_width, document_height):
    bbox = layer_info.get("bbox") if isinstance(layer_info, dict) else None
    if not isinstance(bbox, dict):
        return 0, 0, int(document_width), int(document_height)

    left = clamp_int(bbox.get("left"), 0, -MAX_RESOLUTION, MAX_RESOLUTION)
    top = clamp_int(bbox.get("top"), 0, -MAX_RESOLUTION, MAX_RESOLUTION)
    right = clamp_int(bbox.get("right"), left + 1, -MAX_RESOLUTION, MAX_RESOLUTION * 2)
    bottom = clamp_int(bbox.get("bottom"), top + 1, -MAX_RESOLUTION, MAX_RESOLUTION * 2)
    if right <= left:
        right = left + 1
    if bottom <= top:
        bottom = top + 1
    return left, top, right, bottom


def collect_structure_layers(layers, mode="top_level", group_path=None):
    if group_path is None:
        group_path = []

    collected = []
    if not isinstance(layers, list):
        return collected

    for index, layer_info in enumerate(layers):
        if not isinstance(layer_info, dict):
            continue

        if mode == "top_level":
            collected.append((layer_info, group_path))
            continue

        children = layer_info.get("children")
        if isinstance(children, list) and children:
            group_name = structure_layer_name(layer_info, f"Group {index + 1}")
            collected.extend(collect_structure_layers(children, mode, group_path + [group_name]))
        else:
            collected.append((layer_info, group_path))

    return collected


def document_size_from_structure(structure, selected_layers):
    document = structure.get("document", {}) if isinstance(structure, dict) else {}
    width = clamp_int(document.get("width"), 1, 1, MAX_RESOLUTION)
    height = clamp_int(document.get("height"), 1, 1, MAX_RESOLUTION)

    for layer_info, _group_path in selected_layers:
        _left, _top, right, bottom = structure_layer_bbox(layer_info, width, height)
        width = max(width, min(MAX_RESOLUTION, right))
        height = max(height, min(MAX_RESOLUTION, bottom))

    return int(width), int(height)


def source_layer_lookup(source_psd, mode):
    if not is_psd_stack(source_psd):
        return {}, {}

    source_layers = source_psd.get("layers", [])
    by_index_path = {}
    by_name = {}

    source_structure = source_psd.get("structure", {})
    source_selected = collect_structure_layers(source_structure.get("layers", []), mode)
    if len(source_selected) != len(source_layers):
        source_selected = collect_structure_layers(source_structure.get("layers", []), "top_level")

    for index, (layer_info, _group_path) in enumerate(source_selected):
        if index >= len(source_layers):
            break
        index_path = layer_info.get("index_path")
        if isinstance(index_path, list):
            by_index_path[tuple(index_path)] = source_layers[index]

    for layer in source_layers:
        name = layer.get("name")
        if name and name not in by_name:
            by_name[name] = layer

    return by_index_path, by_name


def clone_source_layer_for_structure(source_layer, layer_info, document_width, document_height, batch_size):
    layer = dict(source_layer)
    image = match_batch_size(layer["image"], batch_size)
    layer["image"] = clone_for_psd(image)
    if layer.get("mask") is not None:
        layer["mask"] = clone_for_psd(match_batch_size(layer["mask"], batch_size))

    left, top, _right, _bottom = structure_layer_bbox(layer_info, document_width, document_height)
    layer_width = int(layer["image"].shape[2])
    layer_height = int(layer["image"].shape[1])
    is_full_canvas = layer_width == int(document_width) and layer_height == int(document_height)
    source_x = list(layer.get("x", [0]))
    source_y = list(layer.get("y", [0]))

    if not (is_full_canvas and all(int(x) == 0 for x in source_x) and all(int(y) == 0 for y in source_y)):
        layer["x"] = [int(left)] * batch_size
        layer["y"] = [int(top)] * batch_size
    else:
        layer["x"] = match_position_list(source_x, batch_size)
        layer["y"] = match_position_list(source_y, batch_size)

    return layer


def create_placeholder_layer_for_structure(layer_info, layer_name, document_width, document_height, batch_size):
    left, top, right, bottom = structure_layer_bbox(layer_info, document_width, document_height)
    layer_width = max(1, min(MAX_RESOLUTION, right - left))
    layer_height = max(1, min(MAX_RESOLUTION, bottom - top))

    return {
        "name": layer_name,
        "image": torch.zeros((batch_size, layer_height, layer_width, 3), dtype=torch.float32),
        "mask": torch.zeros((batch_size, layer_height, layer_width), dtype=torch.float32),
        "x": [int(left)] * batch_size,
        "y": [int(top)] * batch_size,
    }


def layer_type_tool_object(layer_info):
    adjustments = layer_info.get("adjustments") if isinstance(layer_info, dict) else None
    if isinstance(adjustments, dict):
        for key, value in adjustments.items():
            if str(key).lower() in ("type_tool_object", "type_tool"):
                if isinstance(value, dict):
                    return value

    descriptors = layer_info.get("descriptors") if isinstance(layer_info, dict) else None
    if isinstance(descriptors, dict):
        for key, value in descriptors.items():
            if "type" in str(key).lower() and isinstance(value, dict):
                return value

    return None


def parse_text_color(value, default=(255, 255, 255)):
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) == 6:
            try:
                return tuple(int(text[index : index + 2], 16) for index in (0, 2, 4))
            except ValueError:
                return default

    if isinstance(value, (list, tuple)) and len(value) >= 3:
        channels = []
        for channel in value[:3]:
            try:
                number = float(channel)
            except (TypeError, ValueError):
                return default
            if 0.0 <= number <= 1.0:
                number *= 255.0
            channels.append(int(np.clip(round(number), 0, 255)))
        return tuple(channels)

    return default


def load_text_font(size, bold=True):
    fonts = [
        "arialbd.ttf" if bold else "arial.ttf",
        "Arial Bold.ttf" if bold else "Arial.ttf",
        "segoeuib.ttf" if bold else "segoeui.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]
    windows_font_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    candidates = []
    for font in fonts:
        candidates.append(os.path.join(windows_font_dir, font))
        candidates.append(font)

    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, int(size))
        except OSError:
            continue

    return ImageFont.load_default()


def text_bbox_size(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox, max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])


def fit_text_font(draw, text, width, height, requested_size=None, bold=True):
    if requested_size is not None:
        size = clamp_int(requested_size, height, 1, MAX_RESOLUTION)
        font = load_text_font(size, bold=bold)
        bbox, text_width, text_height = text_bbox_size(draw, text, font)
        return font, bbox, text_width, text_height

    max_size = max(1, int(height * 0.9))
    min_size = 1
    best = None
    while min_size <= max_size:
        size = (min_size + max_size) // 2
        font = load_text_font(size, bold=bold)
        bbox, text_width, text_height = text_bbox_size(draw, text, font)
        if text_width <= max(1, width * 0.96) and text_height <= max(1, height * 0.9):
            best = (font, bbox, text_width, text_height)
            min_size = size + 1
        else:
            max_size = size - 1

    if best is not None:
        return best

    font = load_text_font(1, bold=bold)
    bbox, text_width, text_height = text_bbox_size(draw, text, font)
    return font, bbox, text_width, text_height


def render_type_tool_layer_pil(layer_info, document_width, document_height):
    type_tool = layer_type_tool_object(layer_info)
    if not type_tool:
        return None

    text = type_tool.get("text", structure_layer_name(layer_info, "Text"))
    text = str(text)
    if not text:
        return None

    left, top, right, bottom = structure_layer_bbox(layer_info, document_width, document_height)
    width = max(1, min(MAX_RESOLUTION, right - left))
    height = max(1, min(MAX_RESOLUTION, bottom - top))
    rgba = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(rgba)

    color = parse_text_color(type_tool.get("color"), default=(255, 255, 255))
    alignment = str(type_tool.get("alignment", "center")).lower()
    bold = bool_from_json(type_tool.get("bold"), True)
    requested_size = type_tool.get("font_size", type_tool.get("size"))
    font, bbox, text_width, text_height = fit_text_font(draw, text, width, height, requested_size, bold=bold)

    if alignment in ("left", "start"):
        x = max(0, int(width * 0.02) - bbox[0])
    elif alignment in ("right", "end"):
        x = width - text_width - max(0, int(width * 0.02)) - bbox[0]
    else:
        x = (width - text_width) // 2 - bbox[0]

    y = (height - text_height) // 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=(*color, 255))
    return rgba, int(left), int(top)


def render_structure_layer_pil(layer_info, document_width, document_height):
    rendered = render_type_tool_layer_pil(layer_info, document_width, document_height)
    if rendered is not None:
        return rendered

    children = layer_info.get("children") if isinstance(layer_info, dict) else None
    if not isinstance(children, list) or not children:
        return None

    left, top, right, bottom = structure_layer_bbox(layer_info, document_width, document_height)
    width = max(1, min(MAX_RESOLUTION, right - left))
    height = max(1, min(MAX_RESOLUTION, bottom - top))
    rgba = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    rendered_any = False

    for child in children:
        child_rendered = render_structure_layer_pil(child, document_width, document_height)
        if child_rendered is None:
            continue
        child_rgba, child_left, child_top = child_rendered
        rgba.alpha_composite(child_rgba, (int(child_left - left), int(child_top - top)))
        rendered_any = True

    if not rendered_any:
        return None

    return rgba, int(left), int(top)


def create_rendered_layer_for_structure(layer_info, layer_name, document_width, document_height, batch_size):
    rendered = render_structure_layer_pil(layer_info, document_width, document_height)
    if rendered is None:
        return None

    rgba, left, top = rendered
    rgb, alpha = pil_rgba_to_tensors(rgba)
    return {
        "name": layer_name,
        "image": match_batch_size(rgb, batch_size),
        "mask": match_batch_size(alpha, batch_size),
        "x": [int(left)] * batch_size,
        "y": [int(top)] * batch_size,
    }


def decode_psd_structure_json(json_text, source_psd=None, layer_mode="top_level", batch_size=1):
    try:
        structure = json.loads(json_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid PSD structure JSON: {error}") from error

    if not isinstance(structure, dict):
        raise ValueError("PSD structure JSON must decode to an object.")

    layers_json = structure.get("layers")
    selected_layers = collect_structure_layers(layers_json, layer_mode)
    if not selected_layers:
        logging.warning("PSDC JSON decoder found no layers; returning an empty PSD stack.")

    if is_psd_stack(source_psd):
        batch_size = max(int(batch_size), int(source_psd.get("batch_size", 1)))
    else:
        batch_size = max(1, int(batch_size))

    document_width, document_height = document_size_from_structure(structure, selected_layers)
    by_index_path, by_name = source_layer_lookup(source_psd, layer_mode)

    decoded_layers = []
    for index, (layer_info, group_path) in enumerate(selected_layers):
        name = structure_layer_name(layer_info, f"Layer {index + 1}")
        if group_path:
            name = "/".join(group_path + [name])

        index_path = layer_info.get("index_path")
        source_layer = None
        if isinstance(index_path, list):
            source_layer = by_index_path.get(tuple(index_path))
        if source_layer is None:
            source_layer = by_name.get(layer_info.get("name"))

        rendered_layer = create_rendered_layer_for_structure(layer_info, name, document_width, document_height, batch_size)
        if rendered_layer is not None:
            layer = rendered_layer
        elif source_layer is not None:
            layer = clone_source_layer_for_structure(source_layer, layer_info, document_width, document_height, batch_size)
        else:
            layer = create_placeholder_layer_for_structure(layer_info, name, document_width, document_height, batch_size)

        layer["name"] = name
        visible = bool_from_json(layer_info.get("visible"), True)
        opacity = clamp_int(layer_info.get("opacity"), 255, 0, 255)
        layer["opacity"] = opacity if visible else 0
        layer["visible"] = visible
        layer["blend_mode"] = layer_info.get("blend_mode", "normal")
        layer["structure"] = layer_info
        decoded_layers.append(layer)

    decoded_structure = copy.deepcopy(structure)
    decoded_document = decoded_structure.setdefault("document", {})
    decoded_document["width"] = int(document_width)
    decoded_document["height"] = int(document_height)
    decoded_document["decoded_layer_count"] = len(decoded_layers)
    decoded_document["decoded_layer_mode"] = layer_mode

    return {
        "type": PSD_STACK_TYPE,
        "version": 1,
        "width": int(document_width),
        "height": int(document_height),
        "batch_size": int(batch_size),
        "layers": decoded_layers,
        "structure": decoded_structure,
    }


def psd_stack_source_path(psd_stack):
    if not is_psd_stack(psd_stack):
        raise ValueError("Native PSD JSON apply requires a PSDC PSD stack from PSDC Load PSD.")

    structure = psd_stack.get("structure")
    if not isinstance(structure, dict):
        raise ValueError("PSD stack does not contain source structure metadata.")

    source = structure.get("source")
    if not isinstance(source, dict):
        raise ValueError("PSD stack does not contain a native PSD source path.")

    source_path = source.get("path")
    if source_path and os.path.isfile(source_path):
        return source_path

    filename = source.get("filename")
    if filename:
        try:
            candidate = folder_paths.get_annotated_filepath(filename)
        except Exception:
            candidate = None
        if candidate and os.path.isfile(candidate):
            return candidate

    raise ValueError("Could not resolve the original PSD file. Load it with PSDC Load PSD before applying native JSON edits.")


def iter_native_psd_layers(container, index_path=()):
    for index, layer in enumerate(container):
        current_path = index_path + (index,)
        yield current_path, layer
        if layer.is_group():
            yield from iter_native_psd_layers(layer, current_path)


def iter_json_structure_layers(layers):
    if not isinstance(layers, list):
        return

    for layer_info in layers:
        if not isinstance(layer_info, dict):
            continue
        yield layer_info
        children = layer_info.get("children")
        if isinstance(children, list):
            yield from iter_json_structure_layers(children)


def native_psd_layer_lookup(psd):
    by_index_path = {}
    by_id = {}
    by_name = {}

    for index_path, layer in iter_native_psd_layers(psd):
        by_index_path[index_path] = layer

        layer_id = layer_id_from_tags(layer)
        if layer_id is not None and layer_id not in by_id:
            by_id[layer_id] = layer

        name = getattr(layer, "name", None)
        if name and name not in by_name:
            by_name[name] = layer

    return by_index_path, by_id, by_name


def find_native_layer_for_json(layer_info, by_index_path, by_id, by_name):
    layer_id = layer_info.get("id")
    if layer_id is not None and layer_id in by_id:
        return by_id[layer_id]

    index_path = layer_info.get("index_path")
    if isinstance(index_path, list):
        try:
            path_key = tuple(int(value) for value in index_path)
        except (TypeError, ValueError):
            path_key = None
        if path_key in by_index_path:
            return by_index_path[path_key]

    name = layer_info.get("name")
    if name in by_name:
        return by_name[name]

    return None


def enum_name_or_value(value):
    if isinstance(value, dict):
        if "value" in value:
            return value["value"]
        if "name" in value:
            return value["name"]
    return value


def parse_blend_mode(value):
    value = enum_name_or_value(value)
    if isinstance(value, bytes):
        return BlendMode(value)
    if isinstance(value, PyEnum):
        return BlendMode(value.value)

    text = str(value).strip()
    if not text:
        return BlendMode.NORMAL

    try:
        return BlendMode(text.encode("ascii"))
    except Exception:
        pass

    normalized = text.upper().replace(" ", "_").replace("-", "_")
    if normalized in BlendMode.__members__:
        return BlendMode[normalized]

    compact = normalized.replace("_", "")
    for mode in BlendMode:
        if mode.name.replace("_", "") == compact:
            return mode

    raise ValueError(f"Unsupported blend mode: {value}")


def parse_enum_like(value, enum_type):
    if isinstance(value, enum_type):
        return value

    if isinstance(value, PyEnum):
        value = value.value

    value = enum_name_or_value(value)
    if isinstance(value, bytes):
        return enum_type(value)

    text = str(value).strip()
    normalized = text.upper().replace(" ", "_").replace("-", "_")
    if normalized in getattr(enum_type, "__members__", {}):
        return enum_type[normalized]

    try:
        return enum_type(text.encode("ascii"))
    except Exception:
        return enum_type(text)


def coerce_native_value(value, current_value):
    if isinstance(value, dict) and "value" in value:
        value = value["value"]

    if isinstance(current_value, bool):
        return bool_from_json(value, current_value)
    if isinstance(current_value, int) and not isinstance(current_value, bool):
        return int(round(float(value)))
    if isinstance(current_value, float):
        return float(value)
    if isinstance(current_value, str):
        return str(value)
    if isinstance(current_value, bytes):
        if isinstance(value, dict):
            value = value.get("value", value.get("name", ""))
        if isinstance(value, bytes):
            return value
        return str(value).encode("latin-1", errors="replace")
    if isinstance(current_value, PyEnum):
        return parse_enum_like(value, type(current_value))
    return value


def patch_native_sequence(current_value, json_value):
    if not isinstance(json_value, list):
        return False, current_value

    if isinstance(current_value, tuple):
        if len(current_value) != len(json_value):
            return False, current_value
        updated = tuple(coerce_native_value(value, current_value[index]) for index, value in enumerate(json_value))
        return updated != current_value, updated

    is_list_like = (
        isinstance(current_value, list)
        or (
            not isinstance(current_value, (str, bytes, tuple, dict))
            and not (hasattr(current_value, "items") and callable(current_value.items))
            and hasattr(current_value, "__len__")
            and hasattr(current_value, "__getitem__")
            and hasattr(current_value, "__setitem__")
        )
    )
    if is_list_like:
        changed = False
        for index, item_json in enumerate(json_value[: len(current_value)]):
            item = current_value[index]
            if isinstance(item_json, dict) and not isinstance(item, (str, bytes, int, float, bool, tuple, list)):
                changed = patch_native_object(item, item_json) or changed
            elif isinstance(item, tuple):
                item_changed, updated = patch_native_sequence(item, item_json)
                if item_changed:
                    current_value[index] = updated
                    changed = True
            elif isinstance(item, list):
                item_changed, updated = patch_native_sequence(item, item_json)
                if item_changed:
                    current_value[index] = updated
                    changed = True
            else:
                updated = coerce_native_value(item_json, item)
                if updated != item:
                    current_value[index] = updated
                    changed = True
        return changed, current_value

    return False, current_value


def patch_native_mapping(current_value, json_value):
    if not (hasattr(current_value, "items") and callable(current_value.items) and isinstance(json_value, dict)):
        return False

    changed = False
    key_lookup = {psd_key_to_string(key): key for key in current_value.keys()}

    for json_key, item_json in json_value.items():
        if str(json_key).startswith("_"):
            continue
        if json_key not in key_lookup:
            continue

        key = key_lookup[json_key]
        item = current_value[key]
        if patch_native_object(item, item_json):
            changed = True
        elif hasattr(item, "value"):
            try:
                updated = coerce_native_value(item_json, item.value)
            except Exception:
                continue
            if updated != item.value:
                item.value = updated
                changed = True

    return changed


def patch_native_object(current_value, json_value):
    if not isinstance(json_value, dict):
        return False

    changed = False

    if hasattr(current_value, "items") and callable(current_value.items):
        changed = patch_native_mapping(current_value, json_value) or changed

    for attr, item_json in json_value.items():
        if attr.startswith("_") or attr in ("items",):
            continue
        if not hasattr(current_value, attr):
            continue

        try:
            existing = getattr(current_value, attr)
        except Exception:
            continue

        try:
            if isinstance(existing, (list, tuple)):
                item_changed, updated = patch_native_sequence(existing, item_json)
                if item_changed:
                    if isinstance(existing, tuple):
                        setattr(current_value, attr, updated)
                    changed = True
                continue

            if isinstance(item_json, dict) and not isinstance(existing, (str, bytes, int, float, bool)):
                changed = patch_native_object(existing, item_json) or changed
                continue

            updated = coerce_native_value(item_json, existing)
            if updated != existing:
                setattr(current_value, attr, updated)
                changed = True
        except Exception:
            continue

    if hasattr(current_value, "value") and "value" in json_value:
        try:
            updated = coerce_native_value(json_value["value"], current_value.value)
            if updated != current_value.value:
                current_value.value = updated
                changed = True
        except Exception:
            pass

    return changed


def curve_channel_data_from_json(curves_json):
    is_map = bool_from_json(curves_json.get("is_map"), False)
    channels = curves_json.get("channels")
    if not isinstance(channels, list):
        return None, None, None

    data = []
    channel_bits = 0

    for default_index, channel in enumerate(channels):
        if not isinstance(channel, dict):
            continue

        index = clamp_int(channel.get("index"), default_index, 0, 31)
        channel_bits |= 1 << index

        if is_map:
            lookup = channel.get("lookup")
            if not isinstance(lookup, list) or len(lookup) != 256:
                continue
            data.append([clamp_int(value, 0, 0, 255) for value in lookup])
            continue

        points_json = channel.get("points")
        if not isinstance(points_json, list):
            continue

        points = []
        for point in points_json:
            if isinstance(point, dict):
                output = clamp_int(point.get("output"), 0, 0, 65535)
                input_value = clamp_int(point.get("input"), 0, 0, 65535)
            elif isinstance(point, (list, tuple)) and len(point) >= 2:
                output = clamp_int(point[0], 0, 0, 65535)
                input_value = clamp_int(point[1], 0, 0, 65535)
            else:
                continue
            points.append((output, input_value))

        if len(points) < 2:
            continue
        data.append(points[:19])

    if not data:
        return None, None, None

    return is_map, data, channel_bits


def patch_curves_adjustment(curves, curves_json):
    if not isinstance(curves_json, dict):
        return False

    is_map, data, channel_bits = curve_channel_data_from_json(curves_json)
    if data is None:
        return False

    changed = False
    version = clamp_int(curves_json.get("version"), getattr(curves, "version", 1), 1, 4)
    count_map = clamp_int(curves_json.get("count_map"), channel_bits, 0, 2**32 - 1)
    if version == 1 and bin(count_map).count("1") != len(data):
        count_map = channel_bits
    if version != 1 and count_map != len(data):
        count_map = len(data)

    updates = {
        "is_map": bool(is_map),
        "version": int(version),
        "count_map": int(count_map),
        "data": data,
    }

    for attr, value in updates.items():
        if getattr(curves, attr, None) != value:
            setattr(curves, attr, value)
            changed = True

    return changed


def native_tag_lookup(tagged_blocks):
    lookup = {}
    for tag in tagged_blocks.keys():
        lookup[layer_tag_name(tag)] = tag
    return lookup


def patch_native_layer_tags(layer, layer_info):
    tagged_blocks = getattr(layer, "tagged_blocks", None)
    if not tagged_blocks:
        return 0

    tags_by_name = native_tag_lookup(tagged_blocks)
    changed_count = 0

    for category in ("adjustments", "effect_descriptors", "descriptors"):
        category_updates = layer_info.get(category)
        if not isinstance(category_updates, dict):
            continue

        for tag_name, tag_json in category_updates.items():
            tag = tags_by_name.get(str(tag_name))
            if tag is None:
                continue

            try:
                tag_data = tagged_blocks.get(tag).data
            except Exception:
                continue

            try:
                if psd_value_to_json(tag_data) == tag_json:
                    continue
            except Exception:
                pass

            if str(tag_name) == "CURVES" or type(tag_data).__name__ == "Curves":
                changed = patch_curves_adjustment(tag_data, tag_json)
            elif isinstance(tag_json, list):
                changed, _updated = patch_native_sequence(tag_data, tag_json)
            elif hasattr(tag_data, "value") and not isinstance(tag_json, dict):
                try:
                    updated = coerce_native_value(tag_json, tag_data.value)
                    if updated != tag_data.value:
                        tag_data.value = updated
                        changed = True
                except Exception:
                    changed = False
            elif type(tag_data).__name__ == "EmptyElement":
                changed = False
            else:
                changed = patch_native_object(tag_data, tag_json)

            if changed:
                changed_count += 1

    return changed_count


def patch_native_layer_metadata(layer, layer_info):
    changed = 0

    if "name" in layer_info:
        name = structure_layer_name(layer_info, getattr(layer, "name", "Layer"))
        if getattr(layer, "name", None) != name:
            layer.name = name
            changed += 1

    if "visible" in layer_info:
        visible = bool_from_json(layer_info.get("visible"), getattr(layer, "visible", True))
        if getattr(layer, "visible", None) != visible:
            layer.visible = visible
            changed += 1

    if "opacity" in layer_info:
        opacity = clamp_int(layer_info.get("opacity"), getattr(layer, "opacity", 255), 0, 255)
        if getattr(layer, "opacity", None) != opacity:
            layer.opacity = opacity
            changed += 1

    if "fill_opacity" in layer_info and hasattr(layer, "fill_opacity"):
        fill_opacity = clamp_int(layer_info.get("fill_opacity"), getattr(layer, "fill_opacity", 255), 0, 255)
        if getattr(layer, "fill_opacity", None) != fill_opacity:
            layer.fill_opacity = fill_opacity
            changed += 1

    if "blend_mode" in layer_info and hasattr(layer, "blend_mode"):
        try:
            blend_mode = parse_blend_mode(layer_info.get("blend_mode"))
            if getattr(layer, "blend_mode", None) != blend_mode:
                layer.blend_mode = blend_mode
                changed += 1
        except Exception as error:
            logging.warning("PSDC native JSON apply skipped unsupported blend mode on %s: %s", layer.name, error)

    if "clipping" in layer_info and hasattr(layer, "clipping"):
        try:
            clipping = bool_from_json(layer_info.get("clipping"), getattr(layer, "clipping", False))
            if getattr(layer, "clipping", None) != clipping:
                layer.clipping = clipping
                changed += 1
        except Exception as error:
            logging.warning("PSDC native JSON apply skipped clipping update on %s: %s", layer.name, error)

    return changed


def parse_psd_structure_json(json_text):
    try:
        structure = json.loads(json_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid PSD structure JSON: {error}") from error

    if not isinstance(structure, dict):
        raise ValueError("PSD structure JSON must decode to an object.")

    return structure


def apply_structure_to_native_psd_object(psd, structure):
    by_index_path, by_id, by_name = native_psd_layer_lookup(psd)

    matched_layers = 0
    metadata_updates = 0
    native_tag_updates = 0
    unmatched_layers = []

    for layer_info in iter_json_structure_layers(structure.get("layers")):
        layer = find_native_layer_for_json(layer_info, by_index_path, by_id, by_name)
        if layer is None:
            unmatched_layers.append(layer_info)
            continue

        matched_layers += 1
        metadata_updates += patch_native_layer_metadata(layer, layer_info)
        native_tag_updates += patch_native_layer_tags(layer, layer_info)

    if metadata_updates or native_tag_updates:
        psd._mark_updated()

    return {
        "matched_layers": matched_layers,
        "metadata_updates": metadata_updates,
        "native_tag_updates": native_tag_updates,
        "unmatched_layers": unmatched_layers,
    }


def apply_structure_json_to_native_psd(source_path, json_text, output_path):
    structure = parse_psd_structure_json(json_text)
    psd = PSDImage.open(source_path)
    result = apply_structure_to_native_psd_object(psd, structure)

    psd.save(output_path)
    return {
        **{key: value for key, value in result.items() if key != "unmatched_layers"},
        "created_layers": 0,
        "source_path": source_path,
        "output_path": output_path,
    }


def native_prototype_adjustment_tags(layer):
    tags = native_tag_lookup(getattr(layer, "tagged_blocks", {}))
    return [tag_name for tag_name in tags if classify_layer_tag(tag_name) == "adjustments"]


def load_native_prototype_lookup(library_path=None):
    library_path = library_path or NATIVE_PROTOTYPE_LIBRARY
    if not os.path.isfile(library_path):
        raise ValueError(f"Missing PSDC native prototype library: {library_path}")

    library_psd = PSDImage.open(library_path)
    lookup = {}
    for layer in library_psd:
        for tag_name in native_prototype_adjustment_tags(layer):
            lookup.setdefault(tag_name, layer)
        lookup.setdefault(str(getattr(layer, "kind", "")).lower(), layer)
        lookup.setdefault(type(layer).__name__.lower(), layer)

    return lookup


def native_layer_info_tag_names(layer_info):
    adjustments = layer_info.get("adjustments")
    if not isinstance(adjustments, dict):
        return []
    return [str(tag_name) for tag_name in adjustments.keys()]


def prototype_key_for_layer_info(layer_info):
    tag_names = native_layer_info_tag_names(layer_info)
    if tag_names:
        return tag_names[0]

    kind = str(layer_info.get("kind", "")).lower()
    if kind:
        return kind

    class_name = str(layer_info.get("class", "")).lower()
    if class_name:
        return class_name

    return None


def max_native_layer_id(psd):
    max_layer_id = 0
    for _index_path, layer in iter_native_psd_layers(psd):
        try:
            layer_id = int(layer_id_from_tags(layer) or 0)
        except (TypeError, ValueError):
            layer_id = 0
        max_layer_id = max(max_layer_id, layer_id)
    return max_layer_id


def assign_native_layer_id(layer, layer_id):
    try:
        layer.tagged_blocks.set_data(Tag.LAYER_ID, int(layer_id))
    except Exception:
        pass


def clone_native_prototype_layer(layer_info, prototype_lookup, layer_id):
    prototype_key = prototype_key_for_layer_info(layer_info)
    if not prototype_key:
        return None

    prototype = prototype_lookup.get(prototype_key)
    if prototype is None:
        prototype = prototype_lookup.get(str(prototype_key).lower())
    if prototype is None:
        return None

    layer = copy.deepcopy(prototype)
    assign_native_layer_id(layer, layer_id)
    patch_native_layer_metadata(layer, layer_info)
    patch_native_layer_tags(layer, layer_info)
    return layer


def document_size_from_json_structure(structure):
    selected = collect_structure_layers(structure.get("layers"), "all_layers")
    return document_size_from_structure(structure, selected)


def create_native_psd_from_structure_json(json_text, output_path, source_psd=None, layer_mode="all_layers"):
    structure = parse_psd_structure_json(json_text)
    source_path = psd_stack_source_path(source_psd) if is_psd_stack(source_psd) else None
    if source_path:
        psd = PSDImage.open(source_path)
        patch_result = apply_structure_to_native_psd_object(psd, structure)
        candidate_layers = patch_result["unmatched_layers"]
    else:
        width, height = document_size_from_json_structure(structure)
        psd = PSDImage.new("RGB", (int(width), int(height)))
        patch_result = {
            "matched_layers": 0,
            "metadata_updates": 0,
            "native_tag_updates": 0,
            "unmatched_layers": list(iter_json_structure_layers(structure.get("layers"))),
        }
        candidate_layers = patch_result["unmatched_layers"]

    if layer_mode == "top_level":
        candidate_layers = [
            layer_info for layer_info, _group_path in collect_structure_layers(structure.get("layers"), "top_level")
            if layer_info in candidate_layers
        ]

    prototype_lookup = load_native_prototype_lookup()
    next_layer_id = max_native_layer_id(psd) + 1
    created_layers = 0
    skipped_layers = 0

    for layer_info in candidate_layers:
        if not native_layer_info_tag_names(layer_info):
            continue
        layer = clone_native_prototype_layer(layer_info, prototype_lookup, next_layer_id)
        if layer is None:
            skipped_layers += 1
            continue
        next_layer_id += 1
        psd.append(layer)
        created_layers += 1

    if created_layers:
        psd._mark_updated()

    psd.save(output_path)
    return {
        "matched_layers": patch_result["matched_layers"],
        "metadata_updates": patch_result["metadata_updates"],
        "native_tag_updates": patch_result["native_tag_updates"],
        "created_layers": created_layers,
        "skipped_layers": skipped_layers,
        "source_path": source_path,
        "output_path": output_path,
    }


def load_psd_file_to_stack(path):
    psd = PSDImage.open(path)
    width = int(psd.width)
    height = int(psd.height)
    viewport = (0, 0, width, height)

    layers = []
    # psd_tools iterates top-level layers bottom-to-top, matching the order
    # flatten_psd_stack/composite expect (layers[-1] is the topmost layer).
    for layer in psd:
        pil_image = layer.composite(viewport=viewport)
        if pil_image is None:
            continue
        rgb, alpha = pil_rgba_to_tensors(pil_image)
        layers.append(
            {
                "name": layer.name or f"Layer {len(layers) + 1}",
                "image": rgb,
                "mask": alpha,
                "x": [0],
                "y": [0],
                "opacity": 255,
                "native_source_layer": True,
                "source_index_path": [len(layers)],
            }
        )

    if not layers:
        # Flattened PSD with no addressable layers: keep the whole image as one layer.
        rgb, alpha = pil_rgba_to_tensors(psd.composite(viewport=viewport))
        layers.append(
            {
                "name": "Background",
                "image": rgb,
                "mask": alpha,
                "x": [0],
                "y": [0],
                "opacity": 255,
                "native_source_layer": True,
                "source_index_path": [0],
            }
        )

    return {
        "type": PSD_STACK_TYPE,
        "version": 1,
        "width": width,
        "height": height,
        "batch_size": 1,
        "layers": layers,
        "structure": serialize_psd_document(psd, path),
        "native_passthrough": {
            "enabled": True,
            "source_path": str(path),
            "sha256": file_sha256(path),
        },
    }


class PSDC_PSDLoad:
    @classmethod
    def INPUT_TYPES(cls):
        input_dir = folder_paths.get_input_directory()
        files = []
        if os.path.isdir(input_dir):
            files = [f for f in os.listdir(input_dir) if f.lower().endswith(".psd")]
        return {
            "required": {
                "psd_file": (
                    sorted(files),
                    {"tooltip": "PSD files in the ComfyUI input folder. You can also drag/drop a PSD or use the node's Choose PSD button."},
                ),
            },
        }

    RETURN_TYPES = ("PSD",)
    RETURN_NAMES = ("psd",)
    FUNCTION = "load"
    CATEGORY = "PSDC/Image"

    def load(self, psd_file):
        path = folder_paths.get_annotated_filepath(psd_file)
        return (load_psd_file_to_stack(path),)

    @classmethod
    def IS_CHANGED(cls, psd_file):
        path = folder_paths.get_annotated_filepath(psd_file)
        return file_sha256(path)

    @classmethod
    def VALIDATE_INPUTS(cls, psd_file):
        if not folder_paths.exists_annotated_filepath(psd_file):
            return f"Invalid PSD file: {psd_file}"
        return True


class PSDC_LegacyPSDLoad(PSDC_PSDLoad):
    DEPRECATED = True


class PSDC_ImageToPSD:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            },
            "optional": {
                "mask": ("MASK",),
                "psd": ("PSD",),
            },
        }

    RETURN_TYPES = ("IMAGE", "PSD")
    RETURN_NAMES = ("image", "psd")
    FUNCTION = "execute"
    CATEGORY = "PSDC/Image"

    def execute(self, image, mask=None, psd=None):
        return PSDC_ImageCompositePSD().execute(0, 0, 0, 0, source=image, mask=mask, psd=psd)


def combine_psd_stacks(stacks):
    if not stacks:
        empty = torch.zeros((1, 1, 1, 3), dtype=torch.float32)
        return empty, create_empty_psd_stack(1, 1, 1)

    width = max(int(stack["width"]) for stack in stacks)
    height = max(int(stack["height"]) for stack in stacks)
    batch_size = max(int(stack.get("batch_size", 1)) for stack in stacks)
    combined = create_empty_psd_stack(width, height, batch_size)

    for stack in stacks:
        resized_stack = resize_psd_stack_to_canvas(stack, width, height, batch_size)
        combined["layers"].extend([dict(layer) for layer in resized_stack.get("layers", [])])

    return flatten_psd_stack(combined), combined


if HAS_AUTOGROW:
    PSD_IO = io.Custom("PSD")

    class PSDC_PSDLayerCombine(io.ComfyNode):
        @classmethod
        def define_schema(cls):
            psd_template = io.Autogrow.TemplatePrefix(PSD_IO.Input("psd"), prefix="psd", min=2, max=50)
            return io.Schema(
                node_id="PSDC PSD Layer Combine",
                display_name="PSDC PSD Layer Combine",
                category="PSDC/Image",
                inputs=[
                    io.Autogrow.Input(
                        "psds",
                        template=psd_template,
                        tooltip="Connect two or more PSD stacks. A new PSD socket appears as the last one is used.",
                    )
                ],
                outputs=[
                    io.Image.Output(display_name="image"),
                    PSD_IO.Output(display_name="psd"),
                ],
            )

        @classmethod
        def execute(cls, psds) -> "io.NodeOutput":
            stacks = [psd for psd in psds.values() if is_psd_stack(psd)]
            image, combined = combine_psd_stacks(stacks)
            return io.NodeOutput(image, combined)
else:

    class PSDC_PSDLayerCombine:
        """Fixed-socket combine for ComfyUI versions without the v3 Autogrow API."""

        @classmethod
        def INPUT_TYPES(cls):
            return {
                "required": {
                    "psd_1": ("PSD",),
                    "psd_2": ("PSD",),
                },
                "optional": {f"psd_{index}": ("PSD",) for index in range(3, 9)},
            }

        RETURN_TYPES = ("IMAGE", "PSD")
        RETURN_NAMES = ("image", "psd")
        FUNCTION = "execute"
        CATEGORY = "PSDC/Image"

        def execute(self, **kwargs):
            stacks = [kwargs[key] for key in sorted(kwargs) if is_psd_stack(kwargs.get(key))]
            image, combined = combine_psd_stacks(stacks)
            return (image, combined)


class PSDC_PSDStructureJSON:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "psd": ("PSD",),
                "pretty": ("BOOLEAN", {"default": True}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("json",)
    FUNCTION = "execute"
    CATEGORY = "PSDC/Image"

    def execute(self, psd, pretty=True):
        if is_psd_stack(psd) and psd_stack_structure_matches_current_layers(psd):
            structure = psd["structure"]
        elif is_psd_stack(psd):
            structure = synthesize_stack_structure(psd)
        else:
            structure = {
                "schema": "psdc.psd_structure.v1",
                "error": "Input was not a PSDC PSD stack.",
            }

        indent = 2 if pretty else None
        return (json.dumps(structure, indent=indent, ensure_ascii=False),)


class PSDC_PSDStructureJSONDecode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "json_text": ("STRING", {"multiline": True, "dynamicPrompts": False}),
                "layer_mode": (["top_level", "all_layers"],),
                "batch_size": ("INT", {"default": 1, "min": 1, "max": 4096}),
            },
            "optional": {
                "source_psd": ("PSD",),
            },
        }

    RETURN_TYPES = ("IMAGE", "PSD")
    RETURN_NAMES = ("image", "psd")
    FUNCTION = "execute"
    CATEGORY = "PSDC/Image"

    def execute(self, json_text, layer_mode="top_level", batch_size=1, source_psd=None):
        psd = decode_psd_structure_json(
            json_text,
            source_psd=source_psd,
            layer_mode=layer_mode,
            batch_size=batch_size,
        )
        return (flatten_psd_stack(psd), psd)


class PSDC_NativePSDStructureJSONApply:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "source_psd": ("PSD",),
                "json_text": ("STRING", {"multiline": True, "dynamicPrompts": False}),
                "filename_prefix": ("STRING", {"default": "PSDC_Native_JSON"}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("path",)
    FUNCTION = "execute"
    OUTPUT_NODE = True
    CATEGORY = "PSDC/Image"

    def execute(self, source_psd, json_text, filename_prefix="PSDC_Native_JSON"):
        source_path = psd_stack_source_path(source_psd)
        width = int(source_psd.get("width", 1)) if is_psd_stack(source_psd) else 1
        height = int(source_psd.get("height", 1)) if is_psd_stack(source_psd) else 1

        full_output_folder, filename, counter, _subfolder, _filename_prefix = folder_paths.get_save_image_path(
            filename_prefix,
            self.output_dir,
            width,
            height,
        )
        file = f"{filename.replace('%batch_num%', '0')}_{counter:05}_.psd"
        output_path = os.path.join(full_output_folder, file)

        result = apply_structure_json_to_native_psd(source_path, json_text, output_path)
        message = (
            f"Saved native PSD: {output_path} "
            f"(matched_layers={result['matched_layers']}, "
            f"metadata_updates={result['metadata_updates']}, "
            f"native_tag_updates={result['native_tag_updates']})"
        )
        logging.info("PSDC native JSON apply %s", message)
        return {"ui": {"text": [message]}, "result": (output_path,)}


class PSDC_NativePSDStructureJSONDecode:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "json_text": ("STRING", {"multiline": True, "dynamicPrompts": False}),
                "filename_prefix": ("STRING", {"default": "PSDC_Native_JSON_Decode"}),
                "layer_mode": (["all_layers", "top_level"],),
            },
            "optional": {
                "source_psd": ("PSD",),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("path",)
    FUNCTION = "execute"
    OUTPUT_NODE = True
    CATEGORY = "PSDC/Image"

    def execute(self, json_text, filename_prefix="PSDC_Native_JSON_Decode", layer_mode="all_layers", source_psd=None):
        structure = parse_psd_structure_json(json_text)
        if is_psd_stack(source_psd):
            width = int(source_psd.get("width", 1))
            height = int(source_psd.get("height", 1))
        else:
            width, height = document_size_from_json_structure(structure)

        full_output_folder, filename, counter, _subfolder, _filename_prefix = folder_paths.get_save_image_path(
            filename_prefix,
            self.output_dir,
            int(width),
            int(height),
        )
        file = f"{filename.replace('%batch_num%', '0')}_{counter:05}_.psd"
        output_path = os.path.join(full_output_folder, file)

        result = create_native_psd_from_structure_json(
            json_text,
            output_path,
            source_psd=source_psd,
            layer_mode=layer_mode,
        )
        message = (
            f"Saved native decoded PSD: {output_path} "
            f"(matched_layers={result['matched_layers']}, "
            f"metadata_updates={result['metadata_updates']}, "
            f"native_tag_updates={result['native_tag_updates']}, "
            f"created_layers={result['created_layers']}, "
            f"skipped_layers={result['skipped_layers']})"
        )
        logging.info("PSDC native JSON decode %s", message)
        return {"ui": {"text": [message]}, "result": (output_path,)}


class PSDC_PreviewPSD:
    def __init__(self):
        self.output_dir = folder_paths.get_temp_directory()
        self.type = "temp"
        self.prefix_append = "_temp_" + "".join(random.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(5))
        self.compress_level = 1

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "psd": ("PSD",),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ()
    FUNCTION = "preview_psd"
    OUTPUT_NODE = True
    CATEGORY = "PSDC/Image"

    def preview_psd(self, psd, prompt=None, extra_pnginfo=None):
        if not is_psd_stack(psd):
            logging.warning("PSDC Preview PSD received an invalid PSD stack; nothing was previewed.")
            return {}

        images = flatten_psd_stack(psd)
        filename_prefix = "PSDC_Preview" + self.prefix_append
        full_output_folder, filename, counter, subfolder, filename_prefix = folder_paths.get_save_image_path(
            filename_prefix,
            self.output_dir,
            images[0].shape[1],
            images[0].shape[0],
        )

        results = []
        for batch_number, image in enumerate(images):
            array = 255.0 * image.detach().cpu().numpy()
            img = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8))
            file = f"{filename.replace('%batch_num%', str(batch_number))}_{counter:05}_.png"
            img.save(os.path.join(full_output_folder, file), compress_level=self.compress_level)
            results.append(
                {
                    "filename": file,
                    "subfolder": subfolder,
                    "type": self.type,
                }
            )
            counter += 1

        return {"ui": {"images": results}}


class PSDC_SavePSD:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "filename_prefix": ("STRING", {"default": "ComfyUI"}),
                "file_mode": (["multi_file", "single_file"],),
                "alpha_name": ("STRING", {"default": "_mask_"}),
                "alpha_name_mode": (["simple", "suffix"],),
            },
            "optional": {
                "images": ("IMAGE",),
                "psd": ("PSD",),
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "save_rgba_psd"
    OUTPUT_NODE = True
    CATEGORY = "PSDC/Image"

    def save_rgba_psd(self, filename_prefix, file_mode, alpha_name="_mask_", alpha_name_mode="simple", images=None, psd=None):
        if is_psd_stack(psd):
            try:
                return self.save_psd_stack(psd, filename_prefix, file_mode)
            except Exception as error:
                logging.warning("Falling back to legacy image PSD save after PSD stack error: %s", str(error))

        if images is None:
            logging.warning("PSDC Save PSD received neither images nor a PSD stack; nothing was saved.")
            return {}

        full_output_folder, filename, counter, subfolder, filename_prefix = folder_paths.get_save_image_path(
            filename_prefix,
            self.output_dir,
            images[0].shape[1],
            images[0].shape[0],
        )

        batch_size, height, width, channels = images.shape

        try:
            if file_mode == "single_file":
                psd = PSDImage.new("RGB", (width, height))

                for batch_number, img_tensor in enumerate(reversed(images)):
                    layer_name = f"Layer {batch_number + 1}"
                    alpha_mask, rgb_image = extract_alpha_mask(img_tensor)
                    append_pixel_layer_with_mask(psd, rgb_image, alpha_mask, layer_name, channels == 4)

                filename_with_batch_num = filename.replace("%batch_num%", str(batch_number))
                file = f"{filename_with_batch_num}_{counter:05}_.psd"
                psd.save(os.path.join(full_output_folder, file))
                logging.info("PSD file was successfully saved: %s", file)

            else:
                for batch_number, img_tensor in enumerate(images):
                    psd = PSDImage.new("RGB", (width, height))
                    alpha_mask, rgb_image = extract_alpha_mask(img_tensor)
                    append_pixel_layer_with_mask(psd, rgb_image, alpha_mask, "Layer 1", channels == 4)

                    filename_with_batch_num = filename.replace("%batch_num%", str(batch_number))
                    file = f"{filename_with_batch_num}_{counter:05}_{batch_number}.psd"
                    psd.save(os.path.join(full_output_folder, file))
                    logging.info("PSD file %s/%s was successfully saved: %s", batch_number + 1, batch_size, file)

        except Exception as error:
            logging.warning("Error occurred while saving PSD: %s", str(error))
            logging.warning("Saving in PNG format as an alternative...")

            for index, img_tensor in enumerate(images):
                try:
                    img_pil = create_pil_from_tensor(img_tensor)
                    alt_file = f"{filename.replace('%batch_num%', str(index))}_{counter:05}_.png"
                    alt_path = os.path.join(full_output_folder, alt_file)
                    img_pil.save(alt_path)
                except Exception as alt_error:
                    logging.warning("Failed to save as PNG as well: %s", str(alt_error))

        return {}

    def save_psd_stack(self, psd_stack, filename_prefix, file_mode):
        full_output_folder, filename, counter, subfolder, filename_prefix = folder_paths.get_save_image_path(
            filename_prefix,
            self.output_dir,
            int(psd_stack["width"]),
            int(psd_stack["height"]),
        )

        batch_size = int(psd_stack.get("batch_size", 1))
        native_source_path = native_passthrough_source_path(psd_stack)
        if native_source_path is not None:
            return self.save_native_source_psd(
                psd_stack,
                native_source_path,
                filename,
                counter,
                full_output_folder,
                file_mode,
                batch_size,
            )

        try:
            if file_mode == "single_file":
                file = f"{filename.replace('%batch_num%', '0')}_{counter:05}_.psd"
                psd_image = create_psd_image_from_stack(psd_stack, 0)
                psd_image.save(os.path.join(full_output_folder, file))
                logging.info("PSD stack file was successfully saved: %s", file)

                if batch_size > 1:
                    logging.info(
                        "PSD stack had %s batch entries; single_file mode saved batch 0 only. "
                        "Use multi_file to save every PSD stack entry.",
                        batch_size,
                    )
            else:
                for batch_number in range(batch_size):
                    file = f"{filename.replace('%batch_num%', str(batch_number))}_{counter:05}_{batch_number}.psd"
                    psd_image = create_psd_image_from_stack(psd_stack, batch_number)
                    psd_image.save(os.path.join(full_output_folder, file))
                    logging.info("PSD stack file %s/%s was successfully saved: %s", batch_number + 1, batch_size, file)

        except Exception as error:
            logging.warning("Error occurred while saving PSD stack: %s", str(error))
            raise

        return {}

    def create_native_psd_from_source_stack(self, psd_stack, source_path, batch_index):
        psd_image = PSDImage.open(source_path)
        source_width = int(psd_image.width)
        source_height = int(psd_image.height)
        target_width = int(psd_stack.get("width", source_width))
        target_height = int(psd_stack.get("height", source_height))

        if target_width != source_width or target_height != source_height:
            logging.warning(
                "Native PSD source size %sx%s differs from PSDC stack size %sx%s. "
                "Keeping native layers unscaled and expanding the canvas only.",
                source_width,
                source_height,
                target_width,
                target_height,
            )
            psd_image._record.header.width = max(source_width, target_width)
            psd_image._record.header.height = max(source_height, target_height)
            psd_image._mark_updated()

        width = int(psd_image.width)
        height = int(psd_image.height)
        for layer in psd_overlay_layers(psd_stack):
            rgb_image, alpha_mask, has_alpha, opacity = layer_to_pil(layer, batch_index, width, height)
            append_pixel_layer_with_mask(psd_image, rgb_image, alpha_mask, layer["name"], has_alpha, opacity)

        return psd_image

    def save_native_source_psd(self, psd_stack, source_path, filename, counter, full_output_folder, file_mode, batch_size):
        overlay_layers = psd_overlay_layers(psd_stack)
        if not overlay_layers:
            return self.save_native_passthrough_psd(source_path, filename, counter, full_output_folder, file_mode, batch_size)

        try:
            if file_mode == "single_file":
                file = f"{filename.replace('%batch_num%', '0')}_{counter:05}_.psd"
                psd_image = self.create_native_psd_from_source_stack(psd_stack, source_path, 0)
                psd_image.save(os.path.join(full_output_folder, file))
                logging.info(
                    "Native PSD source file was saved with %s PSDC overlay layer(s): %s",
                    len(overlay_layers),
                    file,
                )

                if batch_size > 1:
                    logging.info(
                        "Native PSD source stack had %s batch entries; single_file mode saved batch 0 only. "
                        "Use multi_file to save every overlay batch on top of the native PSD source.",
                        batch_size,
                    )
            else:
                for batch_number in range(batch_size):
                    file = f"{filename.replace('%batch_num%', str(batch_number))}_{counter:05}_{batch_number}.psd"
                    psd_image = self.create_native_psd_from_source_stack(psd_stack, source_path, batch_number)
                    psd_image.save(os.path.join(full_output_folder, file))
                    logging.info(
                        "Native PSD source file %s/%s was saved with %s PSDC overlay layer(s): %s",
                        batch_number + 1,
                        batch_size,
                        len(overlay_layers),
                        file,
                    )
        except Exception as error:
            logging.warning("Error occurred while saving native PSD source stack: %s", str(error))
            raise

        return {}

    def save_native_passthrough_psd(self, source_path, filename, counter, full_output_folder, file_mode, batch_size):
        if file_mode == "single_file":
            file = f"{filename.replace('%batch_num%', '0')}_{counter:05}_.psd"
        else:
            file = f"{filename.replace('%batch_num%', '0')}_{counter:05}_0.psd"
            if batch_size > 1:
                logging.info(
                    "Native PSD passthrough saved the original source once; "
                    "batch size %s is ignored because the native PSD source is a single document.",
                    batch_size,
                )

        output_path = os.path.join(full_output_folder, file)
        shutil.copyfile(source_path, output_path)
        logging.info("Native PSD passthrough copied source PSD to: %s", output_path)
        return {}


class PSDC_ExtractAlpha:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            }
        }

    RETURN_TYPES = ("MASK", "IMAGE")
    FUNCTION = "extract_alpha"
    CATEGORY = "PSDC/Image"

    def extract_alpha(self, image):
        alpha_tensors = []
        rgba_tensors = []

        for img_tensor in image:
            alpha_pil, rgb_pil = extract_alpha_mask(img_tensor)
            alpha_np = np.array(alpha_pil)
            rgb_np = np.array(rgb_pil)

            alpha_tensor = torch.from_numpy(alpha_np).float() / 255.0
            rgb_tensor = torch.from_numpy(rgb_np).float() / 255.0

            rgba_tensor = torch.ones(
                (rgb_tensor.shape[0], rgb_tensor.shape[1], 4),
                dtype=rgb_tensor.dtype,
                device=rgb_tensor.device,
            )
            rgba_tensor[..., :3] = rgb_tensor
            rgba_tensor[..., 3] = alpha_tensor

            alpha_tensors.append(alpha_tensor)
            rgba_tensors.append(rgba_tensor)

        return (torch.stack(alpha_tensors), torch.stack(rgba_tensors))


NODE_CLASS_MAPPINGS = {
    "PSDC Apply Alpha Channel": PSDC_ApplyAlphaChannel,
    "PSDC Image Composite PSD": PSDC_ImageCompositePSD,
    "PSDC Load PSD": PSDC_PSDLoad,
    "PSD Load": PSDC_LegacyPSDLoad,
    "PSDC Image To PSD": PSDC_ImageToPSD,
    "PSDC PSD Layer Combine": PSDC_PSDLayerCombine,
    "PSDC PSD Structure JSON": PSDC_PSDStructureJSON,
    "PSDC PSD Structure JSON Decode": PSDC_PSDStructureJSONDecode,
    "PSDC Native PSD Structure JSON Apply": PSDC_NativePSDStructureJSONApply,
    "PSDC Native PSD Structure JSON Decode": PSDC_NativePSDStructureJSONDecode,
    "PSDC Preview PSD": PSDC_PreviewPSD,
    "PSDC Save PSD": PSDC_SavePSD,
    "PSDC Extract Alpha": PSDC_ExtractAlpha,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PSDC Apply Alpha Channel": "PSDC Apply Alpha Channel",
    "PSDC Image Composite PSD": "PSDC Image Composite PSD",
    "PSDC Load PSD": "PSDC Load PSD",
    "PSD Load": "PSDC Load PSD",
    "PSDC Image To PSD": "PSDC Image To PSD",
    "PSDC PSD Layer Combine": "PSDC PSD Layer Combine",
    "PSDC PSD Structure JSON": "PSDC PSD Structure JSON",
    "PSDC PSD Structure JSON Decode": "PSDC PSD Structure JSON Decode",
    "PSDC Native PSD Structure JSON Apply": "PSDC Native PSD Structure JSON Apply",
    "PSDC Native PSD Structure JSON Decode": "PSDC Native PSD Structure JSON Decode",
    "PSDC Preview PSD": "PSDC Preview PSD",
    "PSDC Save PSD": "PSDC Save PSD",
    "PSDC Extract Alpha": "PSDC Extract Alpha",
}
