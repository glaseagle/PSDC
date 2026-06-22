import copy
import hashlib
import json
import logging
import os
import random
import shutil
from enum import Enum as PyEnum
from io import BytesIO

import folder_paths
import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFont
from psd_tools import PSDImage
from psd_tools.api import layers as psd_layers
from psd_tools.constants import BlendMode, LinkedLayerType, Tag
from psd_tools.psd.tagged_blocks import TaggedBlock

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
NATIVE_TEXT_PROTOTYPE_LIBRARY = os.path.join(NODE_DIR, "assets", "psdc_type_prototypes.psd")
DEFAULT_NATIVE_DOCUMENT_SIZE = 1024


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_sha256_head(path, length=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read(length))
    return digest.hexdigest()


def read_u32(data, offset):
    if offset < 0 or offset + 4 > len(data):
        raise ValueError("Offset outside file while reading uint32.")
    return int.from_bytes(data[offset : offset + 4], "big")


def read_u64(data, offset):
    if offset < 0 or offset + 8 > len(data):
        raise ValueError("Offset outside file while reading uint64.")
    return int.from_bytes(data[offset : offset + 8], "big")


def write_u32(data, offset, value):
    if offset < 0 or offset + 4 > len(data):
        raise ValueError("Offset outside file while writing uint32.")
    data[offset : offset + 4] = int(value).to_bytes(4, "big")


def padded_length(length, padding):
    if padding <= 1:
        return int(length)
    return int(length) + ((int(padding) - (int(length) % int(padding))) % int(padding))


def normalize_lnk2_lifd_versions_in_psd(psd):
    record = getattr(psd, "_record", None)
    layer_mask_info = getattr(record, "layer_and_mask_information", None)
    tagged_blocks = getattr(layer_mask_info, "tagged_blocks", None)
    if not tagged_blocks:
        return 0

    linked_layers = None
    try:
        if Tag.LINKED_LAYER2 in tagged_blocks:
            linked_layers = tagged_blocks.get_data(Tag.LINKED_LAYER2)
    except Exception:
        linked_layers = None

    if linked_layers is None:
        return 0

    changed = 0
    for linked_layer in linked_layers:
        try:
            if linked_layer.kind == LinkedLayerType.DATA and int(linked_layer.version) == 8:
                linked_layer.version = 7
                changed += 1
        except Exception:
            continue

    if changed:
        try:
            psd._mark_updated()
        except Exception:
            pass
    return changed


def psd_tagged_block_length_size(key, psd_version):
    try:
        tag = Tag(key)
    except ValueError:
        tag = key
    fmt = TaggedBlock._length_format(tag, int(psd_version))
    return 8 if fmt == "Q" else 4


def patch_lnk2_payload_lifd_versions(data, payload_start, payload_end):
    patched = 0
    cursor = int(payload_start)
    payload_end = int(payload_end)

    while cursor + 8 <= payload_end:
        record_len = read_u64(data, cursor)
        record_payload_start = cursor + 8
        record_payload_end = record_payload_start + record_len
        if record_payload_end > payload_end:
            break

        if record_len >= 8 and data[record_payload_start : record_payload_start + 4] == b"liFD":
            version_offset = record_payload_start + 4
            if read_u32(data, version_offset) == 8:
                write_u32(data, version_offset, 7)
                patched += 1

        cursor = record_payload_start + padded_length(record_len, 4)

    return patched


def normalize_lnk2_lifd_versions_in_file(path):
    path = os.fspath(path)
    try:
        data = bytearray(open(path, "rb").read())
    except OSError:
        return 0

    if len(data) < 30 or data[0:4] != b"8BPS":
        return 0

    psd_version = int.from_bytes(data[4:6], "big")
    if psd_version not in (1, 2):
        return 0

    try:
        cursor = 26
        color_mode_len = read_u32(data, cursor)
        cursor += 4 + color_mode_len
        image_resources_len = read_u32(data, cursor)
        cursor += 4 + image_resources_len

        length_size = 8 if psd_version == 2 else 4
        layer_mask_len = read_u64(data, cursor) if length_size == 8 else read_u32(data, cursor)
        cursor += length_size
        layer_mask_end = cursor + layer_mask_len
        if layer_mask_len == 0 or layer_mask_end > len(data):
            return 0

        layer_info_len = read_u64(data, cursor) if length_size == 8 else read_u32(data, cursor)
        cursor += length_size + layer_info_len
        if cursor > layer_mask_end:
            return 0

        global_mask_len = read_u32(data, cursor)
        cursor += 4 + global_mask_len
        if cursor > layer_mask_end:
            return 0

        patched = 0
        while cursor + 12 <= layer_mask_end:
            signature = bytes(data[cursor : cursor + 4])
            if signature not in (b"8BIM", b"8B64"):
                break
            key = bytes(data[cursor + 4 : cursor + 8])
            cursor += 8
            block_length_size = psd_tagged_block_length_size(key, psd_version)
            if cursor + block_length_size > layer_mask_end:
                break
            block_len = read_u64(data, cursor) if block_length_size == 8 else read_u32(data, cursor)
            cursor += block_length_size
            payload_start = cursor
            payload_end = payload_start + block_len
            if payload_end > layer_mask_end:
                break
            if key == b"lnk2":
                patched += patch_lnk2_payload_lifd_versions(data, payload_start, payload_end)
            cursor = payload_start + padded_length(block_len, 4)

        if patched:
            with open(path, "wb") as handle:
                handle.write(data)
        return patched
    except Exception as error:
        logging.warning("PSDC could not normalize lnk2 liFD records in %s: %s", path, error)
        return 0


def save_psd_photoshop_safe(psd, path):
    normalized_before = normalize_lnk2_lifd_versions_in_psd(psd)
    psd.save(path)
    normalized_after = normalize_lnk2_lifd_versions_in_file(path)
    if normalized_before or normalized_after:
        logging.info(
            "PSDC normalized lnk2 liFD smart-object record versions for Photoshop compatibility: before_save=%s post_save=%s path=%s",
            normalized_before,
            normalized_after,
            path,
        )
    return path


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


def has_native_passthrough(psd_stack):
    return native_passthrough_source_path(psd_stack) is not None


def layer_as_raster_overlay(layer):
    overlay = dict(layer)
    overlay.pop("native_source_layer", None)
    overlay.pop("source_index_path", None)
    return overlay


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


def resize_psd_stack_to_canvas(psd, width, height, batch_size=None, preserve_native=True):
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

    if preserve_native and has_native_passthrough(psd):
        resized = copy_psd_stack(psd)
        resized["width"] = int(width)
        resized["height"] = int(height)
        resized["batch_size"] = int(batch_size)
        return resized

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
    if is_psd_stack(psd):
        return resize_psd_stack_to_canvas(psd, width, height, batch_size, preserve_native=True)

    if destination is not None:
        return create_psd_stack_from_destination(destination)

    return create_empty_psd_stack(width, height, batch_size)


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
    "OBJECT_BASED_EFFECTS",
    "EFFECTS_LAYER",
    "EFFECT",
    "STROKE",
    "SHADOW",
    "GLOW",
    "BEVEL",
    "OVERLAY",
)

EFFECT_CLASS_ALIASES = {
    "DRSH": ("DrSh", "DropShadow", "drop_shadow", "dropshadow"),
    "IRSH": ("IrSh", "InnerShadow", "inner_shadow", "innershadow"),
    "ORGL": ("OrGl", "OuterGlow", "outer_glow", "outerglow"),
    "IRGL": ("IrGl", "InnerGlow", "inner_glow", "innerglow"),
    "FRFX": ("FrFX", "Stroke", "stroke"),
    "EBBL": ("ebbl", "BevelEmboss", "bevel_emboss", "bevelemboss"),
}

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


def stable_json_sha256(value):
    try:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    except Exception:
        payload = repr(value).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def tagged_block_names(layer):
    tagged_blocks = getattr(layer, "tagged_blocks", None)
    if not tagged_blocks:
        return []
    return [layer_tag_name(tag) for tag in tagged_blocks.keys()]


def native_layer_common(layer_info):
    return {
        "visible": layer_info.get("visible", True),
        "opacity": layer_info.get("opacity", 255),
        "fill_opacity": layer_info.get("fill_opacity", 255),
        "blend_mode": layer_info.get("blend_mode"),
        "clipping": layer_info.get("clipping", False),
        "locked": False,
    }


def native_layer_raw_refs(layer, tags):
    descriptor_checksums = {}
    for category in ("adjustments", "effect_descriptors", "descriptors"):
        category_tags = tags.get(category, {})
        if not isinstance(category_tags, dict):
            continue
        for tag_name, tag_value in category_tags.items():
            descriptor_checksums[str(tag_name)] = "sha256:" + stable_json_sha256(tag_value)[:24]

    return {
        "layer_id_tag": "LAYER_ID" if layer_id_from_tags(layer) is not None else None,
        "descriptor_checksums": descriptor_checksums,
        "tagged_blocks": tagged_block_names(layer),
    }


def engine_data_value(value):
    if hasattr(value, "value"):
        return value.value
    return value


def engine_data_list_values(value):
    if not isinstance(value, (list, tuple)):
        return []
    return [engine_data_value(item) for item in value]


def serialize_text_editable(layer):
    if str(getattr(layer, "kind", "")).lower() != "type":
        return None

    try:
        contents = str(layer.text)
    except Exception:
        return None

    text_type = None
    try:
        text_type = psd_value_to_json(layer.text_type)
        if isinstance(text_type, dict):
            text_type = text_type.get("name") or text_type.get("value")
    except Exception:
        pass

    style_run_lengths = []
    paragraph_run_lengths = []
    fonts = []
    runs = []

    try:
        engine_dict = layer.engine_dict
        style_run = engine_dict.get("StyleRun", {})
        paragraph_run = engine_dict.get("ParagraphRun", {})
        style_run_lengths = engine_data_list_values(style_run.get("RunLengthArray", []))
        paragraph_run_lengths = engine_data_list_values(paragraph_run.get("RunLengthArray", []))
        run_array = style_run.get("RunArray", [])
        resource_dict = layer.resource_dict
        font_set = resource_dict.get("FontSet", []) if resource_dict else []

        for index, font in enumerate(font_set):
            font_json = psd_value_to_json(font)
            font_info = {"index": index}
            if isinstance(font_json, dict):
                font_info["postscript_name"] = font_json.get("Name") or font_json.get("PostScriptName")
                font_info["family"] = font_json.get("FontFamilyName") or font_json.get("FamilyName")
                font_info["style"] = font_json.get("FontStyleName") or font_json.get("StyleName")
            fonts.append(font_info)

        cursor = 0
        for run_index, run in enumerate(run_array):
            run_length = int(style_run_lengths[run_index]) if run_index < len(style_run_lengths) else len(contents)
            style_data = {}
            try:
                style_data = run.get("StyleSheet", {}).get("StyleSheetData", {})
            except Exception:
                style_data = {}
            run_info = {
                "start": cursor,
                "end": max(cursor, cursor + max(0, run_length - 1)),
                "font": engine_data_value(style_data.get("Font")) if hasattr(style_data, "get") else None,
                "font_size": engine_data_value(style_data.get("FontSize")) if hasattr(style_data, "get") else None,
            }
            runs.append(run_info)
            cursor += run_length
    except Exception:
        pass

    single_style_run = len(style_run_lengths) <= 1 and len(paragraph_run_lengths) <= 1
    result = {
        "contents": contents,
        "text_type": text_type,
        "single_style_run": single_style_run,
        "style_run_count": len(style_run_lengths),
        "paragraph_run_count": len(paragraph_run_lengths),
        "fonts": fonts,
        "runs": runs,
        "supported_operations": ["replace_text"],
    }
    if not single_style_run:
        result["multi_run_replacement"] = "supported_by_redistributing_existing_style_and_paragraph_run_lengths"
    return result


def semantic_adjustment_projection(layer_info):
    adjustments = layer_info.get("adjustments")
    if not isinstance(adjustments, dict) or not adjustments:
        return None

    if "CURVES" in adjustments and isinstance(adjustments["CURVES"], dict):
        curves = copy.deepcopy(adjustments["CURVES"])
        curves["type"] = "curves"
        curves["supported_operations"] = ["set_adjustment"]
        return curves

    first_tag, first_value = next(iter(adjustments.items()))
    result = {
        "type": normalize_prototype_key(first_tag),
        "supported_operations": ["set_adjustment"],
    }
    if isinstance(first_value, dict):
        result["raw"] = first_value
    return result


def semantic_fill_projection(layer_info):
    kind = str(layer_info.get("kind", "")).lower()
    adjustments = layer_info.get("adjustments")
    if not isinstance(adjustments, dict):
        adjustments = {}

    if "solid" in kind:
        return {
            "type": "solid_color",
            "supported_operations": ["set_adjustment"],
            "raw": adjustments,
        }
    if "gradient" in kind:
        return {
            "type": "gradient",
            "supported_operations": ["set_adjustment"],
            "raw": adjustments,
        }
    return None


def semantic_effects_projection(layer_info):
    effect_descriptors = layer_info.get("effect_descriptors")
    effects = layer_info.get("effects")
    if not effect_descriptors and not effects:
        return None
    if effect_descriptors:
        return {
            "supported_operations": ["set_effect"],
            "raw_descriptors": effect_descriptors,
            "parsed": effects or [],
        }
    return {
        "supported_operations": [],
        "raw_descriptors": {},
        "parsed": effects or [],
        "unsupported_reasons": {
            "set_effect": "This layer reports layer effects, but psd-tools did not expose an editable native effect descriptor block. PSDC will preserve the existing effect and reject semantic edits instead of silently dropping it.",
        },
    }


def native_layer_supported_operations(layer, layer_info):
    operations = [
        "rename_layer",
        "set_visibility",
        "set_opacity",
        "set_blend_mode",
        "set_clipping",
        "duplicate_layer",
        "move_layer",
        "reorder_layer",
        "translate_layer",
        "transform_layer",
    ]
    if hasattr(layer, "fill_opacity"):
        operations.append("set_fill_opacity")
    if str(getattr(layer, "kind", "")).lower() == "type":
        text = serialize_text_editable(layer)
        if text and "replace_text" in text.get("supported_operations", []):
            operations.append("replace_text")
    smart_object = layer_info.get("smart_object")
    if isinstance(smart_object, dict) and smart_object.get("editable_embedded_psd"):
        operations.append("replace_text")
    if layer_info.get("adjustments"):
        operations.append("set_adjustment")
    if layer_info.get("effect_descriptors"):
        operations.append("set_effect")
    if layer.is_group():
        operations.append("create_group")
    return operations


def native_layer_editable_projection(layer, layer_info):
    editable = {}

    text = serialize_text_editable(layer)
    if text:
        editable["text"] = text

    adjustment = semantic_adjustment_projection(layer_info)
    if adjustment:
        editable["adjustment"] = adjustment

    fill = semantic_fill_projection(layer_info)
    if fill:
        editable["fill"] = fill

    effects = semantic_effects_projection(layer_info)
    if effects:
        editable["effects"] = effects

    smart_object = layer_info.get("smart_object")
    if isinstance(smart_object, dict):
        editable["smart_object"] = {
            **smart_object,
            "supported_operations": ["replace_text"] if smart_object.get("editable_embedded_psd") else [],
        }

    if layer.is_group():
        editable["group"] = {
            "expanded": True,
            "pass_through": True,
            "supported_operations": ["rename_layer", "set_visibility", "move_layer", "reorder_layer", "duplicate_layer", "create_group"],
        }

    return editable


def smart_object_chain_entry(layer, smart_object):
    return {
        "layer_id": layer_id_from_tags(layer),
        "name": getattr(layer, "name", ""),
        "filename": smart_object.get("filename") if isinstance(smart_object, dict) else None,
        "filetype": smart_object.get("filetype") if isinstance(smart_object, dict) else None,
        "kind": smart_object.get("kind") if isinstance(smart_object, dict) else None,
    }


def serialize_embedded_psd_document(layer, layer_path, smart_object_chain, include_nested_smart_objects):
    smart_object = getattr(layer, "smart_object", None)
    if not smart_object:
        return None

    try:
        if not smart_object.is_psd():
            return None
        with smart_object.open() as handle:
            embedded_psd = PSDImage.open(handle)
    except Exception as error:
        return {"error": str(error)}

    return {
        "width": int(embedded_psd.width),
        "height": int(embedded_psd.height),
        "layers": [
            serialize_layer_structure(
                child,
                (index,),
                parent_id=None,
                path=layer_path,
                include_nested_smart_objects=include_nested_smart_objects,
                smart_object_chain=smart_object_chain,
            )
            for index, child in enumerate(embedded_psd)
        ],
    }


def serialize_layer_structure(
    layer,
    index_path,
    parent_id=None,
    path=None,
    include_nested_smart_objects=True,
    smart_object_chain=None,
):
    layer_name = layer.name or ""
    layer_path = list(path or []) + [layer_name]
    layer_id = layer_id_from_tags(layer)
    bbox = tuple(int(value) for value in getattr(layer, "bbox", (0, 0, 0, 0)))
    tags = serialize_layer_tags(layer)
    layer_info = {
        "index_path": list(index_path),
        "id": layer_id,
        "parent_id": parent_id,
        "path": layer_path,
        "smart_object_chain": copy.deepcopy(smart_object_chain) if smart_object_chain else [],
        "name": layer_name,
        "kind": str(getattr(layer, "kind", "")),
        "class": type(layer).__name__,
        "order": {
            "sibling_index": int(index_path[-1]) if index_path else 0,
            "stack_order": "psd-tools-iteration-order",
        },
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
    layer_info["common"] = native_layer_common(layer_info)

    smart_object = serialize_smart_object(layer)
    if smart_object:
        layer_info["smart_object"] = smart_object
        try:
            smart_object["editable_embedded_psd"] = bool(getattr(layer, "smart_object").is_psd())
        except Exception:
            smart_object["editable_embedded_psd"] = False

    effects = serialize_layer_effects(layer)
    if effects:
        layer_info["effects"] = effects

    if tags["adjustments"]:
        layer_info["adjustments"] = tags["adjustments"]
    if tags["effect_descriptors"]:
        layer_info["effect_descriptors"] = tags["effect_descriptors"]
    if tags["descriptors"]:
        layer_info["descriptors"] = tags["descriptors"]

    layer_info["editable"] = native_layer_editable_projection(layer, layer_info)
    layer_info["raw_refs"] = native_layer_raw_refs(layer, tags)
    layer_info["supported_operations"] = native_layer_supported_operations(layer, layer_info)

    if smart_object and include_nested_smart_objects and smart_object.get("editable_embedded_psd"):
        nested_chain = list(copy.deepcopy(smart_object_chain) if smart_object_chain else [])
        nested_chain.append(smart_object_chain_entry(layer, smart_object))
        layer_info["embedded_document"] = serialize_embedded_psd_document(
            layer,
            layer_path,
            nested_chain,
            include_nested_smart_objects,
        )

    if layer.is_group():
        layer_info["children"] = [
            serialize_layer_structure(
                child,
                index_path + (index,),
                parent_id=layer_id,
                path=layer_path,
                include_nested_smart_objects=include_nested_smart_objects,
                smart_object_chain=smart_object_chain,
            )
            for index, child in enumerate(layer)
        ]

    return layer_info


def serialize_psd_document(psd, source_path=None, include_nested_smart_objects=True):
    fingerprint = {}
    if source_path and os.path.isfile(source_path):
        try:
            fingerprint = {
                "size": os.path.getsize(source_path),
                "mtime": str(os.path.getmtime(source_path)),
                "sha256_head": file_sha256_head(source_path)[:24],
            }
        except Exception:
            fingerprint = {}

    return {
        "schema": "psdc.native_snapshot.v1",
        "description": "Layer/effect/adjustment metadata extracted from a Photoshop PSD. Pixel tensors are not embedded.",
        "source": {
            "path": str(source_path) if source_path else None,
            "filename": os.path.basename(str(source_path)) if source_path else None,
            "fingerprint": fingerprint,
        },
        "document": {
            "width": int(psd.width),
            "height": int(psd.height),
            "color_mode": "RGB",
            "depth": int(getattr(getattr(psd, "_record", None), "header", {}).depth) if hasattr(getattr(psd, "_record", None), "header") else 8,
            "layer_count_top_level": len(psd),
            "layer_order": "array order follows psd-tools iteration order used by PSDC; children preserve their group nesting.",
        },
        "capabilities": {
            "supports_patch_schema": ["psdc.native_patch.v1"],
            "supports_operations": [
                "rename_layer",
                "set_visibility",
                "set_opacity",
                "set_fill_opacity",
                "set_blend_mode",
                "set_clipping",
                "replace_text",
                "set_adjustment",
                "set_effect",
                "create_group",
                "create_adjustment",
                "create_effect_layer",
                "create_text",
            ],
        },
        "layers": [
            serialize_layer_structure(
                layer,
                (index,),
                include_nested_smart_objects=include_nested_smart_objects,
            )
            for index, layer in enumerate(psd)
        ],
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


def iter_native_psd_layers(container, index_path=(), name_path=()):
    for index, layer in enumerate(container):
        current_path = index_path + (index,)
        current_name_path = name_path + (getattr(layer, "name", "") or "",)
        yield current_path, layer
        if layer.is_group():
            yield from iter_native_psd_layers(layer, current_path, current_name_path)


def iter_native_psd_layers_with_name_paths(container, index_path=(), name_path=()):
    for index, layer in enumerate(container):
        current_index_path = index_path + (index,)
        current_name_path = name_path + (getattr(layer, "name", "") or "",)
        yield current_index_path, current_name_path, layer
        if layer.is_group():
            yield from iter_native_psd_layers_with_name_paths(layer, current_index_path, current_name_path)


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
    by_path = {}

    for index_path, name_path, layer in iter_native_psd_layers_with_name_paths(psd):
        by_index_path[index_path] = layer
        by_path[name_path] = layer

        layer_id = layer_id_from_tags(layer)
        if layer_id is not None and layer_id not in by_id:
            by_id[layer_id] = layer

        name = getattr(layer, "name", None)
        if name and name not in by_name:
            by_name[name] = layer

    return by_index_path, by_id, by_name, by_path


def find_native_layer_for_json(layer_info, by_index_path, by_id, by_name, by_path):
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

    path = layer_info.get("path")
    if isinstance(path, list):
        path_key = tuple(str(value) for value in path)
        if path_key in by_path:
            return by_path[path_key]

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
    by_index_path, by_id, by_name, by_path = native_psd_layer_lookup(psd)

    matched_layers = 0
    metadata_updates = 0
    native_tag_updates = 0
    unmatched_layers = []

    for layer_info in iter_json_structure_layers(structure.get("layers")):
        layer = find_native_layer_for_json(layer_info, by_index_path, by_id, by_name, by_path)
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

    save_psd_photoshop_safe(psd, output_path)
    return {
        **{key: value for key, value in result.items() if key != "unmatched_layers"},
        "created_layers": 0,
        "source_path": source_path,
        "output_path": output_path,
    }


NATIVE_PATCH_SCHEMA = "psdc.native_patch.v1"
NATIVE_PATCH_REPORT_SCHEMA = "psdc.native_patch_report.v1"
SUPPORTED_NATIVE_PATCH_OPERATIONS = {
    "rename_layer",
    "set_visibility",
    "set_opacity",
    "set_fill_opacity",
    "set_blend_mode",
    "set_clipping",
    "duplicate_layer",
    "move_layer",
    "reorder_layer",
    "translate_layer",
    "transform_layer",
    "crop_layer",
    "warp_layer",
    "replace_text",
    "set_adjustment",
    "set_effect",
    "create_group",
    "create_adjustment",
    "create_effect_layer",
    "create_effect",
    "create_text",
}

ADJUSTMENT_PATCH_TAGS = {
    "vibrance": "VIBRANCE",
    "curves": "CURVES",
    "levels": "LEVELS",
    "hue_saturation": "HUE_SATURATION",
    "brightness_contrast": "BRIGHTNESS_AND_CONTRAST",
    "brightness_and_contrast": "BRIGHTNESS_AND_CONTRAST",
    "exposure": "EXPOSURE",
    "color_balance": "COLOR_BALANCE",
    "black_and_white": "BLACK_AND_WHITE",
    "photo_filter": "PHOTO_FILTER",
    "channel_mixer": "CHANNEL_MIXER",
    "color_lookup": "COLOR_LOOKUP",
    "selective_color": "SELECTIVE_COLOR",
    "invert": "INVERT",
    "posterize": "POSTERIZE",
    "threshold": "THRESHOLD",
    "gradient_map": "GRADIENT_MAP",
    "solid_color": "SOLID_COLOR_SHEET_SETTING",
    "solid_color_fill": "SOLID_COLOR_SHEET_SETTING",
}


def parse_native_patch_json(patch_json):
    try:
        patch = json.loads(patch_json)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid PSDC native patch JSON: {error}") from error

    if not isinstance(patch, dict):
        raise ValueError("PSDC native patch JSON must decode to an object.")

    schema = patch.get("schema", NATIVE_PATCH_SCHEMA)
    if schema != NATIVE_PATCH_SCHEMA:
        raise ValueError(f"Unsupported native patch schema: {schema}")

    operations = patch.get("operations")
    if not isinstance(operations, list):
        raise ValueError("PSDC native patch JSON must contain an operations array.")

    normalized = {
        "schema": NATIVE_PATCH_SCHEMA,
        "document": copy.deepcopy(patch.get("document", {})) if isinstance(patch.get("document", {}), dict) else {},
        "source": patch.get("source", {}) if isinstance(patch.get("source", {}), dict) else {},
        "operations": [],
    }
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            normalized["operations"].append({"op": None, "_error": f"Operation {index} is not an object."})
            continue
        normalized["operations"].append(copy.deepcopy(operation))
    return normalized


def document_size_from_native_patch(patch):
    document = patch.get("document") if isinstance(patch, dict) else None
    if not isinstance(document, dict):
        source = patch.get("source") if isinstance(patch, dict) else None
        document = source.get("document") if isinstance(source, dict) else None
    if not isinstance(document, dict):
        document = {}

    width = clamp_int(document.get("width"), DEFAULT_NATIVE_DOCUMENT_SIZE, 1, MAX_RESOLUTION)
    height = clamp_int(document.get("height"), DEFAULT_NATIVE_DOCUMENT_SIZE, 1, MAX_RESOLUTION)

    operations = patch.get("operations") if isinstance(patch, dict) else None
    if isinstance(operations, list):
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            bbox = operation.get("bbox")
            if not isinstance(bbox, dict):
                continue
            width = max(width, clamp_int(bbox.get("right"), width, 1, MAX_RESOLUTION))
            height = max(height, clamp_int(bbox.get("bottom"), height, 1, MAX_RESOLUTION))

    return int(width), int(height)


def document_size_from_json_or_patch(edit):
    if isinstance(edit, dict) and isinstance(edit.get("layers"), list):
        return document_size_from_json_structure(edit)
    if isinstance(edit, dict) and (edit.get("schema") == NATIVE_PATCH_SCHEMA or isinstance(edit.get("operations"), list)):
        return document_size_from_native_patch(edit)
    document = edit.get("document") if isinstance(edit, dict) else None
    if isinstance(document, dict):
        return (
            clamp_int(document.get("width"), DEFAULT_NATIVE_DOCUMENT_SIZE, 1, MAX_RESOLUTION),
            clamp_int(document.get("height"), DEFAULT_NATIVE_DOCUMENT_SIZE, 1, MAX_RESOLUTION),
        )
    return DEFAULT_NATIVE_DOCUMENT_SIZE, DEFAULT_NATIVE_DOCUMENT_SIZE


def operation_target_name(operation, fallback):
    target = operation.get("target")
    if isinstance(target, dict):
        if target.get("name"):
            return str(target.get("name"))
        path = target.get("path")
        if isinstance(path, list) and path:
            return str(path[-1])
    return fallback


def blank_native_patch_operation(operation):
    op_name = operation.get("op")
    if op_name == "set_adjustment":
        adjustment_type = operation.get("adjustment", operation.get("type"))
        return {
            "op": "create_adjustment",
            "type": adjustment_type,
            "name": operation.get("name") or operation_target_name(operation, f"PSDC {str(adjustment_type).replace('_', ' ').title()}"),
            "value": operation.get("value", {}),
            "visible": operation.get("visible", True),
            "opacity": operation.get("opacity", 255),
            "opacity_unit": operation.get("opacity_unit", operation.get("unit")),
            "blend_mode": operation.get("blend_mode", "norm"),
            "clipping": operation.get("clipping", False),
            "bbox": operation.get("bbox"),
        }
    if op_name == "set_effect":
        effect = operation.get("effect", operation.get("type"))
        return {
            "op": "create_effect_layer",
            "effect": effect,
            "name": operation.get("name") or operation_target_name(operation, f"PSDC {str(effect).replace('_', ' ').title()}"),
            "value": operation.get("value", {}),
            "visible": operation.get("visible", True),
            "opacity": operation.get("opacity", 255),
            "opacity_unit": operation.get("opacity_unit", operation.get("unit")),
            "fill_opacity": operation.get("fill_opacity", 255),
            "blend_mode": operation.get("blend_mode", "norm"),
            "clipping": operation.get("clipping", False),
            "bbox": operation.get("bbox"),
        }
    if op_name == "replace_text":
        return {
            "op": "create_text",
            "name": operation.get("name") or operation_target_name(operation, "PSDC Text"),
            "value": operation.get("value", ""),
            "visible": operation.get("visible", True),
            "opacity": operation.get("opacity", 255),
            "opacity_unit": operation.get("opacity_unit", operation.get("unit")),
            "blend_mode": operation.get("blend_mode", "norm"),
            "clipping": operation.get("clipping", False),
            "bbox": operation.get("bbox"),
        }
    return operation


def iter_native_psd_layers_with_paths(container, index_path=(), name_path=()):
    for index, layer in enumerate(container):
        current_index_path = index_path + (index,)
        current_name_path = name_path + (getattr(layer, "name", "") or "",)
        yield current_index_path, current_name_path, layer
        if layer.is_group():
            yield from iter_native_psd_layers_with_paths(layer, current_index_path, current_name_path)


def native_layer_target_index(psd):
    by_index_path = {}
    by_id = {}
    by_path = {}
    by_name = {}

    for index_path, name_path, layer in iter_native_psd_layers_with_paths(psd):
        by_index_path[index_path] = layer
        by_path.setdefault(name_path, []).append(layer)
        by_name.setdefault(getattr(layer, "name", "") or "", []).append(layer)

        layer_id = layer_id_from_tags(layer)
        if layer_id is not None:
            by_id.setdefault(str(layer_id), layer)

    return {
        "by_index_path": by_index_path,
        "by_id": by_id,
        "by_path": by_path,
        "by_name": by_name,
    }


def target_layer_id(target):
    for key in ("id", "layer_id"):
        if key in target and target[key] is not None:
            return str(target[key])
    return None


def coerce_index_path(value):
    if not isinstance(value, list):
        return None
    try:
        return tuple(int(item) for item in value)
    except (TypeError, ValueError):
        return None


def resolve_native_layer_target(psd, target):
    if not isinstance(target, dict):
        return None, "Target must be an object."

    index = native_layer_target_index(psd)

    layer_id = target_layer_id(target)
    if layer_id is not None and layer_id in index["by_id"]:
        return index["by_id"][layer_id], None

    index_path = coerce_index_path(target.get("index_path"))
    if index_path is not None and index_path in index["by_index_path"]:
        return index["by_index_path"][index_path], None

    path = target.get("path")
    if isinstance(path, list) and path:
        path_tuple = tuple(str(part) for part in path)
        if path_tuple in index["by_path"]:
            matches = index["by_path"][path_tuple]
            if len(matches) == 1:
                return matches[0], None
            return None, f"Layer path {list(path_tuple)!r} is ambiguous; use id or index_path."
        suffix_matches = [
            layer
            for candidate_path, layers in index["by_path"].items()
            for layer in layers
            if len(candidate_path) <= len(path_tuple) and path_tuple[-len(candidate_path) :] == candidate_path
        ]
        if len(suffix_matches) == 1:
            return suffix_matches[0], None
        if len(suffix_matches) > 1:
            return None, f"Layer path suffix {list(path_tuple)!r} is ambiguous; use id or index_path."

    name = target.get("name")
    if not name and isinstance(path, list) and path:
        name = path[-1]
    if name:
        matches = index["by_name"].get(str(name), [])
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, f"Layer name '{name}' is ambiguous; use id or index_path."

    return None, "Could not resolve target layer by id, index_path, path, or unique name."


def opacity_to_native_value(value, unit=None):
    if str(unit or "").lower() == "percent":
        return clamp_int(round(float(value) * 255.0 / 100.0), 255, 0, 255)
    return clamp_int(value, 255, 0, 255)


def text_layer_run_arrays(layer):
    try:
        engine_dict = layer._engine_data.get("EngineDict", {})
        return [
            engine_dict.get("StyleRun", {}).get("RunLengthArray", []),
            engine_dict.get("ParagraphRun", {}).get("RunLengthArray", []),
        ]
    except Exception:
        return []


def text_layer_has_single_run(layer):
    run_arrays = text_layer_run_arrays(layer)
    if not run_arrays:
        return False
    return all(len(run_array) <= 1 for run_array in run_arrays)


def set_engine_run_length_entry(run_array, index, value):
    value = int(value)
    if hasattr(run_array[index], "value"):
        run_array[index].value = value
    else:
        run_array[index] = value


def engine_text_length(text):
    # Photoshop EngineData run lengths include the terminating carriage return.
    return len(str(text)) + 1


def distribute_run_lengths(old_lengths, new_total):
    old_lengths = [max(0, int(engine_data_value(value) or 0)) for value in old_lengths]
    new_total = max(0, int(new_total))
    if not old_lengths:
        return []
    if len(old_lengths) == 1:
        return [new_total]

    old_total = sum(old_lengths)
    if old_total <= 0:
        result = [0] * len(old_lengths)
        result[-1] = new_total
        return result

    result = []
    used = 0
    for index, old_length in enumerate(old_lengths[:-1]):
        length = int(round(new_total * (old_length / old_total)))
        remaining_slots = len(old_lengths) - index - 1
        length = max(0, min(length, max(0, new_total - used - remaining_slots)))
        result.append(length)
        used += length
    result.append(max(0, new_total - used))
    return result


def explicit_run_lengths_from_operation(operation, key, fallback_key=None):
    for current_key in (key, fallback_key):
        if not current_key:
            continue
        value = operation.get(current_key)
        if isinstance(value, list):
            try:
                return [int(item) for item in value]
            except (TypeError, ValueError):
                raise ValueError(f"{current_key} must contain integer run lengths.")
    return None


def set_text_layer_run_lengths(layer, new_text, operation=None):
    operation = operation if isinstance(operation, dict) else {}
    new_total = engine_text_length(new_text)
    run_arrays = text_layer_run_arrays(layer)
    explicit_by_index = [
        explicit_run_lengths_from_operation(operation, "style_run_lengths", "run_lengths"),
        explicit_run_lengths_from_operation(operation, "paragraph_run_lengths"),
    ]

    for array_index, run_array in enumerate(run_arrays):
        if len(run_array) == 0:
            continue
        old_lengths = [engine_data_value(value) for value in run_array]
        new_lengths = explicit_by_index[array_index] if array_index < len(explicit_by_index) else None
        if new_lengths is None:
            new_lengths = distribute_run_lengths(old_lengths, new_total)
        if len(new_lengths) != len(run_array):
            raise ValueError(f"Run length override must contain {len(run_array)} values.")
        if sum(int(value) for value in new_lengths) != new_total:
            raise ValueError(f"Run length override must sum to {new_total}.")
        for index, length in enumerate(new_lengths):
            set_engine_run_length_entry(run_array, index, length)


def replace_type_layer_text(layer, new_text, operation=None):
    if str(getattr(layer, "kind", "")).lower() != "type":
        raise ValueError("Target is not a Photoshop type layer.")

    old_text = str(layer.text)
    new_text = str(new_text)

    text_data = getattr(layer, "_data", None).text_data
    text_data.get(b"Txt ").value = new_text + "\x00"
    layer._engine_data["EngineDict"]["Editor"]["Text"].value = new_text + "\r"
    set_text_layer_run_lengths(layer, new_text, operation)
    return old_text


TEXT_STYLE_KEYS = (
    "font_family",
    "font",
    "font_size",
    "size",
    "color",
    "alignment",
    "tracking",
    "leading",
    "faux_bold",
    "bold",
    "faux_italic",
    "italic",
)

TEXT_ALIGNMENT_VALUES = {
    "left": 0,
    "start": 0,
    "right": 1,
    "end": 1,
    "center": 2,
    "centre": 2,
    "justify": 3,
    "justified": 3,
}


def text_style_from_operation(operation):
    if not isinstance(operation, dict):
        return {}
    style = copy.deepcopy(operation.get("style")) if isinstance(operation.get("style"), dict) else {}
    for key in TEXT_STYLE_KEYS:
        if key in operation:
            style[key] = operation[key]
    return style


def set_engine_mapping_value(mapping, key, value):
    if not hasattr(mapping, "get"):
        return False
    try:
        current = mapping.get(key)
        if hasattr(current, "value"):
            current.value = value
        else:
            mapping[key] = value
        return True
    except Exception:
        return False


def set_engine_list_entry(values, index, value):
    if hasattr(values[index], "value"):
        values[index].value = value
    else:
        values[index] = value


def set_text_fill_color(style_data, value):
    fill_color = style_data.get("FillColor") if hasattr(style_data, "get") else None
    if not hasattr(fill_color, "get"):
        return False
    red, green, blue = parse_text_color(value, default=(255, 255, 255))
    set_engine_mapping_value(fill_color, "Type", 1)
    values = fill_color.get("Values")
    try:
        value_count = len(values)
    except Exception:
        value_count = 0
    if value_count < 4:
        return False
    for index, channel in enumerate((1.0, red / 255.0, green / 255.0, blue / 255.0)):
        set_engine_list_entry(values, index, float(channel))
    return True


def resolve_text_font_index(layer, font_name):
    if not font_name:
        return None
    font_name = normalize_prototype_key(font_name)
    try:
        resource_dict = layer.resource_dict
        font_set = resource_dict.get("FontSet", []) if resource_dict else []
    except Exception:
        return None
    for index, font in enumerate(font_set):
        font_json = psd_value_to_json(font)
        candidates = [str(index)]
        if isinstance(font_json, dict):
            candidates.extend(
                str(font_json.get(key) or "")
                for key in ("Name", "PostScriptName", "FontFamilyName", "FamilyName", "FontStyleName", "StyleName")
            )
        if font_name in {normalize_prototype_key(candidate) for candidate in candidates if candidate}:
            return index
    return None


def apply_text_style_operation(layer, operation):
    style = text_style_from_operation(operation)
    if not style:
        return []
    if str(getattr(layer, "kind", "")).lower() != "type":
        return []

    warnings = []
    try:
        engine_dict = layer.engine_dict
    except Exception as error:
        return [f"Could not read text EngineData: {error}"]

    font_index = None
    if style.get("font_family") or style.get("font"):
        font_index = resolve_text_font_index(layer, style.get("font_family", style.get("font")))
        if font_index is None:
            warnings.append("Requested font was not found in this layer's existing FontSet; font family was preserved.")

    for run in engine_dict.get("StyleRun", {}).get("RunArray", []):
        style_data = run.get("StyleSheet", {}).get("StyleSheetData", {}) if hasattr(run, "get") else {}
        if font_index is not None:
            set_engine_mapping_value(style_data, "Font", font_index)
        if style.get("font_size", style.get("size")) is not None:
            set_engine_mapping_value(style_data, "FontSize", float(style.get("font_size", style.get("size"))))
        if style.get("color") is not None:
            if not set_text_fill_color(style_data, style.get("color")):
                warnings.append("Could not update text fill color because this layer's EngineData color object was not editable.")
        if style.get("tracking") is not None:
            set_engine_mapping_value(style_data, "Tracking", int(round(float(style.get("tracking")))))
        if style.get("leading") is not None:
            set_engine_mapping_value(style_data, "AutoLeading", False)
            set_engine_mapping_value(style_data, "Leading", float(style.get("leading")))
        if style.get("faux_bold", style.get("bold")) is not None:
            set_engine_mapping_value(style_data, "FauxBold", bool_from_json(style.get("faux_bold", style.get("bold")), False))
        if style.get("faux_italic", style.get("italic")) is not None:
            set_engine_mapping_value(style_data, "FauxItalic", bool_from_json(style.get("faux_italic", style.get("italic")), False))

    alignment = style.get("alignment")
    if alignment is not None:
        alignment_value = TEXT_ALIGNMENT_VALUES.get(normalize_prototype_key(alignment))
        if alignment_value is None:
            warnings.append(f"Unsupported text alignment {alignment!r}; alignment was preserved.")
        else:
            for run in engine_dict.get("ParagraphRun", {}).get("RunArray", []):
                properties = run.get("ParagraphSheet", {}).get("Properties", {}) if hasattr(run, "get") else {}
                set_engine_mapping_value(properties, "Justification", alignment_value)

    try:
        layer._psd._mark_updated()
    except Exception:
        pass
    return warnings


def psd_to_bytes(psd):
    output = BytesIO()
    psd.save(output)
    return output.getvalue()


def open_embedded_smart_object_psd(layer):
    smart_object = getattr(layer, "smart_object", None)
    if not smart_object:
        raise ValueError("Target layer is not a smart object.")
    if not smart_object.is_psd():
        raise ValueError("Target smart object is not an embedded PSD/PSB.")
    with smart_object.open() as handle:
        return PSDImage.open(handle)


def write_embedded_smart_object_psd(layer, embedded_psd):
    smart_object = getattr(layer, "smart_object", None)
    if not smart_object or getattr(smart_object, "_data", None) is None:
        raise ValueError("Target smart object has no writable embedded data.")
    data = psd_to_bytes(embedded_psd)
    smart_object._data.data = data
    try:
        smart_object._data.filesize = len(data)
    except Exception:
        pass


def text_layers_in_psd(psd):
    return [layer for _index_path, layer in iter_native_psd_layers(psd) if str(getattr(layer, "kind", "")).lower() == "type"]


def replace_text_in_embedded_smart_object(smart_layer, target, value, smart_object_chain, operation=None):
    embedded_psd = open_embedded_smart_object_psd(smart_layer)

    if smart_object_chain:
        next_smart_layer, error = resolve_native_layer_target(embedded_psd, smart_object_chain[0])
        if next_smart_layer is None:
            raise ValueError(error or "Could not resolve nested smart object target.")
        old_text = replace_text_in_embedded_smart_object(next_smart_layer, target, value, smart_object_chain[1:], operation)
    else:
        child_target = copy.deepcopy(target) if isinstance(target, dict) else {}
        child_target.pop("smart_object_chain", None)
        text_layer, error = resolve_native_layer_target(embedded_psd, child_target)
        if text_layer is None or str(getattr(text_layer, "kind", "")).lower() != "type":
            candidates = text_layers_in_psd(embedded_psd)
            if len(candidates) == 1:
                text_layer = candidates[0]
            else:
                raise ValueError(error or "Could not resolve embedded text layer.")
        old_text = replace_type_layer_text(text_layer, value, operation)
        apply_text_style_operation(text_layer, operation)

    embedded_psd._mark_updated()
    write_embedded_smart_object_psd(smart_layer, embedded_psd)
    return old_text


def replace_text_operation(psd, operation):
    target = operation.get("target", {})
    value = operation.get("value", "")
    smart_object_chain = target.get("smart_object_chain", [])

    if smart_object_chain:
        smart_layer, error = resolve_native_layer_target(psd, smart_object_chain[0])
        if smart_layer is None:
            raise ValueError(error or "Could not resolve smart object chain target.")
        old_text = replace_text_in_embedded_smart_object(smart_layer, target, value, smart_object_chain[1:], operation)
        style_warnings = []
    else:
        layer, error = resolve_native_layer_target(psd, target)
        if layer is None:
            raise ValueError(error or "Could not resolve target layer.")
        if str(getattr(layer, "kind", "")).lower() == "type":
            old_text = replace_type_layer_text(layer, value, operation)
            style_warnings = apply_text_style_operation(layer, operation)
        elif str(getattr(layer, "kind", "")).lower() == "smartobject":
            old_text = replace_text_in_embedded_smart_object(layer, target, value, [], operation)
            style_warnings = []
        else:
            raise ValueError("replace_text target must be a type layer or embedded PSD/PSB smart object.")

    psd._mark_updated()
    suffix = f" Warnings: {'; '.join(style_warnings)}" if style_warnings else ""
    return f"Changed text from {old_text!r} to {str(value)!r}. Cached Photoshop previews may be stale until Photoshop refreshes the document.{suffix}"


def adjustment_patch_tag(layer, operation):
    requested = normalize_prototype_key(operation.get("adjustment", ""))
    tags = native_prototype_adjustment_tags(layer)
    if requested in ADJUSTMENT_PATCH_TAGS and ADJUSTMENT_PATCH_TAGS[requested] in tags:
        return ADJUSTMENT_PATCH_TAGS[requested]
    for tag_name in tags:
        if normalize_prototype_key(tag_name) == requested:
            return tag_name
    if len(tags) == 1:
        return tags[0]
    return None


def set_adjustment_operation(layer, operation):
    tag_name = adjustment_patch_tag(layer, operation)
    if tag_name is None:
        raise ValueError("Could not resolve a supported native adjustment tag on the target layer.")

    value = operation.get("value", {})
    if not isinstance(value, dict):
        raise ValueError("set_adjustment value must be an object.")

    if tag_name == "CURVES":
        changed = patch_native_layer_tags(layer, {"adjustments": {tag_name: value}})
    else:
        raw_value = value.get("raw", value)
        changed = patch_native_layer_tags(layer, {"adjustments": {tag_name: raw_value}})

    if not changed:
        raise ValueError("No adjustment values were changed.")
    return f"Updated {tag_name} adjustment."


def set_effect_operation(layer, operation):
    value = operation.get("value", {})
    if not isinstance(value, dict):
        raise ValueError("set_effect value must be an object.")

    effect_descriptors = serialize_layer_tags(layer).get("effect_descriptors", {})
    if not effect_descriptors:
        if native_layer_effect_keys_from_effects(layer):
            raise ValueError(
                "Target layer has effects, but no editable native effect descriptor block was exposed. "
                "Existing effects were preserved; use raw_descriptors from an encoder snapshot or create a new effect layer."
            )
        raise ValueError("Target layer has no editable effect descriptor block.")

    if "raw_descriptors" in value and isinstance(value["raw_descriptors"], dict):
        patched_descriptors = value["raw_descriptors"]
    else:
        effect = operation.get("effect", operation.get("type"))
        if effect:
            patched_descriptors = apply_semantic_effect_json(effect_descriptors, effect, value)
        else:
            patched_descriptors = value

    changed = patch_native_layer_tags(layer, {"effect_descriptors": patched_descriptors})

    if not changed:
        raise ValueError(
            "No effect descriptor values were changed. Pass changed semantic fields or raw_descriptors copied from the snapshot."
        )
    return "Updated native effect descriptor values."


def rgb_triplet_from_json(value):
    red, green, blue = parse_text_color(value, default=(255, 255, 255))
    return {"Rd": float(red), "Grn": float(green), "Bl": float(blue)}


def adjustment_type_to_tag(value):
    normalized = normalize_prototype_key(value)
    if normalized in ADJUSTMENT_PATCH_TAGS:
        return ADJUSTMENT_PATCH_TAGS[normalized]
    upper = str(value).strip().upper()
    return upper if upper else None


def semantic_adjustment_patch_value(tag_name, value):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("Adjustment creation value must be an object.")

    if "raw" in value and isinstance(value["raw"], dict):
        return value["raw"]

    tag_name = str(tag_name)
    result = copy.deepcopy(value)

    if tag_name == "SOLID_COLOR_SHEET_SETTING":
        color = value.get("color", value.get("fill_color"))
        if color is not None:
            result.pop("color", None)
            result.pop("fill_color", None)
            result["Clr"] = rgb_triplet_from_json(color)
        return result

    if tag_name == "HUE_SATURATION":
        master = value.get("master")
        if isinstance(master, dict):
            result["master"] = [
                clamp_int(master.get("hue"), 0, -180, 180),
                clamp_int(master.get("saturation"), 0, -100, 100),
                clamp_int(master.get("lightness"), 0, -100, 100),
            ]
        if "colorize" in value:
            result["enable"] = 1 if bool_from_json(value.get("colorize"), False) else 0
        return result

    return result


def create_adjustment_layer_info(operation):
    adjustment_type = operation.get("type", operation.get("adjustment"))
    tag_name = adjustment_type_to_tag(adjustment_type)
    if not tag_name:
        raise ValueError("create_adjustment requires a type, for example 'curves' or 'solid_color'.")

    value = semantic_adjustment_patch_value(tag_name, operation.get("value", {}))
    name = str(operation.get("name") or f"PSDC {str(adjustment_type).replace('_', ' ').title()}")
    return {
        "id": None,
        "name": name,
        "kind": normalize_prototype_key(adjustment_type),
        "class": "AdjustmentLayer",
        "visible": bool_from_json(operation.get("visible"), True),
        "opacity": opacity_to_native_value(operation.get("opacity", 255), operation.get("opacity_unit")),
        "fill_opacity": opacity_to_native_value(operation.get("fill_opacity", 255), operation.get("fill_opacity_unit")),
        "blend_mode": operation.get("blend_mode", "norm"),
        "clipping": bool_from_json(operation.get("clipping"), False),
        "bbox": {"left": 0, "top": 0, "right": 0, "bottom": 0},
        "has_mask": False,
        "has_vector_mask": False,
        "has_effects": False,
        "adjustments": {tag_name: value},
        "effects": [],
        "effect_descriptors": {},
        "descriptors": {},
        "smart_object": None,
        "children": [],
    }


def effect_class_id(value):
    aliases = effect_aliases_for_key(value)
    for alias in aliases:
        upper = str(alias).upper()
        if upper in EFFECT_CLASS_ALIASES:
            return upper
    for class_id, values in EFFECT_CLASS_ALIASES.items():
        if str(value).upper() == class_id or normalize_prototype_key(value) in {normalize_prototype_key(alias) for alias in values}:
            return class_id
    return str(value).strip()


def descriptor_class_matches(descriptor, class_id):
    if not isinstance(descriptor, dict):
        return False
    current = descriptor.get("_classID") or descriptor.get("classID")
    if current is None:
        return False
    current_aliases = {normalize_prototype_key(alias) for alias in effect_aliases_for_key(current)}
    target_aliases = {normalize_prototype_key(alias) for alias in effect_aliases_for_key(class_id)}
    return bool(current_aliases & target_aliases)


def find_effect_descriptor_json(value, class_id):
    matches = []

    def walk(item):
        if isinstance(item, dict):
            if descriptor_class_matches(item, class_id):
                matches.append(item)
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    if not matches:
        return None
    for match in matches:
        if bool_from_json(match.get("enab"), False) or bool_from_json(match.get("present"), False):
            return match
    return matches[0]


def set_descriptor_unit_value(descriptor, key, value, unit=None):
    if key not in descriptor:
        return
    current = descriptor[key]
    if isinstance(current, dict) and "value" in current:
        current["value"] = float(value)
        if unit is not None and "unit" in current:
            current["unit"] = unit
    else:
        descriptor[key] = value


def set_descriptor_color(descriptor, key, value):
    if key not in descriptor:
        return
    current = descriptor[key]
    rgb = rgb_triplet_from_json(value)
    if isinstance(current, dict):
        current.update(rgb)
    else:
        descriptor[key] = rgb


def apply_semantic_effect_json(effect_descriptors, effect, value):
    if not isinstance(value, dict):
        raise ValueError("Effect creation value must be an object.")
    if "raw_descriptors" in value and isinstance(value["raw_descriptors"], dict):
        return value["raw_descriptors"]

    effect_info = copy.deepcopy(effect_descriptors)
    class_id = effect_class_id(effect)
    descriptor = find_effect_descriptor_json(effect_info, class_id)
    if descriptor is None:
        raise ValueError(f"Could not find {effect!r} descriptor in the cloned prototype.")

    if "enabled" in value:
        enabled = bool_from_json(value.get("enabled"), True)
    else:
        enabled = True
    descriptor["enab"] = enabled
    descriptor["present"] = enabled
    descriptor["showInDialog"] = True

    if "opacity" in value:
        set_descriptor_unit_value(descriptor, "Opct", value["opacity"], "#Prc")
        set_descriptor_unit_value(descriptor, "hglO", value["opacity"], "#Prc")
        set_descriptor_unit_value(descriptor, "sdwO", value["opacity"], "#Prc")
    if "distance" in value:
        set_descriptor_unit_value(descriptor, "Dstn", value["distance"], "#Pxl")
    if "size" in value:
        if "Sz" in descriptor:
            set_descriptor_unit_value(descriptor, "Sz", value["size"], "#Pxl")
        else:
            set_descriptor_unit_value(descriptor, "blur", value["size"], "#Pxl")
    if "blur" in value:
        set_descriptor_unit_value(descriptor, "blur", value["blur"], "#Pxl")
    if "spread" in value:
        set_descriptor_unit_value(descriptor, "Ckmt", value["spread"], "#Pxl")
    if "choke" in value:
        set_descriptor_unit_value(descriptor, "Ckmt", value["choke"], "#Pxl")
    if "noise" in value:
        set_descriptor_unit_value(descriptor, "Nose", value["noise"], "#Prc")
    if "angle" in value:
        set_descriptor_unit_value(descriptor, "lagl", value["angle"], "#Ang")
    if "use_global_light" in value and "uglg" in descriptor:
        descriptor["uglg"] = bool_from_json(value.get("use_global_light"), True)
    if "color" in value:
        set_descriptor_color(descriptor, "Clr", value["color"])
    if "highlight_color" in value:
        set_descriptor_color(descriptor, "hglC", value["highlight_color"])
    if "shadow_color" in value:
        set_descriptor_color(descriptor, "sdwC", value["shadow_color"])
    if "highlight_opacity" in value:
        set_descriptor_unit_value(descriptor, "hglO", value["highlight_opacity"], "#Prc")
    if "shadow_opacity" in value:
        set_descriptor_unit_value(descriptor, "sdwO", value["shadow_opacity"], "#Prc")
    if "soften" in value:
        set_descriptor_unit_value(descriptor, "Sftn", value["soften"], "#Pxl")
    if "depth" in value:
        set_descriptor_unit_value(descriptor, "srgR", value["depth"], "#Prc")

    return effect_info


def create_effect_layer_info(operation):
    effect = operation.get("effect", operation.get("type"))
    if not effect:
        raise ValueError("create_effect_layer requires an effect, for example 'drop_shadow' or 'stroke'.")

    name = str(operation.get("name") or f"PSDC {str(effect).replace('_', ' ').title()}")
    class_id = effect_class_id(effect)
    bbox = operation.get("bbox") if isinstance(operation.get("bbox"), dict) else {}
    left = clamp_int(bbox.get("left"), 0, -MAX_RESOLUTION, MAX_RESOLUTION)
    top = clamp_int(bbox.get("top"), 0, -MAX_RESOLUTION, MAX_RESOLUTION)
    right = clamp_int(bbox.get("right"), left + 1, -MAX_RESOLUTION, MAX_RESOLUTION)
    bottom = clamp_int(bbox.get("bottom"), top + 1, -MAX_RESOLUTION, MAX_RESOLUTION)
    if right <= left:
        right = left + 1
    if bottom <= top:
        bottom = top + 1
    return {
        "id": None,
        "name": name,
        "kind": "pixel",
        "class": "PixelLayer",
        "visible": bool_from_json(operation.get("visible"), True),
        "opacity": opacity_to_native_value(operation.get("opacity", 255), operation.get("opacity_unit")),
        "fill_opacity": opacity_to_native_value(operation.get("fill_opacity", 255), operation.get("fill_opacity_unit")),
        "blend_mode": operation.get("blend_mode", "norm"),
        "clipping": bool_from_json(operation.get("clipping"), False),
        "bbox": {"left": left, "top": top, "right": right, "bottom": bottom},
        "has_mask": False,
        "has_vector_mask": False,
        "has_effects": True,
        "adjustments": {},
        "effects": [{"_type": "Descriptor", "_classID": class_id}],
        "effect_descriptors": {},
        "descriptors": {},
        "smart_object": None,
        "children": [],
    }


def create_text_layer_info(operation):
    contents = str(operation.get("value", operation.get("text", "Text")))
    name = str(operation.get("name") or (contents.strip().splitlines()[0] if contents.strip() else "PSDC Text"))
    bbox = operation.get("bbox") if isinstance(operation.get("bbox"), dict) else {}
    left = clamp_int(bbox.get("left"), 0, -MAX_RESOLUTION, MAX_RESOLUTION)
    top = clamp_int(bbox.get("top"), 0, -MAX_RESOLUTION, MAX_RESOLUTION)
    right = clamp_int(bbox.get("right"), left + 512, -MAX_RESOLUTION, MAX_RESOLUTION)
    bottom = clamp_int(bbox.get("bottom"), top + 128, -MAX_RESOLUTION, MAX_RESOLUTION)
    if right <= left:
        right = left + 512
    if bottom <= top:
        bottom = top + 128

    return {
        "id": None,
        "name": name,
        "kind": "type",
        "class": "TypeLayer",
        "visible": bool_from_json(operation.get("visible"), True),
        "opacity": opacity_to_native_value(operation.get("opacity", 255), operation.get("opacity_unit")),
        "fill_opacity": opacity_to_native_value(operation.get("fill_opacity", 255), operation.get("fill_opacity_unit")),
        "blend_mode": operation.get("blend_mode", "norm"),
        "clipping": bool_from_json(operation.get("clipping"), False),
        "bbox": {"left": left, "top": top, "right": right, "bottom": bottom},
        "has_mask": False,
        "has_vector_mask": False,
        "has_effects": False,
        "adjustments": {},
        "effects": [],
        "effect_descriptors": {},
        "descriptors": {},
        "smart_object": None,
        "editable": {"text": {"contents": contents}},
        "children": [],
    }


def resolve_create_parent(psd, operation):
    parent_target = operation.get("parent")
    if not isinstance(parent_target, dict) or not parent_target:
        return psd

    parent_layer, error = resolve_native_layer_target(psd, parent_target)
    if parent_layer is None:
        raise ValueError(error or "Could not resolve parent.")
    if not parent_layer.is_group():
        raise ValueError("Parent must be a group layer or omitted for document root.")
    return parent_layer


def insert_created_layer(parent, layer, operation):
    position = operation.get("position") if isinstance(operation.get("position"), dict) else {}
    if bool_from_json(position.get("bottom"), False):
        parent.insert(0, layer)
    else:
        parent.append(layer)


def create_adjustment_operation(psd, operation):
    parent = resolve_create_parent(psd, operation)
    layer_info = create_adjustment_layer_info(operation)
    layer = clone_native_prototype_layer(layer_info, load_native_prototype_lookup(), allocate_native_layer_id(psd))
    if layer is None:
        raise ValueError(f"No native prototype found for adjustment type {operation.get('type', operation.get('adjustment'))!r}.")
    insert_created_layer(parent, layer, operation)
    psd._mark_updated()
    return f"Created native adjustment layer {layer_info['name']!r}."


def create_effect_layer_operation(psd, operation):
    parent = resolve_create_parent(psd, operation)
    layer_info = create_effect_layer_info(operation)
    layer = clone_native_prototype_layer(layer_info, load_native_prototype_lookup(), allocate_native_layer_id(psd))
    if layer is None:
        raise ValueError(f"No native prototype found for effect {operation.get('effect', operation.get('type'))!r}.")

    value = operation.get("value", {})
    if isinstance(value, dict) and value:
        current_descriptors = serialize_layer_tags(layer).get("effect_descriptors", {})
        patched_descriptors = apply_semantic_effect_json(current_descriptors, operation.get("effect", operation.get("type")), value)
        patch_native_layer_tags(layer, {"effect_descriptors": patched_descriptors})

    insert_created_layer(parent, layer, operation)
    psd._mark_updated()
    return f"Created native effect layer {layer_info['name']!r}."


def create_text_operation(psd, operation):
    parent = resolve_create_parent(psd, operation)
    layer_info = create_text_layer_info(operation)
    layer = clone_native_prototype_layer(layer_info, load_native_prototype_lookup(), allocate_native_layer_id(psd))
    if layer is None:
        raise ValueError("No native text prototype is available.")
    style_warnings = apply_text_style_operation(layer, operation)
    insert_created_layer(parent, layer, operation)
    psd._mark_updated()
    suffix = f" Warnings: {'; '.join(style_warnings)}" if style_warnings else ""
    return f"Created native text layer {layer_info['name']!r}.{suffix}"


def create_group_operation(psd, operation):
    parent_target = operation.get("parent")
    parent = psd
    if isinstance(parent_target, dict) and parent_target:
        parent_layer, error = resolve_native_layer_target(psd, parent_target)
        if parent_layer is None:
            raise ValueError(error or "Could not resolve group parent.")
        if not parent_layer.is_group():
            raise ValueError("create_group parent must be a group layer or omitted for document root.")
        parent = parent_layer

    name = str(operation.get("name") or "Group")
    group = psd_layers.Group.new(parent=parent, name=name, open_folder=True)
    assign_native_layer_id(group, allocate_native_layer_id(psd))
    psd._mark_updated()
    return f"Created group {name!r}."


def iter_native_layer_subtree(layer):
    yield layer
    if hasattr(layer, "is_group") and layer.is_group():
        for child in layer:
            yield from iter_native_layer_subtree(child)


def assign_new_layer_ids_recursive(layer, psd):
    for current in iter_native_layer_subtree(layer):
        assign_native_layer_id(current, allocate_native_layer_id(psd))


def layer_parent(layer):
    parent = getattr(layer, "parent", None)
    if parent is None:
        raise ValueError("Target layer has no parent.")
    return parent


def resolve_parent_for_operation(psd, operation, default_parent):
    parent_target = operation.get("parent", operation.get("group"))
    if not isinstance(parent_target, dict) or not parent_target:
        return default_parent
    parent_layer, error = resolve_native_layer_target(psd, parent_target)
    if parent_layer is None:
        raise ValueError(error or "Could not resolve parent group.")
    if not parent_layer.is_group():
        raise ValueError("Parent target must resolve to a group layer.")
    return parent_layer


def clamp_insert_index(parent, index, append_default=True):
    if index is None:
        return len(parent) if append_default else 0
    return max(0, min(int(index), len(parent)))


def operation_index(operation):
    for key in ("index", "position_index", "absolute_index"):
        if operation.get(key) is not None:
            return int(operation.get(key))
    position = operation.get("position")
    if isinstance(position, dict) and position.get("index") is not None:
        return int(position.get("index"))
    return None


def translate_native_layer(layer, dx=0, dy=0, absolute_x=None, absolute_y=None):
    if layer.is_group():
        for child in layer:
            translate_native_layer(child, dx=dx, dy=dy, absolute_x=None, absolute_y=None)
        return

    left = int(getattr(layer, "left", 0))
    top = int(getattr(layer, "top", 0))
    if absolute_x is not None:
        left = int(absolute_x)
    else:
        left += int(dx)
    if absolute_y is not None:
        top = int(absolute_y)
    else:
        top += int(dy)
    layer.left = left
    layer.top = top


def duplicate_layer_operation(psd, operation):
    layer, error = resolve_native_layer_target(psd, operation.get("target", {}))
    if layer is None:
        raise ValueError(error or "Could not resolve target layer.")

    parent = resolve_parent_for_operation(psd, operation, layer_parent(layer))
    duplicate = copy.deepcopy(layer)
    duplicate.name = str(operation.get("name") or f"{getattr(layer, 'name', 'Layer')} copy")
    assign_new_layer_ids_recursive(duplicate, psd)

    dx = clamp_int(operation.get("dx"), 0, -MAX_RESOLUTION, MAX_RESOLUTION)
    dy = clamp_int(operation.get("dy"), 0, -MAX_RESOLUTION, MAX_RESOLUTION)
    if dx or dy or operation.get("x") is not None or operation.get("y") is not None:
        translate_native_layer(
            duplicate,
            dx=dx,
            dy=dy,
            absolute_x=operation.get("x"),
            absolute_y=operation.get("y"),
        )

    index = operation_index(operation)
    if index is None and parent is layer_parent(layer):
        index = parent.index(layer) + 1
    parent.insert(clamp_insert_index(parent, index), duplicate)
    psd._mark_updated()
    return f"Duplicated layer {getattr(layer, 'name', '')!r} as {duplicate.name!r}."


def move_layer_operation(psd, operation):
    layer, error = resolve_native_layer_target(psd, operation.get("target", {}))
    if layer is None:
        raise ValueError(error or "Could not resolve target layer.")

    current_parent = layer_parent(layer)
    parent = resolve_parent_for_operation(psd, operation, current_parent)
    direction = normalize_prototype_key(operation.get("direction", ""))
    offset = clamp_int(operation.get("offset"), 1, -MAX_RESOLUTION, MAX_RESOLUTION)
    index = operation_index(operation)

    if direction in ("up", "above"):
        index = current_parent.index(layer) + abs(offset)
    elif direction in ("down", "below"):
        index = current_parent.index(layer) - abs(offset)
    elif direction == "top":
        index = len(parent)
    elif direction == "bottom":
        index = 0

    if index is None and parent is not current_parent:
        index = len(parent)
    elif index is None:
        raise ValueError("move_layer requires direction, index, or parent/group.")

    if parent is current_parent:
        current_parent.remove(layer)
    else:
        current_parent.remove(layer)

    parent.insert(clamp_insert_index(parent, index), layer)
    psd._mark_updated()
    return f"Moved layer {getattr(layer, 'name', '')!r} to index {parent.index(layer)}."


def translate_layer_operation(psd, operation):
    layer, error = resolve_native_layer_target(psd, operation.get("target", {}))
    if layer is None:
        raise ValueError(error or "Could not resolve target layer.")
    dx = clamp_int(operation.get("dx"), 0, -MAX_RESOLUTION, MAX_RESOLUTION)
    dy = clamp_int(operation.get("dy"), 0, -MAX_RESOLUTION, MAX_RESOLUTION)
    translate_native_layer(layer, dx=dx, dy=dy, absolute_x=operation.get("x"), absolute_y=operation.get("y"))
    psd._mark_updated()
    return f"Translated layer {getattr(layer, 'name', '')!r} to bbox {getattr(layer, 'bbox', None)}."


def transform_layer_operation(psd, operation):
    unsupported = []
    for key in ("scale", "scale_x", "scale_y", "rotate", "rotation", "angle", "crop", "warp", "perspective"):
        value = operation.get(key)
        if value not in (None, 0, 0.0, 1, 1.0, False, {}, []):
            unsupported.append(key)
    if unsupported:
        raise ValueError(
            "Native scale/rotate/crop/warp transforms are not implemented because psd-tools does not expose a safe Photoshop transform descriptor writer for arbitrary layers. "
            f"Unsupported fields: {', '.join(unsupported)}. The source PSD was preserved."
        )
    return translate_layer_operation(psd, operation)


def apply_native_patch_operation(psd, operation):
    op_name = operation.get("op")
    if op_name not in SUPPORTED_NATIVE_PATCH_OPERATIONS:
        raise ValueError(f"Unsupported native patch operation: {op_name}")

    if op_name == "create_group":
        return create_group_operation(psd, operation)
    if op_name == "create_adjustment":
        return create_adjustment_operation(psd, operation)
    if op_name in ("create_effect_layer", "create_effect"):
        return create_effect_layer_operation(psd, operation)
    if op_name == "create_text":
        return create_text_operation(psd, operation)
    if op_name == "duplicate_layer":
        return duplicate_layer_operation(psd, operation)
    if op_name in ("move_layer", "reorder_layer"):
        return move_layer_operation(psd, operation)
    if op_name == "translate_layer":
        return translate_layer_operation(psd, operation)
    if op_name == "transform_layer":
        return transform_layer_operation(psd, operation)
    if op_name in ("crop_layer", "warp_layer"):
        raise ValueError(
            f"{op_name} is recognized but not implemented as a native edit. PSDC preserves the source layer instead of rasterizing or corrupting Photoshop-only transform data."
        )
    if op_name == "replace_text":
        return replace_text_operation(psd, operation)

    target = operation.get("target", {})
    layer, error = resolve_native_layer_target(psd, target)
    if layer is None:
        raise ValueError(error or "Could not resolve target layer.")

    if op_name == "rename_layer":
        value = str(operation.get("value", "Layer"))
        old = getattr(layer, "name", "")
        layer.name = value
        psd._mark_updated()
        return f"Renamed layer from {old!r} to {value!r}."
    if op_name == "set_visibility":
        layer.visible = bool_from_json(operation.get("value"), getattr(layer, "visible", True))
        psd._mark_updated()
        return f"Set visibility to {layer.visible}."
    if op_name == "set_opacity":
        layer.opacity = opacity_to_native_value(operation.get("value"), operation.get("unit"))
        psd._mark_updated()
        return f"Set opacity to {layer.opacity}."
    if op_name == "set_fill_opacity":
        if not hasattr(layer, "fill_opacity"):
            raise ValueError("Target layer does not support fill_opacity.")
        layer.fill_opacity = opacity_to_native_value(operation.get("value"), operation.get("unit"))
        psd._mark_updated()
        return f"Set fill opacity to {layer.fill_opacity}."
    if op_name == "set_blend_mode":
        layer.blend_mode = parse_blend_mode(operation.get("value"))
        psd._mark_updated()
        return f"Set blend mode to {operation.get('value')}."
    if op_name == "set_clipping":
        if not hasattr(layer, "clipping"):
            raise ValueError("Target layer does not support clipping.")
        layer.clipping = bool_from_json(operation.get("value"), getattr(layer, "clipping", False))
        psd._mark_updated()
        return f"Set clipping to {layer.clipping}."
    if op_name == "set_adjustment":
        message = set_adjustment_operation(layer, operation)
        psd._mark_updated()
        return message
    if op_name == "set_effect":
        message = set_effect_operation(layer, operation)
        psd._mark_updated()
        return message

    raise ValueError(f"Unsupported native patch operation: {op_name}")


def validate_native_patch(source_path, patch_json, snapshot_json=None):
    patch = parse_native_patch_json(patch_json)
    psd = PSDImage.open(source_path)
    report = {
        "schema": NATIVE_PATCH_REPORT_SCHEMA,
        "valid": True,
        "applied": [],
        "skipped": [],
        "failed": [],
        "warnings": [],
    }

    if snapshot_json:
        try:
            snapshot = json.loads(snapshot_json)
            if isinstance(snapshot, dict) and snapshot.get("schema") not in ("psdc.native_snapshot.v1", "psdc.psd_structure.v1"):
                report["warnings"].append(f"Snapshot schema is {snapshot.get('schema')!r}; expected psdc.native_snapshot.v1.")
        except Exception as error:
            report["warnings"].append(f"Could not parse snapshot_json for validation context: {error}")

    for index, operation in enumerate(patch["operations"]):
        op_name = operation.get("op")
        if operation.get("_error"):
            report["failed"].append({"index": index, "op": op_name, "target": operation.get("target"), "message": operation["_error"]})
            continue
        if op_name not in SUPPORTED_NATIVE_PATCH_OPERATIONS:
            report["failed"].append({"index": index, "op": op_name, "target": operation.get("target"), "message": f"Unsupported operation: {op_name}"})
            continue
        if op_name in ("create_group", "create_adjustment", "create_effect_layer", "create_effect", "create_text"):
            report["applied"].append({"index": index, "op": op_name, "target": operation.get("parent"), "message": f"Validated {op_name} operation."})
            continue
        try:
            if op_name == "replace_text":
                target = operation.get("target", {})
                chain = target.get("smart_object_chain", []) if isinstance(target, dict) else []
                if chain:
                    layer, error = resolve_native_layer_target(psd, chain[0])
                else:
                    layer, error = resolve_native_layer_target(psd, target)
                if layer is None:
                    raise ValueError(error or "Could not resolve target layer.")
                if str(getattr(layer, "kind", "")).lower() not in ("type", "smartobject"):
                    raise ValueError("replace_text target must resolve to a type layer or embedded PSD/PSB smart object.")
                report["applied"].append({"index": index, "op": op_name, "target": operation.get("target"), "message": "Validated replace_text target."})
                continue

            layer, error = resolve_native_layer_target(psd, operation.get("target", {}))
            if layer is None:
                raise ValueError(error or "Could not resolve target layer.")
            if op_name in ("crop_layer", "warp_layer"):
                raise ValueError(f"{op_name} is recognized but not implemented as a native edit.")
            if op_name == "transform_layer":
                unsupported = []
                for key in ("scale", "scale_x", "scale_y", "rotate", "rotation", "angle", "crop", "warp", "perspective"):
                    value = operation.get(key)
                    if value not in (None, 0, 0.0, 1, 1.0, False, {}, []):
                        unsupported.append(key)
                if unsupported:
                    raise ValueError(f"Native transform validation failed for unsupported fields: {', '.join(unsupported)}.")
            if op_name == "set_adjustment" and adjustment_patch_tag(layer, operation) is None:
                raise ValueError("Target layer does not expose a matching adjustment tag.")
            if op_name == "set_effect" and not serialize_layer_tags(layer).get("effect_descriptors"):
                raise ValueError("Target layer does not expose effect descriptors.")
            report["applied"].append({"index": index, "op": op_name, "target": operation.get("target"), "message": "Validated operation."})
        except Exception as error:
            report["failed"].append({"index": index, "op": op_name, "target": operation.get("target"), "message": str(error)})

    if report["failed"]:
        report["valid"] = False

    return patch, report


def apply_native_patch_json_to_native_psd(source_path, patch_json, output_path):
    patch = parse_native_patch_json(patch_json)
    if source_path:
        psd = PSDImage.open(source_path)
        base_mode = "native_source"
        blank_mode = False
    else:
        width, height = document_size_from_native_patch(patch)
        psd = PSDImage.new("RGB", (int(width), int(height)))
        base_mode = "blank"
        blank_mode = True

    expand_psd_canvas_for_patch(psd, patch)
    report = apply_native_patch_to_psd_object(psd, patch, source_path=source_path, blank_mode=blank_mode, base_mode=base_mode)
    save_psd_photoshop_safe(psd, output_path)
    report["output_path"] = output_path
    report["source_path"] = source_path
    return report


def apply_native_patch_to_psd_object(psd, patch, source_path=None, blank_mode=False, base_mode=None):
    report = {
        "schema": NATIVE_PATCH_REPORT_SCHEMA,
        "applied": [],
        "skipped": [],
        "failed": [],
        "warnings": [],
        "base_mode": base_mode or ("blank" if blank_mode else "psd_object"),
        "source_path": source_path,
    }

    for index, operation in enumerate(patch["operations"]):
        operation = copy.deepcopy(operation)
        if blank_mode:
            operation = blank_native_patch_operation(operation)
        op_name = operation.get("op")
        try:
            message = apply_native_patch_operation(psd, operation)
            report["applied"].append({"index": index, "op": op_name, "target": operation.get("target", operation.get("parent")), "message": message})
        except Exception as error:
            report["failed"].append({"index": index, "op": op_name, "target": operation.get("target", operation.get("parent")), "message": str(error)})

    if report["applied"]:
        report["warnings"].append(
            "Native metadata was patched and PSDC marked the document dirty so psd-tools refreshes merged preview image data during save when possible. Photoshop may still need to refresh cached text, smart object, adjustment, or effect previews on open."
        )

    return report


def native_prototype_adjustment_tags(layer):
    tags = native_tag_lookup(getattr(layer, "tagged_blocks", {}))
    return [tag_name for tag_name in tags if classify_layer_tag(tag_name) == "adjustments"]


def normalize_prototype_key(value):
    text = str(value).strip()
    return text.lower().replace(" ", "_").replace("-", "_")


def effect_aliases_for_key(value):
    if value is None:
        return []

    text = str(value)
    upper = text.upper()
    aliases = {text, normalize_prototype_key(text)}

    if upper in EFFECT_CLASS_ALIASES:
        aliases.update(EFFECT_CLASS_ALIASES[upper])

    for class_id, values in EFFECT_CLASS_ALIASES.items():
        if upper in {str(alias).upper() for alias in values}:
            aliases.add(class_id)
            aliases.update(values)

    return list(aliases)


def native_layer_effect_keys_from_effects(layer):
    keys = []
    try:
        effects = list(layer.effects)
    except Exception:
        return keys

    for effect in effects:
        keys.append(type(effect).__name__)
        effect_json = psd_value_to_json(effect)
        if isinstance(effect_json, dict):
            class_id = effect_json.get("_classID")
            if class_id:
                keys.append(class_id)

    return keys


def load_native_prototype_lookup(library_path=None):
    library_paths = [library_path] if library_path else [NATIVE_PROTOTYPE_LIBRARY, NATIVE_TEXT_PROTOTYPE_LIBRARY]
    lookup = {}

    for current_path in library_paths:
        if not current_path:
            continue
        if not os.path.isfile(current_path):
            if current_path == NATIVE_PROTOTYPE_LIBRARY:
                raise ValueError(f"Missing PSDC native prototype library: {current_path}")
            logging.warning("Missing optional PSDC native prototype library: %s", current_path)
            continue

        library_psd = PSDImage.open(current_path)
        for layer in library_psd:
            for tag_name in native_prototype_adjustment_tags(layer):
                lookup.setdefault(tag_name, layer)
                lookup.setdefault(normalize_prototype_key(tag_name), layer)
            for effect_key in native_layer_effect_keys_from_effects(layer):
                for alias in effect_aliases_for_key(effect_key):
                    lookup.setdefault(alias, layer)
                    lookup.setdefault(normalize_prototype_key(alias), layer)
            lookup.setdefault(str(getattr(layer, "kind", "")).lower(), layer)
            lookup.setdefault(normalize_prototype_key(getattr(layer, "kind", "")), layer)
            lookup.setdefault(type(layer).__name__.lower(), layer)
            lookup.setdefault(normalize_prototype_key(type(layer).__name__), layer)

    return lookup


def native_layer_info_tag_names(layer_info):
    adjustments = layer_info.get("adjustments")
    if not isinstance(adjustments, dict):
        return []
    return [str(tag_name) for tag_name in adjustments.keys()]


def native_layer_info_effect_keys(layer_info):
    effects = layer_info.get("effects")
    keys = []
    if isinstance(effects, list):
        for effect in effects:
            if not isinstance(effect, dict):
                continue
            class_id = effect.get("_classID")
            if class_id:
                keys.append(class_id)
            effect_type = effect.get("_type")
            if effect_type and effect_type != "Descriptor":
                keys.append(effect_type)

    keys.extend(native_layer_info_effect_descriptor_keys(layer_info))
    return keys


def native_layer_info_effect_descriptor_keys(layer_info):
    effect_descriptors = layer_info.get("effect_descriptors")
    if not isinstance(effect_descriptors, dict):
        return []

    keys = []

    def add_effect_key(value):
        normalized = normalize_prototype_key(value)
        for class_id, aliases in EFFECT_CLASS_ALIASES.items():
            candidates = {normalize_prototype_key(class_id)}
            candidates.update(normalize_prototype_key(alias) for alias in aliases)
            if normalized in candidates:
                keys.append(class_id)
                keys.extend(str(alias) for alias in aliases)

    def walk(value):
        if isinstance(value, dict):
            for marker in ("_classID", "classID", "_type"):
                marker_value = value.get(marker)
                if marker_value:
                    add_effect_key(marker_value)
            for item_key, item_value in value.items():
                add_effect_key(item_key)
                walk(item_value)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(effect_descriptors)
    return list(dict.fromkeys(keys))


def layer_info_has_native_prototype_payload(layer_info):
    if str(layer_info.get("kind", "")).lower() == "type" or str(layer_info.get("class", "")).lower() == "typelayer":
        return True
    if layer_info_text_contents(layer_info) is not None:
        return True
    if native_layer_info_tag_names(layer_info):
        return True
    if native_layer_info_effect_keys(layer_info):
        return True
    effect_descriptors = layer_info.get("effect_descriptors")
    return isinstance(effect_descriptors, dict) and bool(effect_descriptors)


def prototype_key_for_layer_info(layer_info):
    tag_names = native_layer_info_tag_names(layer_info)
    if tag_names:
        return tag_names[0]

    effect_keys = native_layer_info_effect_keys(layer_info)
    if effect_keys:
        return effect_keys[0]

    kind = str(layer_info.get("kind", "")).lower()
    if kind:
        return kind

    class_name = str(layer_info.get("class", "")).lower()
    if class_name:
        return class_name

    return None


def layer_info_text_contents(layer_info):
    editable = layer_info.get("editable")
    if isinstance(editable, dict):
        text_info = editable.get("text")
        if isinstance(text_info, dict) and text_info.get("contents") is not None:
            return str(text_info.get("contents"))

    type_tool = layer_type_tool_object(layer_info)
    if isinstance(type_tool, dict) and type_tool.get("text") is not None:
        return str(type_tool.get("text"))

    if layer_info.get("text") is not None:
        return str(layer_info.get("text"))

    return None


def position_native_layer_from_info(layer, layer_info):
    bbox = layer_info.get("bbox")
    if not isinstance(bbox, dict):
        return

    try:
        left = int(bbox.get("left", getattr(layer, "left", 0)))
        top = int(bbox.get("top", getattr(layer, "top", 0)))
    except (TypeError, ValueError):
        return

    try:
        layer.left = left
        layer.top = top
    except Exception:
        return


def apply_text_layer_info(layer, layer_info):
    if str(getattr(layer, "kind", "")).lower() != "type":
        return 0

    contents = layer_info_text_contents(layer_info)
    if contents is None:
        return 0

    try:
        if str(layer.text) == contents:
            return 0
        replace_type_layer_text(layer, contents)
        return 1
    except Exception as error:
        logging.warning("PSDC could not apply editable text contents to %s: %s", getattr(layer, "name", "text layer"), error)
        return 0


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


def allocate_native_layer_id(psd):
    next_layer_id = getattr(psd, "_psdc_next_layer_id", None)
    if next_layer_id is None:
        next_layer_id = max_native_layer_id(psd) + 1
    setattr(psd, "_psdc_next_layer_id", int(next_layer_id) + 1)
    return int(next_layer_id)


def json_layer_identity(layer_info):
    layer_id = layer_info.get("id")
    if layer_id is not None:
        return ("id", str(layer_id))

    index_path = layer_info.get("index_path")
    if isinstance(index_path, list):
        try:
            return ("index_path", tuple(int(value) for value in index_path))
        except (TypeError, ValueError):
            pass

    name = layer_info.get("name")
    if name:
        return ("name", str(name))

    return ("object", id(layer_info))


def clone_native_prototype_layer(layer_info, prototype_lookup, layer_id):
    prototype_key = prototype_key_for_layer_info(layer_info)
    if not prototype_key:
        return None

    prototype = prototype_lookup.get(prototype_key)
    if prototype is None:
        prototype = prototype_lookup.get(normalize_prototype_key(prototype_key))
    if prototype is None:
        return None

    layer = copy.deepcopy(prototype)
    assign_native_layer_id(layer, layer_id)
    patch_native_layer_metadata(layer, layer_info)
    position_native_layer_from_info(layer, layer_info)
    patch_native_layer_tags(layer, layer_info)
    apply_text_layer_info(layer, layer_info)
    return layer


def is_group_layer_info(layer_info):
    if not isinstance(layer_info, dict):
        return False
    kind = str(layer_info.get("kind", "")).lower()
    class_name = str(layer_info.get("class", "")).lower()
    return kind in ("group", "artboard") or "group" in class_name or "artboard" in class_name


def layer_info_has_existing_identity(layer_info, existing_identities):
    return json_layer_identity(layer_info) in existing_identities


def create_group_from_layer_info(parent, layer_info):
    group = psd_layers.Group.new(parent=parent, name=structure_layer_name(layer_info, "Group"), open_folder=True)
    patch_native_layer_metadata(group, layer_info)
    return group


def create_native_layers_from_structure_items(
    parent,
    layers,
    prototype_lookup,
    next_layer_id,
    existing_identities,
    layer_mode,
    native_lookup,
):
    created_layers = 0
    created_groups = 0
    skipped_layers = 0

    if not isinstance(layers, list):
        return created_layers, created_groups, skipped_layers, next_layer_id

    for layer_info in layers:
        if not isinstance(layer_info, dict):
            continue

        exists = layer_info_has_existing_identity(layer_info, existing_identities)
        children = layer_info.get("children")
        if exists:
            if is_group_layer_info(layer_info) and isinstance(children, list):
                existing_group = find_native_layer_for_json(layer_info, *native_lookup)
                if existing_group is not None and existing_group.is_group():
                    child_created, child_groups, child_skipped, next_layer_id = create_native_layers_from_structure_items(
                        existing_group,
                        children,
                        prototype_lookup,
                        next_layer_id,
                        existing_identities,
                        "all_layers",
                        native_lookup,
                    )
                    created_layers += child_created
                    created_groups += child_groups
                    skipped_layers += child_skipped
            continue

        if is_group_layer_info(layer_info):
            if layer_mode == "top_level" and parent is not parent._psd:
                continue
            group = create_group_from_layer_info(parent, layer_info)
            assign_native_layer_id(group, next_layer_id)
            next_layer_id += 1
            created_groups += 1

            child_created, child_groups, child_skipped, next_layer_id = create_native_layers_from_structure_items(
                group,
                children,
                prototype_lookup,
                next_layer_id,
                existing_identities,
                "all_layers",
                native_lookup,
            )
            created_layers += child_created
            created_groups += child_groups
            skipped_layers += child_skipped
            continue

        if not layer_info_has_native_prototype_payload(layer_info):
            continue

        layer = clone_native_prototype_layer(layer_info, prototype_lookup, next_layer_id)
        if layer is None:
            skipped_layers += 1
            continue

        next_layer_id += 1
        parent.append(layer)
        created_layers += 1

    return created_layers, created_groups, skipped_layers, next_layer_id


def document_size_from_json_structure(structure):
    selected = collect_structure_layers(structure.get("layers"), "all_layers")
    return document_size_from_structure(structure, selected)


def create_native_psd_from_structure_json(json_text, output_path, source_psd=None, layer_mode="all_layers"):
    structure = parse_psd_structure_json(json_text)
    source_path = psd_stack_source_path(source_psd) if is_psd_stack(source_psd) else None
    if source_path:
        psd = PSDImage.open(source_path)
        patch_result = apply_structure_to_native_psd_object(psd, structure)
    else:
        width, height = document_size_from_json_structure(structure)
        psd = PSDImage.new("RGB", (int(width), int(height)))
        patch_result = {
            "matched_layers": 0,
            "metadata_updates": 0,
            "native_tag_updates": 0,
            "unmatched_layers": list(iter_json_structure_layers(structure.get("layers"))),
        }

    prototype_lookup = load_native_prototype_lookup()
    next_layer_id = max_native_layer_id(psd) + 1
    existing_identities = {json_layer_identity(layer_info) for layer_info in iter_json_structure_layers(structure.get("layers"))}
    unmatched_identities = {json_layer_identity(layer_info) for layer_info in patch_result["unmatched_layers"]}
    existing_identities -= unmatched_identities
    native_lookup = native_psd_layer_lookup(psd)

    created_layers, created_groups, skipped_layers, _next_layer_id = create_native_layers_from_structure_items(
        psd,
        structure.get("layers"),
        prototype_lookup,
        next_layer_id,
        existing_identities,
        layer_mode,
        native_lookup,
    )

    if created_layers or created_groups:
        psd._mark_updated()

    save_psd_photoshop_safe(psd, output_path)
    return {
        "matched_layers": patch_result["matched_layers"],
        "metadata_updates": patch_result["metadata_updates"],
        "native_tag_updates": patch_result["native_tag_updates"],
        "created_layers": created_layers,
        "created_groups": created_groups,
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


def psdc_output_psd_path(output_dir, filename_prefix, width, height):
    full_output_folder, filename, counter, _subfolder, _filename_prefix = folder_paths.get_save_image_path(
        filename_prefix,
        output_dir,
        int(width),
        int(height),
    )
    file = f"{filename.replace('%batch_num%', '0')}_{counter:05}_.psd"
    return os.path.join(full_output_folder, file)


def native_psd_file_to_output_stack(path):
    stack = load_psd_file_to_stack(path)
    for layer in stack.get("layers", []):
        layer["native_source_layer"] = True
    return stack


def explicit_patch_canvas_size(patch, current_width, current_height):
    width = int(current_width)
    height = int(current_height)

    document = patch.get("document") if isinstance(patch, dict) else None
    if not isinstance(document, dict):
        source = patch.get("source") if isinstance(patch, dict) else None
        document = source.get("document") if isinstance(source, dict) else None
    if isinstance(document, dict):
        if document.get("width") is not None:
            width = max(width, clamp_int(document.get("width"), width, 1, MAX_RESOLUTION))
        if document.get("height") is not None:
            height = max(height, clamp_int(document.get("height"), height, 1, MAX_RESOLUTION))

    operations = patch.get("operations") if isinstance(patch, dict) else None
    if isinstance(operations, list):
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            bbox = operation.get("bbox")
            if not isinstance(bbox, dict):
                continue
            width = max(width, clamp_int(bbox.get("right"), width, 1, MAX_RESOLUTION))
            height = max(height, clamp_int(bbox.get("bottom"), height, 1, MAX_RESOLUTION))

    return int(width), int(height)


def expand_psd_canvas_for_patch(psd, patch):
    target_width, target_height = explicit_patch_canvas_size(patch, int(psd.width), int(psd.height))
    if target_width == int(psd.width) and target_height == int(psd.height):
        return False
    psd._record.header.width = target_width
    psd._record.header.height = target_height
    psd._mark_updated()
    return True


def create_native_psd_from_source_stack(psd_stack, source_path, batch_index):
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


def create_native_stack_from_structure_json(json_text, output_path, source_psd=None, layer_mode="all_layers"):
    result = create_native_psd_from_structure_json(
        json_text,
        output_path,
        source_psd=source_psd,
        layer_mode=layer_mode,
    )
    stack = native_psd_file_to_output_stack(output_path)
    stack["native_decode_report"] = result
    return stack, result


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
    native_base_index = next((index for index, stack in enumerate(stacks) if has_native_passthrough(stack)), None)

    if native_base_index is None:
        combined = create_empty_psd_stack(width, height, batch_size)
        for stack in stacks:
            resized_stack = resize_psd_stack_to_canvas(stack, width, height, batch_size)
            combined["layers"].extend([dict(layer) for layer in resized_stack.get("layers", [])])
    else:
        combined = copy_psd_stack(
            resize_psd_stack_to_canvas(stacks[native_base_index], width, height, batch_size, preserve_native=True)
        )
        if native_base_index != 0:
            logging.warning(
                "PSDC PSD Layer Combine preserved native layer context from input %s. "
                "Earlier inputs will be saved as raster overlays above that native PSD source.",
                native_base_index + 1,
            )

        overlay_layers = []
        for index, stack in enumerate(stacks):
            if index == native_base_index:
                continue
            resized_stack = resize_psd_stack_to_canvas(stack, width, height, batch_size, preserve_native=False)
            overlay_layers.extend(layer_as_raster_overlay(layer) for layer in resized_stack.get("layers", []))

        combined["layers"].extend(overlay_layers)

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


class PSDC_JSONEncoder:
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
        if is_psd_stack(psd) and isinstance(psd.get("native_passthrough"), dict) and isinstance(psd.get("structure"), dict):
            structure = psd["structure"]
        elif is_psd_stack(psd) and psd_stack_structure_matches_current_layers(psd):
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


class PSDC_JSONDecoder:
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


class PSDC_PSDEncoder(PSDC_JSONEncoder):
    CATEGORY = "PSDC/Text"


class PSDC_PSDDecoder:
    def __init__(self):
        self.output_dir = folder_paths.get_temp_directory()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "json_text": ("STRING", {"multiline": True, "dynamicPrompts": False}),
                "batch_size": ("INT", {"default": 1, "min": 1, "max": 4096}),
            },
            "optional": {
                "psd": ("PSD",),
            },
        }

    RETURN_TYPES = ("IMAGE", "PSD")
    RETURN_NAMES = ("image", "psd")
    FUNCTION = "execute"
    CATEGORY = "PSDC/Text"

    def execute(self, json_text, batch_size=1, psd=None):
        try:
            structure = parse_psd_structure_json(json_text)
            width, height = document_size_from_json_structure(structure)
            output_path = psdc_output_psd_path(self.output_dir, "PSDC_PSD_Decoder", width, height)
            decoded_psd, _result = create_native_stack_from_structure_json(
                json_text,
                output_path,
                source_psd=psd,
                layer_mode="all_layers",
            )
        except Exception as error:
            logging.warning("PSDC PSD Decoder fell back to raster stack decode after native decode failed: %s", error)
            decoded_psd = decode_psd_structure_json(
                json_text,
                source_psd=psd,
                layer_mode="top_level",
                batch_size=batch_size,
            )
        return (flatten_psd_stack(decoded_psd), decoded_psd)


class PSDC_PSDEffector:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "edit_json": ("STRING", {"multiline": True, "dynamicPrompts": False}),
                "filename_prefix": ("STRING", {"default": "PSDC_PSD_Effector"}),
            },
            "optional": {
                "psd": ("PSD",),
            },
        }

    RETURN_TYPES = ("IMAGE", "PSD")
    RETURN_NAMES = ("image", "psd")
    FUNCTION = "execute"
    OUTPUT_NODE = True
    CATEGORY = "PSDC/Text"

    def execute(self, edit_json, filename_prefix="PSDC_PSD_Effector", psd=None):
        try:
            edit = json.loads(edit_json)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid PSDC edit JSON: {error}") from error

        source_path = None
        if is_psd_stack(psd):
            try:
                source_path = psd_stack_source_path(psd)
            except Exception:
                source_path = native_passthrough_source_path(psd)

        if is_psd_stack(psd):
            width = int(psd.get("width", 1))
            height = int(psd.get("height", 1))
        else:
            width, height = document_size_from_json_or_patch(edit)

        output_path = psdc_output_psd_path(self.output_dir, filename_prefix, width, height)

        if isinstance(edit, dict) and (edit.get("schema") == NATIVE_PATCH_SCHEMA or isinstance(edit.get("operations"), list)):
            patch = parse_native_patch_json(edit_json)
            base_overlay_layers = 0
            base_stack_layers = 0

            if source_path:
                base_mode = "native_source"
                if is_psd_stack(psd) and psd_overlay_layers(psd):
                    base_overlay_layers = len(psd_overlay_layers(psd))
                    base_stack_layers = len(psd.get("layers", []))
                    base_psd = create_native_psd_from_source_stack(psd, source_path, 0)
                else:
                    base_psd = PSDImage.open(source_path)
                blank_mode = False
            elif is_psd_stack(psd):
                base_mode = "synthetic_stack"
                base_stack_layers = len(psd.get("layers", []))
                base_overlay_layers = base_stack_layers
                base_psd = create_psd_image_from_stack(psd, 0)
                blank_mode = False
            else:
                base_mode = "blank"
                blank_width, blank_height = document_size_from_native_patch(patch)
                base_psd = PSDImage.new("RGB", (int(blank_width), int(blank_height)))
                blank_mode = True

            expanded_canvas = expand_psd_canvas_for_patch(base_psd, patch)
            report = apply_native_patch_to_psd_object(
                base_psd,
                patch,
                source_path=source_path,
                blank_mode=blank_mode,
                base_mode=base_mode,
            )
            report["base_stack_layers"] = base_stack_layers
            report["base_overlay_layers"] = base_overlay_layers
            report["expanded_canvas_for_patch"] = expanded_canvas
            if is_psd_stack(psd) and int(psd.get("batch_size", 1)) > 1:
                report["warnings"].append(
                    "PSDC PSD Effector used batch 0 from the connected PSD stack as the native patch base. Use separate Effector runs for additional batch entries."
                )
            save_psd_photoshop_safe(base_psd, output_path)
            report["output_path"] = output_path
            report["source_path"] = source_path
            mode = "native_patch"
            applied = len(report.get("applied", []))
            failed = len(report.get("failed", []))
        else:
            native_source = psd if is_psd_stack(psd) else None
            result = create_native_psd_from_structure_json(
                edit_json,
                output_path,
                source_psd=native_source,
                layer_mode="all_layers",
            )
            mode = "native_snapshot"
            applied = int(result.get("metadata_updates", 0)) + int(result.get("native_tag_updates", 0))
            failed = 0
            report = {
                "schema": "psdc.native_snapshot_apply_report.v1",
                "mode": mode,
                **result,
            }

        report["mode"] = mode
        report_text = json.dumps(report, indent=2, ensure_ascii=False)
        message = f"Saved effected PSD: {output_path} (mode={mode}, applied={applied}, failed={failed})"
        logging.info("PSDC PSD Effector %s", message)
        output_psd = native_psd_file_to_output_stack(output_path)
        return {"ui": {"text": [message, report_text]}, "result": (flatten_psd_stack(output_psd), output_psd)}


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
                logging.warning("Falling back to direct image PSD save after PSD stack error: %s", str(error))

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
                save_psd_photoshop_safe(psd, os.path.join(full_output_folder, file))
                logging.info("PSD file was successfully saved: %s", file)

            else:
                for batch_number, img_tensor in enumerate(images):
                    psd = PSDImage.new("RGB", (width, height))
                    alpha_mask, rgb_image = extract_alpha_mask(img_tensor)
                    append_pixel_layer_with_mask(psd, rgb_image, alpha_mask, "Layer 1", channels == 4)

                    filename_with_batch_num = filename.replace("%batch_num%", str(batch_number))
                    file = f"{filename_with_batch_num}_{counter:05}_{batch_number}.psd"
                    save_psd_photoshop_safe(psd, os.path.join(full_output_folder, file))
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
                save_psd_photoshop_safe(psd_image, os.path.join(full_output_folder, file))
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
                    save_psd_photoshop_safe(psd_image, os.path.join(full_output_folder, file))
                    logging.info("PSD stack file %s/%s was successfully saved: %s", batch_number + 1, batch_size, file)

        except Exception as error:
            logging.warning("Error occurred while saving PSD stack: %s", str(error))
            raise

        return {}

    def create_native_psd_from_source_stack(self, psd_stack, source_path, batch_index):
        return create_native_psd_from_source_stack(psd_stack, source_path, batch_index)

    def save_native_source_psd(self, psd_stack, source_path, filename, counter, full_output_folder, file_mode, batch_size):
        overlay_layers = psd_overlay_layers(psd_stack)
        if not overlay_layers:
            return self.save_native_passthrough_psd(source_path, filename, counter, full_output_folder, file_mode, batch_size)

        try:
            if file_mode == "single_file":
                file = f"{filename.replace('%batch_num%', '0')}_{counter:05}_.psd"
                psd_image = self.create_native_psd_from_source_stack(psd_stack, source_path, 0)
                save_psd_photoshop_safe(psd_image, os.path.join(full_output_folder, file))
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
                    save_psd_photoshop_safe(psd_image, os.path.join(full_output_folder, file))
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
    "PSDC Image To PSD": PSDC_ImageToPSD,
    "PSDC PSD Layer Combine": PSDC_PSDLayerCombine,
    "PSDC PSD Encoder": PSDC_PSDEncoder,
    "PSDC PSD Effector": PSDC_PSDEffector,
    "PSDC PSD Decoder": PSDC_PSDDecoder,
    "PSDC Preview PSD": PSDC_PreviewPSD,
    "PSDC Save PSD": PSDC_SavePSD,
    "PSDC Extract Alpha": PSDC_ExtractAlpha,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PSDC Apply Alpha Channel": "PSDC Apply Alpha Channel",
    "PSDC Image Composite PSD": "PSDC Image Composite PSD",
    "PSDC Load PSD": "PSDC Load PSD",
    "PSDC Image To PSD": "PSDC Image To PSD",
    "PSDC PSD Layer Combine": "PSDC PSD Layer Combine",
    "PSDC PSD Encoder": "PSDC PSD Encoder",
    "PSDC PSD Effector": "PSDC PSD Effector",
    "PSDC PSD Decoder": "PSDC PSD Decoder",
    "PSDC Preview PSD": "PSDC Preview PSD",
    "PSDC Save PSD": "PSDC Save PSD",
    "PSDC Extract Alpha": "PSDC Extract Alpha",
}
