import { app } from "../../scripts/app.js";

const PSD_LOAD_NODE = "PSDC Load PSD";
const LEGACY_PSD_LOAD_NODE = "PSD Load";
const PSD_WIDGET = "psd_file";
const PSDC_NODE_TYPES = new Set([
    "PSDC Apply Alpha Channel",
    "PSDC Image Composite PSD",
    "PSDC Load PSD",
    "PSD Load",
    "PSDC Image To PSD",
    "PSDC PSD Layer Combine",
    "PSDC PSD Structure JSON",
    "PSDC PSD Structure JSON Decode",
    "PSDC Preview PSD",
    "PSDC Save PSD",
    "PSDC Extract Alpha",
]);

function isPsdFile(file) {
    return !!file?.name && file.name.toLowerCase().endsWith(".psd");
}

function getPsdWidget(node) {
    return node?.widgets?.find((widget) => widget.name === PSD_WIDGET);
}

function setPsdWidgetValue(node, filename) {
    const widget = getPsdWidget(node);
    if (!widget) return;

    const values = widget.options?.values;
    if (Array.isArray(values) && !values.includes(filename)) {
        values.push(filename);
        values.sort((a, b) => String(a).localeCompare(String(b)));
    }

    widget.value = filename;
    widget.callback?.(filename, app.canvas, node, undefined, widget);
    node.setDirtyCanvas?.(true, true);
    node.graph?.setDirtyCanvas?.(true, true);
    app.graph?.setDirtyCanvas?.(true, true);
}

async function uploadPsd(file) {
    if (!isPsdFile(file)) {
        throw new Error("Only .psd files can be uploaded into PSDC Load PSD.");
    }

    const body = new FormData();
    body.append("overwrite", "false");
    body.append("psd", file, file.name);

    const response = await fetch("/psdc/upload/psd", {
        method: "POST",
        body,
    });

    if (!response.ok) {
        throw new Error(`PSD upload failed: ${response.status} ${response.statusText}`);
    }

    const data = await response.json();
    return data?.name || file.name;
}

async function uploadIntoNode(node, file) {
    const filename = await uploadPsd(file);
    setPsdWidgetValue(node, filename);
    return filename;
}

function eventToCanvasPosition(event, index = 0) {
    const canvas = app.canvas;
    const offset = index * 40;

    if (canvas?.convertEventToCanvasOffset) {
        const position = canvas.convertEventToCanvasOffset(event);
        return [position[0] + offset, position[1] + offset];
    }

    if (canvas?.ds?.convertOffsetToCanvas && canvas.canvas?.getBoundingClientRect) {
        const rect = canvas.canvas.getBoundingClientRect();
        const position = canvas.ds.convertOffsetToCanvas([event.clientX - rect.left, event.clientY - rect.top]);
        return [position[0] + offset, position[1] + offset];
    }

    const mouse = canvas?.graph_mouse || [100, 100];
    return [mouse[0] + offset, mouse[1] + offset];
}

function createPsdLoadNode(filename, event, index = 0) {
    const graph = app.graph || app.canvas?.graph;
    const LiteGraph = window.LiteGraph;
    if (!graph || !LiteGraph?.createNode) return null;

    const node = LiteGraph.createNode(PSD_LOAD_NODE);
    if (!node) return null;

    node.pos = eventToCanvasPosition(event, index);
    graph.add(node);
    setPsdWidgetValue(node, filename);
    node.setSize?.(node.computeSize?.() || node.size);
    app.canvas?.selectNode?.(node);
    app.canvas?.setDirty?.(true, true);
    app.canvas?.setDirtyCanvas?.(true, true);
    return node;
}

function eventHasPsdFiles(event) {
    return [...(event.dataTransfer?.files || [])].some(isPsdFile);
}

async function handleWindowDrop(event) {
    const files = [...(event.dataTransfer?.files || [])].filter(isPsdFile);
    if (!files.length) return;
    if (event.defaultPrevented || event.target?.closest?.(".psdc-psd-upload-widget")) return;

    event.preventDefault();
    event.stopPropagation();

    for (const [index, file] of files.entries()) {
        try {
            const filename = await uploadPsd(file);
            createPsdLoadNode(filename, event, index);
        } catch (error) {
            console.error("[PSDC] PSD drop failed", error);
            alert(error.message || String(error));
        }
    }
}

function installWindowDropHandler() {
    if (window.__psdcPsdDropHandlerInstalled) return;
    window.__psdcPsdDropHandlerInstalled = true;

    window.addEventListener(
        "dragover",
        (event) => {
            if (!eventHasPsdFiles(event)) return;
            event.preventDefault();
            event.dataTransfer.dropEffect = "copy";
        },
        true,
    );

    window.addEventListener("drop", handleWindowDrop);
}

function createUploadWidget(node) {
    const container = document.createElement("div");
    container.className = "psdc-psd-upload-widget";
    container.style.display = "flex";
    container.style.gap = "6px";
    container.style.alignItems = "center";
    container.style.width = "100%";

    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Choose PSD";
    button.style.flex = "0 0 auto";

    const label = document.createElement("span");
    label.textContent = "or drop PSD";
    label.style.opacity = "0.75";
    label.style.fontSize = "11px";
    label.style.whiteSpace = "nowrap";

    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".psd,image/vnd.adobe.photoshop,application/x-photoshop,application/octet-stream";
    input.style.display = "none";

    async function chooseFile(file) {
        if (!file) return;
        try {
            const filename = await uploadIntoNode(node, file);
            label.textContent = filename;
        } catch (error) {
            console.error("[PSDC] PSD upload failed", error);
            alert(error.message || String(error));
        } finally {
            input.value = "";
        }
    }

    button.addEventListener("click", () => input.click());
    input.addEventListener("change", () => chooseFile(input.files?.[0]));

    container.addEventListener("dragover", (event) => {
        if (!eventHasPsdFiles(event)) return;
        event.preventDefault();
        event.stopPropagation();
        event.dataTransfer.dropEffect = "copy";
    });

    container.addEventListener("drop", (event) => {
        const file = [...(event.dataTransfer?.files || [])].find(isPsdFile);
        if (!file) return;
        event.preventDefault();
        event.stopPropagation();
        chooseFile(file);
    });

    container.append(button, label, input);

    node.addDOMWidget("psd_upload", "PSD", container, {
        serialize: false,
        getValue: () => "",
        setValue: () => {},
    });
}

app.registerExtension({
    name: "PSDC.PSDUpload",
    async setup() {
        installWindowDropHandler();
    },
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (PSDC_NODE_TYPES.has(nodeData.name) || nodeData.python_module?.includes("D2-SavePSD-ComfyUI")) {
            nodeData.python_module = "custom_nodes.PSDC";
        }

        if (nodeData.name !== PSD_LOAD_NODE && nodeData.name !== LEGACY_PSD_LOAD_NODE) return;
        if (nodeType.prototype.__psdcUploadPatched) return;
        nodeType.prototype.__psdcUploadPatched = true;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
            if (!this.__psdcUploadWidgetAdded) {
                this.__psdcUploadWidgetAdded = true;
                createUploadWidget(this);
            }
            return result;
        };

        const onDragOver = nodeType.prototype.onDragOver;
        nodeType.prototype.onDragOver = function (event) {
            if (eventHasPsdFiles(event)) return true;
            return onDragOver ? onDragOver.apply(this, arguments) : false;
        };

        const onDropFile = nodeType.prototype.onDropFile;
        nodeType.prototype.onDropFile = function (file) {
            if (isPsdFile(file)) {
                uploadIntoNode(this, file).catch((error) => {
                    console.error("[PSDC] PSD node drop failed", error);
                    alert(error.message || String(error));
                });
                return true;
            }
            return onDropFile ? onDropFile.apply(this, arguments) : false;
        };
    },
});
