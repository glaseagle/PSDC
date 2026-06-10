"""
@author: glaseagle
@title: PSDC
@description: Save PSD files with native Photoshop layer masks.
"""
import os

import folder_paths
from aiohttp import web
from server import PromptServer

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = "./web"

PSDC_MAX_UPLOAD_SIZE = 2 * 1024 * 1024 * 1024


def unique_upload_path(directory, filename, overwrite=False):
    name, extension = os.path.splitext(os.path.basename(filename).replace("\x00", ""))
    if extension.lower() != ".psd" or not name:
        raise ValueError("Only .psd files can be uploaded.")

    candidate = f"{name}{extension}"
    filepath = os.path.abspath(os.path.join(directory, candidate))
    if os.path.commonpath((os.path.abspath(directory), filepath)) != os.path.abspath(directory):
        raise ValueError("Invalid upload path.")

    if overwrite:
        return filepath, candidate

    index = 1
    while os.path.exists(filepath):
        candidate = f"{name} ({index}){extension}"
        filepath = os.path.abspath(os.path.join(directory, candidate))
        index += 1

    return filepath, candidate


@PromptServer.instance.routes.post("/psdc/upload/psd")
async def upload_psd(request):
    # Comfy's default image upload limit can be too small for layered PSDs.
    request._client_max_size = max(getattr(request, "_client_max_size", 0), PSDC_MAX_UPLOAD_SIZE)

    input_dir = folder_paths.get_input_directory()
    os.makedirs(input_dir, exist_ok=True)

    reader = await request.multipart()
    overwrite = False
    saved = None

    async for part in reader:
        if part.filename:
            filepath, filename = unique_upload_path(input_dir, part.filename, overwrite=overwrite)
            with open(filepath, "wb") as handle:
                while True:
                    chunk = await part.read_chunk(size=1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)

            saved = {"name": filename, "subfolder": "", "type": "input"}
        elif part.name == "overwrite":
            value = (await part.text()).strip().lower()
            overwrite = value in ("true", "1", "yes")

    if saved is None:
        return web.json_response({"error": "No PSD file was uploaded."}, status=400)

    return web.json_response(saved)


__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
