bl_info = {
    "name": "Backrooms Generator",
    "author": "Kuzey Kayra Eyioğlu",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Backrooms",
    "description": "Procedural Backrooms generator with custom textures, UV mapping and configurable ceiling lights",
    "category": "Add Mesh",
}

import bpy
import random
import time
import math
import os
from mathutils import Vector
from bpy.props import (
    IntProperty,
    FloatProperty,
    BoolProperty,
    EnumProperty,
    StringProperty,
    PointerProperty,
)
from bpy.types import Operator, Panel, PropertyGroup


ROOT_COLLECTION = "Backrooms_Generated"
GEOMETRY_COLLECTION = "Geometry"
FIXTURE_COLLECTION = "Light_Fixtures"
AREA_LIGHT_COLLECTION = "Area_Lights"
OBJECT_NAME = "Backrooms"

MAT_WALL = "Backrooms_Wall"
MAT_FLOOR = "Backrooms_Floor"
MAT_CEILING = "Backrooms_Ceiling"
MAT_LIGHT = "Backrooms_Light_Emission"
FIXTURE_MESH_NAME = "Backrooms_Shared_Fixture_Mesh"
AREA_LIGHT_DATA_NAME = "Backrooms_Shared_Area_Light"


# ------------------------------------------------------------
# Utility
# ------------------------------------------------------------

def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def safe_remove_collection(collection):
    if not collection:
        return

    for child in list(collection.children):
        safe_remove_collection(child)

    for obj in list(collection.objects):
        bpy.data.objects.remove(obj, do_unlink=True)

    bpy.data.collections.remove(collection)


def ensure_root_collections(scene):
    old = bpy.data.collections.get(ROOT_COLLECTION)
    if old:
        safe_remove_collection(old)

    root = bpy.data.collections.new(ROOT_COLLECTION)
    scene.collection.children.link(root)

    geometry = bpy.data.collections.new(GEOMETRY_COLLECTION)
    fixtures = bpy.data.collections.new(FIXTURE_COLLECTION)
    area_lights = bpy.data.collections.new(AREA_LIGHT_COLLECTION)

    root.children.link(geometry)
    root.children.link(fixtures)
    root.children.link(area_lights)

    return root, geometry, fixtures, area_lights


def load_image(filepath):
    if not filepath:
        return None

    absolute = bpy.path.abspath(filepath)
    if not os.path.isfile(absolute):
        return None

    try:
        return bpy.data.images.load(absolute, check_existing=True)
    except RuntimeError:
        return None


def remove_material_if_exists(name):
    mat = bpy.data.materials.get(name)
    if mat:
        bpy.data.materials.remove(mat)


def safe_tiling(value):
    """Prevent zero/negative mapping values without imposing an upper limit."""
    return max(float(value), 0.000001)


def resolve_tiling(image, tiling_x, tiling_y, keep_aspect):
    """Return repetitions per meter while optionally preserving image aspect."""
    tx = safe_tiling(tiling_x)
    ty = safe_tiling(tiling_y)

    if (
        keep_aspect
        and image
        and len(image.size) >= 2
        and image.size[0] > 0
        and image.size[1] > 0
    ):
        ty = tx * (image.size[0] / image.size[1])

    return tx, ty


def create_surface_material(
    name,
    image_path,
    default_color,
    roughness,
    tiling_x,
    tiling_y,
    keep_aspect,
):
    """Create a Base Color material using the explicit BackroomsUV map."""
    remove_material_if_exists(name)

    mat = bpy.data.materials.new(name)
    mat.use_nodes = True

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (620, 0)

    principled = nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (340, 0)
    principled.inputs["Base Color"].default_value = default_color
    principled.inputs["Roughness"].default_value = roughness

    links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    image = load_image(image_path)
    if not image:
        return mat

    tiling_x, tiling_y = resolve_tiling(
        image,
        tiling_x,
        tiling_y,
        keep_aspect,
    )

    uv_map = nodes.new("ShaderNodeUVMap")
    uv_map.location = (-760, 0)
    uv_map.uv_map = "BackroomsUV"
    uv_map.label = "Explicit BackroomsUV"

    mapping = nodes.new("ShaderNodeMapping")
    mapping.location = (-520, 0)
    mapping.vector_type = "POINT"
    mapping.inputs["Scale"].default_value = (
        tiling_x,
        tiling_y,
        1.0,
    )
    mapping.label = (
        f"Tiling X {tiling_x:.4g} / Y {tiling_y:.4g} per meter"
    )

    image_node = nodes.new("ShaderNodeTexImage")
    image_node.location = (-180, 0)
    image_node.image = image
    image_node.extension = "REPEAT"
    image_node.interpolation = "Linear"
    image_node.projection = "FLAT"
    image_node.label = (
        f"{image.size[0]} × {image.size[1]}"
        + (" — Aspect Preserved" if keep_aspect else "")
    )

    try:
        image.colorspace_settings.name = "sRGB"
    except Exception:
        pass

    links.new(uv_map.outputs["UV"], mapping.inputs["Vector"])
    links.new(mapping.outputs["Vector"], image_node.inputs["Vector"])
    links.new(image_node.outputs["Color"], principled.inputs["Base Color"])

    return mat



def create_emission_material(color, strength):
    remove_material_if_exists(MAT_LIGHT)

    mat = bpy.data.materials.new(MAT_LIGHT)
    mat.use_nodes = True

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputMaterial")
    emission = nodes.new("ShaderNodeEmission")

    emission.inputs["Color"].default_value = color
    emission.inputs["Strength"].default_value = strength

    links.new(emission.outputs["Emission"], output.inputs["Surface"])

    return mat


# ------------------------------------------------------------
# Layout generation
# ------------------------------------------------------------

def carve_brush(occupied, x, y, radius, width, depth):
    for oy in range(-radius, radius + 1):
        for ox in range(-radius, radius + 1):
            px = x + ox
            py = y + oy

            if 1 <= px < width - 1 and 1 <= py < depth - 1:
                occupied.add((px, py))


def carve_room(occupied, center_x, center_y, room_w, room_d, width, depth):
    half_w = room_w // 2
    half_d = room_d // 2

    min_x = clamp(center_x - half_w, 1, width - 2)
    max_x = clamp(center_x + half_w, 1, width - 2)
    min_y = clamp(center_y - half_d, 1, depth - 2)
    max_y = clamp(center_y + half_d, 1, depth - 2)

    for y in range(min_y, max_y + 1):
        for x in range(min_x, max_x + 1):
            occupied.add((x, y))


def build_layout(settings, rng):
    width = settings.grid_width
    depth = settings.grid_depth
    target = int(width * depth * settings.floor_density)

    center = (width // 2, depth // 2)
    occupied = set()

    carve_room(
        occupied,
        center[0],
        center[1],
        max(3, settings.corridor_width + 2),
        max(3, settings.corridor_width + 2),
        width,
        depth,
    )

    walkers = [{
        "x": center[0],
        "y": center[1],
        "dx": 1,
        "dy": 0,
    }]

    directions = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    max_steps = width * depth * 35
    steps = 0

    while len(occupied) < target and steps < max_steps:
        steps += 1

        if len(walkers) < settings.branch_count and rng.random() < 0.025:
            bx, by = rng.choice(tuple(occupied))
            dx, dy = rng.choice(directions)
            walkers.append({"x": bx, "y": by, "dx": dx, "dy": dy})

        walker = rng.choice(walkers)

        if rng.random() > settings.straightness:
            choices = [
                d for d in directions
                if d != (-walker["dx"], -walker["dy"])
            ]
            walker["dx"], walker["dy"] = rng.choice(choices)

        next_x = walker["x"] + walker["dx"]
        next_y = walker["y"] + walker["dy"]

        if not (1 <= next_x < width - 1 and 1 <= next_y < depth - 1):
            walker["dx"], walker["dy"] = rng.choice(directions)
            continue

        walker["x"] = next_x
        walker["y"] = next_y

        radius = max(0, settings.corridor_width - 1)
        carve_brush(
            occupied,
            walker["x"],
            walker["y"],
            radius,
            width,
            depth,
        )

        if rng.random() < settings.room_chance:
            room_w = rng.randint(
                settings.room_min_size,
                settings.room_max_size,
            )
            room_d = rng.randint(
                settings.room_min_size,
                settings.room_max_size,
            )

            carve_room(
                occupied,
                walker["x"],
                walker["y"],
                room_w,
                room_d,
                width,
                depth,
            )

        if rng.random() < 0.018 and occupied:
            walker["x"], walker["y"] = rng.choice(tuple(occupied))

    return occupied


# ------------------------------------------------------------
# Geometry generation
# ------------------------------------------------------------

def create_backrooms_mesh(context, settings, occupied, geometry_collection):
    cell = settings.cell_size
    height = settings.wall_height
    width = settings.grid_width
    depth = settings.grid_depth

    offset_x = -(width * cell) * 0.5
    offset_y = -(depth * cell) * 0.5

    vertices = []
    faces = []
    face_materials = []
    face_uvs = []

    floor_vertices = {}
    ceiling_vertices = {}

    def world_xy(gx, gy):
        return (
            offset_x + gx * cell,
            offset_y + gy * cell,
        )

    def floor_vertex(gx, gy):
        key = (gx, gy)
        if key not in floor_vertices:
            wx, wy = world_xy(gx, gy)
            floor_vertices[key] = len(vertices)
            vertices.append((wx, wy, 0.0))
        return floor_vertices[key]

    def ceiling_vertex(gx, gy):
        key = (gx, gy)
        if key not in ceiling_vertices:
            wx, wy = world_xy(gx, gy)
            ceiling_vertices[key] = len(vertices)
            vertices.append((wx, wy, height))
        return ceiling_vertices[key]

    def add_face(indices, material_index, uvs):
        faces.append(indices)
        face_materials.append(material_index)
        face_uvs.append(uvs)

    for x, y in occupied:
        wx0, wy0 = world_xy(x, y)
        wx1, wy1 = world_xy(x + 1, y + 1)

        # Positive local meter coordinates make Repeat mapping predictable.
        uvx0 = x * cell
        uvy0 = y * cell
        uvx1 = (x + 1) * cell
        uvy1 = (y + 1) * cell

        f00 = floor_vertex(x, y)
        f10 = floor_vertex(x + 1, y)
        f11 = floor_vertex(x + 1, y + 1)
        f01 = floor_vertex(x, y + 1)

        add_face(
            (f00, f10, f11, f01),
            1,
            (
                (uvx0, uvy0),
                (uvx1, uvy0),
                (uvx1, uvy1),
                (uvx0, uvy1),
            ),
        )

        if settings.generate_ceiling:
            c00 = ceiling_vertex(x, y)
            c10 = ceiling_vertex(x + 1, y)
            c11 = ceiling_vertex(x + 1, y + 1)
            c01 = ceiling_vertex(x, y + 1)

            add_face(
                (c01, c11, c10, c00),
                2,
                (
                    (uvx0, uvy1),
                    (uvx1, uvy1),
                    (uvx1, uvy0),
                    (uvx0, uvy0),
                ),
            )

        # South wall: use world X along U and height along V.
        if (x, y - 1) not in occupied:
            b0 = floor_vertex(x, y)
            b1 = floor_vertex(x + 1, y)
            t0 = ceiling_vertex(x, y)
            t1 = ceiling_vertex(x + 1, y)

            add_face(
                (b1, b0, t0, t1),
                0,
                (
                    (uvx1, 0.0),
                    (uvx0, 0.0),
                    (uvx0, height),
                    (uvx1, height),
                ),
            )

        # North wall.
        if (x, y + 1) not in occupied:
            b0 = floor_vertex(x, y + 1)
            b1 = floor_vertex(x + 1, y + 1)
            t0 = ceiling_vertex(x, y + 1)
            t1 = ceiling_vertex(x + 1, y + 1)

            add_face(
                (b0, b1, t1, t0),
                0,
                (
                    (uvx0, 0.0),
                    (uvx1, 0.0),
                    (uvx1, height),
                    (uvx0, height),
                ),
            )

        # West wall: use world Y along U and height along V.
        if (x - 1, y) not in occupied:
            b0 = floor_vertex(x, y)
            b1 = floor_vertex(x, y + 1)
            t0 = ceiling_vertex(x, y)
            t1 = ceiling_vertex(x, y + 1)

            add_face(
                (b0, b1, t1, t0),
                0,
                (
                    (uvy0, 0.0),
                    (uvy1, 0.0),
                    (uvy1, height),
                    (uvy0, height),
                ),
            )

        # East wall.
        if (x + 1, y) not in occupied:
            b0 = floor_vertex(x + 1, y)
            b1 = floor_vertex(x + 1, y + 1)
            t0 = ceiling_vertex(x + 1, y)
            t1 = ceiling_vertex(x + 1, y + 1)

            add_face(
                (b1, b0, t0, t1),
                0,
                (
                    (uvy1, 0.0),
                    (uvy0, 0.0),
                    (uvy0, height),
                    (uvy1, height),
                ),
            )

    mesh = bpy.data.meshes.new(f"{OBJECT_NAME}_Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update(calc_edges=True)

    # UV data is stored per polygon loop, so shared mesh vertices can still
    # have different UV coordinates on floor, wall and ceiling faces.
    uv_layer = mesh.uv_layers.new(name="BackroomsUV")
    for polygon, uvs in zip(mesh.polygons, face_uvs):
        for loop_index, uv in zip(polygon.loop_indices, uvs):
            uv_layer.data[loop_index].uv = uv

    obj = bpy.data.objects.new(OBJECT_NAME, mesh)
    geometry_collection.objects.link(obj)

    wall_mat = create_surface_material(
        MAT_WALL,
        settings.wall_texture,
        (0.48, 0.42, 0.18, 1.0),
        settings.wall_roughness,
        settings.wall_tiling_x,
        settings.wall_tiling_y,
        settings.wall_keep_aspect,
    )
    floor_mat = create_surface_material(
        MAT_FLOOR,
        settings.floor_texture,
        (0.28, 0.20, 0.07, 1.0),
        settings.floor_roughness,
        settings.floor_tiling_x,
        settings.floor_tiling_y,
        settings.floor_keep_aspect,
    )
    ceiling_mat = create_surface_material(
        MAT_CEILING,
        settings.ceiling_texture,
        (0.72, 0.69, 0.55, 1.0),
        settings.ceiling_roughness,
        settings.ceiling_tiling_x,
        settings.ceiling_tiling_y,
        settings.ceiling_keep_aspect,
    )

    obj.data.materials.append(wall_mat)
    obj.data.materials.append(floor_mat)
    obj.data.materials.append(ceiling_mat)

    for polygon, material_index in zip(mesh.polygons, face_materials):
        polygon.material_index = material_index

    for scene_obj in context.selected_objects:
        scene_obj.select_set(False)

    obj.select_set(True)
    context.view_layer.objects.active = obj

    if settings.add_bevel:
        bevel = obj.modifiers.new("Soft Wall Edges", "BEVEL")
        bevel.width = settings.bevel_width
        bevel.segments = 2
        bevel.limit_method = "ANGLE"

    return obj


# ------------------------------------------------------------
# Light placement
# ------------------------------------------------------------

def get_corridor_orientation(cell, occupied):
    x, y = cell

    left = (x - 1, y) in occupied
    right = (x + 1, y) in occupied
    down = (x, y - 1) in occupied
    up = (x, y + 1) in occupied

    horizontal = int(left) + int(right)
    vertical = int(down) + int(up)

    if horizontal > vertical:
        return "HORIZONTAL"

    if vertical > horizontal:
        return "VERTICAL"

    return "ROOM"


def choose_bucket_cell(bucket, bucket_key, spacing, pattern, rng):
    """Choose one representative floor cell from a spacing-sized region."""
    if pattern == "SYMMETRIC":
        bx, by = bucket_key
        center_x = bx * spacing + (spacing - 1) * 0.5
        center_y = by * spacing + (spacing - 1) * 0.5
        return min(
            bucket,
            key=lambda cell: (
                (cell[0] - center_x) ** 2
                + (cell[1] - center_y) ** 2
            ),
        )

    return rng.choice(bucket)


def choose_map_wide_subset(candidates, target_count, rng):
    """Choose a random subset while spreading selections across the map."""
    candidates = list(dict.fromkeys(candidates))

    if target_count <= 0 or not candidates:
        return []

    if target_count >= len(candidates):
        return candidates[:]

    min_x = min(cell[0] for cell in candidates)
    max_x = max(cell[0] for cell in candidates)
    min_y = min(cell[1] for cell in candidates)
    max_y = max(cell[1] for cell in candidates)

    # Divide the map into broad macro regions. Selecting from these first
    # prevents low chance values from clustering in only one part of the map.
    side = max(1, math.ceil(math.sqrt(target_count)))
    span_x = max(1, max_x - min_x + 1)
    span_y = max(1, max_y - min_y + 1)

    macro_regions = {}

    for cell in candidates:
        nx = (cell[0] - min_x) / span_x
        ny = (cell[1] - min_y) / span_y

        gx = min(side - 1, int(nx * side))
        gy = min(side - 1, int(ny * side))

        macro_regions.setdefault((gx, gy), []).append(cell)

    region_keys = list(macro_regions)
    rng.shuffle(region_keys)

    selected = []
    selected_set = set()

    for key in region_keys:
        options = [
            cell for cell in macro_regions[key]
            if cell not in selected_set
        ]

        if not options:
            continue

        chosen = rng.choice(options)
        selected.append(chosen)
        selected_set.add(chosen)

        if len(selected) >= target_count:
            return selected

    remaining = [
        cell for cell in candidates
        if cell not in selected_set
    ]
    rng.shuffle(remaining)
    selected.extend(remaining[:target_count - len(selected)])

    return selected


def select_light_cells(settings, occupied, rng):
    """Select crash-safe, broadly distributed ceiling-light positions.

    Light Chance controls the percentage of spacing-sized map regions that
    receive a light. At 1.0 every occupied region receives one light, rather
    than creating one separate Area Light for every floor polygon.

    Maximum Lights is always enforced.
    """
    cells = sorted(occupied)
    if not cells:
        return [], False

    spacing = max(1, settings.light_spacing)
    buckets = {}

    for cell in cells:
        key = (cell[0] // spacing, cell[1] // spacing)
        buckets.setdefault(key, []).append(cell)

    candidates = []

    for key in sorted(buckets):
        candidates.append(
            choose_bucket_cell(
                buckets[key],
                key,
                spacing,
                settings.light_pattern,
                rng,
            )
        )

    full_density = (
        settings.light_coverage == "EVERYWHERE"
        or settings.light_chance >= 0.999
    )

    if full_density:
        target_count = len(candidates)
    else:
        chance = clamp(settings.light_chance, 0.0, 1.0)
        target_count = round(len(candidates) * chance)

        if chance > 0.0 and target_count == 0:
            target_count = 1

    # The cap is intentionally never ignored. This is the main crash guard.
    max_lights = max(1, settings.max_lights)
    target_count = min(target_count, max_lights)

    selected = choose_map_wide_subset(
        candidates,
        target_count,
        rng,
    )

    selected.sort()
    return selected, full_density



def get_shared_fixture_mesh():
    """Return one reusable unit-cube mesh for every visible fixture."""
    mesh = bpy.data.meshes.get(FIXTURE_MESH_NAME)
    if mesh:
        return mesh

    vertices = [
        (-0.5, -0.5, -0.5),
        ( 0.5, -0.5, -0.5),
        ( 0.5,  0.5, -0.5),
        (-0.5,  0.5, -0.5),
        (-0.5, -0.5,  0.5),
        ( 0.5, -0.5,  0.5),
        ( 0.5,  0.5,  0.5),
        (-0.5,  0.5,  0.5),
    ]

    faces = [
        (0, 1, 2, 3),
        (4, 7, 6, 5),
        (0, 4, 5, 1),
        (1, 5, 6, 2),
        (2, 6, 7, 3),
        (4, 0, 3, 7),
    ]

    mesh = bpy.data.meshes.new(FIXTURE_MESH_NAME)
    mesh.from_pydata(vertices, [], faces)
    mesh.update()

    return mesh


def get_shared_area_light_data(settings):
    """Return one reusable Area Light datablock shared by all light objects."""
    light_data = bpy.data.lights.get(AREA_LIGHT_DATA_NAME)

    if not light_data or light_data.type != "AREA":
        if light_data:
            bpy.data.lights.remove(light_data)

        light_data = bpy.data.lights.new(
            name=AREA_LIGHT_DATA_NAME,
            type="AREA",
        )

    light_data.energy = settings.area_light_power
    light_data.shape = "RECTANGLE"
    light_data.size = settings.area_light_size
    light_data.size_y = settings.area_light_size_y
    light_data.color = settings.light_color[:3]
    light_data.use_shadow = True

    return light_data


def create_light_fixture(
    settings,
    location,
    orientation,
    fixture_collection,
    area_collection,
    emission_material,
    fixture_mesh,
    shared_light_data,
    index,
):
    x, y, z = location

    length = settings.fixture_length
    width = settings.fixture_width
    thickness = settings.fixture_thickness

    if orientation == "VERTICAL":
        dimensions = (width, length, thickness)
    else:
        dimensions = (length, width, thickness)

    # All fixtures share the same cube mesh. Only object transforms differ.
    fixture = bpy.data.objects.new(
        f"Fixture_{index:04d}",
        fixture_mesh,
    )
    fixture_collection.objects.link(fixture)
    fixture.location = (x, y, z)
    fixture.scale = dimensions

    if emission_material.name not in fixture.data.materials:
        fixture.data.materials.append(emission_material)

    # All Area Light objects share one light datablock. This uses far less
    # memory than creating a new bpy.data.lights datablock for every fixture.
    light_obj = bpy.data.objects.new(
        f"AreaLight_{index:04d}",
        shared_light_data,
    )
    area_collection.objects.link(light_obj)

    light_obj.location = (
        x,
        y,
        z - settings.area_light_drop,
    )

    # Area lights emit along local -Z.
    light_obj.rotation_euler = (0.0, 0.0, 0.0)



def generate_lights(
    context,
    settings,
    occupied,
    fixture_collection,
    area_collection,
    rng,
):
    if not settings.generate_lights or not settings.generate_ceiling:
        return 0

    emission_material = create_emission_material(
        settings.light_color,
        settings.emission_strength,
    )
    fixture_mesh = get_shared_fixture_mesh()
    shared_light_data = get_shared_area_light_data(settings)

    cell = settings.cell_size
    width = settings.grid_width
    depth = settings.grid_depth

    offset_x = -(width * cell) * 0.5
    offset_y = -(depth * cell) * 0.5

    selected_cells, force_everywhere = select_light_cells(
        settings,
        occupied,
        rng,
    )

    created = 0
    used_positions = set()

    for x, y in selected_cells:
        orientation = get_corridor_orientation((x, y), occupied)

        world_x = offset_x + (x + 0.5) * cell
        world_y = offset_y + (y + 0.5) * cell

        # At full density the fixture stays centered in every cell so lights
        # do not overlap walls. Sparse asymmetric mode can offset fixtures.
        if (
            not force_everywhere
            and settings.light_pattern == "ASYMMETRIC"
        ):
            max_offset = cell * settings.asymmetry_offset
            world_x += rng.uniform(-max_offset, max_offset)
            world_y += rng.uniform(-max_offset, max_offset)

        key = (round(world_x, 3), round(world_y, 3))
        if key in used_positions:
            continue
        used_positions.add(key)

        create_light_fixture(
            settings,
            (
                world_x,
                world_y,
                settings.wall_height
                - settings.fixture_thickness * 0.5,
            ),
            orientation,
            fixture_collection,
            area_collection,
            emission_material,
            fixture_mesh,
            shared_light_data,
            created + 1,
        )

        created += 1

    return created


# ------------------------------------------------------------
# Properties
# ------------------------------------------------------------

class BACKROOMS_PG_Settings(PropertyGroup):
    grid_width: IntProperty(
        name="Width",
        default=30,
        min=8,
        max=250,
    )

    grid_depth: IntProperty(
        name="Depth",
        default=30,
        min=8,
        max=250,
    )

    cell_size: FloatProperty(
        name="Cell Size",
        default=2.0,
        min=0.25,
        max=20.0,
        unit="LENGTH",
    )

    wall_height: FloatProperty(
        name="Wall Height",
        default=2.8,
        min=0.5,
        max=20.0,
        unit="LENGTH",
    )

    floor_density: FloatProperty(
        name="Open Area",
        default=0.58,
        min=0.15,
        max=0.90,
        subtype="FACTOR",
    )

    corridor_width: IntProperty(
        name="Corridor Width",
        default=1,
        min=1,
        max=4,
    )

    straightness: FloatProperty(
        name="Corridor Straightness",
        default=0.78,
        min=0.0,
        max=0.98,
        subtype="FACTOR",
    )

    room_chance: FloatProperty(
        name="Room Chance",
        default=0.055,
        min=0.0,
        max=0.30,
        subtype="FACTOR",
    )

    room_min_size: IntProperty(
        name="Minimum Room",
        default=3,
        min=2,
        max=20,
    )

    room_max_size: IntProperty(
        name="Maximum Room",
        default=8,
        min=3,
        max=40,
    )

    branch_count: IntProperty(
        name="Branches",
        default=5,
        min=1,
        max=30,
    )

    generate_ceiling: BoolProperty(
        name="Generate Ceiling",
        default=True,
    )

    add_bevel: BoolProperty(
        name="Add Bevel Modifier",
        default=True,
    )

    bevel_width: FloatProperty(
        name="Bevel Width",
        default=0.04,
        min=0.0,
        max=1.0,
        unit="LENGTH",
    )

    use_random_seed: BoolProperty(
        name="New Result Every Time",
        default=True,
    )

    seed: IntProperty(
        name="Seed",
        default=1,
        min=0,
        max=2_147_483_647,
    )

    enter_edit_mode: BoolProperty(
        name="Enter Edit Mode",
        default=False,
    )

    wall_texture: StringProperty(
        name="Wall Texture",
        subtype="FILE_PATH",
    )

    floor_texture: StringProperty(
        name="Floor Texture",
        subtype="FILE_PATH",
    )

    ceiling_texture: StringProperty(
        name="Ceiling Texture",
        subtype="FILE_PATH",
    )

    wall_keep_aspect: BoolProperty(
        name="Keep Wall Image Aspect",
        description="Automatically calculate Y tiling from the image dimensions",
        default=True,
    )

    wall_tiling_x: FloatProperty(
        name="Wall Tiling X / m",
        description="Horizontal repetitions per meter; no upper limit",
        default=0.5,
        min=0.000001,
        soft_min=0.01,
        soft_max=20.0,
        precision=4,
    )

    wall_tiling_y: FloatProperty(
        name="Wall Tiling Y / m",
        description="Vertical repetitions per meter when aspect correction is disabled; no upper limit",
        default=0.5,
        min=0.000001,
        soft_min=0.01,
        soft_max=20.0,
        precision=4,
    )

    floor_keep_aspect: BoolProperty(
        name="Keep Floor Image Aspect",
        description="Automatically calculate Y tiling from the image dimensions",
        default=True,
    )

    floor_tiling_x: FloatProperty(
        name="Floor Tiling X / m",
        description="Horizontal repetitions per meter; no upper limit",
        default=0.5,
        min=0.000001,
        soft_min=0.01,
        soft_max=20.0,
        precision=4,
    )

    floor_tiling_y: FloatProperty(
        name="Floor Tiling Y / m",
        description="Vertical repetitions per meter when aspect correction is disabled; no upper limit",
        default=0.5,
        min=0.000001,
        soft_min=0.01,
        soft_max=20.0,
        precision=4,
    )

    ceiling_keep_aspect: BoolProperty(
        name="Keep Ceiling Image Aspect",
        description="Automatically calculate Y tiling from the image dimensions",
        default=True,
    )

    ceiling_tiling_x: FloatProperty(
        name="Ceiling Tiling X / m",
        description="Horizontal repetitions per meter; no upper limit",
        default=0.5,
        min=0.000001,
        soft_min=0.01,
        soft_max=20.0,
        precision=4,
    )

    ceiling_tiling_y: FloatProperty(
        name="Ceiling Tiling Y / m",
        description="Vertical repetitions per meter when aspect correction is disabled; no upper limit",
        default=0.5,
        min=0.000001,
        soft_min=0.01,
        soft_max=20.0,
        precision=4,
    )

    wall_roughness: FloatProperty(
        name="Wall Roughness",
        default=0.65,
        min=0.0,
        max=1.0,
    )

    floor_roughness: FloatProperty(
        name="Floor Roughness",
        default=0.85,
        min=0.0,
        max=1.0,
    )

    ceiling_roughness: FloatProperty(
        name="Ceiling Roughness",
        default=0.75,
        min=0.0,
        max=1.0,
    )

    generate_lights: BoolProperty(
        name="Generate Ceiling Lights",
        default=True,
    )

    light_coverage: EnumProperty(
        name="Coverage",
        items=[
            ("SOME", "Some Areas", "Only some valid areas receive lights"),
            ("EVERYWHERE", "Everywhere", "Place lights at every valid interval"),
        ],
        default="SOME",
    )

    light_pattern: EnumProperty(
        name="Pattern",
        items=[
            ("SYMMETRIC", "Symmetrical", "Regular centered light pattern"),
            ("ASYMMETRIC", "Asymmetrical", "Irregular offset light pattern"),
            ("RANDOM", "Random", "Fully randomized light positions"),
        ],
        default="ASYMMETRIC",
    )

    light_spacing: IntProperty(
        name="Spacing",
        description="Approximate spacing in grid cells",
        default=4,
        min=1,
        max=20,
    )

    light_chance: FloatProperty(
        name="Light Chance",
        description="1.0 fills every floor cell; lower values remain random but distributed across the map",
        default=0.55,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )

    asymmetry_offset: FloatProperty(
        name="Asymmetry Offset",
        description="Maximum light offset as a portion of one cell",
        default=0.18,
        min=0.0,
        max=0.45,
        subtype="FACTOR",
    )

    max_lights: IntProperty(
        name="Maximum Lights",
        description="Sparse-mode cap; ignored at Light Chance 1.0 or Coverage Everywhere",
        default=300,
        min=1,
        max=5000,
    )

    fixture_length: FloatProperty(
        name="Fixture Length",
        default=1.5,
        min=0.1,
        max=10.0,
        unit="LENGTH",
    )

    fixture_width: FloatProperty(
        name="Fixture Width",
        default=0.25,
        min=0.05,
        max=5.0,
        unit="LENGTH",
    )

    fixture_thickness: FloatProperty(
        name="Fixture Thickness",
        default=0.06,
        min=0.01,
        max=1.0,
        unit="LENGTH",
    )

    emission_strength: FloatProperty(
        name="Emission Strength",
        default=8.0,
        min=0.0,
        max=1000.0,
    )

    area_light_power: FloatProperty(
        name="Area Light Power",
        default=900.0,
        min=0.0,
        max=100000.0,
    )

    area_light_size: FloatProperty(
        name="Area Size X",
        description="Keep this small for harder shadows",
        default=0.18,
        min=0.01,
        max=20.0,
        unit="LENGTH",
    )

    area_light_size_y: FloatProperty(
        name="Area Size Y",
        description="Keep this small for harder shadows",
        default=0.18,
        min=0.01,
        max=20.0,
        unit="LENGTH",
    )

    area_light_drop: FloatProperty(
        name="Light Drop",
        description="Distance below the visible fixture",
        default=0.08,
        min=0.0,
        max=3.0,
        unit="LENGTH",
    )

    light_color: bpy.props.FloatVectorProperty(
        name="Light Color",
        subtype="COLOR",
        size=4,
        default=(1.0, 1.0, 1.0, 1.0),
        min=0.0,
        max=1.0,
    )


# ------------------------------------------------------------
# Operators
# ------------------------------------------------------------

class BACKROOMS_OT_Generate(Operator):
    bl_idname = "backrooms.generate"
    bl_label = "Generate Backrooms"
    bl_description = "Generate a new textured Backrooms level"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.backrooms_settings

        if settings.room_max_size < settings.room_min_size:
            self.report(
                {"ERROR"},
                "Maximum Room cannot be smaller than Minimum Room",
            )
            return {"CANCELLED"}

        if context.object and context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        if settings.use_random_seed:
            actual_seed = (
                time.time_ns()
                ^ random.SystemRandom().randint(0, 2_147_483_647)
            ) & 0x7FFFFFFF
            settings.seed = actual_seed
        else:
            actual_seed = settings.seed

        rng = random.Random(actual_seed)
        occupied = build_layout(settings, rng)

        if not occupied:
            self.report({"ERROR"}, "No floor cells could be generated")
            return {"CANCELLED"}

        _, geometry, fixtures, area_lights = ensure_root_collections(
            context.scene
        )

        obj = create_backrooms_mesh(
            context,
            settings,
            occupied,
            geometry,
        )

        light_count = generate_lights(
            context,
            settings,
            occupied,
            fixtures,
            area_lights,
            rng,
        )

        if settings.enter_edit_mode:
            context.view_layer.objects.active = obj
            obj.select_set(True)
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")

        self.report(
            {"INFO"},
            (
                f"Generated {len(occupied)} floor cells, "
                f"{light_count} lights, seed {actual_seed}"
            ),
        )

        return {"FINISHED"}


class BACKROOMS_OT_Delete(Operator):
    bl_idname = "backrooms.delete_generated"
    bl_label = "Delete Generated"
    bl_description = "Delete the generated Backrooms collection"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if context.object and context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        collection = bpy.data.collections.get(ROOT_COLLECTION)

        if not collection:
            self.report(
                {"WARNING"},
                "No generated Backrooms collection found",
            )
            return {"CANCELLED"}

        safe_remove_collection(collection)
        return {"FINISHED"}


# ------------------------------------------------------------
# UI
# ------------------------------------------------------------

class BACKROOMS_PT_MainPanel(Panel):
    bl_label = "Backrooms Generator"
    bl_idname = "BACKROOMS_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Backrooms"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.backrooms_settings

        release_box = layout.box()
        release_box.label(
            text="Backrooms Generator v1.0.0",
            icon="WORLD",
        )

        generate_row = release_box.row()
        generate_row.scale_y = 1.35
        generate_row.operator(
            "backrooms.generate",
            text="Generate Backrooms",
            icon="MOD_BUILD",
        )

        release_box.operator(
            "backrooms.delete_generated",
            text="Delete Generated",
            icon="TRASH",
        )

        size_box = layout.box()
        size_box.label(text="Map Size")
        row = size_box.row(align=True)
        row.prop(settings, "grid_width")
        row.prop(settings, "grid_depth")
        size_box.prop(settings, "cell_size")
        size_box.prop(settings, "wall_height")

        layout_box = layout.box()
        layout_box.label(text="Layout")
        layout_box.prop(settings, "floor_density")
        layout_box.prop(settings, "corridor_width")
        layout_box.prop(settings, "straightness")
        layout_box.prop(settings, "branch_count")
        layout_box.prop(settings, "room_chance")

        row = layout_box.row(align=True)
        row.prop(settings, "room_min_size")
        row.prop(settings, "room_max_size")

        texture_box = layout.box()
        texture_box.label(text="Textures — Aspect Correct UV")

        wall_box = texture_box.box()
        wall_box.label(text="Walls")
        wall_box.prop(settings, "wall_texture")
        wall_box.prop(settings, "wall_keep_aspect")
        row = wall_box.row(align=True)
        row.prop(settings, "wall_tiling_x")
        if not settings.wall_keep_aspect:
            row.prop(settings, "wall_tiling_y")
        wall_box.prop(settings, "wall_roughness")

        floor_box = texture_box.box()
        floor_box.label(text="Floor")
        floor_box.prop(settings, "floor_texture")
        floor_box.prop(settings, "floor_keep_aspect")
        row = floor_box.row(align=True)
        row.prop(settings, "floor_tiling_x")
        if not settings.floor_keep_aspect:
            row.prop(settings, "floor_tiling_y")
        floor_box.prop(settings, "floor_roughness")

        ceiling_tex_box = texture_box.box()
        ceiling_tex_box.label(text="Ceiling")
        ceiling_tex_box.prop(settings, "ceiling_texture")
        ceiling_tex_box.prop(settings, "ceiling_keep_aspect")
        row = ceiling_tex_box.row(align=True)
        row.prop(settings, "ceiling_tiling_x")
        if not settings.ceiling_keep_aspect:
            row.prop(settings, "ceiling_tiling_y")
        ceiling_tex_box.prop(settings, "ceiling_roughness")

        texture_box.label(
            text="Tiling fields have no upper limit",
            icon="INFO",
        )

        geometry_box = layout.box()
        geometry_box.label(text="Geometry")
        geometry_box.prop(settings, "generate_ceiling")
        geometry_box.prop(settings, "add_bevel")

        if settings.add_bevel:
            geometry_box.prop(settings, "bevel_width")

        geometry_box.prop(settings, "enter_edit_mode")

        light_box = layout.box()
        light_box.label(text="Ceiling Lights")
        light_box.prop(settings, "generate_lights")

        if settings.generate_lights:
            light_box.prop(settings, "light_coverage")
            light_box.prop(settings, "light_pattern")
            light_box.prop(settings, "light_spacing")

            if settings.light_coverage == "SOME":
                light_box.prop(settings, "light_chance")

            if settings.light_pattern == "ASYMMETRIC":
                light_box.prop(settings, "asymmetry_offset")

            light_box.prop(settings, "max_lights")
            if (
                settings.light_coverage == "EVERYWHERE"
                or settings.light_chance >= 0.999
            ):
                light_box.label(
                    text="Full density: one light per Spacing region",
                    icon="INFO",
                )
            else:
                light_box.label(
                    text="Low chance remains distributed map-wide",
                    icon="INFO",
                )
            light_box.label(
                text="Maximum Lights is always enforced",
                icon="INFO",
            )
            light_box.prop(settings, "light_color")
            light_box.prop(settings, "emission_strength")

            fixture_box = light_box.box()
            fixture_box.label(text="Fixture")
            fixture_box.prop(settings, "fixture_length")
            fixture_box.prop(settings, "fixture_width")
            fixture_box.prop(settings, "fixture_thickness")

            area_box = light_box.box()
            area_box.label(text="Area Light")
            area_box.prop(settings, "area_light_power")
            area_box.prop(settings, "area_light_size")
            area_box.prop(settings, "area_light_size_y")
            area_box.prop(settings, "area_light_drop")
            area_box.label(
                text="Smaller size = harder shadows",
                icon="INFO",
            )

        seed_box = layout.box()
        seed_box.label(text="Randomness")
        seed_box.prop(settings, "use_random_seed")
        seed_box.prop(settings, "seed")

        layout.separator()

        bottom_generate = layout.row()
        bottom_generate.scale_y = 1.25
        bottom_generate.operator(
            "backrooms.generate",
            text="Generate Backrooms",
            icon="MOD_BUILD",
        )

        layout.operator(
            "backrooms.delete_generated",
            text="Delete Generated",
            icon="TRASH",
        )


classes = (
    BACKROOMS_PG_Settings,
    BACKROOMS_OT_Generate,
    BACKROOMS_OT_Delete,
    BACKROOMS_PT_MainPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.backrooms_settings = PointerProperty(
        type=BACKROOMS_PG_Settings
    )


def unregister():
    if hasattr(bpy.types.Scene, "backrooms_settings"):
        del bpy.types.Scene.backrooms_settings

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()