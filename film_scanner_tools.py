#!/usr/bin/env python3
"""
DNG Converter Web Interface
A simple web interface for converting Raspberry Pi Bayer JPEGs to DNG files.
"""

import asyncio
import ctypes
import io
import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import AsyncGenerator

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="DNG Converter")

# Global state
conversion_state = {
    "is_running": False,
    "cancel_requested": False,
    "total_files": 0,
    "processed_files": 0,
    "failed_files": 0,
    "current_file": "",
    "errors": [],
}


class ConversionRequest(BaseModel):
    source_dir: str
    dest_dir: str
    delete_originals: bool = False
    bayer_pattern: int = 2  # 0=GRBG (preview), 2=RGGB (corrected colors)
    rotation: int = 0  # 0=none, 90=right, 180=flip, 270=left


class BrowseRequest(BaseModel):
    path: str


class VideoExportRequest(BaseModel):
    source_dir: str
    output_file: str
    fps: int = 18  # Super 8 standard fps
    codec: str = "h265"  # h265 or dnxhr


class RotateImagesRequest(BaseModel):
    source_dir: str
    rotation: int = 90  # 90=right, 180=flip, 270=left
    filter_mode: str = "portrait"  # "portrait", "landscape", "all"


# Video export state
video_export_state = {
    "is_running": False,
    "cancel_requested": False,
    "progress": 0,
    "total_frames": 0,
    "current_frame": 0,
    "status": "",
    "error": "",
}

# Rotate images state
rotate_state = {
    "is_running": False,
    "cancel_requested": False,
    "total_files": 0,
    "processed_files": 0,
    "current_file": "",
    "error": "",
}

# Darktable develop state
develop_state = {
    "is_running": False,
    "cancel_requested": False,
    "total_files": 0,
    "processed_files": 0,
    "current_file": "",
    "error": "",
}


class DevelopRequest(BaseModel):
    source_dir: str
    dest_dir: str
    style: str = ""  # Darktable style name
    quality: int = 95  # JPEG quality


class PreviewRequest(BaseModel):
    source_dir: str
    bayer_pattern: int = 2  # 0=GRBG, 2=RGGB
    style: str = ""  # Darktable style name (optional)
    count: int = 5  # Number of preview images


class WorkflowRequest(BaseModel):
    source_dir: str
    output_dir: str
    bayer_pattern: int = 2  # 0=GRBG, 2=RGGB
    style: str = ""  # Darktable style name
    fps: int = 18  # Video framerate
    codec: str = "h265"  # h265 or dnxhr


# Workflow state
workflow_state = {
    "is_running": False,
    "cancel_requested": False,
    "step": "",  # dng, develop, video, done
    "progress": 0,
    "current_file": "",
    "error": "",
}


# ============================================================================
# DNG Conversion - Direct implementation without EXIF dependencies
# ============================================================================

class BroadcomRawHeader(ctypes.Structure):
    """Broadcom RAW header structure embedded in Pi Camera JPEGs."""
    _fields_ = [
        ('name',          ctypes.c_char * 32),
        ('width',         ctypes.c_uint16),
        ('height',        ctypes.c_uint16),
        ('padding_right', ctypes.c_uint16),
        ('padding_down',  ctypes.c_uint16),
        ('dummy',         ctypes.c_uint32 * 6),
        ('transform',     ctypes.c_uint16),
        ('format',        ctypes.c_uint16),
        ('bayer_order',   ctypes.c_uint8),
        ('bayer_format',  ctypes.c_uint8),
    ]


BAYER_ORDER = {
    0: [0, 1, 1, 2],
    1: [1, 2, 0, 1],
    2: [2, 1, 1, 0],
    3: [1, 0, 2, 1],
}


def extract_raw_from_jpeg(jpg_path: Path) -> tuple[np.ndarray, int]:
    """
    Extract RAW Bayer data from a Raspberry Pi Camera JPEG.
    Works for Pi Camera HQ (IMX477) - 12-bit RAW, 4056x3040.

    Returns:
        raw_data: Unpacked 16-bit Bayer data
        bayer_order: Bayer pattern order (0-3)
    """
    with open(jpg_path, 'rb') as f:
        data = f.read()

    # Find JPEG end marker
    jpeg_end = data.find(b'\xff\xd9')
    if jpeg_end < 0:
        raise ValueError("No JPEG end marker found")

    # Raw data is everything after JPEG
    raw_data = data[jpeg_end + 2:]

    if len(raw_data) < 1000000:
        raise ValueError(f"Raw data too small: {len(raw_data)} bytes")

    # Check for BRCM header (standard Pi Camera format)
    if raw_data[:4] == b'BRCM':
        # Extract header for bayer order
        header = BroadcomRawHeader.from_buffer_copy(
            raw_data[176:176 + ctypes.sizeof(BroadcomRawHeader)]
        )
        bayer_order = header.bayer_order
        # Skip header to get to raw pixels
        raw_pixels = raw_data[32768:]
    else:
        # No BRCM header - raw data starts directly after JPEG
        # Pi Camera HQ uses RGGB pattern
        bayer_order = 2  # RGGB
        raw_pixels = raw_data

    raw_pixels = np.frombuffer(raw_pixels, dtype=np.uint8)

    # Pi Camera HQ dimensions: 6112 x 3040 packed, 4056 x 3040 unpacked
    # Try to detect dimensions from data size
    raw_size = len(raw_pixels)

    # Known configurations
    configs = [
        (6112, 3040, 6084, 3040),  # Standard Pi HQ (stride 6112, 3040 rows)
        (6112, 3056, 6084, 3040),  # With padding rows
    ]

    unpacked = None
    for stride, rows, crop_w, crop_h in configs:
        expected = stride * rows
        if raw_size >= expected:
            try:
                pixels = raw_pixels[:expected].reshape((rows, stride))
                pixels = pixels[:crop_h, :crop_w]

                # Unpack 12-bit to 16-bit
                pixels = pixels.astype(np.uint16)
                shape = pixels.shape
                unpacked = np.zeros((shape[0], int(shape[1] / 3 * 2)), dtype=np.uint16)
                unpacked[:, ::2] = (pixels[:, ::3] << 4) + (pixels[:, 2::3] & 0x0F)
                unpacked[:, 1::2] = (pixels[:, 1::3] << 4) + ((pixels[:, 2::3] >> 4) & 0x0F)
                break
            except Exception:
                continue

    if unpacked is None:
        raise ValueError(f"Could not parse raw data of size {raw_size}")

    return unpacked, bayer_order


def pack12(data: np.ndarray) -> bytes:
    """Pack 16-bit data to 12-bit packed format."""
    out = np.zeros((data.shape[0], int(data.shape[1] * 1.5)), dtype=np.uint8)
    out[:, ::3] = data[:, ::2] >> 4
    out[:, 1::3] = ((data[:, ::2] & 0x0F) << 4) + (data[:, 1::2] >> 8)
    out[:, 2::3] = data[:, 1::2] & 0xFF
    return out.tobytes()


def rotate_bayer(raw_data: np.ndarray, bayer_order: int, rotation: int) -> tuple[np.ndarray, int]:
    """
    Rotate RAW Bayer data and adjust Bayer pattern accordingly.
    Rotation must be in 2-pixel increments to preserve Bayer pattern.

    Args:
        raw_data: 2D numpy array of raw sensor data
        bayer_order: Original Bayer pattern (0-3)
        rotation: Rotation in degrees (0, 90, 180, 270)

    Returns:
        rotated_data: Rotated raw data
        new_bayer_order: Adjusted Bayer pattern
    """
    if rotation == 0:
        return raw_data, bayer_order

    # Bayer pattern transformation for rotations
    # Original patterns: 0=GRBG, 1=RGGB shifted, 2=BGGR shifted, 3=GBRG
    # When rotating CW 90°: pattern shifts
    bayer_rotation_map = {
        # (original_order, rotation) -> new_order
        (0, 90): 3, (0, 180): 0, (0, 270): 1,
        (1, 90): 0, (1, 180): 1, (1, 270): 2,
        (2, 90): 1, (2, 180): 2, (2, 270): 3,
        (3, 90): 2, (3, 180): 3, (3, 270): 0,
    }

    if rotation == 90:
        rotated = np.rot90(raw_data, k=-1)  # CW 90
    elif rotation == 180:
        rotated = np.rot90(raw_data, k=2)
    elif rotation == 270:
        rotated = np.rot90(raw_data, k=1)  # CCW 90 = CW 270
    else:
        return raw_data, bayer_order

    new_order = bayer_rotation_map.get((bayer_order, rotation), bayer_order)
    return rotated, new_order


def create_dng(raw_data: np.ndarray, bayer_order: int, output_path: Path, rotation: int = 0) -> None:
    """
    Create a DNG file from raw Bayer data.
    Uses pidng's DNG writing infrastructure.
    """
    from pidng.dng import Tag, dngIFD, dngTag, DNG

    # Apply rotation if requested
    if rotation != 0:
        raw_data, bayer_order = rotate_bayer(raw_data, bayer_order, rotation)

    # Get dimensions from raw data
    height, width = raw_data.shape
    bpp = 12
    cfa_pattern = BAYER_ORDER[bayer_order]

    # Pi Camera HQ color matrices (from pidng)
    ccm1 = [[6759, 10000], [-2379, 10000], [751, 10000],
            [-4432, 10000], [13871, 10000], [5465, 10000],
            [-401, 10000], [1664, 10000], [7845, 10000]]

    ccm2 = [[5603, 10000], [-1351, 10000], [-600, 10000],
            [-2872, 10000], [11180, 10000], [2132, 10000],
            [600, 10000], [453, 10000], [5821, 10000]]

    fm1 = [[7889, 10000], [1273, 10000], [482, 10000],
           [2401, 10000], [9705, 10000], [-2106, 10000],
           [-26, 10000], [-4406, 10000], [12683, 10000]]

    fm2 = [[6591, 10000], [3034, 10000], [18, 10000],
           [1991, 10000], [10585, 10000], [-2575, 10000],
           [-493, 10000], [-919, 10000], [9663, 10000]]

    camera_calibration = [[1, 1], [0, 1], [0, 1],
                          [0, 1], [1, 1], [0, 1],
                          [0, 1], [0, 1], [1, 1]]

    # Default white balance (daylight-ish)
    as_shot_neutral = [[1000, 1800], [1000, 1000], [1000, 1500]]

    sensor_black = 256  # 4096 >> (16 - 12)
    sensor_white = 4095  # (1 << 12) - 1

    # Pack raw data
    tile = pack12(raw_data)

    # Build DNG
    dngTemplate = DNG()
    dngTemplate.ImageDataStrips.append(tile)

    mainIFD = dngIFD()
    mainTagStripOffset = dngTag(Tag.TileOffsets, [0])
    mainIFD.tags.append(mainTagStripOffset)
    mainIFD.tags.append(dngTag(Tag.NewSubfileType, [0]))
    mainIFD.tags.append(dngTag(Tag.TileByteCounts, [len(tile)]))
    mainIFD.tags.append(dngTag(Tag.ImageWidth, [width]))
    mainIFD.tags.append(dngTag(Tag.ImageLength, [height]))
    mainIFD.tags.append(dngTag(Tag.SamplesPerPixel, [1]))
    mainIFD.tags.append(dngTag(Tag.BitsPerSample, [bpp]))
    mainIFD.tags.append(dngTag(Tag.TileWidth, [width]))
    mainIFD.tags.append(dngTag(Tag.TileLength, [height]))
    mainIFD.tags.append(dngTag(Tag.Compression, [1]))  # Uncompressed
    mainIFD.tags.append(dngTag(Tag.PhotometricInterpretation, [32803]))  # CFA
    mainIFD.tags.append(dngTag(Tag.CFARepeatPatternDim, [2, 2]))
    mainIFD.tags.append(dngTag(Tag.CFAPattern, cfa_pattern))
    mainIFD.tags.append(dngTag(Tag.BlackLevel, [sensor_black]))
    mainIFD.tags.append(dngTag(Tag.WhiteLevel, [sensor_white]))
    mainIFD.tags.append(dngTag(Tag.Make, "Raspberry Pi"))
    mainIFD.tags.append(dngTag(Tag.Model, "HQ Camera"))
    mainIFD.tags.append(dngTag(Tag.Software, "DNG Converter"))
    mainIFD.tags.append(dngTag(Tag.Orientation, [1]))
    mainIFD.tags.append(dngTag(Tag.DNGVersion, [1, 4, 0, 0]))
    mainIFD.tags.append(dngTag(Tag.DNGBackwardVersion, [1, 2, 0, 0]))
    mainIFD.tags.append(dngTag(Tag.UniqueCameraModel, "Raspberry Pi High Quality Camera"))
    mainIFD.tags.append(dngTag(Tag.ColorMatrix1, ccm1))
    mainIFD.tags.append(dngTag(Tag.ColorMatrix2, ccm2))
    mainIFD.tags.append(dngTag(Tag.ForwardMatrix1, fm1))
    mainIFD.tags.append(dngTag(Tag.ForwardMatrix2, fm2))
    mainIFD.tags.append(dngTag(Tag.CameraCalibration1, camera_calibration))
    mainIFD.tags.append(dngTag(Tag.CameraCalibration2, camera_calibration))
    mainIFD.tags.append(dngTag(Tag.AsShotNeutral, as_shot_neutral))
    mainIFD.tags.append(dngTag(Tag.BaselineExposure, [[0, 1]]))
    mainIFD.tags.append(dngTag(Tag.CalibrationIlluminant1, [17]))  # StdA
    mainIFD.tags.append(dngTag(Tag.CalibrationIlluminant2, [21]))  # D65
    mainIFD.tags.append(dngTag(Tag.ProfileName, "Raspberry Pi HQ Camera"))
    mainIFD.tags.append(dngTag(Tag.ProfileEmbedPolicy, [3]))
    mainIFD.tags.append(dngTag(Tag.DefaultBlackRender, [0]))
    mainIFD.tags.append(dngTag(Tag.PreviewColorSpace, [2]))

    dngTemplate.IFDs.append(mainIFD)

    totalLength = dngTemplate.dataLen()
    mainTagStripOffset.setValue([k for offset, k in dngTemplate.StripOffsets.items()])

    buf = bytearray(totalLength)
    dngTemplate.setBuffer(buf)
    dngTemplate.write()

    with open(output_path, 'wb') as f:
        f.write(buf)


def convert_single_file(jpg_path: str, dest_dir: str, delete: bool, bayer_pattern: int = 2, rotation: int = 0) -> tuple[bool, str]:
    """Convert a single JPEG to DNG. Returns (success, error_message)."""
    try:
        source = Path(jpg_path)
        dest_path = Path(dest_dir)
        output_file = dest_path / source.with_suffix(".dng").name

        print(f"Converting: {source.name}")

        # Extract RAW and create DNG
        raw_data, _ = extract_raw_from_jpeg(source)
        create_dng(raw_data, bayer_pattern, output_file, rotation)

        # Validate output
        if not output_file.exists() or output_file.stat().st_size < 1_000_000:
            return False, "DNG file invalid"

        # Delete original if requested
        if delete:
            source.unlink()

        print(f"Done: {source.name}")
        return True, ""

    except Exception as e:
        print(f"Error: {Path(jpg_path).name} - {e}")
        return False, str(e)


# ============================================================================
# Web Interface
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main page."""
    return """<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Film Scanner Tools</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 text-white min-h-screen">
    <div class="container mx-auto px-4 py-8 max-w-2xl">
        <h1 class="text-3xl font-bold mb-6 text-center">Film Scanner Tools</h1>

        <!-- Tabs -->
        <div class="flex mb-6 border-b border-gray-700 flex-wrap">
            <button onclick="switchTab('workflow')" id="tabWorkflow"
                class="px-4 py-3 font-medium border-b-2 border-blue-500 text-blue-400 text-sm">
                Workflow
            </button>
            <button onclick="switchTab('dng')" id="tabDng"
                class="px-4 py-3 font-medium border-b-2 border-transparent text-gray-400 hover:text-white text-sm">
                RAW → DNG
            </button>
            <button onclick="switchTab('develop')" id="tabDevelop"
                class="px-4 py-3 font-medium border-b-2 border-transparent text-gray-400 hover:text-white text-sm">
                Darktable
            </button>
            <button onclick="switchTab('video')" id="tabVideo"
                class="px-4 py-3 font-medium border-b-2 border-transparent text-gray-400 hover:text-white text-sm">
                Vidéo
            </button>
        </div>

        <!-- Workflow Tab (Main) -->
        <div id="panelWorkflow" class="bg-gray-800 rounded-lg p-6 space-y-5">
            <!-- Source folder -->
            <div>
                <label class="block text-sm font-medium mb-2">1. Dossier source (JPEGs RAW)</label>
                <div class="flex gap-2">
                    <input type="text" id="workflowSourceDir"
                        class="flex-1 bg-gray-700 rounded px-4 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
                        placeholder="/chemin/vers/les/frames">
                    <button onclick="browse('workflowSource')"
                        class="bg-gray-600 hover:bg-gray-500 px-4 py-2 rounded">Parcourir</button>
                </div>
                <p id="workflowSourceInfo" class="text-sm text-gray-400 mt-1"></p>
            </div>

            <!-- Output folder -->
            <div>
                <label class="block text-sm font-medium mb-2">2. Dossier de sortie</label>
                <div class="flex gap-2">
                    <input type="text" id="workflowOutputDir"
                        class="flex-1 bg-gray-700 rounded px-4 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
                        placeholder="/chemin/vers/sortie">
                    <button onclick="browse('workflowOutput')"
                        class="bg-gray-600 hover:bg-gray-500 px-4 py-2 rounded">Parcourir</button>
                </div>
                <p class="text-xs text-gray-500 mt-1">Sous-dossiers: /dng, /jpeg, + fichier vidéo</p>
            </div>

            <!-- Bayer Pattern -->
            <div>
                <label class="block text-sm font-medium mb-2">3. Pattern Bayer</label>
                <div class="flex gap-4">
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="workflowBayer" value="2" checked class="w-4 h-4">
                        <span>RGGB (couleurs corrigées)</span>
                    </label>
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="workflowBayer" value="0" class="w-4 h-4">
                        <span>GRBG (preview original)</span>
                    </label>
                </div>
            </div>

            <!-- Darktable Style -->
            <div>
                <label class="block text-sm font-medium mb-2">4. Style Darktable</label>
                <select id="workflowStyle"
                    class="w-full bg-gray-700 rounded px-4 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500">
                    <option value="">Aucun (paramètres par défaut)</option>
                </select>
            </div>

            <!-- Video settings -->
            <div class="grid grid-cols-2 gap-4">
                <div>
                    <label class="block text-sm font-medium mb-2">5. Framerate</label>
                    <select id="workflowFps" class="w-full bg-gray-700 rounded px-4 py-2">
                        <option value="18" selected>18 fps (Super 8)</option>
                        <option value="16">16 fps (8mm)</option>
                        <option value="24">24 fps</option>
                    </select>
                </div>
                <div>
                    <label class="block text-sm font-medium mb-2">6. Codec vidéo</label>
                    <select id="workflowCodec" class="w-full bg-gray-700 rounded px-4 py-2">
                        <option value="h265" selected>H.265/MP4 (GPU)</option>
                        <option value="dnxhr">DNxHR/MOV (édition)</option>
                    </select>
                </div>
            </div>

            <!-- Preview section -->
            <div class="border-t border-gray-700 pt-4">
                <div class="flex gap-2 mb-3">
                    <button onclick="generateWorkflowPreview('rggb')"
                        class="flex-1 bg-yellow-600 hover:bg-yellow-500 py-2 px-3 rounded font-medium transition text-sm">
                        Aperçu RGGB
                    </button>
                    <button onclick="generateWorkflowPreview('grbg')"
                        class="flex-1 bg-orange-600 hover:bg-orange-500 py-2 px-3 rounded font-medium transition text-sm">
                        Aperçu GRBG
                    </button>
                </div>
                <div id="workflowPreviewLoading" class="hidden text-center py-4">
                    <div class="inline-block animate-spin rounded-full h-6 w-6 border-b-2 border-yellow-500"></div>
                    <p class="text-sm text-gray-400 mt-2">Génération des aperçus...</p>
                </div>
                <div id="workflowPreviewGrid" class="grid grid-cols-5 gap-2 hidden"></div>
                <p id="workflowPreviewStatus" class="text-xs text-gray-500 mt-2 text-center"></p>
            </div>

            <!-- Progress section -->
            <div id="workflowProgressSection" class="hidden border-t border-gray-700 pt-4">
                <div class="space-y-2">
                    <div class="flex justify-between text-sm mb-1">
                        <span id="workflowStepLabel">Étape 1/3: Conversion DNG</span>
                        <span id="workflowStepProgress">0%</span>
                    </div>
                    <div class="w-full bg-gray-700 rounded-full h-3">
                        <div id="workflowProgressBar" class="bg-blue-500 h-3 rounded-full transition-all duration-300" style="width: 0%"></div>
                    </div>
                    <p id="workflowCurrentFile" class="text-xs text-gray-400"></p>
                </div>
            </div>

            <!-- Action button -->
            <button id="workflowActionBtn" onclick="startWorkflow()"
                class="w-full bg-green-600 hover:bg-green-500 py-4 rounded-lg font-bold text-lg transition">
                Lancer le traitement complet
            </button>

            <p id="workflowStatus" class="text-center text-gray-400"></p>
        </div>

        <!-- DNG Tab -->
        <div id="panelDng" class="bg-gray-800 rounded-lg p-6 space-y-6 hidden">
            <div>
                <label class="block text-sm font-medium mb-2">Dossier source (JPEGs RAW)</label>
                <div class="flex gap-2">
                    <input type="text" id="sourceDir"
                        class="flex-1 bg-gray-700 rounded px-4 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
                        placeholder="/chemin/vers/les/frames">
                    <button onclick="browse('source')"
                        class="bg-gray-600 hover:bg-gray-500 px-4 py-2 rounded">Parcourir</button>
                </div>
                <p id="sourceInfo" class="text-sm text-gray-400 mt-1"></p>
            </div>

            <div>
                <label class="block text-sm font-medium mb-2">Dossier destination</label>
                <div class="flex gap-2">
                    <input type="text" id="destDir"
                        class="flex-1 bg-gray-700 rounded px-4 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
                        placeholder="/chemin/vers/destination">
                    <button onclick="browse('dest')"
                        class="bg-gray-600 hover:bg-gray-500 px-4 py-2 rounded">Parcourir</button>
                </div>
            </div>

            <div>
                <label class="block text-sm font-medium mb-2">Pattern Bayer</label>
                <div class="flex gap-6">
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="bayerPattern" value="2" checked class="w-4 h-4">
                        <span>Couleurs corrigées (RGGB)</span>
                    </label>
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="bayerPattern" value="0" class="w-4 h-4">
                        <span>Preview original (GRBG)</span>
                    </label>
                </div>
            </div>

            <div>
                <label class="block text-sm font-medium mb-2">Rotation</label>
                <div class="flex gap-4">
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="rotation" value="0" checked class="w-4 h-4">
                        <span>Aucune</span>
                    </label>
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="rotation" value="90" class="w-4 h-4">
                        <span>90° droite</span>
                    </label>
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="rotation" value="180" class="w-4 h-4">
                        <span>180°</span>
                    </label>
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="rotation" value="270" class="w-4 h-4">
                        <span>90° gauche</span>
                    </label>
                </div>
            </div>

            <div class="flex items-center gap-2">
                <input type="checkbox" id="deleteOriginals" class="w-4 h-4">
                <label for="deleteOriginals">Supprimer les fichiers JPEG après conversion</label>
            </div>

            <div id="progressSection" class="hidden">
                <div class="flex justify-between text-sm mb-1">
                    <span id="progressText">0/0 fichiers</span>
                    <span id="progressPercent">0%</span>
                </div>
                <div class="w-full bg-gray-700 rounded-full h-4">
                    <div id="progressBar" class="bg-blue-500 h-4 rounded-full transition-all duration-300" style="width: 0%"></div>
                </div>
                <p id="currentFile" class="text-sm text-gray-400 mt-2"></p>
            </div>

            <button id="actionBtn" onclick="toggleConversion()"
                class="w-full bg-blue-600 hover:bg-blue-500 py-3 rounded-lg font-medium transition">
                Démarrer conversion DNG
            </button>

            <p id="status" class="text-center text-gray-400"></p>
        </div>

        <!-- Darktable Develop Tab -->
        <div id="panelDevelop" class="bg-gray-800 rounded-lg p-6 space-y-6 hidden">
            <div>
                <label class="block text-sm font-medium mb-2">Dossier source (DNG ou images RAW)</label>
                <div class="flex gap-2">
                    <input type="text" id="developSourceDir"
                        class="flex-1 bg-gray-700 rounded px-4 py-2 focus:outline-none focus:ring-2 focus:ring-purple-500"
                        placeholder="/chemin/vers/les/dng">
                    <button onclick="browse('developSource')"
                        class="bg-gray-600 hover:bg-gray-500 px-4 py-2 rounded">Parcourir</button>
                </div>
                <p id="developSourceInfo" class="text-sm text-gray-400 mt-1"></p>
            </div>

            <div>
                <label class="block text-sm font-medium mb-2">Dossier destination (JPEG)</label>
                <div class="flex gap-2">
                    <input type="text" id="developDestDir"
                        class="flex-1 bg-gray-700 rounded px-4 py-2 focus:outline-none focus:ring-2 focus:ring-purple-500"
                        placeholder="/chemin/vers/destination">
                    <button onclick="browse('developDest')"
                        class="bg-gray-600 hover:bg-gray-500 px-4 py-2 rounded">Parcourir</button>
                </div>
            </div>

            <div>
                <label class="block text-sm font-medium mb-2">Style Darktable</label>
                <select id="developStyle"
                    class="w-full bg-gray-700 rounded px-4 py-2 focus:outline-none focus:ring-2 focus:ring-purple-500">
                    <option value="">Aucun (paramètres par défaut)</option>
                </select>
                <p class="text-xs text-gray-500 mt-1">Styles depuis ~/.config/darktable/styles/</p>
            </div>

            <div>
                <label class="block text-sm font-medium mb-2">Qualité JPEG</label>
                <div class="flex gap-4">
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="developQuality" value="95" checked class="w-4 h-4">
                        <span>95 (haute qualité)</span>
                    </label>
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="developQuality" value="100" class="w-4 h-4">
                        <span>100 (max)</span>
                    </label>
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="developQuality" value="85" class="w-4 h-4">
                        <span>85 (léger)</span>
                    </label>
                </div>
            </div>

            <div id="developProgressSection" class="hidden">
                <div class="flex justify-between text-sm mb-1">
                    <span id="developProgressText">0/0 fichiers</span>
                    <span id="developProgressPercent">0%</span>
                </div>
                <div class="w-full bg-gray-700 rounded-full h-4">
                    <div id="developProgressBar" class="bg-purple-500 h-4 rounded-full transition-all duration-300" style="width: 0%"></div>
                </div>
                <p id="developCurrentFile" class="text-sm text-gray-400 mt-2"></p>
            </div>

            <button id="developActionBtn" onclick="toggleDevelop()"
                class="w-full bg-purple-600 hover:bg-purple-500 py-3 rounded-lg font-medium transition">
                Développer avec Darktable
            </button>

            <p id="developStatus" class="text-center text-gray-400"></p>
        </div>

        <!-- Video Export Tab -->
        <div id="panelVideo" class="bg-gray-800 rounded-lg p-6 space-y-6 hidden">
            <div>
                <label class="block text-sm font-medium mb-2">Dossier des images (JPG/PNG/TIFF)</label>
                <div class="flex gap-2">
                    <input type="text" id="videoSourceDir"
                        class="flex-1 bg-gray-700 rounded px-4 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
                        placeholder="/chemin/vers/les/images">
                    <button onclick="browse('videoSource')"
                        class="bg-gray-600 hover:bg-gray-500 px-4 py-2 rounded">Parcourir</button>
                </div>
                <p id="videoSourceInfo" class="text-sm text-gray-400 mt-1"></p>
            </div>

            <div>
                <label class="block text-sm font-medium mb-2">Fichier de sortie</label>
                <div class="flex gap-2">
                    <input type="text" id="videoOutputFile"
                        class="flex-1 bg-gray-700 rounded px-4 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
                        placeholder="/chemin/vers/film.mp4">
                    <button onclick="browse('videoOutput')"
                        class="bg-gray-600 hover:bg-gray-500 px-4 py-2 rounded">Parcourir</button>
                </div>
            </div>

            <div>
                <label class="block text-sm font-medium mb-2">Format de sortie</label>
                <div class="flex gap-4 flex-wrap">
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="videoCodec" value="h265" checked class="w-4 h-4">
                        <span>H.265/MP4 <span class="text-xs text-gray-500">(GPU, léger)</span></span>
                    </label>
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="videoCodec" value="dnxhr" class="w-4 h-4">
                        <span>DNxHR/MOV <span class="text-xs text-gray-500">(édition pro)</span></span>
                    </label>
                </div>
            </div>

            <div>
                <label class="block text-sm font-medium mb-2">Framerate</label>
                <div class="flex gap-4">
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="fps" value="18" checked class="w-4 h-4">
                        <span>18 fps (Super 8)</span>
                    </label>
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="fps" value="16" class="w-4 h-4">
                        <span>16 fps (8mm standard)</span>
                    </label>
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="fps" value="24" class="w-4 h-4">
                        <span>24 fps</span>
                    </label>
                </div>
            </div>

            <div id="videoProgressSection" class="hidden">
                <div class="flex justify-between text-sm mb-1">
                    <span id="videoProgressText">0%</span>
                    <span id="videoFrameInfo"></span>
                </div>
                <div class="w-full bg-gray-700 rounded-full h-4">
                    <div id="videoProgressBar" class="bg-green-500 h-4 rounded-full transition-all duration-300" style="width: 0%"></div>
                </div>
                <p id="videoCurrentStatus" class="text-sm text-gray-400 mt-2"></p>
            </div>

            <button id="videoActionBtn" onclick="toggleVideoExport()"
                class="w-full bg-green-600 hover:bg-green-500 py-3 rounded-lg font-medium transition">
                Exporter en vidéo MOV
            </button>

            <p id="videoStatus" class="text-center text-gray-400"></p>
        </div>

        <!-- Rotate Tab -->
        <div id="panelRotate" class="bg-gray-800 rounded-lg p-6 space-y-6 hidden">
            <div>
                <label class="block text-sm font-medium mb-2">Dossier des images</label>
                <div class="flex gap-2">
                    <input type="text" id="rotateSourceDir"
                        class="flex-1 bg-gray-700 rounded px-4 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500"
                        placeholder="/chemin/vers/les/images">
                    <button onclick="browse('rotateSource')"
                        class="bg-gray-600 hover:bg-gray-500 px-4 py-2 rounded">Parcourir</button>
                </div>
                <p id="rotateSourceInfo" class="text-sm text-gray-400 mt-1"></p>
            </div>

            <div>
                <label class="block text-sm font-medium mb-2">Filtrer les images</label>
                <div class="flex gap-4">
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="filterMode" value="portrait" checked class="w-4 h-4">
                        <span>Portrait uniquement (hauteur > largeur)</span>
                    </label>
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="filterMode" value="landscape" class="w-4 h-4">
                        <span>Paysage uniquement</span>
                    </label>
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="filterMode" value="all" class="w-4 h-4">
                        <span>Toutes</span>
                    </label>
                </div>
            </div>

            <div>
                <label class="block text-sm font-medium mb-2">Rotation à appliquer</label>
                <div class="flex gap-4">
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="rotateAngle" value="90" checked class="w-4 h-4">
                        <span>90° droite</span>
                    </label>
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="rotateAngle" value="180" class="w-4 h-4">
                        <span>180°</span>
                    </label>
                    <label class="flex items-center gap-2 cursor-pointer">
                        <input type="radio" name="rotateAngle" value="270" class="w-4 h-4">
                        <span>90° gauche</span>
                    </label>
                </div>
            </div>

            <div id="rotateProgressSection" class="hidden">
                <div class="flex justify-between text-sm mb-1">
                    <span id="rotateProgressText">0/0 fichiers</span>
                    <span id="rotateProgressPercent">0%</span>
                </div>
                <div class="w-full bg-gray-700 rounded-full h-4">
                    <div id="rotateProgressBar" class="bg-orange-500 h-4 rounded-full transition-all duration-300" style="width: 0%"></div>
                </div>
                <p id="rotateCurrentFile" class="text-sm text-gray-400 mt-2"></p>
            </div>

            <button id="rotateActionBtn" onclick="toggleRotate()"
                class="w-full bg-orange-600 hover:bg-orange-500 py-3 rounded-lg font-medium transition">
                Appliquer la rotation
            </button>

            <p id="rotateStatus" class="text-center text-gray-400"></p>
        </div>

        <!-- File Browser Modal -->
        <div id="browserModal" class="fixed inset-0 bg-black/50 hidden items-center justify-center z-50">
            <div class="bg-gray-800 rounded-lg p-6 w-full max-w-lg mx-4 max-h-[80vh] flex flex-col">
                <div class="flex justify-between items-center mb-4">
                    <h2 class="text-xl font-bold">Sélectionner un dossier</h2>
                    <button onclick="closeModal()" class="text-gray-400 hover:text-white text-2xl">&times;</button>
                </div>
                <p id="currentPath" class="text-sm text-gray-400 mb-2 truncate"></p>
                <div id="folderList" class="flex-1 overflow-y-auto space-y-1 mb-4"></div>
                <div class="flex gap-2">
                    <button onclick="selectFolder()" class="flex-1 bg-blue-600 hover:bg-blue-500 py-2 rounded">
                        Sélectionner ce dossier
                    </button>
                    <button onclick="closeModal()" class="bg-gray-600 hover:bg-gray-500 px-4 py-2 rounded">
                        Annuler
                    </button>
                </div>
            </div>
        </div>
    </div>

    <script>
        const STORAGE_KEY = 'dng_converter_paths';
        let browserTarget = 'source';
        let currentBrowserPath = '/mnt/Collections/Vidéos/Home Made/Super8';
        let eventSource = null;
        let videoEventSource = null;
        let workflowEventSource = null;
        let currentTab = 'workflow';

        // Tab switching
        function switchTab(tab) {
            currentTab = tab;
            const tabs = ['workflow', 'dng', 'develop', 'video'];
            tabs.forEach(t => {
                document.getElementById('tab' + t.charAt(0).toUpperCase() + t.slice(1)).classList.toggle('border-blue-500', t === tab);
                document.getElementById('tab' + t.charAt(0).toUpperCase() + t.slice(1)).classList.toggle('text-blue-400', t === tab);
                document.getElementById('tab' + t.charAt(0).toUpperCase() + t.slice(1)).classList.toggle('border-transparent', t !== tab);
                document.getElementById('tab' + t.charAt(0).toUpperCase() + t.slice(1)).classList.toggle('text-gray-400', t !== tab);
                document.getElementById('panel' + t.charAt(0).toUpperCase() + t.slice(1)).classList.toggle('hidden', t !== tab);
            });
            saveSettings();
        }

        // Get selected values
        function getSelectedBayerPattern() {
            const radio = document.querySelector('input[name="bayerPattern"]:checked');
            return radio ? parseInt(radio.value) : 2;
        }
        function setBayerPattern(value) {
            const radio = document.querySelector(`input[name="bayerPattern"][value="${value}"]`);
            if (radio) radio.checked = true;
        }
        function getSelectedFps() {
            const radio = document.querySelector('input[name="fps"]:checked');
            return radio ? parseInt(radio.value) : 18;
        }
        function setFps(value) {
            const radio = document.querySelector(`input[name="fps"][value="${value}"]`);
            if (radio) radio.checked = true;
        }
        function getSelectedRotation() {
            const radio = document.querySelector('input[name="rotation"]:checked');
            return radio ? parseInt(radio.value) : 0;
        }
        function setRotation(value) {
            const radio = document.querySelector(`input[name="rotation"][value="${value}"]`);
            if (radio) radio.checked = true;
        }
        function getSelectedFilterMode() {
            const radio = document.querySelector('input[name="filterMode"]:checked');
            return radio ? radio.value : 'portrait';
        }
        function setFilterMode(value) {
            const radio = document.querySelector(`input[name="filterMode"][value="${value}"]`);
            if (radio) radio.checked = true;
        }
        function getSelectedRotateAngle() {
            const radio = document.querySelector('input[name="rotateAngle"]:checked');
            return radio ? parseInt(radio.value) : 90;
        }
        function setRotateAngle(value) {
            const radio = document.querySelector(`input[name="rotateAngle"][value="${value}"]`);
            if (radio) radio.checked = true;
        }
        function getSelectedVideoCodec() {
            const radio = document.querySelector('input[name="videoCodec"]:checked');
            return radio ? radio.value : 'h265';
        }
        function setVideoCodec(value) {
            const radio = document.querySelector(`input[name="videoCodec"][value="${value}"]`);
            if (radio) radio.checked = true;
        }
        function getVideoExtension() {
            return getSelectedVideoCodec() === 'dnxhr' ? '.mov' : '.mp4';
        }
        function updateVideoOutputExtension() {
            const input = document.getElementById('videoOutputFile');
            const value = input.value;
            if (value) {
                const ext = getVideoExtension();
                const newValue = value.replace(/\\.(mp4|mov)$/i, ext);
                if (newValue !== value) {
                    input.value = newValue;
                    saveSettings();
                }
            }
        }
        function getSelectedDevelopQuality() {
            const radio = document.querySelector('input[name="developQuality"]:checked');
            return radio ? parseInt(radio.value) : 95;
        }
        function setDevelopQuality(value) {
            const radio = document.querySelector(`input[name="developQuality"][value="${value}"]`);
            if (radio) radio.checked = true;
        }

        // Load/save settings
        function loadSettings() {
            // Helper to safely set element value
            const setVal = (id, val) => { const el = document.getElementById(id); if (el && val) el.value = val; };
            const setChecked = (id, val) => { const el = document.getElementById(id); if (el && val !== undefined) el.checked = val; };

            try {
                const saved = localStorage.getItem(STORAGE_KEY);
                if (saved) {
                    const s = JSON.parse(saved);
                    setVal('sourceDir', s.source);
                    setVal('destDir', s.dest);
                    setChecked('deleteOriginals', s.deleteOriginals);
                    if (s.bayerPattern !== undefined) setBayerPattern(s.bayerPattern);
                    if (s.rotation !== undefined) setRotation(s.rotation);
                    setVal('videoSourceDir', s.videoSource);
                    setVal('videoOutputFile', s.videoOutput);
                    if (s.fps !== undefined) setFps(s.fps);
                    if (s.videoCodec) setVideoCodec(s.videoCodec);
                    setVal('rotateSourceDir', s.rotateSource);
                    if (s.filterMode) setFilterMode(s.filterMode);
                    if (s.rotateAngle !== undefined) setRotateAngle(s.rotateAngle);
                    setVal('developSourceDir', s.developSource);
                    setVal('developDestDir', s.developDest);
                    setVal('developStyle', s.developStyle);
                    if (s.developQuality !== undefined) setDevelopQuality(s.developQuality);
                    setVal('workflowSourceDir', s.workflowSource);
                    setVal('workflowOutputDir', s.workflowOutput);
                    setVal('workflowStyle', s.workflowStyle);
                    if (s.activeTab) switchTab(s.activeTab);
                    return;
                }
            } catch (e) { console.error('loadSettings error:', e); }
            setVal('sourceDir', currentBrowserPath);
            setVal('destDir', currentBrowserPath);
        }

        function saveSettings() {
            // Helper to safely get element value
            const getVal = (id) => { const el = document.getElementById(id); return el ? el.value : ''; };
            const getChecked = (id) => { const el = document.getElementById(id); return el ? el.checked : false; };

            const settings = {
                source: getVal('sourceDir'),
                dest: getVal('destDir'),
                deleteOriginals: getChecked('deleteOriginals'),
                bayerPattern: getSelectedBayerPattern(),
                rotation: getSelectedRotation(),
                videoSource: getVal('videoSourceDir'),
                videoOutput: getVal('videoOutputFile'),
                fps: getSelectedFps(),
                videoCodec: getSelectedVideoCodec(),
                rotateSource: getVal('rotateSourceDir'),
                filterMode: getSelectedFilterMode(),
                rotateAngle: getSelectedRotateAngle(),
                developSource: getVal('developSourceDir'),
                developDest: getVal('developDestDir'),
                developStyle: getVal('developStyle'),
                developQuality: getSelectedDevelopQuality(),
                workflowSource: getVal('workflowSourceDir'),
                workflowOutput: getVal('workflowOutputDir'),
                workflowStyle: getVal('workflowStyle'),
                activeTab: currentTab
            };
            localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
        }

        // Initialize
        loadSettings();
        checkSourceDir();
        checkVideoSourceDir();
        loadDarktableStyles();
        loadPreviewStyles();

        // Check if any operations are in progress (restore state after page refresh)
        checkRunningOperations();

        async function checkRunningOperations() {
            // Check DNG conversion
            try {
                const dngResp = await fetch('/api/status');
                const dngData = await dngResp.json();
                if (dngData.is_running) {
                    document.getElementById('progressSection').classList.remove('hidden');
                    document.getElementById('actionBtn').textContent = 'Arrêter';
                    updateProgress(dngData);
                    // Reconnect to SSE
                    if (eventSource) eventSource.close();
                    eventSource = new EventSource('/api/progress');
                    eventSource.onmessage = (e) => updateProgress(JSON.parse(e.data));
                    eventSource.onerror = () => { eventSource.close(); checkFinalStatus(); };
                }
            } catch (e) {}

            // Check video export
            try {
                const videoResp = await fetch('/api/video/status');
                const videoData = await videoResp.json();
                if (videoData.is_running) {
                    document.getElementById('videoProgressSection').classList.remove('hidden');
                    document.getElementById('videoActionBtn').textContent = 'Arrêter';
                    updateVideoProgress(videoData);
                    // Reconnect to SSE
                    if (videoEventSource) videoEventSource.close();
                    videoEventSource = new EventSource('/api/video/progress');
                    videoEventSource.onmessage = (e) => updateVideoProgress(JSON.parse(e.data));
                    videoEventSource.onerror = () => { videoEventSource.close(); checkVideoFinalStatus(); };
                }
            } catch (e) {}

            // Check rotation
            try {
                const rotateResp = await fetch('/api/rotate/status');
                const rotateData = await rotateResp.json();
                if (rotateData.is_running) {
                    document.getElementById('rotateProgressSection').classList.remove('hidden');
                    document.getElementById('rotateActionBtn').textContent = 'Arrêter';
                    updateRotateProgress(rotateData);
                    // Reconnect to SSE
                    if (rotateEventSource) rotateEventSource.close();
                    rotateEventSource = new EventSource('/api/rotate/progress');
                    rotateEventSource.onmessage = (e) => updateRotateProgress(JSON.parse(e.data));
                    rotateEventSource.onerror = () => { rotateEventSource.close(); checkRotateFinalStatus(); };
                }
            } catch (e) {}
        }

        // Event listeners
        document.getElementById('sourceDir').addEventListener('change', () => { saveSettings(); checkSourceDir(); });
        document.getElementById('destDir').addEventListener('change', saveSettings);
        document.getElementById('deleteOriginals').addEventListener('change', saveSettings);
        document.querySelectorAll('input[name="bayerPattern"]').forEach(r => r.addEventListener('change', saveSettings));
        document.querySelectorAll('input[name="rotation"]').forEach(r => r.addEventListener('change', saveSettings));
        document.getElementById('videoSourceDir').addEventListener('change', () => { saveSettings(); checkVideoSourceDir(); });
        document.getElementById('videoOutputFile').addEventListener('change', saveSettings);
        document.querySelectorAll('input[name="fps"]').forEach(r => r.addEventListener('change', saveSettings));
        document.querySelectorAll('input[name="videoCodec"]').forEach(r => r.addEventListener('change', () => { saveSettings(); updateVideoOutputExtension(); }));
        document.getElementById('rotateSourceDir').addEventListener('change', () => { saveSettings(); checkRotateSourceDir(); });
        document.querySelectorAll('input[name="filterMode"]').forEach(r => r.addEventListener('change', () => { saveSettings(); checkRotateSourceDir(); }));
        document.querySelectorAll('input[name="rotateAngle"]').forEach(r => r.addEventListener('change', saveSettings));

        // Browse functionality
        async function browse(target) {
            browserTarget = target;
            let inputId;
            if (target === 'source') inputId = 'sourceDir';
            else if (target === 'dest') inputId = 'destDir';
            else if (target === 'videoSource') inputId = 'videoSourceDir';
            else if (target === 'videoOutput') inputId = 'videoOutputFile';
            else if (target === 'rotateSource') inputId = 'rotateSourceDir';
            else if (target === 'developSource') inputId = 'developSourceDir';
            else if (target === 'developDest') inputId = 'developDestDir';
            else if (target === 'previewSource') inputId = 'previewSourceDir';
            else if (target === 'workflowSource') inputId = 'workflowSourceDir';
            else if (target === 'workflowOutput') inputId = 'workflowOutputDir';
            else inputId = 'sourceDir';
            let initPath = document.getElementById(inputId).value || '/';
            // For file paths, extract the directory part
            if (target === 'videoOutput' && initPath.includes('/')) {
                initPath = initPath.substring(0, initPath.lastIndexOf('/')) || '/';
            }
            currentBrowserPath = initPath;
            await loadFolder(currentBrowserPath);
            document.getElementById('browserModal').classList.remove('hidden');
            document.getElementById('browserModal').classList.add('flex');
        }

        async function loadFolder(path) {
            try {
                const resp = await fetch('/api/browse', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({path})
                });
                const data = await resp.json();
                if (!resp.ok) { alert(data.detail || 'Erreur'); return; }

                currentBrowserPath = data.current_path;
                document.getElementById('currentPath').textContent = data.current_path;

                const list = document.getElementById('folderList');
                list.innerHTML = '';

                if (data.parent_path) {
                    const div = document.createElement('div');
                    div.className = 'flex items-center gap-2 p-2 hover:bg-gray-700 rounded cursor-pointer';
                    div.innerHTML = '<span>📁</span><span>..</span>';
                    div.onclick = () => loadFolder(data.parent_path);
                    list.appendChild(div);
                }

                for (const entry of data.entries) {
                    const div = document.createElement('div');
                    div.className = 'flex items-center gap-2 p-2 hover:bg-gray-700 rounded cursor-pointer';
                    div.innerHTML = `<span>📁</span><span>${entry.name}</span>`;
                    div.onclick = () => loadFolder(entry.path);
                    list.appendChild(div);
                }

                if (data.jpeg_count > 0) {
                    const info = document.createElement('div');
                    info.className = 'text-sm text-green-400 mt-2 p-2';
                    info.textContent = `${data.jpeg_count} fichiers frame-*.jpg trouvés`;
                    list.appendChild(info);
                }
                if (data.image_count > 0) {
                    const info = document.createElement('div');
                    info.className = 'text-sm text-blue-400 mt-2 p-2';
                    info.textContent = `${data.image_count} images exportées trouvées`;
                    list.appendChild(info);
                }
            } catch (e) { alert('Erreur: ' + e.message); }
        }

        function selectFolder() {
            let inputId;
            if (browserTarget === 'source') inputId = 'sourceDir';
            else if (browserTarget === 'dest') inputId = 'destDir';
            else if (browserTarget === 'videoSource') inputId = 'videoSourceDir';
            else if (browserTarget === 'videoOutput') inputId = 'videoOutputFile';
            else if (browserTarget === 'rotateSource') inputId = 'rotateSourceDir';
            else if (browserTarget === 'developSource') inputId = 'developSourceDir';
            else if (browserTarget === 'developDest') inputId = 'developDestDir';
            else if (browserTarget === 'previewSource') inputId = 'previewSourceDir';
            else if (browserTarget === 'workflowSource') inputId = 'workflowSourceDir';
            else if (browserTarget === 'workflowOutput') inputId = 'workflowOutputDir';
            else inputId = 'sourceDir';

            // For video output, append filename to the selected folder
            if (browserTarget === 'videoOutput') {
                document.getElementById(inputId).value = currentBrowserPath + '/film' + getVideoExtension();
            } else {
                document.getElementById(inputId).value = currentBrowserPath;
            }
            saveSettings();
            closeModal();
            if (browserTarget === 'source') {
                checkSourceDir();
                if (!document.getElementById('destDir').value) {
                    document.getElementById('destDir').value = currentBrowserPath;
                    saveSettings();
                }
            } else if (browserTarget === 'videoSource') {
                checkVideoSourceDir();
                // Auto-fill output filename
                if (!document.getElementById('videoOutputFile').value) {
                    document.getElementById('videoOutputFile').value = currentBrowserPath + '/film' + getVideoExtension();
                    saveSettings();
                }
            } else if (browserTarget === 'rotateSource') {
                checkRotateSourceDir();
            } else if (browserTarget === 'developSource') {
                checkDevelopSourceDir();
                // Auto-fill dest dir
                if (!document.getElementById('developDestDir').value) {
                    document.getElementById('developDestDir').value = currentBrowserPath + '/export';
                    saveSettings();
                }
            } else if (browserTarget === 'previewSource') {
                checkPreviewSourceDir();
            } else if (browserTarget === 'workflowSource') {
                checkWorkflowSourceDir();
                // Auto-fill output dir
                if (!document.getElementById('workflowOutputDir').value) {
                    document.getElementById('workflowOutputDir').value = currentBrowserPath;
                    saveSettings();
                }
            }
        }

        function closeModal() {
            document.getElementById('browserModal').classList.add('hidden');
            document.getElementById('browserModal').classList.remove('flex');
        }

        async function checkSourceDir() {
            const path = document.getElementById('sourceDir').value;
            if (!path) return;
            try {
                const resp = await fetch('/api/browse', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({path})
                });
                const data = await resp.json();
                if (resp.ok && data.jpeg_count > 0) {
                    document.getElementById('sourceInfo').textContent = `${data.jpeg_count} fichiers frame-*.jpg trouvés`;
                    document.getElementById('sourceInfo').className = 'text-sm text-green-400 mt-1';
                } else {
                    document.getElementById('sourceInfo').textContent = 'Aucun fichier frame-*.jpg trouvé';
                    document.getElementById('sourceInfo').className = 'text-sm text-yellow-400 mt-1';
                }
            } catch (e) {}
        }

        async function checkVideoSourceDir() {
            const path = document.getElementById('videoSourceDir').value;
            if (!path) return;
            try {
                const resp = await fetch('/api/browse', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({path})
                });
                const data = await resp.json();
                if (resp.ok && data.image_count > 0) {
                    document.getElementById('videoSourceInfo').textContent = `${data.image_count} images trouvées`;
                    document.getElementById('videoSourceInfo').className = 'text-sm text-green-400 mt-1';
                } else {
                    document.getElementById('videoSourceInfo').textContent = 'Aucune image trouvée';
                    document.getElementById('videoSourceInfo').className = 'text-sm text-yellow-400 mt-1';
                }
            } catch (e) {}
        }

        async function checkRotateSourceDir() {
            const path = document.getElementById('rotateSourceDir').value;
            if (!path) return;
            const filterMode = getSelectedFilterMode();
            try {
                const resp = await fetch('/api/rotate/check', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({source_dir: path, filter_mode: filterMode})
                });
                const data = await resp.json();
                if (resp.ok) {
                    const info = document.getElementById('rotateSourceInfo');
                    if (data.filtered_count > 0) {
                        info.textContent = `${data.filtered_count} images à traiter (${data.total_count} total)`;
                        info.className = 'text-sm text-green-400 mt-1';
                    } else if (data.total_count > 0) {
                        info.textContent = `Aucune image ${filterMode === 'portrait' ? 'portrait' : filterMode === 'landscape' ? 'paysage' : ''} (${data.total_count} total)`;
                        info.className = 'text-sm text-yellow-400 mt-1';
                    } else {
                        info.textContent = 'Aucune image trouvée';
                        info.className = 'text-sm text-yellow-400 mt-1';
                    }
                }
            } catch (e) {}
        }

        // DNG Conversion
        async function toggleConversion() {
            const btn = document.getElementById('actionBtn');
            if (btn.textContent.includes('Arrêter')) {
                await fetch('/api/cancel', {method: 'POST'});
                btn.textContent = 'Annulation...';
                btn.disabled = true;
            } else {
                startConversion();
            }
        }

        async function startConversion() {
            const sourceDir = document.getElementById('sourceDir').value;
            const destDir = document.getElementById('destDir').value;
            const deleteOriginals = document.getElementById('deleteOriginals').checked;
            const bayerPattern = getSelectedBayerPattern();
            const rotation = getSelectedRotation();

            if (!sourceDir || !destDir) {
                alert('Veuillez sélectionner les dossiers source et destination');
                return;
            }
            saveSettings();

            try {
                const resp = await fetch('/api/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({source_dir: sourceDir, dest_dir: destDir, delete_originals: deleteOriginals, bayer_pattern: bayerPattern, rotation: rotation})
                });
                const data = await resp.json();
                if (!resp.ok) {
                    document.getElementById('status').textContent = data.detail || 'Erreur';
                    return;
                }

                document.getElementById('progressSection').classList.remove('hidden');
                document.getElementById('actionBtn').textContent = 'Arrêter';
                document.getElementById('status').textContent = '';

                if (eventSource) eventSource.close();
                eventSource = new EventSource('/api/progress');
                eventSource.onmessage = (e) => updateProgress(JSON.parse(e.data));
                eventSource.onerror = () => { eventSource.close(); checkFinalStatus(); };
            } catch (e) {
                document.getElementById('status').textContent = 'Erreur: ' + e.message;
            }
        }

        function updateProgress(data) {
            const total = data.total_files;
            const done = data.processed_files + data.failed_files;
            const percent = total > 0 ? Math.round(done / total * 100) : 0;
            document.getElementById('progressText').textContent = `${done}/${total} fichiers`;
            document.getElementById('progressPercent').textContent = `${percent}%`;
            document.getElementById('progressBar').style.width = `${percent}%`;
            document.getElementById('currentFile').textContent = data.current_file ? `En cours: ${data.current_file}` : '';
            if (!data.is_running) finishConversion(data);
        }

        function finishConversion(data) {
            if (eventSource) eventSource.close();
            const btn = document.getElementById('actionBtn');
            btn.textContent = 'Démarrer conversion DNG';
            btn.disabled = false;
            if (data.cancel_requested) {
                document.getElementById('status').textContent = `Annulé - ${data.processed_files} fichiers traités`;
            } else if (data.failed_files > 0) {
                document.getElementById('status').textContent = `Terminé - ${data.failed_files} erreur(s)`;
            } else {
                document.getElementById('status').textContent = 'Conversion terminée !';
                document.getElementById('progressBar').style.width = '100%';
            }
        }

        async function checkFinalStatus() {
            try {
                const resp = await fetch('/api/status');
                updateProgress(await resp.json());
            } catch (e) {}
        }

        // Video Export
        async function toggleVideoExport() {
            const btn = document.getElementById('videoActionBtn');
            if (btn.textContent.includes('Arrêter')) {
                await fetch('/api/video/cancel', {method: 'POST'});
                btn.textContent = 'Annulation...';
                btn.disabled = true;
            } else {
                startVideoExport();
            }
        }

        async function startVideoExport() {
            const sourceDir = document.getElementById('videoSourceDir').value;
            const outputFile = document.getElementById('videoOutputFile').value;
            const fps = getSelectedFps();
            const codec = getSelectedVideoCodec();

            if (!sourceDir || !outputFile) {
                alert('Veuillez remplir tous les champs');
                return;
            }
            saveSettings();

            try {
                const resp = await fetch('/api/video/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({source_dir: sourceDir, output_file: outputFile, fps: fps, codec: codec})
                });
                const data = await resp.json();
                if (!resp.ok) {
                    document.getElementById('videoStatus').textContent = data.detail || 'Erreur';
                    return;
                }

                document.getElementById('videoProgressSection').classList.remove('hidden');
                document.getElementById('videoActionBtn').textContent = 'Arrêter';
                document.getElementById('videoStatus').textContent = '';

                if (videoEventSource) videoEventSource.close();
                videoEventSource = new EventSource('/api/video/progress');
                videoEventSource.onmessage = (e) => updateVideoProgress(JSON.parse(e.data));
                videoEventSource.onerror = () => { videoEventSource.close(); checkVideoFinalStatus(); };
            } catch (e) {
                document.getElementById('videoStatus').textContent = 'Erreur: ' + e.message;
            }
        }

        function updateVideoProgress(data) {
            const percent = data.progress || 0;
            document.getElementById('videoProgressText').textContent = `${percent}%`;
            document.getElementById('videoFrameInfo').textContent = data.total_frames ? `${data.current_frame || 0}/${data.total_frames} frames` : '';
            document.getElementById('videoProgressBar').style.width = `${percent}%`;
            document.getElementById('videoCurrentStatus').textContent = data.status || '';
            if (!data.is_running) finishVideoExport(data);
        }

        function finishVideoExport(data) {
            if (videoEventSource) videoEventSource.close();
            const btn = document.getElementById('videoActionBtn');
            btn.textContent = 'Exporter en vidéo MOV';
            btn.disabled = false;
            if (data.cancel_requested) {
                document.getElementById('videoStatus').textContent = 'Export annulé';
            } else if (data.error) {
                document.getElementById('videoStatus').textContent = `Erreur: ${data.error}`;
            } else {
                document.getElementById('videoStatus').textContent = 'Export terminé !';
                document.getElementById('videoProgressBar').style.width = '100%';
            }
        }

        async function checkVideoFinalStatus() {
            try {
                const resp = await fetch('/api/video/status');
                updateVideoProgress(await resp.json());
            } catch (e) {}
        }

        // Rotate Images
        let rotateEventSource = null;

        async function toggleRotate() {
            const btn = document.getElementById('rotateActionBtn');
            if (btn.textContent.includes('Arrêter')) {
                await fetch('/api/rotate/cancel', {method: 'POST'});
                btn.textContent = 'Annulation...';
                btn.disabled = true;
            } else {
                startRotate();
            }
        }

        async function startRotate() {
            const sourceDir = document.getElementById('rotateSourceDir').value;
            const filterMode = getSelectedFilterMode();
            const rotateAngle = getSelectedRotateAngle();

            if (!sourceDir) {
                alert('Veuillez sélectionner un dossier');
                return;
            }
            saveSettings();

            try {
                const resp = await fetch('/api/rotate/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({source_dir: sourceDir, filter_mode: filterMode, rotation: rotateAngle})
                });
                const data = await resp.json();
                if (!resp.ok) {
                    document.getElementById('rotateStatus').textContent = data.detail || 'Erreur';
                    return;
                }

                document.getElementById('rotateProgressSection').classList.remove('hidden');
                document.getElementById('rotateActionBtn').textContent = 'Arrêter';
                document.getElementById('rotateStatus').textContent = '';

                if (rotateEventSource) rotateEventSource.close();
                rotateEventSource = new EventSource('/api/rotate/progress');
                rotateEventSource.onmessage = (e) => updateRotateProgress(JSON.parse(e.data));
                rotateEventSource.onerror = () => { rotateEventSource.close(); checkRotateFinalStatus(); };
            } catch (e) {
                document.getElementById('rotateStatus').textContent = 'Erreur: ' + e.message;
            }
        }

        function updateRotateProgress(data) {
            const total = data.total_files;
            const done = data.processed_files;
            const percent = total > 0 ? Math.round(done / total * 100) : 0;
            document.getElementById('rotateProgressText').textContent = `${done}/${total} fichiers`;
            document.getElementById('rotateProgressPercent').textContent = `${percent}%`;
            document.getElementById('rotateProgressBar').style.width = `${percent}%`;
            document.getElementById('rotateCurrentFile').textContent = data.current_file ? `En cours: ${data.current_file}` : '';
            if (!data.is_running) finishRotate(data);
        }

        function finishRotate(data) {
            if (rotateEventSource) rotateEventSource.close();
            const btn = document.getElementById('rotateActionBtn');
            btn.textContent = 'Appliquer la rotation';
            btn.disabled = false;
            if (data.cancel_requested) {
                document.getElementById('rotateStatus').textContent = `Annulé - ${data.processed_files} fichiers traités`;
            } else if (data.error) {
                document.getElementById('rotateStatus').textContent = `Erreur: ${data.error}`;
            } else {
                document.getElementById('rotateStatus').textContent = `Terminé ! ${data.processed_files} images modifiées`;
                document.getElementById('rotateProgressBar').style.width = '100%';
            }
        }

        async function checkRotateFinalStatus() {
            try {
                const resp = await fetch('/api/rotate/status');
                updateRotateProgress(await resp.json());
            } catch (e) {}
        }

        // Initialize rotate source check
        checkRotateSourceDir();

        // === Darktable Develop Functions ===
        let developEventSource = null;

        async function loadDarktableStyles() {
            try {
                const resp = await fetch('/api/develop/styles');
                const data = await resp.json();
                const select = document.getElementById('developStyle');
                // Keep the first "Aucun" option
                while (select.options.length > 1) select.remove(1);
                for (const style of data.styles) {
                    const opt = document.createElement('option');
                    opt.value = style;
                    opt.textContent = style;
                    select.appendChild(opt);
                }
                // Restore saved selection
                const saved = localStorage.getItem(STORAGE_KEY);
                if (saved) {
                    const s = JSON.parse(saved);
                    if (s.developStyle) select.value = s.developStyle;
                }
            } catch (e) {
                console.error('Erreur chargement styles:', e);
            }
        }

        async function checkDevelopSourceDir() {
            const sourceDir = document.getElementById('developSourceDir').value;
            if (!sourceDir) {
                document.getElementById('developSourceInfo').textContent = '';
                return;
            }
            try {
                const resp = await fetch('/api/develop/check', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({path: sourceDir})
                });
                const data = await resp.json();
                if (resp.ok) {
                    document.getElementById('developSourceInfo').textContent = `${data.dng_count} DNG, ${data.raw_count} RAW (jpg/tiff) trouvés`;
                }
            } catch (e) {
                document.getElementById('developSourceInfo').textContent = '';
            }
        }

        async function toggleDevelop() {
            console.log('toggleDevelop called');
            try {
                const resp = await fetch('/api/develop/status');
                const status = await resp.json();
                console.log('develop status:', status);

                if (status.is_running) {
                    // Cancel
                    await fetch('/api/develop/cancel', {method: 'POST'});
                    return;
                }

                // Start develop - with null checks
                const srcEl = document.getElementById('developSourceDir');
                const destEl = document.getElementById('developDestDir');
                const styleEl = document.getElementById('developStyle');

                if (!srcEl || !destEl || !styleEl) {
                    console.error('Missing elements:', {srcEl: !!srcEl, destEl: !!destEl, styleEl: !!styleEl});
                    alert('Erreur: éléments du formulaire introuvables. Rafraîchissez la page.');
                    return;
                }

                const sourceDir = srcEl.value;
                const destDir = destEl.value;
                const style = styleEl.value;
                const quality = getSelectedDevelopQuality();
                console.log('develop params:', {sourceDir, destDir, style, quality});

                if (!sourceDir || !destDir) {
                    alert('Veuillez remplir tous les champs (source et destination)');
                    return;
                }
                saveSettings();

                const resp2 = await fetch('/api/develop/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({source_dir: sourceDir, dest_dir: destDir, style: style, quality: quality})
                });
                const data = await resp2.json();
                console.log('develop start response:', data);
                if (!resp2.ok) {
                    document.getElementById('developStatus').textContent = data.detail || 'Erreur';
                    return;
                }

                document.getElementById('developProgressSection').classList.remove('hidden');
                document.getElementById('developActionBtn').textContent = 'Arrêter';
                document.getElementById('developStatus').textContent = '';

                if (developEventSource) developEventSource.close();
                developEventSource = new EventSource('/api/develop/progress');
                developEventSource.onmessage = (e) => updateDevelopProgress(JSON.parse(e.data));
                developEventSource.onerror = () => { developEventSource.close(); checkDevelopFinalStatus(); };
            } catch (e) {
                console.error('toggleDevelop error:', e);
                document.getElementById('developStatus').textContent = 'Erreur: ' + e.message;
            }
        }

        function updateDevelopProgress(data) {
            const percent = data.total_files > 0 ? Math.round(data.processed_files / data.total_files * 100) : 0;
            document.getElementById('developProgressText').textContent = `${data.processed_files}/${data.total_files} fichiers`;
            document.getElementById('developProgressPercent').textContent = `${percent}%`;
            document.getElementById('developProgressBar').style.width = `${percent}%`;
            document.getElementById('developCurrentFile').textContent = data.current_file || '';
            if (!data.is_running) finishDevelop(data);
        }

        function finishDevelop(data) {
            if (developEventSource) developEventSource.close();
            const btn = document.getElementById('developActionBtn');
            btn.textContent = 'Développer avec Darktable';
            btn.disabled = false;
            if (data.cancel_requested) {
                document.getElementById('developStatus').textContent = `Annulé - ${data.processed_files} fichiers traités`;
            } else if (data.error) {
                document.getElementById('developStatus').textContent = `Erreur: ${data.error}`;
            } else {
                document.getElementById('developStatus').textContent = `Terminé ! ${data.processed_files} images exportées`;
                document.getElementById('developProgressBar').style.width = '100%';
            }
        }

        async function checkDevelopFinalStatus() {
            try {
                const resp = await fetch('/api/develop/status');
                updateDevelopProgress(await resp.json());
            } catch (e) {}
        }

        // Event listeners for develop tab (with null checks)
        const devSrcDir = document.getElementById('developSourceDir');
        const devDestDir = document.getElementById('developDestDir');
        const devStyle = document.getElementById('developStyle');
        if (devSrcDir) devSrcDir.addEventListener('change', () => { saveSettings(); checkDevelopSourceDir(); });
        if (devDestDir) devDestDir.addEventListener('change', saveSettings);
        if (devStyle) devStyle.addEventListener('change', saveSettings);
        document.querySelectorAll('input[name="developQuality"]').forEach(r => r.addEventListener('change', saveSettings));

        // === Preview Functions ===
        async function loadPreviewStyles() {
            try {
                const resp = await fetch('/api/develop/styles');
                const data = await resp.json();
                const select = document.getElementById('previewStyle');
                while (select.options.length > 1) select.remove(1);
                for (const style of data.styles) {
                    const opt = document.createElement('option');
                    opt.value = style;
                    opt.textContent = style;
                    select.appendChild(opt);
                }
                // Restore saved selection
                const saved = localStorage.getItem(STORAGE_KEY);
                if (saved) {
                    const s = JSON.parse(saved);
                    if (s.previewStyle) select.value = s.previewStyle;
                }
            } catch (e) {
                console.error('Erreur chargement styles preview:', e);
            }
        }

        async function checkPreviewSourceDir() {
            const sourceDir = document.getElementById('previewSourceDir').value;
            if (!sourceDir) {
                document.getElementById('previewSourceInfo').textContent = '';
                return;
            }
            try {
                const resp = await fetch('/api/preview/check', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({path: sourceDir})
                });
                const data = await resp.json();
                if (resp.ok) {
                    document.getElementById('previewSourceInfo').textContent = `${data.count} fichiers frame-*.jpg trouvés`;
                }
            } catch (e) {
                document.getElementById('previewSourceInfo').textContent = '';
            }
        }

        async function generatePreview(bayerPattern) {
            const sourceDir = document.getElementById('previewSourceDir').value;
            const style = document.getElementById('previewStyle').value;

            if (!sourceDir) {
                alert('Veuillez sélectionner un dossier source');
                return;
            }

            // Show loading
            document.getElementById('previewLoading').classList.remove('hidden');
            document.getElementById('previewResults').classList.add('hidden');
            document.getElementById('previewStatus').textContent = '';
            document.getElementById('previewLoadingText').textContent = `Génération aperçus ${bayerPattern.toUpperCase()}...`;

            try {
                const resp = await fetch('/api/preview/generate', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        source_dir: sourceDir,
                        bayer_pattern: bayerPattern === 'rggb' ? 2 : 0,
                        style: style,
                        count: 5
                    })
                });

                const data = await resp.json();
                document.getElementById('previewLoading').classList.add('hidden');

                if (!resp.ok) {
                    document.getElementById('previewStatus').textContent = data.detail || 'Erreur';
                    return;
                }

                // Display previews
                const grid = document.getElementById('previewGrid');
                grid.innerHTML = '';

                for (const preview of data.previews) {
                    const div = document.createElement('div');
                    div.className = 'relative cursor-pointer group';
                    div.innerHTML = `
                        <img src="${preview.url}" alt="${preview.name}"
                            class="w-full h-32 object-cover rounded border border-gray-600 group-hover:border-yellow-500 transition"
                            onclick="showFullPreview('${preview.url}', '${preview.name}')">
                        <p class="text-xs text-gray-400 mt-1 truncate">${preview.name}</p>
                        <span class="absolute top-1 right-1 bg-${bayerPattern === 'rggb' ? 'yellow' : 'orange'}-600 text-xs px-1 rounded">${bayerPattern.toUpperCase()}</span>
                    `;
                    grid.appendChild(div);
                }

                document.getElementById('previewResults').classList.remove('hidden');
                document.getElementById('previewStatus').textContent = `${data.previews.length} aperçus générés (${bayerPattern.toUpperCase()}${style ? ' + ' + style : ''})`;

            } catch (e) {
                document.getElementById('previewLoading').classList.add('hidden');
                document.getElementById('previewStatus').textContent = 'Erreur: ' + e.message;
            }
        }

        function showFullPreview(url, name) {
            // Create modal for full preview
            const modal = document.createElement('div');
            modal.className = 'fixed inset-0 bg-black/90 flex items-center justify-center z-50 cursor-pointer';
            modal.onclick = () => modal.remove();
            modal.innerHTML = `
                <div class="max-w-[95vw] max-h-[95vh] relative">
                    <img src="${url}" alt="${name}" class="max-w-full max-h-[90vh] object-contain">
                    <p class="text-center text-white mt-2">${name}</p>
                    <button class="absolute top-2 right-2 text-white text-3xl hover:text-yellow-400">&times;</button>
                </div>
            `;
            document.body.appendChild(modal);
        }

        // Event listeners for preview tab (if elements exist)
        const prevSrcDir = document.getElementById('previewSourceDir');
        const prevStyle = document.getElementById('previewStyle');
        if (prevSrcDir) prevSrcDir.addEventListener('change', () => { saveSettings(); checkPreviewSourceDir(); });
        if (prevStyle) prevStyle.addEventListener('change', saveSettings);

        // === Workflow Functions ===
        async function loadWorkflowStyles() {
            try {
                const resp = await fetch('/api/develop/styles');
                const data = await resp.json();
                const select = document.getElementById('workflowStyle');
                while (select.options.length > 1) select.remove(1);
                for (const style of data.styles) {
                    const opt = document.createElement('option');
                    opt.value = style;
                    opt.textContent = style;
                    select.appendChild(opt);
                }
            } catch (e) {
                console.error('Erreur chargement styles workflow:', e);
            }
        }

        async function checkWorkflowSourceDir() {
            const sourceDir = document.getElementById('workflowSourceDir').value;
            if (!sourceDir) {
                document.getElementById('workflowSourceInfo').textContent = '';
                return;
            }
            try {
                const resp = await fetch('/api/preview/check', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({path: sourceDir})
                });
                const data = await resp.json();
                if (resp.ok) {
                    document.getElementById('workflowSourceInfo').textContent = `${data.count} fichiers frame-*.jpg trouvés`;
                }
            } catch (e) {
                document.getElementById('workflowSourceInfo').textContent = '';
            }
        }

        function getWorkflowBayer() {
            const radio = document.querySelector('input[name="workflowBayer"]:checked');
            return radio ? parseInt(radio.value) : 2;
        }

        async function generateWorkflowPreview(bayerType) {
            const sourceDir = document.getElementById('workflowSourceDir').value;
            const style = document.getElementById('workflowStyle').value;
            const bayer = bayerType === 'rggb' ? 2 : 0;

            if (!sourceDir) {
                alert('Veuillez sélectionner un dossier source');
                return;
            }

            document.getElementById('workflowPreviewLoading').classList.remove('hidden');
            document.getElementById('workflowPreviewGrid').classList.add('hidden');
            document.getElementById('workflowPreviewStatus').textContent = '';

            try {
                const resp = await fetch('/api/preview/generate', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        source_dir: sourceDir,
                        bayer_pattern: bayer,
                        style: style,
                        count: 5
                    })
                });

                const data = await resp.json();
                document.getElementById('workflowPreviewLoading').classList.add('hidden');

                if (!resp.ok) {
                    document.getElementById('workflowPreviewStatus').textContent = data.detail || 'Erreur';
                    return;
                }

                const grid = document.getElementById('workflowPreviewGrid');
                grid.innerHTML = '';
                grid.classList.remove('hidden');

                for (const preview of data.previews) {
                    const div = document.createElement('div');
                    div.className = 'cursor-pointer';
                    div.innerHTML = `<img src="${preview.url}" alt="${preview.name}"
                        class="w-full h-20 object-cover rounded border border-gray-600 hover:border-blue-500"
                        onclick="showFullPreview('${preview.url}', '${preview.name}')">`;
                    grid.appendChild(div);
                }

                document.getElementById('workflowPreviewStatus').textContent =
                    `${bayerType.toUpperCase()}${style ? ' + ' + style : ''} - Cliquer pour agrandir`;

                // Update bayer selection to match preview
                document.querySelector(`input[name="workflowBayer"][value="${bayer}"]`).checked = true;

            } catch (e) {
                document.getElementById('workflowPreviewLoading').classList.add('hidden');
                document.getElementById('workflowPreviewStatus').textContent = 'Erreur: ' + e.message;
            }
        }

        async function startWorkflow() {
            const sourceDir = document.getElementById('workflowSourceDir').value;
            const outputDir = document.getElementById('workflowOutputDir').value;
            const bayer = getWorkflowBayer();
            const style = document.getElementById('workflowStyle').value;
            const fps = parseInt(document.getElementById('workflowFps').value);
            const codec = document.getElementById('workflowCodec').value;

            if (!sourceDir || !outputDir) {
                alert('Veuillez remplir les dossiers source et sortie');
                return;
            }

            // Check if already running
            const statusResp = await fetch('/api/workflow/status');
            const status = await statusResp.json();
            if (status.is_running) {
                // Cancel
                await fetch('/api/workflow/cancel', {method: 'POST'});
                return;
            }

            document.getElementById('workflowProgressSection').classList.remove('hidden');
            document.getElementById('workflowActionBtn').textContent = 'Arrêter';
            document.getElementById('workflowActionBtn').classList.remove('bg-green-600', 'hover:bg-green-500');
            document.getElementById('workflowActionBtn').classList.add('bg-red-600', 'hover:bg-red-500');
            document.getElementById('workflowStatus').textContent = '';

            try {
                const resp = await fetch('/api/workflow/start', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        source_dir: sourceDir,
                        output_dir: outputDir,
                        bayer_pattern: bayer,
                        style: style,
                        fps: fps,
                        codec: codec
                    })
                });

                if (!resp.ok) {
                    const data = await resp.json();
                    document.getElementById('workflowStatus').textContent = data.detail || 'Erreur';
                    resetWorkflowBtn();
                    return;
                }

                if (workflowEventSource) workflowEventSource.close();
                workflowEventSource = new EventSource('/api/workflow/progress');
                workflowEventSource.onmessage = (e) => updateWorkflowProgress(JSON.parse(e.data));
                workflowEventSource.onerror = () => { workflowEventSource.close(); checkWorkflowFinalStatus(); };

            } catch (e) {
                document.getElementById('workflowStatus').textContent = 'Erreur: ' + e.message;
                resetWorkflowBtn();
            }
        }

        function updateWorkflowProgress(data) {
            const stepLabels = {
                'dng': 'Étape 1/3: Conversion DNG',
                'develop': 'Étape 2/3: Développement Darktable',
                'video': 'Étape 3/3: Export vidéo',
                'done': 'Terminé !'
            };

            document.getElementById('workflowStepLabel').textContent = stepLabels[data.step] || data.step;
            document.getElementById('workflowStepProgress').textContent = `${data.progress}%`;
            document.getElementById('workflowProgressBar').style.width = `${data.progress}%`;
            document.getElementById('workflowCurrentFile').textContent = data.current_file || '';

            // Color based on step
            const bar = document.getElementById('workflowProgressBar');
            bar.classList.remove('bg-blue-500', 'bg-purple-500', 'bg-green-500');
            if (data.step === 'dng') bar.classList.add('bg-blue-500');
            else if (data.step === 'develop') bar.classList.add('bg-purple-500');
            else bar.classList.add('bg-green-500');

            if (!data.is_running) finishWorkflow(data);
        }

        function finishWorkflow(data) {
            if (workflowEventSource) workflowEventSource.close();
            resetWorkflowBtn();

            if (data.cancel_requested) {
                document.getElementById('workflowStatus').textContent = 'Annulé';
            } else if (data.error) {
                document.getElementById('workflowStatus').textContent = `Erreur: ${data.error}`;
            } else {
                document.getElementById('workflowStatus').textContent = 'Traitement terminé !';
                document.getElementById('workflowProgressBar').style.width = '100%';
            }
        }

        function resetWorkflowBtn() {
            const btn = document.getElementById('workflowActionBtn');
            btn.textContent = 'Lancer le traitement complet';
            btn.classList.remove('bg-red-600', 'hover:bg-red-500');
            btn.classList.add('bg-green-600', 'hover:bg-green-500');
        }

        async function checkWorkflowFinalStatus() {
            try {
                const resp = await fetch('/api/workflow/status');
                updateWorkflowProgress(await resp.json());
            } catch (e) {}
        }

        // Event listeners for workflow tab
        document.getElementById('workflowSourceDir').addEventListener('change', () => { saveSettings(); checkWorkflowSourceDir(); });
        document.getElementById('workflowOutputDir').addEventListener('change', saveSettings);
        document.getElementById('workflowStyle').addEventListener('change', saveSettings);
        document.getElementById('workflowFps').addEventListener('change', saveSettings);
        document.getElementById('workflowCodec').addEventListener('change', saveSettings);

        // Initialize workflow
        loadWorkflowStyles();
        checkWorkflowSourceDir();
    </script>
</body>
</html>"""


@app.post("/api/browse")
async def browse_directory(request: BrowseRequest):
    """List contents of a directory."""
    path = Path(request.path)

    if not path.exists():
        raise HTTPException(status_code=404, detail="Chemin introuvable")
    if not path.is_dir():
        raise HTTPException(status_code=400, detail="Ce n'est pas un dossier")

    entries = []
    try:
        for entry in sorted(path.iterdir()):
            if entry.is_dir() and not entry.name.startswith('.'):
                entries.append({"name": entry.name, "path": str(entry)})
    except PermissionError:
        raise HTTPException(status_code=403, detail="Accès refusé")

    jpeg_count = len(list(path.glob("frame-*.jpg"))) + len(list(path.glob("frame_*.jpg")))

    # Count exported images (jpg, png, tiff from darktable export)
    image_count = 0
    for ext in ['*.jpg', '*.jpeg', '*.png', '*.tif', '*.tiff']:
        image_count += len([f for f in path.glob(ext) if not f.name.startswith('frame-')])

    return {
        "current_path": str(path),
        "parent_path": str(path.parent) if path.parent != path else None,
        "entries": entries,
        "jpeg_count": jpeg_count,
        "image_count": image_count
    }


@app.post("/api/start")
async def start_conversion(request: ConversionRequest):
    """Start the conversion process."""
    global conversion_state

    if conversion_state["is_running"]:
        raise HTTPException(status_code=400, detail="Une conversion est déjà en cours")

    source_path = Path(request.source_dir)
    dest_path = Path(request.dest_dir)

    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Dossier source introuvable")

    jpeg_files = sorted(list(source_path.glob("frame-*.jpg")) + list(source_path.glob("frame_*.jpg")))
    if not jpeg_files:
        raise HTTPException(status_code=400, detail="Aucun fichier frame-*.jpg ou frame_*.jpg trouvé")

    dest_path.mkdir(parents=True, exist_ok=True)

    # Reset state
    conversion_state = {
        "is_running": True,
        "cancel_requested": False,
        "total_files": len(jpeg_files),
        "processed_files": 0,
        "failed_files": 0,
        "current_file": "",
        "errors": [],
    }

    # Start conversion in background
    asyncio.create_task(run_conversion(jpeg_files, str(dest_path), request.delete_originals, request.bayer_pattern, request.rotation))

    return {"total_files": len(jpeg_files)}


async def run_conversion(jpeg_files: list[Path], dest_dir: str, delete: bool, bayer_pattern: int = 2, rotation: int = 0):
    """Run the conversion in background."""
    global conversion_state

    loop = asyncio.get_event_loop()

    with ProcessPoolExecutor(max_workers=2) as executor:
        for jpg_path in jpeg_files:
            if conversion_state["cancel_requested"]:
                break

            conversion_state["current_file"] = jpg_path.name

            try:
                success, error = await loop.run_in_executor(
                    executor, convert_single_file, str(jpg_path), dest_dir, delete, bayer_pattern, rotation
                )
                if success:
                    conversion_state["processed_files"] += 1
                else:
                    conversion_state["failed_files"] += 1
                    conversion_state["errors"].append(f"{jpg_path.name}: {error}")
            except Exception as e:
                conversion_state["failed_files"] += 1
                conversion_state["errors"].append(f"{jpg_path.name}: {e}")

    conversion_state["is_running"] = False
    conversion_state["current_file"] = ""


@app.get("/api/progress")
async def progress_stream():
    """Stream progress updates via SSE."""
    async def generate() -> AsyncGenerator[str, None]:
        while conversion_state["is_running"]:
            data = json.dumps(conversion_state)
            yield f"data: {data}\n\n"
            await asyncio.sleep(0.3)
        # Send final state
        yield f"data: {json.dumps(conversion_state)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/status")
async def get_status():
    """Get current conversion status."""
    return conversion_state


@app.post("/api/cancel")
async def cancel_conversion():
    """Cancel the running conversion."""
    global conversion_state
    if not conversion_state["is_running"]:
        raise HTTPException(status_code=400, detail="Aucune conversion en cours")
    conversion_state["cancel_requested"] = True
    return {"message": "Annulation demandée"}


# ============================================================================
# Video Export API
# ============================================================================

ffmpeg_process = None


def find_images(source_dir: Path) -> tuple[list[Path], str]:
    """Find image sequence in directory. Returns (files, pattern)."""
    # Try different patterns - check both dash and underscore versions
    patterns = [
        ('frame-*.jpg', 'frame-%05d.jpg'),
        ('frame_*.jpg', 'frame_%05d.jpg'),
        ('frame-*.jpeg', 'frame-%05d.jpeg'),
        ('frame_*.jpeg', 'frame_%05d.jpeg'),
        ('frame-*.png', 'frame-%05d.png'),
        ('frame_*.png', 'frame_%05d.png'),
        ('*.jpg', '%05d.jpg'),
        ('*.jpeg', '%05d.jpeg'),
        ('*.png', '%05d.png'),
        ('*.tif', '%05d.tif'),
        ('*.tiff', '%05d.tiff'),
    ]

    for glob_pattern, ffmpeg_pattern in patterns:
        files = sorted(source_dir.glob(glob_pattern))
        if files:
            # Check if files have sequential numbering
            return files, ffmpeg_pattern

    return [], ''


@app.post("/api/video/start")
async def start_video_export(request: VideoExportRequest):
    """Start video export with ffmpeg."""
    global video_export_state, ffmpeg_process

    if video_export_state["is_running"]:
        raise HTTPException(status_code=400, detail="Un export est déjà en cours")

    source_path = Path(request.source_dir)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Dossier source introuvable")

    files, pattern = find_images(source_path)
    if not files:
        raise HTTPException(status_code=400, detail="Aucune image trouvée")

    # Reset state
    video_export_state = {
        "is_running": True,
        "cancel_requested": False,
        "progress": 0,
        "total_frames": len(files),
        "current_frame": 0,
        "status": "Démarrage...",
        "error": "",
    }

    # Start ffmpeg in background
    asyncio.create_task(run_ffmpeg_export(
        source_path, request.output_file, request.fps, files, pattern, request.codec
    ))

    return {"total_frames": len(files)}


async def run_ffmpeg_export(source_dir: Path, output_file: str, fps: int, files: list[Path], pattern: str, codec: str = "h265"):
    """Run ffmpeg to create video."""
    global video_export_state, ffmpeg_process
    import subprocess
    import re
    import tempfile
    import time

    # Debug log file
    debug_log = Path("/tmp/ffmpeg_debug.log")

    def log_debug(msg: str):
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}"
        print(line)
        with open(debug_log, 'a') as f:
            f.write(line + "\n")

    # Clear old debug log
    with open(debug_log, 'w') as f:
        f.write(f"=== FFmpeg Debug Log - {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")

    log_debug(f"Starting export: {len(files)} frames, {fps} fps, codec={codec}")
    log_debug(f"Output file: {output_file}")

    try:
        # Create a concat file with all images (handles non-sequential numbering)
        concat_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        frame_duration = 1.0 / fps

        for f in files:
            # Escape single quotes in path
            escaped_path = str(f).replace("'", "'\\''")
            concat_file.write(f"file '{escaped_path}'\n")
            concat_file.write(f"duration {frame_duration}\n")

        # Add last file again (ffmpeg concat demuxer quirk)
        escaped_path = str(files[-1]).replace("'", "'\\''")
        concat_file.write(f"file '{escaped_path}'\n")
        concat_file.close()

        log_debug(f"Concat file created: {concat_file.name}")

        # Create progress file for ffmpeg output
        progress_file = tempfile.NamedTemporaryFile(mode='w', suffix='_progress.txt', delete=False)
        progress_file.close()

        log_debug(f"Progress file: {progress_file.name}")

        # Build ffmpeg command based on codec choice
        if codec == "dnxhr":
            # DNxHR for professional editing (Resolve, Kdenlive, etc.)
            cmd = [
                'ffmpeg', '-y',
                '-f', 'concat',
                '-safe', '0',
                '-r', str(fps),             # Force constant input framerate
                '-i', concat_file.name,
                '-vf', 'format=yuv422p',    # Force consistent pixel format (avoids reconfiguring)
                '-r', str(fps),             # Force constant output framerate (CFR)
                '-vsync', 'cfr',            # Constant frame rate mode
                '-c:v', 'dnxhd',
                '-profile:v', 'dnxhr_hq',   # High quality DNxHR
                '-progress', progress_file.name,
                output_file
            ]
        else:
            # H.265 NVENC (GPU) - default, good for archiving and playback
            cmd = [
                'ffmpeg', '-y',
                '-f', 'concat',
                '-safe', '0',
                '-r', str(fps),             # Force constant input framerate
                '-i', concat_file.name,
                '-vf', 'format=yuv420p',    # Force consistent pixel format (avoids reconfiguring)
                '-r', str(fps),             # Force constant output framerate (CFR)
                '-vsync', 'cfr',            # Constant frame rate mode
                '-c:v', 'hevc_nvenc',       # NVIDIA GPU encoding
                '-preset', 'p4',            # Balanced quality/speed
                '-cq', '18',                # High quality (lower = better, 18-23 is good)
                '-bf', '0',                 # Disable B-frames to prevent freezing
                '-progress', progress_file.name,
                output_file
            ]

        log_debug(f"FFmpeg command: {' '.join(cmd)}")

        video_export_state["status"] = "Encodage en cours..."

        # Create stderr log file
        stderr_log = Path("/tmp/ffmpeg_stderr.log")
        stderr_file = open(stderr_log, 'w')

        ffmpeg_process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=stderr_file
        )

        log_debug(f"FFmpeg started with PID: {ffmpeg_process.pid}")

        total_frames = len(files)
        last_frame = 0
        last_progress_time = time.time()
        stall_count = 0

        while ffmpeg_process.returncode is None:
            loop_start = time.time()

            # Check if process finished
            try:
                await asyncio.wait_for(ffmpeg_process.wait(), timeout=1.0)
                log_debug(f"FFmpeg process finished with returncode: {ffmpeg_process.returncode}")
            except asyncio.TimeoutError:
                pass

            # Read progress file
            current_frame = last_frame
            try:
                with open(progress_file.name, 'r') as f:
                    content = f.read()
                    # Find the last frame= line
                    matches = re.findall(r'frame=(\d+)', content)
                    if matches:
                        current_frame = int(matches[-1])
            except Exception as e:
                log_debug(f"Error reading progress file: {e}")

            # Check for progress
            if current_frame > last_frame:
                frames_per_sec = (current_frame - last_frame) / (time.time() - last_progress_time)
                log_debug(f"Frame {current_frame}/{total_frames} ({current_frame*100//total_frames}%) - {frames_per_sec:.1f} fps")
                last_frame = current_frame
                last_progress_time = time.time()
                stall_count = 0
                video_export_state["current_frame"] = current_frame
                video_export_state["progress"] = min(100, int(current_frame / total_frames * 100))
            else:
                stall_count += 1
                if stall_count % 10 == 0:  # Log every 10 seconds of stall
                    log_debug(f"WARNING: No progress for {stall_count} seconds! Last frame: {last_frame}")
                    # Check if process is still alive
                    if ffmpeg_process.returncode is None:
                        log_debug(f"FFmpeg PID {ffmpeg_process.pid} is still running")
                        # Read last stderr lines
                        try:
                            stderr_file.flush()
                            with open(stderr_log, 'r') as f:
                                lines = f.readlines()
                                if lines:
                                    last_lines = lines[-5:] if len(lines) >= 5 else lines
                                    log_debug(f"Last stderr: {''.join(last_lines)}")
                        except Exception as e:
                            log_debug(f"Error reading stderr: {e}")
                    else:
                        log_debug(f"FFmpeg has terminated! returncode={ffmpeg_process.returncode}")
                        break

                # If stalled for 60 seconds, try to diagnose
                if stall_count >= 60:
                    log_debug("CRITICAL: Stalled for 60+ seconds, attempting to get diagnostics...")
                    try:
                        import subprocess as sp
                        # Get process status
                        proc_status = sp.run(['ps', '-p', str(ffmpeg_process.pid), '-o', 'state,wchan'],
                                           capture_output=True, text=True)
                        log_debug(f"Process status: {proc_status.stdout}")

                        # Get open files
                        lsof_result = sp.run(['lsof', '-p', str(ffmpeg_process.pid)],
                                           capture_output=True, text=True)
                        log_debug(f"Open files (last 10): {lsof_result.stdout.splitlines()[-10:]}")
                    except Exception as e:
                        log_debug(f"Diagnostics error: {e}")

            if video_export_state["cancel_requested"]:
                log_debug("Cancel requested, terminating ffmpeg...")
                ffmpeg_process.terminate()
                break

            # Small sleep to prevent tight loop
            await asyncio.sleep(0.1)

        # Final wait for process to complete
        log_debug("Waiting for ffmpeg to finish...")
        try:
            await asyncio.wait_for(ffmpeg_process.wait(), timeout=5.0)
            log_debug(f"FFmpeg finished with returncode: {ffmpeg_process.returncode}")
        except asyncio.TimeoutError:
            log_debug("Timeout waiting for ffmpeg, killing...")
            ffmpeg_process.kill()

        # Close stderr file
        stderr_file.close()

        if ffmpeg_process.returncode != 0 and not video_export_state["cancel_requested"]:
            # Read the stderr log
            with open(stderr_log, 'r') as f:
                stderr_content = f.read()
            log_debug(f"FFmpeg failed! Full stderr:\n{stderr_content}")
            video_export_state["error"] = stderr_content[-500:]  # Last 500 chars
        elif not video_export_state["cancel_requested"]:
            video_export_state["progress"] = 100
            video_export_state["status"] = "Terminé !"
            log_debug("Export completed successfully!")

            # Log output file size
            try:
                output_size = Path(output_file).stat().st_size / (1024*1024)
                log_debug(f"Output file size: {output_size:.1f} MB")
            except:
                pass

    except Exception as e:
        log_debug(f"EXCEPTION: {type(e).__name__}: {e}")
        import traceback
        log_debug(traceback.format_exc())
        video_export_state["error"] = str(e)

    finally:
        video_export_state["is_running"] = False
        ffmpeg_process = None
        log_debug("Cleaning up temp files...")
        # Clean up temp files
        try:
            os.unlink(concat_file.name)
        except:
            pass
        try:
            os.unlink(progress_file.name)
        except:
            pass
        log_debug("Export task finished")


@app.get("/api/video/progress")
async def video_progress_stream():
    """Stream video export progress via SSE."""
    async def generate() -> AsyncGenerator[str, None]:
        while video_export_state["is_running"]:
            data = json.dumps(video_export_state)
            yield f"data: {data}\n\n"
            await asyncio.sleep(0.5)
        yield f"data: {json.dumps(video_export_state)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/video/status")
async def get_video_status():
    """Get current video export status."""
    return video_export_state


@app.post("/api/video/cancel")
async def cancel_video_export():
    """Cancel video export."""
    global video_export_state, ffmpeg_process
    if not video_export_state["is_running"]:
        raise HTTPException(status_code=400, detail="Aucun export en cours")
    video_export_state["cancel_requested"] = True
    if ffmpeg_process:
        ffmpeg_process.terminate()
    return {"message": "Annulation demandée"}


# ============================================================================
# Image Rotation API
# ============================================================================

def find_images_for_rotation(source_dir: Path, filter_mode: str) -> tuple[list[Path], int]:
    """Find images to rotate based on filter mode. Returns (filtered_files, total_count)."""
    from PIL import Image

    all_images = []
    for ext in ['*.jpg', '*.jpeg', '*.png', '*.tif', '*.tiff']:
        all_images.extend(source_dir.glob(ext))

    all_images = sorted(all_images)
    total_count = len(all_images)

    if filter_mode == 'all':
        return all_images, total_count

    filtered = []
    for img_path in all_images:
        try:
            with Image.open(img_path) as img:
                width, height = img.size
                is_portrait = height > width
                if filter_mode == 'portrait' and is_portrait:
                    filtered.append(img_path)
                elif filter_mode == 'landscape' and not is_portrait:
                    filtered.append(img_path)
        except Exception:
            pass

    return filtered, total_count


class RotateCheckRequest(BaseModel):
    source_dir: str
    filter_mode: str = "portrait"


@app.post("/api/rotate/check")
async def check_rotate_images(request: RotateCheckRequest):
    """Check how many images match the filter criteria."""
    source_path = Path(request.source_dir)

    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Dossier introuvable")

    filtered_files, total_count = find_images_for_rotation(source_path, request.filter_mode)

    return {
        "filtered_count": len(filtered_files),
        "total_count": total_count
    }


@app.post("/api/rotate/start")
async def start_rotate_images(request: RotateImagesRequest):
    """Start image rotation process."""
    global rotate_state

    if rotate_state["is_running"]:
        raise HTTPException(status_code=400, detail="Une rotation est déjà en cours")

    source_path = Path(request.source_dir)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Dossier source introuvable")

    filtered_files, _ = find_images_for_rotation(source_path, request.filter_mode)
    if not filtered_files:
        raise HTTPException(status_code=400, detail="Aucune image à traiter")

    # Reset state
    rotate_state = {
        "is_running": True,
        "cancel_requested": False,
        "total_files": len(filtered_files),
        "processed_files": 0,
        "current_file": "",
        "error": "",
    }

    # Start rotation in background
    asyncio.create_task(run_rotation(filtered_files, request.rotation))

    return {"total_files": len(filtered_files)}


async def run_rotation(files: list[Path], rotation: int):
    """Run image rotation in background."""
    global rotate_state
    from PIL import Image

    # Map rotation degrees to PIL rotation
    pil_rotation = {
        90: Image.Transpose.ROTATE_270,   # 90° CW = 270° CCW (PIL uses CCW)
        180: Image.Transpose.ROTATE_180,
        270: Image.Transpose.ROTATE_90,   # 270° CW = 90° CCW
    }

    if rotation not in pil_rotation:
        rotate_state["error"] = "Rotation invalide"
        rotate_state["is_running"] = False
        return

    try:
        for img_path in files:
            if rotate_state["cancel_requested"]:
                break

            rotate_state["current_file"] = img_path.name

            try:
                with Image.open(img_path) as img:
                    # Preserve EXIF data if present
                    exif = img.info.get('exif')

                    # Rotate image
                    rotated = img.transpose(pil_rotation[rotation])

                    # Save with same format and quality
                    save_kwargs = {}
                    if exif:
                        save_kwargs['exif'] = exif
                    if img_path.suffix.lower() in ['.jpg', '.jpeg']:
                        save_kwargs['quality'] = 95
                        save_kwargs['optimize'] = True

                    rotated.save(img_path, **save_kwargs)

                rotate_state["processed_files"] += 1

            except Exception as e:
                print(f"Error rotating {img_path.name}: {e}")
                # Continue with next file
                rotate_state["processed_files"] += 1

            await asyncio.sleep(0)  # Yield control

    except Exception as e:
        rotate_state["error"] = str(e)

    finally:
        rotate_state["is_running"] = False
        rotate_state["current_file"] = ""


@app.get("/api/rotate/progress")
async def rotate_progress_stream():
    """Stream rotation progress via SSE."""
    async def generate() -> AsyncGenerator[str, None]:
        while rotate_state["is_running"]:
            data = json.dumps(rotate_state)
            yield f"data: {data}\n\n"
            await asyncio.sleep(0.3)
        yield f"data: {json.dumps(rotate_state)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/api/rotate/status")
async def get_rotate_status():
    """Get current rotation status."""
    return rotate_state


@app.post("/api/rotate/cancel")
async def cancel_rotation():
    """Cancel rotation."""
    global rotate_state
    if not rotate_state["is_running"]:
        raise HTTPException(status_code=400, detail="Aucune rotation en cours")
    rotate_state["cancel_requested"] = True
    return {"message": "Annulation demandée"}


# ============================================================================
# Darktable Develop API
# ============================================================================

DARKTABLE_STYLES_DIR = Path.home() / ".config" / "darktable" / "styles"


@app.get("/api/develop/styles")
async def get_darktable_styles():
    """List available Darktable styles."""
    styles = []
    if DARKTABLE_STYLES_DIR.exists():
        for style_file in sorted(DARKTABLE_STYLES_DIR.glob("*.dtstyle")):
            styles.append(style_file.stem)
    return {"styles": styles}


@app.post("/api/develop/check")
async def check_develop_source(request: BrowseRequest):
    """Check source directory for developable files."""
    path = Path(request.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Dossier introuvable")

    dng_count = len(list(path.glob("*.dng"))) + len(list(path.glob("*.DNG")))
    raw_count = len(list(path.glob("*.tif"))) + len(list(path.glob("*.tiff")))
    # Also count frame-*.jpg and frame_*.jpg as they can be developed
    raw_count += len(list(path.glob("frame-*.jpg")))
    raw_count += len(list(path.glob("frame_*.jpg")))

    return {"dng_count": dng_count, "raw_count": raw_count}


@app.get("/api/develop/status")
async def get_develop_status():
    """Get current develop status."""
    return develop_state


@app.post("/api/develop/start")
async def start_develop(request: DevelopRequest):
    """Start Darktable batch develop process."""
    global develop_state

    if develop_state["is_running"]:
        raise HTTPException(status_code=400, detail="Un développement est déjà en cours")

    source_path = Path(request.source_dir)
    dest_path = Path(request.dest_dir)

    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Dossier source introuvable")

    # Find all developable files
    files = []
    for ext in ['*.dng', '*.DNG', '*.tif', '*.tiff', '*.TIF', '*.TIFF']:
        files.extend(source_path.glob(ext))
    # Also include frame-*.jpg and frame_*.jpg
    files.extend(source_path.glob("frame-*.jpg"))
    files.extend(source_path.glob("frame_*.jpg"))
    files = sorted(files)

    if not files:
        raise HTTPException(status_code=400, detail="Aucun fichier à développer trouvé")

    # Create dest dir if needed
    dest_path.mkdir(parents=True, exist_ok=True)

    # Reset state
    develop_state.update({
        "is_running": True,
        "cancel_requested": False,
        "total_files": len(files),
        "processed_files": 0,
        "current_file": "",
        "error": "",
    })

    # Start develop in background
    asyncio.create_task(run_darktable_develop(files, dest_path, request.style, request.quality))

    return {"total_files": len(files)}


async def run_darktable_develop(files: list[Path], dest_dir: Path, style: str, quality: int):
    """Run darktable-cli batch export in background with parallel processing."""
    global develop_state

    # Number of parallel workers (4 is safe with 64GB RAM)
    NUM_WORKERS = 4

    total_files = len(files)
    completed_count = 0

    async def develop_one(img_path: Path) -> bool:
        """Process a single file with darktable-cli."""
        output_file = dest_dir / (img_path.stem + ".jpg")

        cmd = [
            'darktable-cli',
            str(img_path),
            str(output_file),
            '--hq', 'true',
            '--apply-custom-presets', 'false',  # Permet les instances multiples en parallèle
        ]

        if style:
            cmd.extend(['--style', style])

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await process.wait()
            return output_file.exists()
        except Exception as e:
            print(f"Error developing {img_path.name}: {e}")
            return False

    async def worker(queue: asyncio.Queue):
        """Worker that processes files from the queue."""
        nonlocal completed_count
        while not develop_state["cancel_requested"]:
            try:
                img_path = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if queue.empty():
                    break
                continue

            try:
                await develop_one(img_path)
                completed_count += 1
                develop_state["processed_files"] = completed_count
                develop_state["current_file"] = f"{img_path.name} ({NUM_WORKERS} workers)"
            finally:
                queue.task_done()

    try:
        # Create queue and fill with files
        queue = asyncio.Queue()
        for f in files:
            queue.put_nowait(f)

        # Start workers
        workers = [asyncio.create_task(worker(queue)) for _ in range(NUM_WORKERS)]

        # Wait for all workers to complete
        await asyncio.gather(*workers)

    except Exception as e:
        develop_state["error"] = str(e)

    finally:
        develop_state["is_running"] = False
        develop_state["current_file"] = ""


@app.get("/api/develop/progress")
async def develop_progress_stream():
    """Stream develop progress via SSE."""
    async def generate() -> AsyncGenerator[str, None]:
        while develop_state["is_running"]:
            data = json.dumps(develop_state)
            yield f"data: {data}\n\n"
            await asyncio.sleep(0.5)
        yield f"data: {json.dumps(develop_state)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/develop/cancel")
async def cancel_develop():
    """Cancel develop process."""
    global develop_state
    if not develop_state["is_running"]:
        raise HTTPException(status_code=400, detail="Aucun développement en cours")
    develop_state["cancel_requested"] = True
    return {"message": "Annulation demandée"}


# ============================================================================
# Preview API - Generate preview images for testing settings
# ============================================================================

import tempfile
import random
import base64

# Store preview images in memory for serving
preview_cache = {}


@app.post("/api/preview/check")
async def check_preview_source(request: BrowseRequest):
    """Check source directory for frame files."""
    path = Path(request.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Dossier introuvable")

    count = len(list(path.glob("frame-*.jpg"))) + len(list(path.glob("frame_*.jpg")))
    return {"count": count}


@app.post("/api/preview/generate")
async def generate_preview(request: PreviewRequest):
    """Generate preview images with specified settings."""
    global preview_cache

    source_path = Path(request.source_dir)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Dossier source introuvable")

    # Find all frame files
    files = sorted(list(source_path.glob("frame-*.jpg")) + list(source_path.glob("frame_*.jpg")))
    if not files:
        raise HTTPException(status_code=400, detail="Aucun fichier frame-*.jpg ou frame_*.jpg trouvé")

    # Select random frames
    count = min(request.count, len(files))
    if len(files) > count:
        # Select evenly distributed frames
        step = len(files) // count
        selected_files = [files[i * step] for i in range(count)]
    else:
        selected_files = files[:count]

    # Clear old preview cache
    preview_cache.clear()

    previews = []
    temp_dir = Path(tempfile.mkdtemp(prefix="preview_"))

    try:
        for i, frame_file in enumerate(selected_files):
            # Step 1: Convert to DNG
            try:
                raw_data, bayer = extract_raw_from_jpeg(frame_file)
                dng_file = temp_dir / f"preview_{i}.dng"
                create_dng(raw_data, request.bayer_pattern, dng_file)
            except Exception as e:
                print(f"Error converting {frame_file.name} to DNG: {e}")
                continue

            # Step 2: Export with darktable-cli (with or without style)
            output_file = temp_dir / f"preview_{i}.jpg"
            cmd = [
                'darktable-cli',
                str(dng_file),
                str(output_file),
                '--width', '800',  # Smaller for preview
                '--hq', 'false',   # Faster
                '--apply-custom-presets', 'false',  # Permet les instances multiples en parallèle
            ]

            if request.style:
                cmd.extend(['--style', request.style])

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
                await asyncio.wait_for(process.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                print(f"Timeout processing {frame_file.name}")
                continue
            except Exception as e:
                print(f"Error processing {frame_file.name}: {e}")
                continue

            if output_file.exists():
                # Read and cache the image
                preview_id = f"preview_{i}_{random.randint(1000, 9999)}"
                with open(output_file, 'rb') as f:
                    preview_cache[preview_id] = f.read()

                previews.append({
                    "name": frame_file.name,
                    "url": f"/api/preview/image/{preview_id}"
                })

    finally:
        # Cleanup temp files
        import shutil
        try:
            shutil.rmtree(temp_dir)
        except:
            pass

    if not previews:
        raise HTTPException(status_code=500, detail="Aucun aperçu n'a pu être généré")

    return {"previews": previews}


@app.get("/api/preview/image/{preview_id}")
async def get_preview_image(preview_id: str):
    """Serve a cached preview image."""
    if preview_id not in preview_cache:
        raise HTTPException(status_code=404, detail="Image non trouvée")

    from fastapi.responses import Response
    return Response(content=preview_cache[preview_id], media_type="image/jpeg")


# ============================================================================
# Workflow API - Complete pipeline: RAW → DNG → Darktable → Video
# ============================================================================

@app.get("/api/workflow/status")
async def get_workflow_status():
    """Get current workflow status."""
    return workflow_state


@app.post("/api/workflow/start")
async def start_workflow(request: WorkflowRequest):
    """Start the complete workflow pipeline."""
    global workflow_state

    if workflow_state["is_running"]:
        raise HTTPException(status_code=400, detail="Un workflow est déjà en cours")

    source_path = Path(request.source_dir)
    output_path = Path(request.output_dir)

    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Dossier source introuvable")

    # Find all frame files
    files = sorted(list(source_path.glob("frame-*.jpg")) + list(source_path.glob("frame_*.jpg")))
    if not files:
        raise HTTPException(status_code=400, detail="Aucun fichier frame-*.jpg ou frame_*.jpg trouvé")

    # Create output directories
    dng_dir = output_path / "dng"
    jpeg_dir = output_path / "jpeg"
    dng_dir.mkdir(parents=True, exist_ok=True)
    jpeg_dir.mkdir(parents=True, exist_ok=True)

    # Reset state
    workflow_state.update({
        "is_running": True,
        "cancel_requested": False,
        "step": "dng",
        "progress": 0,
        "current_file": "",
        "error": "",
    })

    # Start workflow in background
    asyncio.create_task(run_workflow(
        files, dng_dir, jpeg_dir, output_path,
        request.bayer_pattern, request.style, request.fps, request.codec
    ))

    return {"total_files": len(files)}


async def run_workflow(files: list[Path], dng_dir: Path, jpeg_dir: Path, output_dir: Path,
                       bayer_pattern: int, style: str, fps: int, codec: str):
    """Run the complete workflow pipeline."""
    global workflow_state
    import re

    total_files = len(files)

    try:
        # === Step 1: Convert RAW to DNG ===
        workflow_state["step"] = "dng"
        dng_files = []

        for i, frame_file in enumerate(files):
            if workflow_state["cancel_requested"]:
                return

            workflow_state["current_file"] = frame_file.name
            workflow_state["progress"] = int((i / total_files) * 100)

            dng_file = dng_dir / (frame_file.stem + ".dng")

            try:
                raw_data, _ = extract_raw_from_jpeg(frame_file)
                create_dng(raw_data, bayer_pattern, dng_file)
                dng_files.append(dng_file)
            except Exception as e:
                print(f"Error converting {frame_file.name}: {e}")

            await asyncio.sleep(0)  # Yield control

        if workflow_state["cancel_requested"]:
            return

        # === Step 2: Develop with Darktable (PARALLEL) ===
        workflow_state["step"] = "develop"

        # Number of parallel workers (adjust based on RAM - darktable uses ~2-4GB each)
        NUM_WORKERS = 4  # Safe with 64GB RAM

        completed_count = 0
        total_dng = len(dng_files)
        jpeg_files = []

        async def develop_one(dng_file: Path) -> Path | None:
            """Process a single DNG file with darktable-cli."""
            jpeg_file = jpeg_dir / (dng_file.stem + ".jpg")

            cmd = [
                'darktable-cli',
                str(dng_file),
                str(jpeg_file),
                '--hq', 'true',
                '--apply-custom-presets', 'false',  # Permet les instances multiples en parallèle
            ]

            if style:
                cmd.extend(['--style', style])

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
                await process.wait()
                if jpeg_file.exists():
                    return jpeg_file
            except Exception as e:
                print(f"Error developing {dng_file.name}: {e}")
            return None

        async def worker(queue: asyncio.Queue, results: list):
            """Worker that processes DNG files from the queue."""
            nonlocal completed_count
            while not workflow_state["cancel_requested"]:
                try:
                    # Use get() with timeout instead of get_nowait()
                    dng_file = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    # Check if queue is really empty (no more items to process)
                    if queue.empty():
                        break
                    continue

                try:
                    result = await develop_one(dng_file)
                    if result:
                        results.append(result)

                    completed_count += 1
                    workflow_state["current_file"] = f"{dng_file.name} ({NUM_WORKERS} workers)"
                    workflow_state["progress"] = int((completed_count / total_dng) * 100)
                finally:
                    queue.task_done()

        # Create queue and fill with DNG files
        queue = asyncio.Queue()
        for dng_file in dng_files:
            queue.put_nowait(dng_file)

        # Start workers
        results = []
        workers = [asyncio.create_task(worker(queue, results)) for _ in range(NUM_WORKERS)]

        # Wait for all workers to complete
        await asyncio.gather(*workers)

        jpeg_files = sorted(results)  # Sort to maintain order

        if workflow_state["cancel_requested"]:
            return

        # === Step 3: Create video ===
        workflow_state["step"] = "video"
        workflow_state["progress"] = 0
        workflow_state["current_file"] = "Création vidéo..."

        # Video output file
        video_ext = ".mov" if codec == "dnxhr" else ".mp4"
        video_file = output_dir / f"film{video_ext}"

        # Create concat file
        import tempfile
        concat_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
        frame_duration = 1.0 / fps

        for f in sorted(jpeg_files):
            escaped_path = str(f).replace("'", "'\\''")
            concat_file.write(f"file '{escaped_path}'\n")
            concat_file.write(f"duration {frame_duration}\n")

        # Add last file again (ffmpeg quirk)
        if jpeg_files:
            escaped_path = str(sorted(jpeg_files)[-1]).replace("'", "'\\''")
            concat_file.write(f"file '{escaped_path}'\n")
        concat_file.close()

        # Progress file
        progress_file = tempfile.NamedTemporaryFile(mode='w', suffix='_progress.txt', delete=False)
        progress_file.close()

        # Build ffmpeg command
        if codec == "dnxhr":
            cmd = [
                'ffmpeg', '-y',
                '-f', 'concat', '-safe', '0',
                '-r', str(fps),
                '-i', concat_file.name,
                '-vf', 'format=yuv422p',
                '-r', str(fps), '-vsync', 'cfr',
                '-c:v', 'dnxhd', '-profile:v', 'dnxhr_hq',
                '-progress', progress_file.name,
                str(video_file)
            ]
        else:
            cmd = [
                'ffmpeg', '-y',
                '-f', 'concat', '-safe', '0',
                '-r', str(fps),
                '-i', concat_file.name,
                '-vf', 'format=yuv420p',
                '-r', str(fps), '-vsync', 'cfr',
                '-c:v', 'hevc_nvenc', '-preset', 'p4', '-cq', '18', '-bf', '0',
                '-progress', progress_file.name,
                str(video_file)
            ]

        ffmpeg_process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )

        total_frames = len(jpeg_files)

        while ffmpeg_process.returncode is None:
            try:
                await asyncio.wait_for(ffmpeg_process.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

            try:
                with open(progress_file.name, 'r') as f:
                    content = f.read()
                    matches = re.findall(r'frame=(\d+)', content)
                    if matches:
                        frame = int(matches[-1])
                        workflow_state["progress"] = min(100, int(frame / total_frames * 100))
                        workflow_state["current_file"] = f"Frame {frame}/{total_frames}"
            except:
                pass

            if workflow_state["cancel_requested"]:
                ffmpeg_process.terminate()
                break

            await asyncio.sleep(0.1)

        # Cleanup
        try:
            os.unlink(concat_file.name)
            os.unlink(progress_file.name)
        except:
            pass

        if workflow_state["cancel_requested"]:
            return

        workflow_state["step"] = "done"
        workflow_state["progress"] = 100
        workflow_state["current_file"] = ""

    except Exception as e:
        workflow_state["error"] = str(e)
        import traceback
        print(traceback.format_exc())

    finally:
        workflow_state["is_running"] = False


@app.get("/api/workflow/progress")
async def workflow_progress_stream():
    """Stream workflow progress via SSE."""
    async def generate() -> AsyncGenerator[str, None]:
        while workflow_state["is_running"]:
            data = json.dumps(workflow_state)
            yield f"data: {data}\n\n"
            await asyncio.sleep(0.5)
        yield f"data: {json.dumps(workflow_state)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/workflow/cancel")
async def cancel_workflow():
    """Cancel the workflow."""
    global workflow_state
    if not workflow_state["is_running"]:
        raise HTTPException(status_code=400, detail="Aucun workflow en cours")
    workflow_state["cancel_requested"] = True
    return {"message": "Annulation demandée"}


if __name__ == "__main__":
    import uvicorn
    print("=" * 50)
    print("Film Scanner Tools - http://localhost:8080")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8080)
