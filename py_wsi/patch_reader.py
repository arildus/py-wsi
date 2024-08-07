'''

These functions acess the .svs files via the openslide-python API and perform patch
sampling as is typically needed for deep learning.

Author: @ysbecca
'''

import numpy as np
from openslide import open_slide
from .customDeepzoomGenerator import CustomDeepZoomGenerator
from glob import glob
from xml.dom import minidom
from shapely.geometry import Polygon, Point
from PIL import Image
import xml.etree.ElementTree as ET
import cv2
from tqdm import tqdm

from .store import *

def check_label_exists(label, label_map):
    ''' Checking if a label is a valid label. 
    '''
    if label in label_map:
        return True
    else:
        print("py_wsi error: provided label " + str(label) + " not present in label map.")
        print("Setting label as -1 for UNRECOGNISED LABEL.")
        print(label_map)
        return False

def generate_label(regions, region_labels, point, label_map):
    ''' Generates a label given an array of regions.
        - regions               array of vertices
        - region_labels         corresponding labels for the regions
        - point                 x, y tuple
        - label_map             the label dictionary mapping string labels to integer labels
    '''
    for i in range(len(region_labels)):
        poly = Polygon(regions[i])
        if poly.contains(Point(point[0], point[1])):
            if check_label_exists(region_labels[i], label_map):
                return label_map[region_labels[i]]
            else:
                return -1
    # By default, we set to "Normal" if it exists in the label map.
    if check_label_exists('Normal', label_map):
        return label_map['Normal']
    else:
        return -1

def get_regions(path):
    ''' Parses the xml at the given path, assuming annotation format importable by ImageScope. '''
    xml = minidom.parse(path)
    # The first region marked is always the tumour delineation
    regions_ = xml.getElementsByTagName("Region")
    regions, region_labels = [], []
    for region in regions_:
        vertices = region.getElementsByTagName("Vertex")
        attribute = region.getElementsByTagName("Attribute")
        if len(attribute) > 0:
            r_label = attribute[0].attributes['Value'].value
        else:
            r_label = region.getAttribute('Text')
        region_labels.append(r_label)

        # Store x, y coordinates into a 2D array in format [x1, y1], [x2, y2], ...
        coords = np.zeros((len(vertices), 2))

        for i, vertex in enumerate(vertices):
            coords[i][0] = vertex.attributes['X'].value
            coords[i][1] = vertex.attributes['Y'].value

        regions.append(coords)
    return regions, region_labels

def patch_to_tile_size(patch_size, overlap):
    return patch_size - overlap*2

def create_binary_mask(annotation_file, dimensions):
    # Get dimensions
    mask_width, mask_height = dimensions

    # Create a blank mask
    mask = np.zeros((mask_height, mask_width), dtype=np.uint8)

    # Parse annotation XML file
    tree = ET.parse(annotation_file)
    root = tree.getroot()

    # Loop through annotations
    for annotation in root.findall('.//Annotation'):
        coords = []
        for coord in annotation.findall('.//Coordinate'):
            x = float(coord.attrib['X'])
            y = float(coord.attrib['Y'])
            # Convert coordinates to image space
            x_img = int(x)
            y_img = int(y)
            coords.append((x_img, y_img))
        # Convert coords list to array
        pts = np.array(coords, np.int32)
        pts = pts.reshape((-1, 1, 2))
        # Draw filled polygon
        cv2.fillPoly(mask, [pts], 255)

    return mask

def is_mostly_white(patch, total_pixels, thresh=0.97):
    # Convert patch to grayscale
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)

    # How "white" the pixel must be to be considered as background
    # 241 seems to be the best value, based on experiments
    background_thresh = 231
    
    # Threshold the patch to separate background from tissue
    _, binary_mask = cv2.threshold(gray, background_thresh, 255, cv2.THRESH_BINARY)
    
    # Calculate the percentage of white pixels
    white_pixels = cv2.countNonZero(binary_mask)
    white_percentage = white_pixels / total_pixels
    
    # Return True if white percentage is above threshold
    return white_percentage > thresh

def sample_and_store_patches(file_name,
                             file_dir,
                             pixel_overlap,
                             env=False,
                             meta_env=False,
                             patch_size=512,
                             level=0,
                             xml_dir=False,
                             label_map={},
                             limit_bounds=True,
                             rows_per_txn=20,
                             db_location='',
                             prefix='',
                             storage_option='lmdb'):
    ''' Sample patches of specified size from .svs file.
        - file_name             name of whole slide image to sample from
        - file_dir              directory file is located in
        - pixel_overlap         pixels overlap on each side
        - env, meta_env         for LMDB only; environment variables
        - level                 0 is lowest resolution; level_count - 1 is highest
        - xml_dir               directory containing annotation XML files
        - label_map             dictionary mapping string labels to integers
        - rows_per_txn          how many patches to load into memory at once
        - storage_option        the patch storage option              

        Note: patch_size is the dimension of the sampled patches, NOT equivalent to openslide's definition
        of tile_size. This implementation was chosen to allow for more intuitive usage.
    '''
    file_path = os.path.join(file_dir, file_name)
    xml_path = os.path.join(xml_dir, file_name[:-4] + ".xml")

    tile_size = patch_to_tile_size(patch_size, pixel_overlap)
    slide = open_slide(file_path)
    tiles = CustomDeepZoomGenerator(slide,
                              tile_size=tile_size,
                              overlap=pixel_overlap,
                              limit_bounds=limit_bounds)
    
    dimensions = slide.dimensions
    Image.MAX_IMAGE_PIXELS = None
    binary_mask_image = create_binary_mask(xml_path, dimensions)
    bm_slide = open_slide(Image.fromarray(binary_mask_image, 'L'))
    bm_tiles = CustomDeepZoomGenerator(bm_slide,
                                tile_size=tile_size,
                                overlap=pixel_overlap,
                                limit_bounds=limit_bounds) 

    if level >= tiles.level_count:
        print("[py-wsi error]: requested level does not exist. Number of slide levels: " + str(tiles.level_count))
        return 0
    x_tiles, y_tiles = tiles.level_tiles[level]

    x = 0
    count, batch_count = 0, 0
    patches, coords, labels = [], [], []
    bm_patches, bm_coords = [], []
    tqdm_bar = tqdm(range(y_tiles), desc='Processing tiles')
    for y in tqdm_bar:
        while x < x_tiles:
            # I had to rewrite this get_tile() method from openslide-python package...
            new_tile = np.array(tiles.get_tile(level, (x, y), 'RGB'), dtype=np.uint8)

            # OpenSlide calculates overlap in such a way that sometimes depending on the dimensions, edge
            # patches are smaller than the others. We will ignore such patches.
            if np.shape(new_tile) == (patch_size, patch_size, 3):
                bm_tile = np.array(bm_tiles.get_tile(level, (x, y), 'L'), dtype=np.uint8)
                contains_glomeruli = np.count_nonzero(bm_tile) > 0
                if contains_glomeruli or not is_mostly_white(new_tile, patch_size**2):
                    patches.append(new_tile)
                    coords.append(np.array([x, y]))

                    bm_patches.append(bm_tile)
                    bm_coords.append(np.array([x ,y]))
                    
                    count += 1
                    labels.append(int(contains_glomeruli))
            x += 1

        # To save memory, we will save data into the dbs every rows_per_txn rows. i.e., each transaction will commit
        # rows_per_txn rows of patches. Write after last row regardless. HDF5 does NOT follow
        # this convention due to efficiency.
        if (y % rows_per_txn == 0 and y != 0) or y == y_tiles-1:
            save_to_disk(db_location, patches, coords, bm_patches, file_name[:-4], labels, 'L')

        x = 0
    return count
