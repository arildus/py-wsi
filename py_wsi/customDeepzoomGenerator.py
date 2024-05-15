from openslide.deepzoom import DeepZoomGenerator
from PIL import Image

class CustomDeepZoomGenerator(DeepZoomGenerator):
    def __init__(self, osr, tile_size=254, overlap=1, limit_bounds=False):
        DeepZoomGenerator.__init__(self, osr, tile_size, overlap, limit_bounds)
    
    def get_tile(self, level, address, mode):
        """Return an RGB PIL.Image for a tile.

        level:     the Deep Zoom level.
        address:   the address of the tile within the level as a (col, row)
                   tuple."""

        # Read tile
        args, z_size = self._get_tile_info(level, address)
        tile = self._osr.read_region(*args)
        profile = tile.info.get('icc_profile')

        # Apply on solid background
        bg = Image.new(mode, tile.size, self._bg_color)
        tile = Image.composite(tile, bg, tile)

        # Scale to the correct size
        if tile.size != z_size:
            # Image.Resampling added in Pillow 9.1.0
            # Image.LANCZOS removed in Pillow 10
            if (mode == '1' or mode == 'L'):
                tile.thumbnail(z_size, getattr(Image, 'Resampling', Image).NEAREST)
            else:
                tile.thumbnail(z_size, getattr(Image, 'Resampling', Image).LANCZOS)

        # Reference ICC profile
        if profile is not None:
            tile.info['icc_profile'] = profile

        return tile


if __name__ == '__main__':
    new = CustomDeepZoomGenerator()