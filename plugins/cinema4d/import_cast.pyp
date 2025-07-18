import c4d
import os
import math
import mxutils
import array

from c4d import plugins, Vector, Vector4d, CPolygon, gui, BaseObject

PLUGIN_RES_DIR = os.path.join(os.path.dirname(__file__), "res")

mxutils.ImportSymbols(PLUGIN_RES_DIR)

with mxutils.LocalImportPath(PLUGIN_RES_DIR):
    from cast import Cast, CastColor, Model, Animation, Instance, Metadata, File, Color

__pluginname__ = "Cast (*.cast)"


class CastLoader(plugins.SceneLoaderData):
    def Init(self, node, isCloneInit):
        data = node.GetDataInstance()
        data.SetBool(CAST_IMPORT_BIND_SKIN, True)
        data.SetBool(CAST_IMPORT_IK_HANDLES, True)
        data.SetBool(CAST_IMPORT_CONSTRAINTS, True)
        return True

    def Identify(self, node, name, probe, size):
        if "cast" in name[-4:]:
            return True
        return False

    def Load(self, node, name, doc, filterflags, error, bt):
        importCast(doc, node, name)
        return c4d.FILEERROR_NONE


def importMetadata(doc, meta):
    doc[c4d.DOCUMENT_INFO_AUTHOR] = meta.Author()
    doc[c4d.DOCUMENT_INFO_PRGCREATOR_NAME] = meta.Software()


def importCast(doc, node, path):
    cast = Cast.load(path)

    instances = []
    meta = None

    for root in cast.Roots():
        for child in root.ChildrenOfType(Model):
            importModelNode(doc, node, child, path)
        #for child in root.ChildrenOfType(Animation):
        #    importAnimationNode(doc, child, path)
        for child in root.ChildrenOfType(Instance):
            instances.append(child)

        # Grab the first defined meta node, if there is one.
        meta = meta or root.ChildOfType(Metadata)

    if len(instances) > 0:
        importInstanceNodes(doc, node, instances, path)

    if meta:
        importMetadata(doc, meta)


def utilityBuildPath(root, asset):
    if os.path.isabs(asset):
        return asset

    root = os.path.dirname(root)
    return os.path.join(root, asset)


def utilityQuaternionToEuler(tempQuat):
    quaternion = c4d.Quaternion()

    w = tempQuat[3]
    ww = 2 * math.acos(w)
    sqrt = math.sqrt(1 - w * w)

    if sqrt <= 1e-9:
        x = y = z = 0
    else:
        x = tempQuat[0] / sqrt
        y = tempQuat[1] / sqrt
        z = tempQuat[2] / sqrt

    quaternion.SetAxis(Vector(x, y, -z), ww)
    return c4d.utils.MatrixToHPB(quaternion.GetMatrix(), c4d.ROTATIONORDER_HPB)


def utilityCreateDefaultMaterial(path, material):
    # We don't care much about this material since 99% of users will use the renderers material system
    # We just create it basic in a most convinient way to let the user convert it to their prefered renderer.

    mat = c4d.Material()
    mat.SetName(material.Name())

    mat[c4d.MATERIAL_USE_COLOR] = True

    reflLayer = mat.GetReflectionLayerIndex(0)

    switcher = {
        "albedo": c4d.MATERIAL_COLOR_SHADER,
        "diffuse": c4d.MATERIAL_COLOR_SHADER,
        "specular": reflLayer.GetDataID() + c4d.REFLECTION_LAYER_COLOR_TEXTURE,
        "normal": c4d.MATERIAL_NORMAL_SHADER,
        "roughness": reflLayer.GetDataID() + c4d.REFLECTION_LAYER_MAIN_SHADER_ROUGHNESS,
        "gloss": reflLayer.GetDataID() + c4d.REFLECTION_LAYER_MAIN_SHADER_ROUGHNESS,
        "emissive": c4d.MATERIAL_LUMINANCE_SHADER,
    }

    # Loop and connect the slots
    slots = material.Slots()
    for slot in slots:
        connection = slots[slot]
        if not connection.__class__ is File:
            continue
        if not slot in switcher:
            continue

        if connection.__class__ is File:
            shader = c4d.BaseList2D(c4d.Xbitmap)
            shader[c4d.BITMAPSHADER_FILENAME] = utilityBuildPath(path, connection.Path())
            shader.SetName(slot)

            if slot in ["metal", "gloss", "roughness", "normal"]:
                shader[c4d.BITMAPSHADER_COLORPROFILE] = c4d.BITMAPSHADER_COLORPROFILE_LINEAR

            mat[switcher[slot]] = shader
            mat.InsertShader(shader)
        elif connection.__class__ is Color:
            # Handle color conversion if necessary, Cinema 4D color input is sRGB.
            # Not necessary but might work as a guide the users own materials
            if connection.ColorSpace() == "linear":
                rgba = CastColor.toSRGBFromLinear(connection.Rgba())
            else:
                rgba = connection.Rgba()
            mat[c4d.MATERIAL_COLOR_COLOR] = Vector(rgba[0], rgba[1], rgba[2])
        else:
            continue
        if slot == "normal":
            mat[c4d.MATERIAL_USE_NORMAL] = True
        elif slot in ["gloss", "roughness"]:
            # Using it to add a roughness texture, so the user convert the material to the prefered render engine
            mat[reflLayer.GetDataID() + c4d.REFLECTION_LAYER_MAIN_DISTRIBUTION] = GGX 
        elif slot == "emissive":
            mat[c4d.MATERIAL_USE_LUMINANCE] = True

    mat[c4d.REFLECTION_LAYER_IMPORTED] = True

    return mat


def importMaterialNode(context, path, material):
    # We're checking if the material is already present in the project and import context
    doc = c4d.documents.GetActiveDocument()
    docMaterials = doc.GetMaterials()
    contextMaterials = context.GetMaterials()
    for mat in docMaterials + contextMaterials:
        if mat.GetName() == material.Name():
            return mat

    mat = utilityCreateDefaultMaterial(path, material)

    # TODO: going through all the updates functions to know which are actually necessary
    mat.Message(c4d.MSG_UPDATE)
    mat.Update(True, True)
    context.InsertMaterial(mat)

    return mat


def importModelNode(doc, node, model, path):
    # Extract the name of this model from the path
    modelName = model.Name() or os.path.splitext(os.path.basename(path))[0]

    # Create a collection for our objects
    modelNull = BaseObject(c4d.Onull)
    modelNull.SetName(modelName)
    modelNull[c4d.ID_BASELIST_ICON_COLORIZE_MODE] = c4d.ID_BASELIST_ICON_COLORIZE_MODE_CUSTOM
    modelNull[c4d.ID_BASELIST_ICON_COLOR] = Vector(0.816, 0.357, 0.259)

    doc.InsertObject(modelNull)

    # Import skeleton for binds, materials for meshes
    bones = importSkeletonNode(modelNull, model.Skeleton())
    materialArray = {x.Name(): importMaterialNode(doc, path, x)
                     for x in model.Materials()}

    meshes = model.Meshes()
    meshHandles = {}

    for mesh in meshes:
        newMesh = BaseObject(c4d.Opolygon)
        newMesh.SetName(mesh.Name() or "CastMesh")

        # Store for later creating blend shapes if necessary.
        meshHandles[mesh.Hash()] = newMesh

        vertexPositions = mesh.VertexPositionBuffer()
        vertexCount = int(len(vertexPositions) / 3)

        faces = mesh.FaceBuffer()
        faceIndicesCount = len(faces)
        facesCount = int(faceIndicesCount / 3)

        newMesh.ResizeObject(vertexCount, facesCount)

        for i in range(0, len(vertexPositions), 3):
            newMesh.SetPoint(
                int(i / 3), Vector(vertexPositions[i], vertexPositions[i + 1], -vertexPositions[i + 2]))

        # Remap face indices to match c4d's winding order
        faces = [face for x in range(0, faceIndicesCount, 3)
                 for face in (faces[x + 2], faces[x + 1], faces[x + 0])]

        for i in range(0, faceIndicesCount, 3):
            newMesh.SetPolygon(
                int(i / 3), CPolygon(faces[i], faces[i + 1], faces[i + 2]))

        meshMaterial = mesh.Material()

        for i in range(mesh.UVLayerCount()):
            uvBuffer = mesh.VertexUVLayerBuffer(i)
            uvTag = c4d.UVWTag(facesCount)

            for j in range(0, faceIndicesCount, 3):
                uvTag.SetSlow(int(j / 3),
                              Vector(uvBuffer[faces[j] * 2],
                                     uvBuffer[(faces[j] * 2) + 1], 0),
                              Vector(uvBuffer[faces[j + 1] * 2],
                                     uvBuffer[(faces[j + 1] * 2) + 1], 0),
                              Vector(uvBuffer[faces[j + 2] * 2],
                                     uvBuffer[(faces[j + 2] * 2) + 1], 0),
                              Vector(0, 0, 0))

            newMesh.InsertTag(uvTag)

            if meshMaterial is not None and i < 1:
                material_tag = newMesh.MakeTag(c4d.Ttexture)
                material_tag[c4d.TEXTURETAG_MATERIAL] = materialArray[meshMaterial.Name()]
                material_tag[c4d.TEXTURETAG_PROJECTION] = c4d.TEXTURETAG_PROJECTION_UVW

        for i in range(mesh.ColorLayerCount()):
            vertexColors = mesh.VertexColorLayerBuffer(i)
            vcTag = c4d.VertexColorTag(vertexCount)
            vcData = vcTag.GetDataAddressW()

            for v in range(vertexCount):
                color = CastColor.fromInteger(vertexColors[v])
                vcTag.SetPoint(vcData, None, None, v,
                               Vector4d(color[0], color[1], color[2], color[3]))

            newMesh.InsertTag(vcTag)

        vertexNormals = mesh.VertexNormalBuffer()
        if vertexNormals is not None:
            vnTag = c4d.NormalTag(facesCount)
            vnData = vnTag.GetDataAddressW()

            for i in range(0, faceIndicesCount, 3):
                vnTag.Set(vnData, int(i / 3), 
                         {"a": Vector(vertexNormals[faces[i] * 3],
                                       vertexNormals[(faces[i] * 3) + 1],
                                       -vertexNormals[(faces[i] * 3) + 2]),
                          "b": Vector(vertexNormals[faces[i + 1] * 3],
                                        vertexNormals[(faces[i + 1] * 3) + 1],
                                        -vertexNormals[(faces[i + 1] * 3) + 2]),
                          "c": Vector(vertexNormals[faces[i + 2] * 3],
                                        vertexNormals[(faces[i + 2] * 3) + 1],
                                        -vertexNormals[(faces[i + 2] * 3) + 2]),
                          "d": Vector(0, 0, 0)})

            newMesh.InsertTag(vnTag)

        if bones is not None and node[CAST_IMPORT_BIND_SKIN]:
            skinObj = BaseObject(c4d.Oskin)

            skinningMethod = mesh.SkinningMethod()

            if skinningMethod == "linear":
                skinObj[c4d.ID_CA_SKIN_OBJECT_TYPE] = c4d.ID_CA_SKIN_OBJECT_TYPE_LINEAR
            elif skinningMethod == "quaternion":
                skinObj[c4d.ID_CA_SKIN_OBJECT_TYPE] = c4d.ID_CA_SKIN_OBJECT_TYPE_QUAT

            doc.InsertObject(skinObj, parent=newMesh)

            weightTag = c4d.modules.character.CAWeightTag()

            newMesh.InsertTag(weightTag)

            for bone in bones.values():
                weightTag.AddJoint(bone)

            maximumInfluence = mesh.MaximumWeightInfluence()
            if maximumInfluence > 1:  # Slower path for complex weights
                weightBoneBuffer = mesh.VertexWeightBoneBuffer()
                weightValueBuffer = mesh.VertexWeightValueBuffer()

                for x in range(vertexCount):
                    for j in range(maximumInfluence):
                        weightIndex = j + (x * maximumInfluence)
                        weightValue = weightTag.GetWeight(
                            weightBoneBuffer[weightIndex], x)
                        weightValue += weightValueBuffer[weightIndex]
                        weightTag.SetWeight(
                            weightBoneBuffer[weightIndex], x, weightValue)
            elif maximumInfluence > 0:  # Fast path for simple weighted meshes
                weightBoneBuffer = mesh.VertexWeightBoneBuffer()
                for x in range(vertexCount):
                    weightTag.SetWeight(weightBoneBuffer[x], x, 1.0)

            weightTag.Message(c4d.MSG_UPDATE)

        doc.InsertObject(newMesh, parent=modelNull)
        newMesh.Message(c4d.MSG_UPDATE)

    if node[CAST_IMPORT_IK_HANDLES]:
        importSkeletonIKNode(model.Skeleton(), bones)

    if node[CAST_IMPORT_CONSTRAINTS]:
        importSkeletonConstraintNode(model.Skeleton(), bones)

    # TODO: Does this do anything?
    c4d.EventAdd()
    modelNull.Message(c4d.MSG_UPDATE)

    return modelNull


def importSkeletonConstraintNode(skeleton, bones):
    if skeleton is None:
        return

    for constraint in skeleton.Constraints():
        constraintBone = bones[constraint.ConstraintBone().Name()]
        targetBone = bones[constraint.TargetBone().Name()]

        type = constraint.ConstraintType()
        customOffset = constraint.CustomOffset()
        maintainOffset = constraint.MaintainOffset()
        weight = constraint.Weight()

        # C4D's constraint system is a bit worse than Blender's
        constraintTag = c4d.BaseTag(c4d.Tcaconstraint)
        constraintBone.InsertTag(constraintTag)
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR] = True

        # Disabling all constraints, cause default is enabled
        constraintTag[CONSTRAIN_POS] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_P_X] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_P_Y] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_P_Z] = False

        constraintTag[CONSTRAIN_SCALE] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_S_X] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_S_Y] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_S_Z] = False

        constraintTag[CONSTRAIN_ROT] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_R_X] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_R_Y] = False
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_R_Z] = False

        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_MAINTAIN] = maintainOffset
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_TWEIGHT] = weight

        if type == "pt":
            constraintTag[CONSTRAIN_POS] = True
            constraintTag[CONSTRAINT_TARGET] = targetBone
            constraintTag[c4d.ID_CA_CONSTRAINT_TAG_LOCAL_P] = True

            if customOffset:
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_P_OFFSET] = Vector(customOffset)
            if not constraint.SkipX():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_P_X] = True
            if not constraint.SkipY():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_P_Y] = True
            if not constraint.SkipZ():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_P_Z] = True
        elif type == "sc":
            constraintTag[CONSTRAIN_SCALE] = True
            constraintTag[CONSTRAINT_TARGET] = targetBone
            constraintTag[c4d.ID_CA_CONSTRAINT_TAG_LOCAL_S] = True

            if customOffset:
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_S_OFFSET] = Vector(customOffset)
            if not constraint.SkipX():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_S_X] = True
            if not constraint.SkipY():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_S_Y] = True
            if not constraint.SkipZ():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_S_Z] = True
        elif type == "or":
            constraintTag[CONSTRAIN_ROT] = True
            constraintTag[CONSTRAINT_TARGET] = targetBone
            constraintTag[c4d.ID_CA_CONSTRAINT_TAG_LOCAL_R] = True

            if customOffset:
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_R_OFFSET] = Vector(customOffset)
            if not constraint.SkipX():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_R_X] = True
            if not constraint.SkipY():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_R_Y] = True
            if not constraint.SkipZ():
                constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR_CONSTRAIN_R_Z] = True
        else:
            continue

        if constraint.Name() is not None:
            constraintTag[c4d.ID_BASELIST_NAME] = constraint.Name()


def importSkeletonIKNode(skeleton, bones):
    if skeleton is None or not skeleton.IKHandles():
        return

    for handle in skeleton.IKHandles():
        startBone = bones[handle.StartBone().Name()]
        endBone = bones[handle.EndBone().Name()]
        targetBone = bones[handle.TargetBone().Name()]

        constraintTag = c4d.BaseTag(c4d.Tcaconstraint)
        endBone.InsertTag(constraintTag)
        constraintTag[c4d.ID_CA_CONSTRAINT_TAG_PSR] = True
        constraintTag[CONSTRAINT_TARGET] = targetBone

        ikTag = c4d.BaseTag(IK_TAG)
        ikTag[c4d.ID_CA_IK_TAG_SOLVER] = 2
        ikTag[c4d.ID_CA_IK_TAG_TIP] = endBone
        ikTag[c4d.ID_CA_IK_TAG_TARGET] = targetBone
        startBone.InsertTag(ikTag)

        poleVectorBone = handle.PoleVectorBone()
        if poleVectorBone is not None:
            # Changing the IK solver from 3D to 2D to activate the pole input
            ikTag[c4d.ID_CA_IK_TAG_SOLVER] = 1
            ikTag[c4d.ID_CA_IK_TAG_POLE] = bones[poleVectorBone.Name()]


        poleBone = handle.PoleBone()
        if poleBone is not None:
            poleBone = bones[poleBone.Name()]
            xpressoTag = c4d.BaseTag(c4d.Texpresso)
            startBone.InsertTag(xpressoTag)
            
            gvNodeMaster = xpressoTag.GetNodeMaster()
            poleNode = gvNodeMaster.CreateNode(parent = gvNodeMaster.GetRoot(),
                                    id = c4d.ID_OPERATOR_OBJECT,
                                    x = 100,
                                    y = 200 )
            poleNode[c4d.GV_OBJECT_OBJECT_ID] = poleBone
            poleRotYPort = poleNode.AddPort(c4d.GV_PORT_OUTPUT, [c4d.ID_BASEOBJECT_REL_ROTATION, c4d.VECTOR_Y])
            
            twistNode = gvNodeMaster.CreateNode(parent = gvNodeMaster.GetRoot(),
                                    id = c4d.ID_OPERATOR_OBJECT,
                                    x = 400,
                                    y = 200 )
            twistNode[c4d.GV_OBJECT_OBJECT_ID] = ikTag
            twistInPort = twistNode.AddPort(c4d.GV_PORT_INPUT, c4d.ID_CA_IK_TAG_POLE_TWIST)

            poleRotYPort.Connect(twistInPort)


def importSkeletonNode(modelNull, skeleton):
    if skeleton is None:
        return None

    bones = skeleton.Bones()
    handles = [None] * len(bones)
    boneNames = {}

    for i, bone in enumerate(bones):
        newBone = BaseObject(c4d.Ojoint)
        newBone.SetName(bone.Name())

        tX, tY, tZ = bone.LocalPosition()
        translation = Vector(tX, tY, -tZ)

        rotation = utilityQuaternionToEuler(bone.LocalRotation())

        scale = bone.Scale() or (1.0, 1.0, 1.0)
        scale = Vector(scale[0], scale[1], scale[2])

        newBone.SetAbsPos(translation)
        newBone.SetAbsRot(rotation)
        newBone.SetAbsScale(scale)

        handles[i] = newBone
        boneNames[bone.Name()] = newBone

    for i, bone in enumerate(bones):
        if bone.ParentIndex() > -1:
            handles[i].InsertUnder(handles[bone.ParentIndex()])
        else:
            handles[i].InsertUnder(modelNull)

    return boneNames


def importAnimationNode():
    gui.MessageDialog(
        text="Animations are currently not supported.", type=c4d.GEMB_ICONSTOP)


def importInstanceNodes(doc, node, instanceNodes, path):
    rootPath = c4d.storage.LoadDialog(
        title='Select the root directory where instance scenes are located', flags=2)

    if rootPath is None:
        return gui.MessageDialog(text="Unable to import instances without a root directory!", type=c4d.GEMB_ICONSTOP)

    uniqueInstances = {}
    instanceImportError = False

    for instance in instanceNodes:
        refs = os.path.join(rootPath, instance.ReferenceFile().Path())

        if refs in uniqueInstances:
            uniqueInstances[refs].append(instance)
        else:
            uniqueInstances[refs] = [instance]

    name = os.path.splitext(os.path.basename(path))[0]

    # Create a collection for our objects
    rootNull = BaseObject(c4d.Onull)
    rootNull.SetName(name)
    rootNull[c4d.ID_BASELIST_ICON_COLORIZE_MODE] = c4d.ID_BASELIST_ICON_COLORIZE_MODE_CUSTOM
    rootNull[c4d.ID_BASELIST_ICON_COLOR] = Vector(0.816, 0.357, 0.259)

    doc.InsertObject(rootNull)

    instanceNull = BaseObject(c4d.Onull)
    instanceNull.SetName("%s_instances" % name)
    instanceNull.InsertUnder(rootNull)

    sceneNull = BaseObject(c4d.Onull)
    sceneNull.SetName("%s_scenes" % name)
    sceneNull.InsertUnder(rootNull)

    # Disable source models visibility
    sceneNull[c4d.ID_BASEOBJECT_VISIBILITY_EDITOR] = c4d.OBJECT_OFF
    sceneNull[c4d.ID_BASEOBJECT_VISIBILITY_RENDER] = c4d.OBJECT_OFF

    for instancePath, instances in uniqueInstances.items():
        instanceName = os.path.splitext(os.path.basename(instancePath))[0]

        try:
            cast = Cast.load(instancePath)
            for root in cast.Roots():
                for child in root.ChildrenOfType(Model):
                    modelNull = importModelNode(doc, node, child, instancePath)
            modelNull.InsertUnder(sceneNull)
        except:
            print("Failed to import instance: %s" % instancePath)
            instanceImportError = True
            continue

        for instance in instances:
            # Creates instance object
            newInstance = c4d.InstanceObject()

            newInstance.SetName(instance.Name() or instanceName)
            newInstance.InsertUnder(instanceNull)

            tX, tY, tZ = instance.Position()
            translation = Vector(tX, tY, -tZ)

            rotation = utilityQuaternionToEuler(instance.Rotation())

            scaleTuple = instance.Scale() or (1.0, 1.0, 1.0)
            scale = Vector(scaleTuple[0], scaleTuple[1], scaleTuple[2])

            newInstance.SetAbsPos(translation)
            newInstance.SetAbsRot(rotation)
            newInstance.SetAbsScale(scale)

            newInstance.SetReferenceObject(modelNull)

    if instanceImportError:
        gui.MessageDialog(text="Some instances failed to import.\nCheck the console for more details. ", type=c4d.GEMB_ICONEXCLAMATION)


def c4d_pos_to_cast_pos(p: c4d.Vector):
    """Converts a C4D coordinate vector to the cast format's coordinate system."""
    return (p.x, p.y, -p.z)


def c4d_quat_to_cast_quat(q):
    """Converts a C4D quaternion to the cast format's coordinate system."""
    # The import code flips the Z component of the rotation axis. We do the reverse on export.
    return (q.v.x, q.v.y, -q.v.z, q.w)

def export_material_node(c4d_mat, cast_model):
    """Exports a C4D material to a cast.Material node."""
    # Check if a material with the same name was already exported for this model
    for mat_node in cast_model.Materials():
        if mat_node.Name() == c4d_mat.GetName():
            return mat_node

    mat = cast_model.CreateMaterial()
    mat.SetName(c4d_mat.GetName())

    # Map C4D channels to cast material slots
    switcher = {
        c4d.MATERIAL_COLOR_SHADER: "albedo",
        c4d.MATERIAL_NORMAL_SHADER: "normal",
        c4d.MATERIAL_LUMINANCE_SHADER: "emissive",
    }
    
    for channel, slot_name in switcher.items():
        if c4d_mat[channel] and c4d_mat[channel].GetType() == c4d.Xbitmap:
            shader = c4d_mat[channel]
            path = shader[c4d.BITMAPSHADER_FILENAME]
            if path:
                file_node = cast_model.CreateFile()
                file_node.SetPath(path)
                mat.SetSlot(slot_name, file_node.Hash())

    # Handle PBR reflection channels (simplified)
    if c4d_mat.GetReflectionLayerIndex(0):
        reflLayer = c4d_mat.GetReflectionLayerIndex(0)
        # Specular Color
        if reflLayer.GetData(c4d.REFLECTION_LAYER_COLOR_TEXTURE) and reflLayer.GetData(c4d.REFLECTION_LAYER_COLOR_TEXTURE).GetType() == c4d.Xbitmap:
            shader = reflLayer.GetData(c4d.REFLECTION_LAYER_COLOR_TEXTURE)
            path = shader[c4d.BITMAPSHADER_FILENAME]
            if path:
                file_node = cast_model.CreateFile()
                file_node.SetPath(path)
                mat.SetSlot("specular", file_node.Hash())
        # Roughness
        if reflLayer.GetData(c4d.REFLECTION_LAYER_MAIN_SHADER_ROUGHNESS) and reflLayer.GetData(c4d.REFLECTION_LAYER_MAIN_SHADER_ROUGHNESS).GetType() == c4d.Xbitmap:
            shader = reflLayer.GetData(c4d.REFLECTION_LAYER_MAIN_SHADER_ROUGHNESS)
            path = shader[c4d.BITMAPSHADER_FILENAME]
            if path:
                file_node = cast_model.CreateFile()
                file_node.SetPath(path)
                mat.SetSlot("roughness", file_node.Hash())

    return mat

def ReadNormalTag(tag: c4d.NormalTag) -> list[c4d.Vector]:
    """Reads a `c4d.NormalTag` to a list of c4d.Vector."""
    if not (isinstance(tag, c4d.BaseTag) and tag.CheckType(c4d.Tnormal)):
        msg = f"Expected normal tag, received: {tag}."
        raise TypeError(tag)

    buffer = tag.GetLowlevelDataAddressR()
    if buffer is None:
        msg = "Failed to retrieve memory buffer for VariableTag."
        raise RuntimeError(msg)

    data = array.array('h')
    data.frombytes(buffer)
    # Convert the int16 representation of the normals to c4d.Vector.
    return [c4d.Vector(data[i-3] / 32000.0,
                       data[i-2] / 32000.0,
                       data[i-1] / 32000.0)
            for i in range(3, len(data) + 3, 3)]

def export_mesh_node(poly_obj, cast_model, bone_map, doc):
    """Exports a PolygonObject to a cast.Mesh node, handling vertex splitting."""
    if not isinstance(poly_obj, c4d.PolygonObject):
        return None

    # Triangulate the mesh on a clone to not affect the original scene
    clone = poly_obj.GetClone(c4d.COPYFLAGS_0)
    res = c4d.utils.SendModelingCommand(
        command=c4d.MCOMMAND_TRIANGULATE,
        list=[clone],
        mode=c4d.MODELINGCOMMANDMODE_POLYGONSELECTION,
        doc=doc
    )
    if not res:
        print(f"Warning: Could not triangulate mesh {poly_obj.GetName()}")
        # Proceed with the original clone
    
    points = clone.GetAllPoints()
    polys = clone.GetAllPolygons()
    
    if not polys:
        return None # Skip empty meshes

    mesh = cast_model.CreateMesh()
    mesh.SetName(poly_obj.GetName())

    # Get Tags
    uv_tag = clone.GetTag(c4d.Tuvw)
    normal_tag = clone.GetTag(c4d.Tnormal)
    vc_tag = clone.GetTag(c4d.Tvertexcolor)
    weight_tag = poly_obj.GetTag(c4d.Tweights) # Use original object for weights
    
    # Data for the new, split-vertex mesh
    vertex_map = {}
    out_verts, out_uvs, out_normals, out_colors, out_faces = [], [], [], [], []
    out_bone_indices, out_bone_weights = [], []
    max_influence = 0
    point_idx_counter = 0
	
    normals = ReadNormalTag(normal_tag)

    # Process faces and split vertices
    for poly_idx, poly in enumerate(polys):
        new_face = []
        indices = [poly.a, poly.b, poly.c]
        
        # C4D stores UVs per-polygon-vertex. We need to map this to a per-vertex format.
        uv_poly_data = uv_tag.GetSlow(poly_idx) if uv_tag else None
		

        for i in range(3):
            p_idx = indices[i]
            pos = points[p_idx]
            
            uv = (0,0)
            if i == 0:
                uv = (uv_poly_data['a'].x, uv_poly_data['a'].y) if uv_poly_data else (0.0, 0.0) 
            elif i == 1:
                uv = (uv_poly_data['b'].x, uv_poly_data['b'].y) if uv_poly_data else (0.0, 0.0)
            elif i == 2:
                uv = (uv_poly_data['c'].x, uv_poly_data['c'].y) if uv_poly_data else (0.0, 0.0)          

            norm = c4d.Vector(0,1,0)
            if normal_tag:
                norm = normals[(poly_idx * 4) + i]

            color = (1.0, 1.0, 1.0, 1.0)
            if vc_tag:
                vc_data = vc_tag.GetDataAddressR()
                all_data = c4d.VertexColorTag.GetAllHighlevelData(vc_data)
                if p_idx < len(all_data):
                    c = all_data[p_idx]
                    color = (c['r'], c['g'], c['b'], c['a'])

            vertex_key = (p_idx, uv, norm, color)

            if vertex_key in vertex_map:
                new_face.append(vertex_map[vertex_key])
            else:
                new_idx = point_idx_counter
                vertex_map[vertex_key] = new_idx
                new_face.append(new_idx)
                
                out_verts.append(c4d_pos_to_cast_pos(pos))
                out_uvs.append(uv)
                out_normals.append(c4d_pos_to_cast_pos(norm))
                out_colors.append(CastColor.toInteger(color))
                
                # Copy skin weights from original vertex                
                if weight_tag and bone_map:
                    joint_count = weight_tag.GetJointCount()
                    num_influences = 0
                    for j in range(joint_count):
                        weight = weight_tag.GetWeight(j, p_idx)
                        if weight > 0.001:
                            c4d_joint = weight_tag.GetJoint(j)                    
                            if c4d_joint.GetName() in bone_map:                                
                                bone_index = bone_map[c4d_joint.GetName()]["index"]
                                out_bone_indices.append(bone_index)
                                out_bone_weights.append(weight)
                                num_influences += 1

                    for x in range(4 - num_influences):
                        out_bone_indices.append(0)
                        out_bone_weights.append(0)
                    
                    if num_influences > max_influence:
                        max_influence = num_influences


                point_idx_counter += 1

        # Adjust winding order for cast format (based on importer)
        out_faces.extend([new_face[2], new_face[1], new_face[0]])

    # Set mesh data
    mesh.SetVertexPositionBuffer(out_verts)
    mesh.SetFaceBuffer(out_faces)

    if out_normals:
        mesh.SetVertexNormalBuffer(out_normals)

    if uv_tag:
        mesh.SetUVLayerCount(1)
        mesh.SetVertexUVLayerBuffer(0, out_uvs)

    if vc_tag:
        mesh.SetColorLayerCount(1)
        mesh.SetVertexColorBuffer(0, out_colors)

    # Link material
    tex_tag = poly_obj.GetTag(c4d.Ttexture)
    if tex_tag and tex_tag.GetMaterial():
        c4d_mat = tex_tag.GetMaterial()
        mat_node = export_material_node(c4d_mat, cast_model)
        mesh.SetMaterial(mat_node.Hash())

    # Set skinning data
    print("bone_map")
    print(bone_map)
    print("max_influence")
    print(max_influence)
    if weight_tag and bone_map and max_influence > 0:
        mesh.SetMaximumWeightInfluence(max_influence)
        mesh.SetSkinningMethod("linear") # or "quaternion"
        # The cast format expects separate, padded buffers. This part is complex and simplified here.
        # A full implementation would need to pad the weight/bone buffers to max_influence for each vertex.
        # This simplified version just writes all found weights.
        if out_bone_indices:
             print("out_bone_indices")
             print(out_bone_indices)
             print("out_bone_weights")
             print(out_bone_weights)
             mesh.SetVertexWeightBoneBuffer(out_bone_indices)
             mesh.SetVertexWeightValueBuffer(out_bone_weights)
    
    return mesh


def export_skeleton_node(skel_root, cast_model):
    """Exports a C4D joint hierarchy to a cast.Skeleton node."""
    if not skel_root:
        print("not skel_root")
        return None, None
        
    skeleton = cast_model.CreateSkeleton()
    
    bones_to_export = []
    # Traverse hierarchy to find all joints
    obj_stack = [skel_root]
    while obj_stack:
        obj = obj_stack.pop(0)
        if obj.IsInstanceOf(c4d.Ojoint):
            bones_to_export.append(obj)            
        obj_stack.extend(obj.GetChildren())

    if not bones_to_export:
        # No joints found, remove skeleton node
        cast_model.childNodes.remove(skeleton)
        return None, None
    
        
    bone_list = []
    bone_map = {} # map c4d object -> { "node": cast_node, "index": i }

    # First pass: create all bone nodes and build map
    for i, c4d_bone in enumerate(bones_to_export):
        bone = skeleton.CreateBone()
        bone.SetName(c4d_bone.GetName())
        bone_list.append(bone)      
        bone_map[c4d_bone.GetName()] = {"node": bone, "index": i}

    # Second pass: set properties and hierarchy
    for i, c4d_bone in enumerate(bones_to_export):
        bone = bone_list[i]
        
        # Set Parent
        parent = c4d_bone.GetUp()
        parent_index = -1
        if parent and parent.GetName() in bone_map:
            parent_index = bone_map[parent.GetName()]["index"]
        bone.SetParentIndex(parent_index)
        
        # Set Transforms
        pos = c4d_bone.GetRelPos()
        rot = c4d_bone.GetRelRot() # HPB
        scale = c4d_bone.GetRelScale()
        scaleTuple = (scale.x, scale.y, scale.z)

        # Convert rotation to quaternion
        matrix = c4d.utils.HPBToMatrix(rot, c4d.ROTATIONORDER_HPB)
        quat = c4d.Quaternion()
        quat.SetMatrix(matrix)

        bone.SetLocalPosition(c4d_pos_to_cast_pos(pos))
        bone.SetLocalRotation(c4d_quat_to_cast_quat(quat))
        bone.SetScale(list(scaleTuple))

    return skeleton, bone_map


def export_model_node(doc, obj, cast_root):
    """Exports a C4D object and its hierarchy as a cast.Model."""
    model = cast_root.CreateModel()
    model.SetName(obj.GetName())
    
    # Export skeleton first to get the bone_map
    # We assume the first child is the skeleton root if it's a joint
    skel_root = None
    for ch in obj.GetChildren():
        if ch.IsInstanceOf(c4d.Ojoint):
            skel_root = ch
            break
    print("skel_root:")
    print(skel_root)
    skeleton, bone_map = export_skeleton_node(skel_root, model)

    # Find and export all mesh objects in the hierarchy
    obj_stack = [obj]
    while obj_stack:
        current_obj = obj_stack.pop(0)
        if current_obj.IsInstanceOf(c4d.Opolygon):
            export_mesh_node(current_obj, model, bone_map, doc)
        
        obj_stack.extend(current_obj.GetChildren())

    # Export metadata (simplified)
    meta = cast_root.CreateMetadata()
    meta.SetAuthor(doc[c4d.DOCUMENT_INFO_AUTHOR] or "Unknown")
    meta.SetSoftware("Cinema 4D")
    meta.SetUpAxis("Y") # C4D is Y-up, but format seems to expect Z flip

    return model


def exportCast(doc, path):
    """Main export function."""
    
    selected_objs = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_SELECTIONORDER)
    if not selected_objs:
        gui.MessageDialog("Please select objects to export.")
        return False
        
    export_root = None
    if len(selected_objs) == 1 and selected_objs[0].IsInstanceOf(c4d.Onull):
        export_root = selected_objs[0]
    else:
        # Create a temporary null to parent the selection
        export_root = BaseObject(c4d.Onull)
        export_root.SetName(os.path.splitext(os.path.basename(path))[0])
        for obj in selected_objs:            
            #obj.InsertUnder(export_root)
            obj.GetClone(c4d.COPYFLAGS_0).InsertUnder(export_root)
    
    cast = Cast()
    root = cast.CreateRoot()

    export_model_node(doc, export_root, root)

    try:
        cast.save(path)
    except Exception as e:
        gui.MessageDialog(f"Failed to save .cast file.\nError: {e}")
        return False

    return True


class CastSaver(plugins.SceneSaverData):
    
    def Identify(self, name):
        if name.lower().endswith(".cast"):
            return True
        return False

    def Save(self, node, name, doc, filterflags):
        if exportCast(doc, name):
            return c4d.FILEERROR_NONE
        else:
            return c4d.FILEERROR_WRITE

if __name__ == '__main__':
    # Register Loader
    loader_reg = plugins.RegisterSceneLoaderPlugin(id=SCENE_LOADER_PLUGIN_ID, # Please use your own unique ID
                                            str=__pluginname__,
                                            info=0,
                                            g=CastLoader,
                                            description="fcastloader",
                                            )
    # Register Saver
    saver_reg = plugins.RegisterSceneSaverPlugin(id=SCENE_SAVER_PLUGIN_ID,
                                                 str=__pluginname__,
                                                 info=0,
                                                 g=CastSaver,
                                                 description="castsaver",
                                                 suffix="cast")
