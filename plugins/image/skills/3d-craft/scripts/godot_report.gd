extends SceneTree
## Report what Godot made of imported 3D assets, as one RESULT {json} line.
##
## Import first, then report on the imported scenes by their res:// paths:
##
##   godot --headless --path /abs/project --import
##   godot --headless --path /abs/project --script /abs/skill/scripts/godot_report.gd \
##       -- res://assets/crate.glb [res://...]
##
## Per asset: every node (class:path), each mesh's origin, scale and box, and per
## surface its primitive type, triangles, vertices, LOD count, vertex colours,
## UV2, tangents and the material that draws it (unshaded, vertex colour as
## albedo, albedo, textures, cull mode, and whether it is an override); the
## collision shapes; and the bounding box of all meshes in the asset's space.
## Under --headless the renderer is a dummy, so this reads data, never pixels.
## Exits 1 when a path is missing, not imported or not a scene.

const PRIMITIVES := ["points", "lines", "line_strip", "triangles", "triangle_strip"]


func _initialize() -> void:
	var paths := OS.get_cmdline_user_args()
	var assets := {}
	var ok := not paths.is_empty()
	for path in paths:
		var entry := _report(path)
		ok = ok and entry.get("ok", false)
		assets[path] = entry
	print("RESULT " + JSON.stringify({"ok": ok, "assets": assets}))
	quit(0 if ok else 1)


func _report(path: String) -> Dictionary:
	if not ResourceLoader.exists(path):
		return {"ok": false, "error": "not imported or not found"}
	var scene = load(path)
	if not scene is PackedScene:
		return {"ok": false, "error": "not a PackedScene"}
	var root: Node = scene.instantiate()
	var out := {"ok": true, "nodes": [], "meshes": [], "bodies": [], "aabb": null}
	var box = null
	for node in _walk(root):
		out.nodes.append("%s:%s" % [node.get_class(), root.get_path_to(node)])
		if node is MeshInstance3D and node.mesh:
			var info := _mesh(root, node)
			var world: AABB = _xform(root, node) * node.mesh.get_aabb()
			info["aabb"] = {"position": _v(world.position), "size": _v(world.size)}
			out.meshes.append(info)
			box = world if box == null else box.merge(world)
		elif node is CollisionShape3D:
			var parent: Node = node.get_parent()
			out.bodies.append({"path": str(root.get_path_to(node)),
				"body": parent.get_class() if parent else null,
				"shape": node.shape.get_class() if node.shape else null})
	if box != null:
		out.aabb = {"position": _v(box.position), "size": _v(box.size)}
	root.free()
	return out


func _mesh(root: Node, node: MeshInstance3D) -> Dictionary:
	var mesh: Mesh = node.mesh
	var t := _xform(root, node)
	var info := {"path": str(root.get_path_to(node)), "origin": _v(t.origin), "scale": _v(t.basis.get_scale()), "surfaces": []}
	for i in mesh.get_surface_count():
		var arrays := mesh.surface_get_arrays(i)
		var index = arrays[Mesh.ARRAY_INDEX]
		var verts = arrays[Mesh.ARRAY_VERTEX]
		var count: int = index.size() if index else verts.size()
		var data: Dictionary = RenderingServer.mesh_get_surface(mesh.get_rid(), i)
		var primitive: int = data.get("primitive", Mesh.PRIMITIVE_TRIANGLES)
		var tris := 0
		if primitive == Mesh.PRIMITIVE_TRIANGLES:
			tris = count / 3
		elif primitive == Mesh.PRIMITIVE_TRIANGLE_STRIP:
			tris = maxi(count - 2, 0)
		var surface := {"primitive": PRIMITIVES[primitive], "triangles": tris, "vertices": verts.size(),
			"colors": arrays[Mesh.ARRAY_COLOR] != null, "uv2": arrays[Mesh.ARRAY_TEX_UV2] != null,
			"tangents": arrays[Mesh.ARRAY_TANGENT] != null,
			"lods": data.get("lods", []).size()}
		var mat = node.get_active_material(i)
		var source := "mesh"
		if node.material_override:
			source = "material_override"
		elif node.get_surface_override_material(i):
			source = "surface_override"
		var own = mesh.surface_get_material(i)
		if source != "mesh":
			surface["mesh_material"] = own.resource_name if own else null
		if mat is BaseMaterial3D:
			surface["material"] = {"class": mat.get_class(), "name": mat.resource_name, "source": source,
				"unshaded": mat.shading_mode == BaseMaterial3D.SHADING_MODE_UNSHADED,
				"vertex_color_albedo": mat.vertex_color_use_as_albedo,
				"albedo": _c(mat.albedo_color), "albedo_texture": mat.albedo_texture != null,
				"normal": mat.normal_enabled, "ao": mat.ao_enabled,
				"metallic": mat.metallic, "roughness": mat.roughness,
				"transparency": mat.transparency, "cull_mode": mat.cull_mode}
		elif mat:
			surface["material"] = {"class": mat.get_class(), "name": mat.resource_name, "source": source}
		info.surfaces.append(surface)
	return info


## Transform from the asset root to node, without needing a scene tree. The chain
## stops at a parent that is not a Node3D and after a top_level node, as Godot's
## own placement does.
func _xform(root: Node, node: Node) -> Transform3D:
	var t := Transform3D.IDENTITY
	var n: Node = node
	while n != root and n is Node3D:
		t = n.transform * t
		if n.top_level:
			break
		n = n.get_parent()
	return t


func _walk(node: Node) -> Array:
	var out := [node]
	for child in node.get_children():
		out.append_array(_walk(child))
	return out


func _v(v: Vector3) -> Array:
	return [snappedf(v.x, 0.0001), snappedf(v.y, 0.0001), snappedf(v.z, 0.0001)]


func _c(c: Color) -> Array:
	return [snappedf(c.r, 0.001), snappedf(c.g, 0.001), snappedf(c.b, 0.001), snappedf(c.a, 0.001)]
