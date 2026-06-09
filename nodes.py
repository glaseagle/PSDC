import hashlib
import logging
import os

import folder_paths
import numpy as np
import torch
from PIL import Image
from psd_tools import PSDImage


MAX_RESOLUTION = 16384
PSD_STACK_TYPE = "PSDC_PSD_STACK"


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
        rgb_canvas = image_array
        if rgb_canvas.shape[0] != height or rgb_canvas.shape[1] != width:
            rgb_image = Image.fromarray(rgb_canvas, "RGB").resize((width, height), Image.Resampling.BICUBIC)
            return rgb_image, None, False, int(layer.get("opacity", 255))
        return Image.fromarray(rgb_canvas, "RGB"), None, False, int(layer.get("opacity", 255))

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

    # Stack convention (matches flatten_psd_stack and PSD Load): layers[0] is the
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
        canvas_height = destination.shape[1]
        canvas_width = destination.shape[2]
        x_positions = normalize_positions(x, batch_size, offset_x)
        y_positions = normalize_positions(y, batch_size, offset_y)

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
            width = int(psd["width"])
            height = int(psd["height"])
            batch_size = 1 if batch_to_layers else max(
                int(psd.get("batch_size", 1)),
                destination_count,
                source_count,
                mask_count,
                1,
            )
            psd_stack = match_psd_stack_batch_size(psd, batch_size)
            image = flatten_psd_stack(psd_stack, dtype=dtype, device=device)
        else:
            batch_size = 1 if batch_to_layers else max(destination_count, source_count, mask_count, 1)
            if destination is not None:
                width = int(destination.shape[2])
                height = int(destination.shape[1])
                base_image = match_batch_size(destination[..., :3], batch_size).to(dtype=dtype, device=device)
                psd_stack = create_psd_stack_from_destination(base_image)
                image = base_image.clone()
            elif source is not None:
                width = int(source.shape[2])
                height = int(source.shape[1])
                psd_stack = create_empty_psd_stack(width, height, batch_size)
                image = torch.zeros((batch_size, height, width, 3), dtype=dtype, device=device)
            else:
                height, width = get_mask_size(mask)
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
            }
        )

    if not layers:
        # Flattened PSD with no addressable layers: keep the whole image as one layer.
        rgb, alpha = pil_rgba_to_tensors(psd.composite(viewport=viewport))
        layers.append(
            {"name": "Background", "image": rgb, "mask": alpha, "x": [0], "y": [0], "opacity": 255}
        )

    return {
        "type": PSD_STACK_TYPE,
        "version": 1,
        "width": width,
        "height": height,
        "batch_size": 1,
        "layers": layers,
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
                "psd_file": (sorted(files),),
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
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            digest.update(handle.read())
        return digest.hexdigest()

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
    "PSD Load": PSDC_PSDLoad,
    "PSDC Image To PSD": PSDC_ImageToPSD,
    "PSDC Save PSD": PSDC_SavePSD,
    "PSDC Extract Alpha": PSDC_ExtractAlpha,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PSDC Apply Alpha Channel": "PSDC Apply Alpha Channel",
    "PSDC Image Composite PSD": "PSDC Image Composite PSD",
    "PSD Load": "PSD Load",
    "PSDC Image To PSD": "PSDC Image To PSD",
    "PSDC Save PSD": "PSDC Save PSD",
    "PSDC Extract Alpha": "PSDC Extract Alpha",
}
