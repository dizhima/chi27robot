from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
import json
import math
import re


ROOT = Path("frontend/public/assets/furnitures/livingroom")
SRC = ROOT / "InteriorTest.obj"
MTL = ROOT / "InteriorTest.mtl"
OUTDIR = ROOT / "meshes_no_walls"

SKIP_MATS = {"Roof"}
SKIP_NAME_RE = re.compile(r"(plafon|ceiling|roof|upperplafon)", re.I)
CEILING_LIGHT_OBJECTS = {
    "Cylinder.001",
    "Cylinder.002",
    "Cylinder.003",
    "Cylinder.004_Cylinder.006",
    "Cylinder.005_Cylinder.007",
    "Cylinder.006_Cylinder.008",
    "Cylinder.007_Cylinder.009",
    "Cylinder.008_Cylinder.011",
    "Sphere",
    "Sphere.001",
    "Sphere.002",
    "Sphere.003",
    "Sphere.004",
    "Sphere.005",
    "Sphere.006",
    "Sphere.007",
}
FALLBACK_COLORS = {
    "Book": [0.18, 0.24, 0.44, 1.0],
    "Book2": [0.58, 0.18, 0.16, 1.0],
    "Carpet": [0.74, 0.63, 0.50, 1.0],
    "Cermin": [0.70, 0.82, 0.88, 0.55],
    "Cusion": [0.78, 0.69, 0.60, 1.0],
    "Emmision": [1.00, 0.86, 0.48, 1.0],
    "Floor": [0.56, 0.38, 0.22, 1.0],
    "Gelas": [0.86, 0.82, 0.74, 1.0],
    "Jam": [0.92, 0.90, 0.82, 1.0],
    "Kaca": [0.58, 0.78, 0.92, 0.38],
    "Lemari": [0.50, 0.31, 0.17, 1.0],
    "Meja": [0.46, 0.30, 0.18, 1.0],
    "Netflix": [0.62, 0.02, 0.03, 1.0],
    "None": [0.42, 0.40, 0.36, 1.0],
    "Picture": [0.20, 0.38, 0.68, 1.0],
    "Picture2": [0.76, 0.48, 0.24, 1.0],
    "Picture3": [0.18, 0.52, 0.68, 1.0],
    "Picture4": [0.72, 0.28, 0.36, 1.0],
    "SideTegel": [0.71, 0.74, 0.79, 1.0],
    "Sofa": [0.84, 0.80, 0.72, 1.0],
    "Sofa_Kecil": [0.49, 0.49, 0.49, 1.0],
    "TiangMeja": [0.38, 0.34, 0.30, 1.0],
    "Tree": [0.20, 0.42, 0.20, 1.0],
    "Tree07_4K": [0.18, 0.50, 0.20, 0.80],
    "Tree_Branch": [0.34, 0.21, 0.11, 1.0],
    "Wall": [0.82, 0.80, 0.74, 1.0],
    "Wall_Down": [0.92, 0.90, 0.84, 1.0],
    "Side_Wall": [0.78, 0.76, 0.70, 1.0],
}


def safe_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", name.strip())
    if not safe or safe[0].isdigit():
        safe = "mat_" + safe
    return safe[:64]


def read_materials() -> tuple[dict[str, list[float]], dict[str, str]]:
    colors: dict[str, list[float]] = {}
    texture_paths: dict[str, str] = {}
    current = None
    if not MTL.exists():
        return colors, texture_paths

    for line in MTL.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("newmtl "):
            current = line.split(None, 1)[1].strip()
            colors[current] = [0.75, 0.75, 0.75, 1.0]
        elif current and line.startswith("Kd "):
            values = [float(v) for v in line.split()[1:4]]
            colors[current] = values + [colors.get(current, [0, 0, 0, 1])[3]]
        elif current and line.startswith("d "):
            colors.setdefault(current, [0.75, 0.75, 0.75, 1.0])[3] = float(line.split()[1])
        elif current and line.startswith("Tr "):
            tr = float(line.split()[1])
            colors.setdefault(current, [0.75, 0.75, 0.75, 1.0])[3] = max(0.0, min(1.0, 1.0 - tr))
        elif current and line.startswith("map_Kd "):
            texture_path = line.split(None, 1)[1].strip().replace("\\", "/")
            texture_paths[current] = re.sub(r"/+", "/", texture_path)

    for material, fallback in FALLBACK_COLORS.items():
        if material not in colors:
            continue
        # Many exported materials use neutral Kd values and expect texture files.
        # If the texture image is absent, use a semantic fallback color.
        texture_path = texture_paths.get(material)
        texture_missing = texture_path is not None and not (ROOT / texture_path).exists()
        neutral = colors[material][:3] in ([0.8, 0.8, 0.8], [1.0, 1.0, 1.0])
        if texture_missing or neutral:
            colors[material] = fallback
    return colors, texture_paths


def parse_ref(token: str, nverts: int, nvts: int, nvns: int) -> tuple[int, int, int]:
    parts = token.split("/")
    vi = int(parts[0]) if parts[0] else 0
    ti = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    ni = int(parts[2]) if len(parts) > 2 and parts[2] else 0
    if vi < 0:
        vi = nverts + 1 + vi
    if ti < 0:
        ti = nvts + 1 + ti
    if ni < 0:
        ni = nvns + 1 + ni
    return vi, ti, ni


def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    colors, texture_paths = read_materials()

    verts: list[str] = []
    vts: list[str] = []
    vns: list[str] = []
    current_object = "__none__"
    current_material = "None"
    skip_object = False
    by_material: dict[str, list[tuple[str, str]]] = defaultdict(list)
    kept_objects: Counter[str] = Counter()
    skipped_objects: Counter[str] = Counter()
    face_total = 0
    face_kept = 0

    with SRC.open("r", encoding="utf-8", errors="ignore") as source:
        for raw in source:
            if raw.startswith("v "):
                verts.append(raw)
            elif raw.startswith("vt "):
                vts.append(raw)
            elif raw.startswith("vn "):
                vns.append(raw)
            elif raw.startswith(("o ", "g ")):
                current_object = raw.split(None, 1)[1].strip()
                skip_object = bool(SKIP_NAME_RE.search(current_object)) or current_object in CEILING_LIGHT_OBJECTS
            elif raw.startswith("usemtl "):
                current_material = raw.split(None, 1)[1].strip()
            elif raw.startswith("f "):
                face_total += 1
                should_skip = skip_object or current_material in SKIP_MATS
                if should_skip:
                    skipped_objects[current_object] += 1
                    continue
                by_material[current_material].append((current_object, raw))
                kept_objects[current_object] += 1
                face_kept += 1

    mesh_infos = []
    for material, entries in sorted(by_material.items(), key=lambda item: safe_name(item[0]).lower()):
        vmap: dict[int, int] = {}
        vtmap: dict[int, int] = {}
        vnmap: dict[int, int] = {}
        body_lines: list[str] = []
        tri_faces = 0

        def remap(token: str) -> str:
            vi, ti, ni = parse_ref(token, len(verts), len(vts), len(vns))
            if vi not in vmap:
                vmap[vi] = len(vmap) + 1
            if ti and ti not in vtmap:
                vtmap[ti] = len(vtmap) + 1
            if ni and ni not in vnmap:
                vnmap[ni] = len(vnmap) + 1

            a = str(vmap[vi])
            b = str(vtmap[ti]) if ti else ""
            c = str(vnmap[ni]) if ni else ""
            if ti or ni:
                return a + "/" + b + ("/" + c if ni else "")
            return a

        for _object_name, line in entries:
            tokens = line.split()[1:]
            if len(tokens) < 3:
                continue
            remapped = [remap(token) for token in tokens]
            for index in range(1, len(remapped) - 1):
                body_lines.append(f"f {remapped[0]} {remapped[index]} {remapped[index + 1]}\n")
                tri_faces += 1

        filename = safe_name(material) + ".obj"
        dest = OUTDIR / filename
        inverse_v = sorted(vmap, key=lambda key: vmap[key])
        inverse_vt = sorted(vtmap, key=lambda key: vtmap[key])
        inverse_vn = sorted(vnmap, key=lambda key: vnmap[key])
        with dest.open("w", encoding="utf-8") as output:
            output.write("# Generated from InteriorTest.obj; wall/roof objects excluded.\n")
            output.write("o " + safe_name(material) + "\n")
            for index in inverse_v:
                output.write(verts[index - 1])
            for index in inverse_vt:
                output.write(vts[index - 1])
            for index in inverse_vn:
                output.write(vns[index - 1])
            output.writelines(body_lines)

        mesh_infos.append(
            {
                "material": material,
                "safe": safe_name(material),
                "file": "meshes_no_walls/" + filename,
                "tri_faces": tri_faces,
                "vertices": len(inverse_v),
                "rgba": colors.get(material, [0.75, 0.75, 0.75, 1.0]),
                "source_texture": texture_paths.get(material),
                "source_texture_present": bool(
                    texture_paths.get(material) and (ROOT / texture_paths[material]).exists()
                ),
            }
        )

    asset_lines = []
    geom_lines = []
    for info in mesh_infos:
        rgba = " ".join(f"{value:.4g}" for value in info["rgba"])
        asset_lines.append(f'    <material name="mat_{info["safe"]}" rgba="{rgba}"/>')
        asset_lines.append(f'    <mesh name="mesh_{info["safe"]}" file="{info["file"]}" inertia="shell"/>')
        geom_lines.append(
            f'      <geom name="geom_{info["safe"]}" type="mesh" mesh="mesh_{info["safe"]}" '
            f'material="mat_{info["safe"]}" euler="90 0 0" contype="0" conaffinity="0"/>'
        )

    scene = (
        '<mujoco model="livingroom_no_walls_scene">\n'
        '  <compiler angle="degree"/>\n'
        '  <option timestep="0.002" gravity="0 0 -9.81"/>\n'
        '  <statistic center="0 0 0.8" extent="5.5"/>\n\n'
        "  <asset>\n"
        '    <texture name="skybox" type="skybox" builtin="gradient" rgb1="0.78 0.84 0.92" rgb2="0.98 0.98 0.95" width="512" height="512"/>\n'
        + "\n".join(asset_lines)
        + "\n  </asset>\n\n"
        "  <worldbody>\n"
        '    <light name="key_light" pos="0 -3 5" dir="0 0 -1" diffuse="0.9 0.86 0.78"/>\n'
        '    <light name="fill_light" pos="-3 3 4" dir="0.4 -0.3 -1" diffuse="0.35 0.40 0.45"/>\n'
        '    <camera name="top_overview" pos="0 0 8" xyaxes="1 0 0 0 1 0"/>\n'
        '    <camera name="livingroom_view" pos="4 -6 3" euler="60 0 36"/>\n'
        '    <geom name="collision_floor" type="plane" pos="0 0 -0.002" size="3 4.5 0.05" rgba="0.72 0.70 0.64 1" friction="1 0.01 0.001"/>\n'
        '    <body name="livingroom_no_walls_visual" pos="0 0 0">\n'
        + "\n".join(geom_lines)
        + "\n    </body>\n"
        '    <site name="livingroom_center_site" pos="0 0 0.04" size="0.07" rgba="0.1 0.85 0.45 0.55"/>\n'
        "  </worldbody>\n"
        "</mujoco>\n"
    )
    (ROOT / "scene.xml").write_text(scene, encoding="utf-8")

    manifest = {
        "scene_path": "scene.xml",
        "model_name": "livingroom_no_walls_scene",
        "source_obj": "InteriorTest.obj",
        "source_url": "https://free3d.com/3d-model/living-room-997877.html",
        "source_attribution": "Living Room 3D model from Free3D.",
        "summary": "Generated MuJoCo scene from InteriorTest.obj with wall/roof objects excluded. Meshes are grouped by material and converted to single-object OBJ files for MuJoCo loading.",
        "robots": [],
        "conversion": {
            "excluded_rules": [
                "object name contains plafon/ceiling/roof/upperplafon",
                "material is Roof",
                "ceiling chandelier objects Cylinder.001-008 and Sphere.000-007",
            ],
            "source_faces": face_total,
            "kept_source_faces": face_kept,
            "generated_meshes": len(mesh_infos),
            "generated_tri_faces": sum(info["tri_faces"] for info in mesh_infos),
            "coordinate_transform": "MJCF geoms use euler=90 0 0 to convert OBJ y-up to MuJoCo z-up.",
            "material_note": "InteriorTest.mtl contains map_Kd texture references, but the Texture directory is not present in this asset folder; semantic fallback colors are used for missing textures.",
        },
        "meshes": mesh_infos,
        "skipped_objects": [{"name": key, "faces": value} for key, value in skipped_objects.most_common()],
        "cameras": ["top_overview", "livingroom_view"],
        "task_readiness_notes": [
            "Four wall meshes are loaded; roof/ceiling objects are intentionally not loaded.",
            "Generated mesh geoms are visual-only; collision uses a simple ground plane.",
            "Furniture collision proxies should be added later if robots need navigation or manipulation around the layout.",
        ],
    }
    (ROOT / "scene_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print("wrote", ROOT / "scene.xml")
    print(
        "meshes",
        len(mesh_infos),
        "source_faces",
        face_total,
        "kept_faces",
        face_kept,
        "tri_faces",
        sum(info["tri_faces"] for info in mesh_infos),
    )
    print("skipped_objects", len(skipped_objects), skipped_objects.most_common(12))


if __name__ == "__main__":
    main()
