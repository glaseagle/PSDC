import logging
import os

import folder_paths
import numpy as np
import torch
from PIL import Image
from psd_tools import PSDImage


MAX_RESOLUTION = 16384


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


def append_pixel_layer_with_mask(psd, rgb_image, alpha_mask, layer_name, has_alpha):
    rgb_layer = psd.create_pixel_layer(rgb_image, name=layer_name)
    if has_alpha:
        rgb_layer.create_mask(alpha_mask)
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


def normalize_positions(value, batch_size, offset):
    if isinstance(value, list):
        positions = value
    else:
        positions = [value]

    if len(positions) < batch_size:
        positions = positions + [positions[-1]] * (batch_size - len(positions))

    return [int(position + offset) for position in positions[:batch_size]]


class D2_ApplyAlphaChannel:
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
    CATEGORY = "D2/Image"

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


class D2_SavePSD:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "filename_prefix": ("STRING", {"default": "ComfyUI"}),
                "file_mode": (["multi_file", "single_file"],),
                "alpha_name": ("STRING", {"default": "_mask_"}),
                "alpha_name_mode": (["simple", "suffix"],),
            }
        }

    RETURN_TYPES = ()
    FUNCTION = "save_rgba_psd"
    OUTPUT_NODE = True
    CATEGORY = "D2/Image"

    def save_rgba_psd(self, images, filename_prefix, file_mode, alpha_name="_mask_", alpha_name_mode="simple"):
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


class D2_ExtractAlpha:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
            }
        }

    RETURN_TYPES = ("MASK", "IMAGE")
    FUNCTION = "extract_alpha"
    CATEGORY = "D2/Image"

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
    "D2 Apply Alpha Channel": D2_ApplyAlphaChannel,
    "D2 Save PSD": D2_SavePSD,
    "D2 Extract Alpha": D2_ExtractAlpha,
}
