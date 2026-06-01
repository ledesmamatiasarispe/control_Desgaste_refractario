import numpy as np
import matplotlib

COLORMAPS = ["plasma", "inferno", "RdYlGn_r", "hot", "viridis", "coolwarm"]


def distances_to_colors(distances: np.ndarray,
                        colormap: str = "plasma",
                        clamp_max: float = None,
                        clamp_pct: float = 95.0) -> np.ndarray:
    """Return (N, 4) float32 RGBA array coloured by wear distances."""
    vmax = clamp_max if (clamp_max is not None and clamp_max > 0) \
           else float(np.percentile(distances, clamp_pct))
    if vmax <= 0:
        vmax = float(distances.max()) or 1.0

    norm   = np.clip(distances / vmax, 0.0, 1.0)
    cmap   = matplotlib.colormaps.get_cmap(colormap)
    colors = cmap(norm).astype(np.float32)
    return colors


def colorbar_image(colormap: str = "plasma",
                   width: int = 20,
                   height: int = 256) -> np.ndarray:
    """Return (H, W, 4) uint8 image of the colorbar."""
    cmap    = matplotlib.colormaps.get_cmap(colormap)
    vals    = np.linspace(1.0, 0.0, height)
    rgba    = (cmap(vals) * 255).astype(np.uint8)
    return np.tile(rgba[:, np.newaxis, :], (1, width, 1))
