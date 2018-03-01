import math
from morse.core.mathutils import Vector, Matrix, Euler
import copy

from morse.builder import bpymorse

from morse.builder.creator import ComponentCreator

from urdf_parser_py.urdf import URDF as URDFparser
from urdf_parser_py.urdf import Mesh, Box, Cylinder, Sphere

# Meshes are referenced in the URDF file relative to their package, eg:
# 'package://pepper_meshes/meshes/1.0/Torso.dae'
# MORSE will replace 'package://' by 'ROS_SHARE_ROOT':
import os
ROS_SHARE_ROOT=os.environ["ROS_PACKAGE_PATH"].split(":")[0] + "/"

MATERIALS = {}

EPSILON = 0.00001

class URDFLink:

    def __init__(self, urdf_link):

        self.name = urdf_link.name

        self.inertial = urdf_link.inertial
        self.visual = urdf_link.visual
        self.collision = urdf_link.collision

        self._get_origin()

        print("..Create Link {}".format(urdf_link.name))

    def _get_origin(self):
        """ Links do not define proper origin. We still try to extract one
        to correctly place the bones' tails when necessary (like, when a bone
        is not connected to any other child bone).
        """
        xyz = (0,0,0)
        rpy = (0,0,0)

        if self.inertial and self.inertial.origin:
            xyz = self.inertial.origin.xyz
            rpy = self.inertial.origin.rpy
        elif self.collision and self.collision.origin:
            xyz = self.collision.origin.xyz
            rpy = self.collision.origin.rpy
        elif self.visual and self.visual.origin:
            xyz = self.visual.origin.xyz
            rpy = self.visual.origin.rpy

        self.xyz = Vector(xyz)
        self.rot = Euler(rpy, 'XYZ').to_quaternion()


class URDFJoint:

    # cf urdf_parser_py.urdf.Joint.TYPES
    FIXED = "fixed"
    PRISMATIC = "prismatic"
    REVOLUTE = "revolute"

    TYPES = [FIXED, PRISMATIC, REVOLUTE]

    def __init__(self, urdf_joint, urdf_link):
        print("Create Joint {}".format(urdf_joint.name))
        
        self.name = urdf_joint.name
        self.type = urdf_joint.type

        xyz = (0,0,0)
        rpy = (0,0,0)

        if urdf_joint.origin:
            xyz = urdf_joint.origin.xyz
            if urdf_joint.origin.rpy:
                # self.rot is the *orientation* of the frame of the joint, in
                # *world coordinates*
                rpy = urdf_joint.origin.rpy

        self.xyz = Vector(xyz)
        self.rot = Euler(rpy, 'XYZ').to_quaternion()

        self.link = URDFLink(urdf_link)

        self.axis = urdf_joint.axis
        self.limit = urdf_joint.limit

        self.children = []

        # edit/access this member *only* in EditMode
        self.editbone = None

        # edit/access this member *only* in PoseMode/ObjectMode
        self.posebone = None

    def add_child(self, urdf_joint, urdf_link):
        child = URDFJoint(urdf_joint, urdf_link)
        self.children.append(child)
        return child

    def build_editmode(self, armature, parent = None):
        # Create Blender bones
        #
        # URDF joints map to bones, URDF links map to objects.
        # Procedure:
        #   - create bone and set it to joint-position
        #   - give the bone an lenght close to 0 (=> EPSILON)
        #   - repeat it for each child

        self.editbone = armature.data.edit_bones.new(self.name)

        if parent:
            self.editbone.parent = parent.editbone
            self.editbone.head = self.rot * self.xyz + parent.editbone.head

        else:
            self.editbone.head = self.rot * self.xyz

        self.editbone.tail = self.rot * Vector((0, 0, EPSILON)) + self.editbone.head

        for child in self.children:
            child.build_editmode(armature, self)

    def build_objectmode(self, armature, parent = None):

        if not self.children and self.type == self.FIXED:
            if not parent:
                return

            target = self.add_link_frame(armature, parent, self.xyz, self.rot)
            
            # Disabled generation of IK targets for now: would require one
            # armature per 'kinematic group' to work well (currently, it creates 
            # cycles
            #
            ## if the parent has only one such 'end frame', use it as IK target
            ## TODO: if more than one, select one randomly?
            #if len(parent.children) == 1:
            #    ik = parent.posebone.constraints.new("IK")
            #    ik.use_rotation = True
            #    ik.use_tail = True
            #    ik.target = target
            ####################################################################

            return

        try:
            self.posebone = armature.pose.bones[self.name]
        except KeyError:
            print("Error: bone %s not yet added to the armature" % self.name)
            return

        # Prevent moving or rotating bones that are not end-effectors (outside of IKs)
        if self.children:
            self.posebone.lock_location = (True, True, True)
            self.posebone.lock_rotation = (True, True, True)
            self.posebone.lock_scale = (True, True, True)

        # initially, lock the IK
        self.posebone.lock_ik_x = True
        self.posebone.lock_ik_y = True
        self.posebone.lock_ik_z = True

        self.configure_joint(self.posebone)

        self.add_link_frame(armature)

        for child in self.children:
            child.build_objectmode(armature, self)

    def configure_joint(self, posebone):

        # First, configure joint axis
        if not self.axis:
            return

        print("Joint axis for <%s> (%s): %s" % (self.name, self.type, self.axis))


        # Then, IK limits
        if self.axis[0]:
            posebone.lock_ik_x = False
            posebone.use_ik_limit_x = True
            if self.limit:
                posebone.ik_max_x = self.limit.upper
                posebone.ik_min_x = self.limit.lower
        elif self.axis[1]:
            posebone.lock_ik_y = False
            posebone.use_ik_limit_y = True
            if self.limit:
                posebone.ik_max_y = self.limit.upper
                posebone.ik_min_y = self.limit.lower
        elif self.axis[2]:
            posebone.lock_ik_z = False
            posebone.use_ik_limit_z = True
            if self.limit:
                posebone.ik_max_z = self.limit.upper
                posebone.ik_min_z = self.limit.lower

    def add_link_frame(self, armature, joint = None, xyz = None, rot = None):
        """
        :param joint: if the link has no proper bone (case for fixed joints at
        the end of an armature), we need to specify the joint we want to attach
        the link to (typically, the parent joint)

        """
        if not self.link.visual:
            return

        if not joint:
            joint = self

        visuals = create_objects_by_link(armature, self.link)

        for v in visuals:
            v.matrix_local = armature.data.bones[joint.name].matrix_local

            if xyz and rot:
                v.location += rot * xyz
            elif xyz:
                v.location += xyz

            # reset rotation
            v.rotation_quaternion = self.link.rot

            self.rescale_object(self.link.visual.geometry, v)

            self.add_material(v)

            # parent the visuals to the armature
            armature.data.bones[joint.name].use_relative_parent = True
            v.parent = armature
            v.parent_bone = joint.name
            v.parent_type = "BONE"

    def rescale_object(self, geometry, obj):
        if isinstance(geometry, Mesh):
            if geometry.scale:
                obj.scale = [obj.scale[0] * geometry.scale[0],
                             obj.scale[1] * geometry.scale[1],
                             obj.scale[2] * geometry.scale[2]]

        elif isinstance(geometry, Box):
            obj.dimensions = geometry.size

        elif isinstance(geometry, Cylinder):
            diameter = geometry.radius*2
            length = geometry.length
            obj.dimensions = (diameter, diameter, length)

        elif isinstance(geometry, Sphere):
            diameter = geometry.radius*2
            obj.dimensions = (diameter, diameter, diameter)

    def add_material(self, obj):
        """ Adding material to scene if not exist and let
            the object use it. We differentiate between local
            and global material (local is first priority).
            We ignore the alpha value of the material color.
            TODO: Adding Texture
        """
        if not self.link.visual or not self.link.visual.material:
            return

        material = self.link.visual.material

        if not material.name:
            print("Found material without name: {}".format(self.link.material))
            return

        rgba = None
        texture = None

        # local material
        if material.color or material.texture:
            if material.color:
                rgba = material.color.rgba

            if material.texture:
                texture = material.texture

        # global material
        else:
            if material.name not in MATERIALS:
                print("Global material not found: {}".format(material.name))
                return

            rgba = MATERIALS[material.name]['color']
            texture = MATERIALS[material.name]['texture']


        mat = bpymorse.get_material(material.name)
        if not mat:
            mat = bpymorse.get_materials().new(name=material.name)

        try:
            obj.data.materials.append(mat)
            mat.diffuse_color = (rgba[0], rgba[1], rgba[2])
        except AttributeError:
            # ==> Camera, Light .. have no material
            # TODO: Remove this try-except block and implement a
            # object-type query for catch this error
            pass


    def __repr__(self):
        return "URDF joint<%s>" % self.name

class URDF(ComponentCreator):

    _classpath="morse.robots.urdf.URDF"

    def __init__(self, name, urdf):

        ComponentCreator.__init__(self, 
                                name, 
                                'robots')

        self.urdf_file = urdf
        self.urdf = URDFparser.from_xml_string(open(urdf,'r').read())

        for mat in self.urdf.materials:
            add_material(mat)

        self.base_link = URDFLink(self.urdf.link_map[self.urdf.get_root()])
        self.roots = self._walk_urdf(self.urdf.link_map[self.urdf.get_root()])

        self.build()

    def _walk_urdf(self, link, parent_bone = None):
        bones = []
        for joint, child_link in self._get_urdf_connections(link):
            if parent_bone:
                bone = parent_bone.add_child(joint, child_link)
            else:
                bone = URDFJoint(joint, child_link)

            self._walk_urdf(child_link, bone)
            bones.append(bone)
        return bones

    def _get_urdf_connections(self, link):
        joints = [joint for joint in self.urdf.joints if joint.parent == link.name]
        return [(joint, self.urdf.link_map[joint.child]) for joint in joints]


    def build(self):
        # Create armature and object
        bpymorse.add_object(
            type='ARMATURE', 
            enter_editmode=True,
            location=(0,0,0))
        ob = bpymorse.get_context_object()
        ob.show_x_ray = True
        ob.name = self.name
        amt = ob.data
        amt.name = self.name+'_armature'
        amt.show_axes = True

        bpymorse.mode_set(mode='EDIT')
        for root in self.roots:
            root.build_editmode(ob)

        # creating base-link visual, if exist
        if self.base_link.visual:
            visuals = create_objects_by_link(ob, self.base_link)
            for v in visuals:
                v.parent = ob
                v.parent_type = "ARMATURE"

        bpymorse.mode_set(mode='OBJECT')
        for root in self.roots:
            root.build_objectmode(ob)

def add_material(urdf_material):
    """ Add a urdf_material to the global MATERIALS dictonary.
    """
    global MATERIALS

    if urdf_material.name in MATERIALS:
        return

    MATERIALS[urdf_material.name] = {'color': None, 'texture': None}

    if urdf_material.color and urdf_material.color.rgba:
        MATERIALS[urdf_material.name]['color'] = urdf_material.color.rgba

    if urdf_material.texture and urdf_material.texture.filename:
        MATERIALS[urdf_material.name]['texture'] = urdf_material.texture.filename

def create_objects_by_link(armature, link):
    """ Creates a object from a given URDFLink.
        Also sets the armature as parent.

        :param armature: parent armature for the object
        :param link: related URDFLink
        :return: list of all created objects
    """
    visuals = []

    if link.visual and link.visual.geometry:
        geometry = link.visual.geometry

        if isinstance(geometry, Mesh):

            path = geometry.filename.replace("package:/", ROS_SHARE_ROOT)
            # Save a list of objects names before importing Collada/STL
            objects_names = [obj.name for obj in bpymorse.get_objects()]

            if ('.dae' in geometry.filename):
                # Import Collada from filepath
                bpymorse.collada_import(filepath=path)
                # Get a list of the imported objects
                visuals = [obj for obj in bpymorse.get_objects() \
                              if obj.name not in objects_names]
            elif ('.stl' in geometry.filename):
                bpymorse.stl_import(filepath=path)
                # Get a list of the imported objects
                visuals = [obj for obj in bpymorse.get_objects() \
                              if obj.name not in objects_names]

            if geometry.scale:
                for v in visuals:
                    v.scale = [v.scale[0] * geometry.scale[0],
                               v.scale[1] * geometry.scale[1],
                               v.scale[2] * geometry.scale[2]]

        elif isinstance(geometry, Box):
            bpymorse.add_mesh_cube()
            ob = bpymorse.active_object()
            ob.name = link.name
            ob.dimensions = geometry.size
            visuals = [ob]

        elif isinstance(geometry, Cylinder):
            radius = geometry.radius
            length = geometry.length
            bpymorse.add_mesh_cylinder(radius=radius, depth=length)
            ob = bpymorse.active_object()
            ob.name = link.name
            visuals = [ob]

        elif isinstance(geometry, Sphere):
            bpymorse.add_mesh_uv_sphere(size=geometry.radius)
            ob = bpymorse.active_object()
            ob.name = link.name
            visuals = [ob]

    else:
        bpymorse.add_empty(type = "ARROWS")

        empty = bpymorse.get_first_selected_object()
        empty.name = link.name
        empty.scale = [0.01, 0.01, 0.01]
        visuals = [empty]

    for v in visuals:
        # set link origin and rotation
        v.location = link.xyz
        v.rotation_mode = "QUATERNION"
        v.rotation_quaternion = link.rot
        bpymorse.origin_set(type='ORIGIN_CURSOR')

    return visuals