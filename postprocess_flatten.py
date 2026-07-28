"""
Post-stitch illumination flattening
===================================
Evens out residual large-scale brightness variation across a stitched die
image (left/right illumination gradients, hot regions) that survives the
per-tile corrections, and normalizes exposure. Hue-preserving: one gain is
applied to all channels.

Method (computed on a 1/16 proxy of the canvas):
  1. Estimate a smooth luminance field (large-sigma Gaussian blur).
  2. Gain = clip(target / field, lo, hi), where target is a percentile of
     the field over valid (non-background) pixels. With hi = 1.0 this is a
     darken-only correction that cannot amplify noise in dark areas.
  3. Exposure normalization: one global scale so p99.95 of the corrected
     image sits just below white (useful after the stitcher's highlight
     protection, which leaves the canvas uniformly dark).
The gain field is upsampled and applied to the full-resolution image in
streamed bands, so memory stays bounded for gigapixel canvases.

Usage:
  # preview only (writes <out_dir>/flatten_preview.jpg + gain .npy):
  python postprocess_flatten.py preview stitched_raw.tif

  # apply in place and re-export alongside the input:
  python postprocess_flatten.py apply stitched_raw.tif

Options: --pct 45 --lo 0.55 --hi 1.30 --sigma-frac 0.045 --no-boost
"""
import argparse
import gc
import os
import time

import numpy as np

DS = 16          # proxy downsample factor
BAND = 2048      # processing band height at full resolution
_T0 = time.time()


def _log(msg):
    print(f"  {msg}  ({time.time() - _T0:.0f}s)", flush=True)


def _open_canvas(path, mode="r"):
    """Open a stitched canvas as a uint16/uint8 array.

    Returns (array, is_memmap). When ``is_memmap`` is False the caller is
    responsible for writing the array back to disk after modifying it.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in (".tif", ".tiff"):
        import tifffile
        try:
            return tifffile.memmap(path, mode=mode), True
        except ValueError:
            # compressed tif cannot be memmapped; load fully
            return tifffile.imread(path), False
    import cv2
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise FileNotFoundError(path)
    if img.ndim == 3 and img.shape[2] == 3:
        img = np.ascontiguousarray(img[:, :, ::-1])  # BGR -> RGB
    return img, False


def _write_canvas(canvas, path):
    """Write an in-RAM canvas back to its source file."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".tif", ".tiff"):
        import tifffile
        tifffile.imwrite(path, canvas,
                         bigtiff=canvas.nbytes > 2_000_000_000)
    else:
        import cv2
        out = canvas[:, :, ::-1] if canvas.ndim == 3 else canvas
        if not cv2.imwrite(path, out):
            raise IOError(f"could not write {path}")
    _log(f"canvas written back -> {path}")


def _load_proxy(canvas):
    h, w = canvas.shape[:2]
    ph, pw = max(2, h // DS), max(2, w // DS)
    if canvas.ndim == 3:
        proxy = np.empty((ph, pw, canvas.shape[2]), np.float32)
    else:
        proxy = np.empty((ph, pw), np.float32)
    for y in range(ph):
        proxy[y] = canvas[y * DS, ::DS][:pw]
    return proxy


def build_gain(canvas, pct=45.0, lo=0.55, hi=1.30, sigma_frac=0.045,
               boost=True):
    """Compute the smooth per-pixel gain field on a 1/16 proxy.

    Returns (gain_2d_float32, proxy) where gain is at proxy resolution.
    """
    import cv2

    proxy = _load_proxy(canvas)
    ph = proxy.shape[0]
    lum = proxy.mean(axis=2) if proxy.ndim == 3 else proxy
    ceiling = 65535.0 if canvas.dtype == np.uint16 else 255.0

    p99 = float(np.percentile(lum, 99.5))
    valid = lum > 0.10 * p99  # exclude black borders and die cores

    sigma = sigma_frac * ph
    field = cv2.GaussianBlur(lum, (0, 0), sigma)
    target = float(np.percentile(field[valid], pct)) if valid.any() else p99
    gain = np.clip(target / np.maximum(field, 1e-3), lo, hi)
    gain = cv2.GaussianBlur(gain.astype(np.float32), (0, 0), sigma * 0.3)

    scale = 1.0
    if boost and valid.any():
        corr_hi = float(np.percentile((lum * gain)[valid], 99.95))
        scale = float(np.clip(0.97 * ceiling / max(corr_hi, 1.0), 1.0, 8.0))
        gain = gain * scale

    _log(f"gain field: target {target:.0f}, exposure boost {scale:.2f}, "
         f"range {gain.min():.3f}-{gain.max():.3f}")
    return gain.astype(np.float32), proxy


def apply_gain(canvas, gain):
    """Apply the proxy-resolution gain field to the full canvas in bands.

    ``canvas`` must be writable (memmap mode r+ or in-RAM array).
    """
    import cv2

    h, w = canvas.shape[:2]
    ph = gain.shape[0]
    ceiling = 65535.0 if canvas.dtype == np.uint16 else 255.0
    gain_w = cv2.resize(gain, (w, ph), interpolation=cv2.INTER_LINEAR)

    for y0 in range(0, h, BAND):
        y1 = min(h, y0 + BAND)
        ys = (np.arange(y0, y1) + 0.5) / DS - 0.5
        r0 = np.clip(np.floor(ys).astype(int), 0, ph - 1)
        r1 = np.clip(r0 + 1, 0, ph - 1)
        t = np.clip(ys - r0, 0, 1).astype(np.float32)[:, None]
        gband = gain_w[r0] * (1 - t) + gain_w[r1] * t
        band = canvas[y0:y1].astype(np.float32)
        if band.ndim == 3:
            band *= gband[:, :, None]
        else:
            band *= gband
        canvas[y0:y1] = np.clip(band, 0, ceiling).astype(canvas.dtype)
        del band, gband
    if hasattr(canvas, "flush"):
        canvas.flush()
    gc.collect()
    _log("gain applied")


def _write_preview(canvas, gain, out_path):
    import cv2

    proxy = _load_proxy(canvas)
    corr = proxy * (gain[:, :, None] if proxy.ndim == 3 else gain)
    p = max(float(np.percentile(corr, 99.7)), 1.0)
    prev = np.clip(corr / p * 255.0, 0, 255).astype(np.uint8)
    if prev.ndim == 3:
        prev = prev[:, :, ::-1]
    cv2.imwrite(out_path, prev, [cv2.IMWRITE_JPEG_QUALITY, 92])
    _log(f"preview -> {out_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[2])
    ap.add_argument("mode", choices=["preview", "apply"])
    ap.add_argument("image", help="stitched canvas (uncompressed tif "
                                  "memmap-able for apply mode)")
    ap.add_argument("--pct", type=float, default=45.0,
                    help="target percentile of the illumination field")
    ap.add_argument("--lo", type=float, default=0.55,
                    help="minimum gain (max darkening of hot regions)")
    ap.add_argument("--hi", type=float, default=1.30,
                    help="maximum gain (max brightening of dark regions)")
    ap.add_argument("--sigma-frac", type=float, default=0.045,
                    help="blur sigma as a fraction of image height")
    ap.add_argument("--no-boost", action="store_true",
                    help="skip global exposure normalization")
    args = ap.parse_args()

    base = os.path.splitext(args.image)[0]
    if args.mode == "preview":
        canvas, _ = _open_canvas(args.image, mode="r")
        gain, _proxy = build_gain(canvas, args.pct, args.lo, args.hi,
                                  args.sigma_frac, boost=not args.no_boost)
        np.save(base + "_flatten_gain.npy", gain)
        _write_preview(canvas, gain, base + "_flatten_preview.jpg")
    else:
        canvas, is_memmap = _open_canvas(args.image, mode="r+")
        expected_shape = (max(2, canvas.shape[0] // DS),
                          max(2, canvas.shape[1] // DS))
        gain_path = base + "_flatten_gain.npy"
        gain = None
        if os.path.exists(gain_path):
            gain = np.load(gain_path)
            if gain.shape != expected_shape:
                _log(f"saved gain field {gain.shape} does not match canvas "
                     f"proxy {expected_shape}; recomputing")
                gain = None
            else:
                _log(f"using saved gain field {gain_path}")
        if gain is None:
            gain, _proxy = build_gain(canvas, args.pct, args.lo, args.hi,
                                      args.sigma_frac, boost=not args.no_boost)
        apply_gain(canvas, gain)
        if not is_memmap:
            _write_canvas(canvas, args.image)
        _write_preview(canvas, np.ones_like(gain), base + "_flatten_preview.jpg")
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
