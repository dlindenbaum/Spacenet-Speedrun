"""Zero-shot building detection with VLMs served by Hugging Face Inference
Providers (OpenAI-compatible router at router.huggingface.co/v1)."""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
import urllib.request

from PIL import Image
from shapely.geometry import Polygon, box

ROUTER = "https://router.huggingface.co/v1/chat/completions"

PROMPT_BOX = (
    "This is a {w}x{h} pixel overhead satellite image (about 0.3 m per pixel). "
    "Detect EVERY building footprint (roof) visible in the image, including small houses, "
    "sheds and partially visible buildings at the image border. Do not merge adjacent buildings. "
    "Return ONLY a JSON list, one entry per building, in the form "
    '[{{"box_2d": [ymin, xmin, ymax, xmax]}}, ...] where coordinates are normalized to 0-1000. '
    "If there are no buildings return []."
)

PROMPT_POLY = (
    "This is a {w}x{h} pixel overhead satellite image (about 0.3 m per pixel). "
    "Trace the outline of EVERY building footprint (roof) visible in the image, including small houses "
    "and partially visible buildings at the image border. Do not merge adjacent buildings. "
    "Return ONLY a JSON list, one entry per building, in the form "
    '[{{"polygon": [[x1, y1], [x2, y2], ...]}}, ...] with 4-8 vertices per building and pixel '
    "coordinates (x to the right, y down, origin top-left). If there are no buildings return []."
)


def encode(img: Image.Image, size: int | None = None) -> str:
    if size:
        img = img.resize((size, size), Image.BICUBIC)
    b = io.BytesIO()
    img.save(b, format="PNG")
    return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()


def chat(model: str, prompt: str, image_url: str, max_tokens=8192, temperature=0.0, retries=4):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": image_url}},
            {"type": "text", "text": prompt},
        ]}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    tok = os.environ.get("HF_TOKEN", "hf_dummy")  # proxy injects the real credential
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(ROUTER, data=json.dumps(body).encode(), headers={
                "Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
            r = json.loads(urllib.request.urlopen(req, timeout=600).read())
            return r["choices"][0]["message"]["content"] or "", r.get("usage", {})
        except urllib.error.HTTPError as e:
            last = f"{e.code} {e.read()[:300]!r}"
        except Exception as e:  # noqa
            last = repr(e)
        time.sleep(3 * 2 ** i)
    raise RuntimeError(f"{model}: {last}")


def _extract_json(text: str):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    m = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    s = m.group(1) if m else text
    i = s.find("[")
    if i < 0:
        return []
    s = s[i:]
    try:
        return json.loads(s)
    except Exception:
        # truncated output: cut at last complete object
        j = s.rfind("}")
        try:
            return json.loads(s[: j + 1] + "]")
        except Exception:
            return []


def parse(text: str, w: int, h: int, mode: str, box_order="yxyx", scale=1000.0):
    out = []
    for d in _extract_json(text):
        try:
            if mode == "box":
                v = d.get("box_2d") or d.get("bbox") or d.get("bbox_2d") if isinstance(d, dict) else d
                v = [float(x) for x in v][:4]
                if box_order == "yxyx":
                    y0, x0, y1, x1 = v
                else:
                    x0, y0, x1, y1 = v
                x0, x1 = sorted((x0 * w / scale, x1 * w / scale))
                y0, y1 = sorted((y0 * h / scale, y1 * h / scale))
                if x1 - x0 > 1 and y1 - y0 > 1:
                    out.append(box(x0, y0, x1, y1))
            else:
                pts = d.get("polygon") if isinstance(d, dict) else d
                pts = [(float(x), float(y)) for x, y in pts]
                if len(pts) >= 3:
                    p = Polygon(pts)
                    if not p.is_valid:
                        p = p.buffer(0)
                    if p.area > 1:
                        out.append(p)
        except Exception:
            continue
    return out
