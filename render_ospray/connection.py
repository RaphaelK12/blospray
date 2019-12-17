# ======================================================================== #
# BLOSPRAY - OSPRay as a Blender render engine                             #
# Paul Melis, SURFsara <paul.melis@surfsara.nl>                            #
# Connection to render server, scene export, result handling               #
# ======================================================================== #
# Copyright 2018-2019 SURFsara                                             #
#                                                                          #
# Licensed under the Apache License, Version 2.0 (the "License");          #
# you may not use this file except in compliance with the License.         #
# You may obtain a copy of the License at                                  #
#                                                                          #
#     http://www.apache.org/licenses/LICENSE-2.0                           #
#                                                                          #
# Unless required by applicable law or agreed to in writing, software      #
# distributed under the License is distributed on an "AS IS" BASIS,        #
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. #
# See the License for the specific language governing permissions and      #
# limitations under the License.                                           #
# ======================================================================== #

# - Make sockets non-blocking and use select() to handle errors on the server side

import bpy, bmesh
#from bgl import *
from mathutils import Vector, Matrix

import sys, array, json, os, select, socket, time, weakref
from math import tan, atan, degrees, radians, sqrt
from struct import pack, unpack

import numpy

sys.path.insert(0, os.path.split(__file__)[0])

from .common import PROTOCOL_VERSION, OSP_FB_RGBA32F, send_protobuf, receive_protobuf, substitute_values
from .messages_pb2 import (
    HelloResult,
    ClientMessage,
    WorldSettings, CameraSettings, LightSettings, RenderSettings,
    UpdateObject, UpdatePluginInstance,
    MeshData,
    GenerateFunctionResult, RenderResult,    
    Volume, Slices, Slice, Color,
    MaterialUpdate, 
    AlloySettings, CarPaintSettings, GlassSettings, LuminousSettings, MetalSettings,
    MetallicPaintSettings, OBJMaterialSettings, PrincipledSettings, ThinGlassSettings
)

# Object to world matrix
#
# Matrix(((0.013929054141044617, 0.0, 0.0, -0.8794544339179993),
#         (0.0, 0.013929054141044617, 0.0, -0.8227154612541199),
#         (0.0, 0.0, 0.013929054141044617, 0.0),
#         (0.0, 0.0, 0.0, 1.0)))
#
# Translation part is in right-most column

def matrix2list(m):
    """Convert to list of 16 floats"""
    values = []
    for row in m:
        values.extend(list(row))
    return values

def customproperties2dict(obj, filepath_keys=['file']):
    user_keys = [k for k in obj.keys() if k not in ['_RNA_UI', 'ospray']]
    properties = {}
    for k in user_keys:
        v = obj[k]
        if hasattr(v, 'to_dict'):
            properties[k] = v.to_dict()
        elif hasattr(v, 'to_list'):
            properties[k] = v.to_list()
        elif isinstance(v, str):
            if k == 'file' or k.endswith('_file'):
                # Convert blendfile-relative paths to full paths, e.g.
                # //.../file.name -> /.../.../file.name
                v = bpy.path.abspath(v)
            properties[k] = v
        else:
            # XXX assumes simple type that can be serialized to json
            properties[k] = v

    return properties


class Connection:

    def __init__(self, engine, host, port):
        self.engine = weakref.ref(engine)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        self.host = host
        self.port = port

        self.framebuffer_width = self.framebuffer_height = None

    def connect(self):
        self.engine().update_stats('', 'Connecting')

        try:            
            self.sock.connect((self.host, self.port))            
        except:            
            return False

        # Handshake
        client_message = ClientMessage()
        client_message.type = ClientMessage.HELLO
        client_message.uint_value = PROTOCOL_VERSION
        send_protobuf(self.sock, client_message)

        result = HelloResult()
        receive_protobuf(self.sock, result)

        if not result.success:
            print('ERROR: Handshake with server:')
            print(result.message)
            return False

        return True

    def request_render_output(self):
        client_message = ClientMessage()
        client_message.type = ClientMessage.REQUEST_RENDER_OUTPUT
        send_protobuf(self.sock, client_message)
        # XXX should have response

    def close(self):
        client_message = ClientMessage()
        client_message.type = ClientMessage.BYE
        send_protobuf(self.sock, client_message)

        self.sock.close()

    def send_protobuf(self, message):
        send_protobuf(self.sock, message)

    def receive_protobuf(self, message):
        receive_protobuf(self.sock, message)

    def update(self, blend_data, depsgraph):

        scene = depsgraph.scene
        render = scene.render
        world = scene.world        

        # Renderer type
        #
        # Make sure this is the first thing we send, other
        # things can depend on it (e.g. created materials)

        self.send_updated_renderer_type(scene.ospray.renderer)

        # Render settings
       
        render_settings = RenderSettings()
        render_settings.renderer = scene.ospray.renderer
        render_settings.max_depth = scene.ospray.max_depth
        render_settings.min_contribution = scene.ospray.min_contribution
        render_settings.variance_threshold = scene.ospray.variance_threshold
        if scene.ospray.renderer == 'scivis':
            render_settings.ao_samples = scene.ospray.ao_samples
            render_settings.ao_radius = scene.ospray.ao_radius
            render_settings.ao_intensity = scene.ospray.ao_intensity
            render_settings.volume_sampling_rate = scene.ospray.volume_sampling_rate
        elif scene.ospray.renderer == 'pathtracer':
            render_settings.roulette_depth = scene.ospray.roulette_depth
            render_settings.max_contribution = scene.ospray.max_contribution
            render_settings.geometry_lights = scene.ospray.geometry_lights

        self.send_updated_render_settings(render_settings)  

        # Framebuffer

        scale = render.resolution_percentage / 100.0
        self.framebuffer_width = int(render.resolution_x * scale)
        self.framebuffer_height = int(render.resolution_y * scale)
        self.framebuffer_aspect = self.framebuffer_width / self.framebuffer_height

        print("%d x %d (scale %d%%) -> %d x %d (aspect %.3f)" % \
            (render.resolution_x, render.resolution_y, render.resolution_percentage,
            self.framebuffer_width, self.framebuffer_height, self.framebuffer_aspect))

        self.render_border = None

        if render.use_border:
            # Blender: X to the right, Y up, i.e. (0,0) is lower-left, same
            # as ospray. BUT: ospray always fills up the complete framebuffer
            # with the specified image region, so we don't have a direct
            # equivalent of only rendering a sub-region of the full
            # framebuffer as in blender. We need to adjust the framebuffer 
            # resolution to the cropped region, plus set the ROI on the camera.

            min_x = render.border_min_x
            min_y = render.border_min_y
            max_x = render.border_max_x
            max_y = render.border_max_y
            print('Border render enabled: %.3f, %.3f -> %.3f, %.3f' % (min_x, min_y, max_x, max_y))

            left = int(min_x*self.framebuffer_width)
            right = int(max_x*self.framebuffer_width)
            bottom = int(min_y*self.framebuffer_height)
            top = int(max_y*self.framebuffer_height)

            # Crop region in ospray is set in normalized screen-space coordinates,
            # i.e. bottom-left of pixel (i,j) is (i,j), but top-right is (i+1,j+1)
            self.render_border = [
                left/self.framebuffer_width, bottom/self.framebuffer_height,
                (right+1)/self.framebuffer_width, (top+1)/self.framebuffer_height
            ]

            self.framebuffer_width = right - left + 1
            self.framebuffer_height = top - bottom + 1
            # XXX we don't update the fb aspect when border render is active as the camera
            # settings need the full fb aspect
            #self.framebuffer_aspect = self.framebuffer_width / self.framebuffer_height

            print('Framebuffer for border render: %d x %d' % (self.framebuffer_width, self.framebuffer_height))
        
        self.send_updated_framebuffer_settings('final', self.framebuffer_width, self.framebuffer_height, OSP_FB_RGBA32F)

        # World settings      

        self.send_updated_world_settings(world.ospray.background_color, 
            world.ospray.ambient_color, world.ospray.ambient_intensity)    

        # Camera

        camera = self.engine().camera_override
        if camera is not None:
            print('Camera override active, object "%s"' % camera)
            #model_matrix = self.engine().camera_model_matrix(camera)
            print('camera.matrix_world', camera.matrix_world)
            #print('model_matrix', model_matrix)
        else:
            camera = scene.camera

        self.send_updated_camera(camera, self.render_border)  

        # Clear scene

        self.send_clear_scene(scene.ospray.clear_scene_keep_plugin_instances)

        # Send scene content

        self.send_scene(blend_data, depsgraph)

        # Connection will be closed by render(), which is always
        # called after update()

    #
    # Scene export
    #

    def _process_properties(self, obj, expression_locals, extract_plugin_parameters=False):
        """
        Get Blender custom properties set on obj, and process where necessary:

        - Custom properties starting with an underscore become
          element properties (with a key without the underscore), 
          all the others become plugin parameters, but only if
          extract_plugin_parameters is True
        - Property values can include references to environment variables,
          of the form "${NAME}". These are substituted with their actual
          value.
        """

        properties = {}
        plugin_parameters = {}

        for k, v in customproperties2dict(obj).items():
            #print('k', k, 'v', v)
            if isinstance(v, str):
                v = substitute_values(v, expression_locals)
                print(v)
            if k[0] == '_':
                #print(properties, k, v)
                properties[k[1:]] = v
            elif extract_plugin_parameters:
                plugin_parameters[k] = v       
            else:
                properties[k] = v
                
        return properties, plugin_parameters
        
    def send_clear_scene(self, keep_plugin_instances=True):
        client_message = ClientMessage()
        client_message.type = ClientMessage.CLEAR_SCENE
        client_message.string_value = 'keep_plugin_instances' if keep_plugin_instances else 'all'
        send_protobuf(self.sock, client_message)    
        # XXX flags to pick which scene items are cleared    

    def send_updated_framebuffer_settings(self, mode, width, height, format):

        client_message = ClientMessage()
        client_message.type = ClientMessage.UPDATE_FRAMEBUFFER_SETTINGS
        client_message.string_value = mode
        client_message.uint_value = format
        client_message.uint_value2 = width
        client_message.uint_value3 = height
        send_protobuf(self.sock, client_message)
                
    def _film_dimensions(self, camdata, aspect_ratio, zoom):
        # Based on blendseed utils.util.calc_film_dimensions()
        horizontal_fit = camdata.sensor_fit == 'HORIZONTAL' or \
                         (camdata.sensor_fit == 'AUTO' and aspect_ratio > 1)

        if camdata.sensor_fit == 'VERTICAL':
            film_height = camdata.sensor_height / 1000 * zoom
            film_width = film_height * aspect_ratio
        elif horizontal_fit:
            film_width = camdata.sensor_width / 1000 * zoom
            film_height = film_width / aspect_ratio
        else:
            film_height = camdata.sensor_width / 1000 * zoom
            film_width = film_height * aspect_ratio
    
        # In meters
        return film_width, film_height
        
    def _film_dimensions(self, camdata, aspect_ratio, zoom):
        # Based on blendseed utils.util.calc_film_dimensions()
        horizontal_fit = camdata.sensor_fit == 'HORIZONTAL' or \
                         (camdata.sensor_fit == 'AUTO' and aspect_ratio > 1)

        if camdata.sensor_fit == 'VERTICAL':
            film_height = camdata.sensor_height / 1000 * zoom
            film_width = film_height * aspect_ratio
        elif horizontal_fit:
            film_width = camdata.sensor_width / 1000 * zoom
            film_height = film_width / aspect_ratio
        else:
            film_height = camdata.sensor_width / 1000 * zoom
            film_width = film_height * aspect_ratio
    
        # In meters
        return film_width, film_height

    def send_updated_camera_for_interactive_view(self, render, region_data, space_data, viewport_width, viewport_height):

        camera_settings = CameraSettings()
        camera_settings.object_name = '<interactive>'
        camera_settings.camera_name = '<none>'

        # Blender provides FOV in radians
        # OSPRay needs (vertical) FOV in degrees      

        viewport_aspect = viewport_width / viewport_height
        view_matrix = region_data.view_matrix
        focal_length = space_data.lens
        clip_start = space_data.clip_start

        # Holy moly, needed sources from Cycles and Blenderseed to get a working set of formulas...
        
        if region_data.view_perspective == 'PERSP':

            zoom = 2.25
            sensor_width = 32 * zoom
            if viewport_aspect >= 1:  
                sensor_height = sensor_width / viewport_aspect
            else:
                sensor_height = sensor_width
            vfov = 2 * atan((sensor_height/2)/focal_length)        

            camera_settings.type = CameraSettings.PERSPECTIVE
            camera_settings.aspect = viewport_aspect
            camera_settings.clip_start = clip_start

            camera_settings.fov_y = degrees(vfov)

            cam_xform = view_matrix.inverted()
            location = cam_xform.translation
            camera_settings.position[:] = list(location)
            camera_settings.view_dir[:] = list(cam_xform @ Vector((0, 0, -1)) - location)
            camera_settings.up_dir[:] = list(cam_xform @ Vector((0, 1, 0)) - location)

            camera_settings.dof_focus_distance = 0
            camera_settings.dof_aperture = 0.0

        elif region_data.view_perspective == 'ORTHO':

            camera_settings.type = CameraSettings.ORTHOGRAPHIC
            camera_settings.aspect = viewport_aspect
            camera_settings.clip_start = clip_start

            zoom = 2.25
            extent_base = space_data.region_3d.view_distance * 32.0 / focal_length
            sensor_width = zoom * extent_base
            if viewport_aspect >= 1:
                sensor_height = sensor_width / viewport_aspect
            else:
                sensor_height = sensor_width
            camera_settings.height = sensor_height

            cam_xform = view_matrix.inverted()
            location = cam_xform.translation
            view_dir = cam_xform @ Vector((0, 0, -1)) - location
            up_dir = cam_xform @ Vector((0, 1, 0)) - location
            
            # XXX
            location += (-view_dir)*1000
            
            camera_settings.position[:] = list(location)
            camera_settings.view_dir[:] = list(view_dir)
            camera_settings.up_dir[:] = list(up_dir)

            camera_settings.dof_focus_distance = 0
            camera_settings.dof_aperture = 0.0

        else:
            # Camera view
            
            # Cycles source: "magic zoom formula"
            zoom = region_data.view_camera_zoom
            zoom = 1.41421 + zoom / 50.0
            zoom *= zoom
            zoom = 2.0 / zoom
            zoom *= 2.0
            
            offset = region_data.view_camera_offset
            
            cam_xform = view_matrix.inverted()
            location = cam_xform.translation
            view_dir = cam_xform @ Vector((0, 0, -1)) - location
            up_dir = cam_xform @ Vector((0, 1, 0)) - location
            
            camera_settings.position[:] = list(location)
            camera_settings.view_dir[:] = list(view_dir)
            camera_settings.up_dir[:] = list(up_dir)

            camobj = space_data.camera
            camdata = camobj.data
            
            camera_aspect = render.resolution_x / render.resolution_y
            focal_length = camdata.lens
            
            sensor_width = camdata.sensor_width
            if camera_aspect >= 1:  
                sensor_height = sensor_width / camera_aspect 
            else:
                sensor_height = sensor_width
                sensor_width = sensor_height * camera_aspect
            
            camera_settings.type = CameraSettings.PERSPECTIVE   # XXX can be ortho?
            camera_settings.aspect = camera_aspect
            camera_settings.fov_y = self._get_camera_vfov(camdata, camera_aspect)
            camera_settings.clip_start = clip_start
            
            film_width, film_height = self._film_dimensions(camdata, viewport_aspect, zoom)
            
            x_aspect_comp = 1 if viewport_aspect > 1 else 1 / viewport_aspect
            y_aspect_comp = viewport_aspect if viewport_aspect > 1 else 1
            
            #shift_x = ((offset[0] * 2 + (camdata.shift_x * x_aspect_comp)) / zoom) * film_width
            #shift_y = ((offset[1] * 2 + (camdata.shift_y * y_aspect_comp)) / zoom) * film_height
            
            print('FILM', film_width, film_height)
            #print('SHIFT', shift_x, shift_y)
            print('SENSOR', sensor_width, sensor_height)
            
            film_normalized_width = film_width / (sensor_width/1000)
            film_normalized_height = film_height / (sensor_height/1000)
            print('FILM (normalized)', film_normalized_width, film_normalized_height)
            
            left = 0.5 - film_normalized_width/2 #- shift_x
            right = 0.5 + film_normalized_width/2 #- shift_x
            bottom = 0.5 - film_normalized_height/2 #- shift_y
            top = 0.5 + film_normalized_height/2 #- shift_y
            
            camera_settings.border[:] = [left, bottom, right, top]
            
            print('CAM', camera_settings)
            
            """
            

            cam_xform = camobj.matrix_world
            
            # pixel aspect
            #focal_length = camdata.lens / 1000
            """
                
        client_message = ClientMessage()
        client_message.type = ClientMessage.UPDATE_CAMERA

        send_protobuf(self.sock, client_message)
        send_protobuf(self.sock, camera_settings)
        
    def _get_camera_vfov(self, cam_data, aspect):
        
        hfov = vfov = None

        if cam_data.sensor_fit == 'AUTO':
            if aspect >= 1:
                # Horizontal
                hfov = cam_data.angle
            else:
                # Vertical
                vfov = cam_data.angle
        elif cam_data.sensor_fit == 'HORIZONTAL':
            hfov = cam_data.angle
        else:
            vfov = cam_data.angle

        # Blender provides FOV in radians
        # OSPRay needs (vertical) FOV in degrees
        if vfov is None:
            image_plane_width = 2 * tan(hfov/2)
            image_plane_height = image_plane_width / aspect
            vfov = 2*atan(image_plane_height/2)
                
        return degrees(vfov)

    def send_updated_camera(self, cam_obj, border=None):
        # Final render from a camera. 
        # Note: not usable for a camera view in interactive render mode
        
        cam_xform = cam_obj.matrix_world
        cam_data = cam_obj.data

        camera_settings = CameraSettings()
        camera_settings.object_name = cam_obj.name
        camera_settings.camera_name = cam_data.name

        camera_settings.aspect = self.framebuffer_aspect
        camera_settings.clip_start = cam_data.clip_start
        # XXX no far clip in ospray :)

        if cam_data.type == 'PERSP':
            camera_settings.type = CameraSettings.PERSPECTIVE
            camera_settings.fov_y = self._get_camera_vfov(cam_data, self.framebuffer_aspect)

        elif cam_data.type == 'ORTHO':
            camera_settings.type = CameraSettings.ORTHOGRAPHIC
            camera_settings.height = cam_data.ortho_scale / self.framebuffer_aspect

        elif cam_data.type == 'PANO':
            camera_settings.type = CameraSettings.PANORAMIC
            # XXX?

        else:
            raise ValueError('Unknown camera type "%s"' % cam_data.type)

        camera_settings.position[:] = list(cam_obj.location)
        camera_settings.view_dir[:] = list(cam_xform @ Vector((0, 0, -1)) - cam_obj.location)
        camera_settings.up_dir[:] = list(cam_xform @ Vector((0, 1, 0)) - cam_obj.location)

        # Depth of field
        camera_settings.dof_focus_distance = 0
        camera_settings.dof_aperture = 0.0

        if cam_data.dof.use_dof:
            dof_settings = cam_data.dof
            if dof_settings.focus_object is not None:
                focus_world = dof_settings.focus_object.matrix_world.translation
                cam_world = cam_obj.matrix_world.translation
                camera_settings.dof_focus_distance = (focus_world - cam_world).length
            else:
                camera_settings.dof_focus_distance = dof_settings.focus_distance

            # Camera focal length in mm + f-stop -> aperture in m
            camera_settings.dof_aperture = (0.5 * cam_data.lens / dof_settings.aperture_fstop) / 1000

        if border is not None:
            camera_settings.border[:] = border
            
        client_message = ClientMessage()
        client_message.type = ClientMessage.UPDATE_CAMERA

        send_protobuf(self.sock, client_message)
        send_protobuf(self.sock, camera_settings)

    def send_updated_renderer_type(self, type):
        client_message = ClientMessage()
        client_message.type = ClientMessage.UPDATE_RENDERER_TYPE
        client_message.string_value = type
        send_protobuf(self.sock, client_message)

    def send_updated_render_settings(self, render_settings):
        client_message = ClientMessage()
        client_message.type = ClientMessage.UPDATE_RENDER_SETTINGS
        send_protobuf(self.sock, client_message)
        send_protobuf(self.sock, render_settings) 

    def send_updated_world_settings(self, background_color, ambient_color, ambient_intensity):

        client_message = ClientMessage()
        client_message.type = ClientMessage.UPDATE_WORLD_SETTINGS

        world_settings = WorldSettings()        
        world_settings.ambient_color[:] = ambient_color
        world_settings.ambient_intensity = ambient_intensity
        world_settings.background_color[:] = background_color
        
        send_protobuf(self.sock, client_message)
        send_protobuf(self.sock, world_settings)

    def send_scene(self, blend_data, depsgraph):

        msg = 'Sending scene'
        self.engine().update_stats('', msg)
        print(msg)    

        scene = depsgraph.scene

        self.mesh_data_exported = set()
        self.materials_exported = set()

        print('DEPSGRAPH STATS:', depsgraph.debug_stats())

        for instance in depsgraph.object_instances:

            obj = instance.object

            #print('DEPSGRAPH object instance "%s", type=%s, is_instance=%d, random_id=%d' % \
            #    (obj.name, obj.type, instance.is_instance, instance.random_id))

            if obj.type == 'LIGHT':
                self.send_updated_light(blend_data, depsgraph, obj)
            elif obj.type == 'MESH':                                        
                self.send_updated_mesh_object(blend_data, depsgraph, obj, obj.data, instance.matrix_world, instance.is_instance, instance.random_id)        
            elif obj.type not in ['CAMERA']:
                print('Warning: not exporting object of type "%s"' % obj.type)

    def send_updated_light(self, blend_data, depsgraph, obj):

        self.engine().update_stats('', 'Light %s' % obj.name)

        TYPE2ENUM = dict(POINT=LightSettings.POINT, SUN=LightSettings.SUN, SPOT=LightSettings.SPOT, AREA=LightSettings.AREA)

        data = obj.data
        xform = obj.matrix_world

        ospray_data = data.ospray

        light_settings = LightSettings()
        light_settings.type = TYPE2ENUM[data.type]
        light_settings.object2world[:] = matrix2list(xform)      # XXX get from updateobject
        light_settings.object_name = obj.name
        light_settings.light_name = data.name

        light_settings.color[:] = data.color
        light_settings.intensity = ospray_data.intensity
        light_settings.visible = ospray_data.visible

        if data.type == 'SUN':
            light_settings.angular_diameter = ospray_data.angular_diameter
        elif data.type != 'AREA':
            light_settings.position[:] = (xform[0][3], xform[1][3], xform[2][3])

        if data.type in ['SUN', 'SPOT']:
            light_settings.direction[:] = obj.matrix_world @ Vector((0, 0, -1)) - obj.location

        if data.type == 'SPOT':
            # Blender:
            # .spot_size = full angle where light shines, in degrees
            # .spot_blend = factor in [0,1], 0 = no penumbra, 1 = penumbra is full angle
            light_settings.opening_angle = degrees(data.spot_size)
            light_settings.penumbra_angle = 0.5*data.spot_blend*degrees(data.spot_size)
            # assert light.penumbra_angle < 0.5*light.opening_angle

        if data.type in ['POINT', 'SPOT']:
            light_settings.radius = data.shadow_soft_size        # XXX what is this called in ospray?

        if data.type == 'AREA':
            size_x = data.size
            size_y = data.size_y

            # Local
            position = Vector((-0.5*size_x, -0.5*size_y, 0))
            edge1 = position + Vector((0, size_y, 0))
            edge2 = position + Vector((size_x, 0, 0))

            # World
            position = obj.matrix_world @ position
            edge1 = obj.matrix_world @ edge1 - position
            edge2 = obj.matrix_world @ edge2 - position

            light_settings.position[:] = position
            light_settings.edge1[:] = edge1
            light_settings.edge2[:] = edge2

        client_message = ClientMessage()
        client_message.type = ClientMessage.UPDATE_OBJECT  

        update = UpdateObject()
        update.type = UpdateObject.LIGHT
        update.name = obj.name
        update.object2world[:] = matrix2list(xform)
        update.data_link = data.name

        expression_locals = {
            'frame': depsgraph.scene.frame_current
        }
        
        custom_properties, plugin_parameters = self._process_properties(obj, expression_locals, False)
        assert len(plugin_parameters.keys()) == 0
        
        update.custom_properties = json.dumps(custom_properties)  

        # XXX using three messages :-/
        send_protobuf(self.sock, client_message)
        send_protobuf(self.sock, update)
        send_protobuf(self.sock, light_settings)

    def send_updated_material(self, blend_data, depsgraph, material, force_update=False):

        name = material.name        
        
        if not force_update and name in self.materials_exported:
            print('Not sending MATERIAL "%s" again' % name)
            return

        print('Updating MATERIAL "%s"' % name)

        if not material.use_nodes:
            print('... WARNING: material does not use nodes, not sending!')
            return

        client_message = ClientMessage()
        client_message.type = ClientMessage.UPDATE_MATERIAL  

        update = MaterialUpdate()        
        update.name = name

        tree = material.node_tree
        assert tree.type == 'SHADER'

        nodes = tree.nodes
        print('... %d shader nodes in tree' % len(nodes))

        # Get output node
        # XXX tree.get_output_node() doesn't return anything?
        outputs = list(filter(lambda n: n.bl_idname == 'OSPRayOutputNode', tree.nodes))

        if len(outputs) == 0:
            print('... WARNING: no Output node found')
            return

        # XXX takes first output
        output = outputs[0]

        # Find node attached to the output
        shadernode = None
        for link in tree.links:
            if link.to_node == output:
                shadernode = link.from_node
                break
        
        if shadernode is None:
            print('... WARNING: no shader node attached to Output!')
            return
        
        idname = shadernode.bl_idname
        inputs = shadernode.inputs

        if idname == 'OSPRayAlloy':
            update.type = MaterialUpdate.ALLOY
            settings = AlloySettings()
            settings.color[:] = inputs['Color'].default_value[:3]
            settings.edge_color[:] = inputs['Edge color'].default_value[:3]
            settings.roughness = inputs['Roughness'].default_value
            
        elif idname == 'OSPRayCarPaint':
            update.type = MaterialUpdate.CAR_PAINT
            settings = CarPaintSettings()
            settings.base_color[:] = inputs['Base color'].default_value[:3]  
            settings.roughness = inputs['Roughness'].default_value
            settings.normal = inputs['Normal'].default_value
            settings.flake_density = inputs['Flake density'].default_value
            settings.flake_scale = inputs['Flake scale'].default_value            
            settings.flake_spread = inputs['Flake spread'].default_value
            settings.flake_jitter = inputs['Flake jitter'].default_value
            settings.flake_roughness = inputs['Flake roughness'].default_value
            settings.coat = inputs['Coat'].default_value
            settings.coat_ior = inputs['Coat IOR'].default_value
            settings.coat_color[:] = inputs['Coat color'].default_value[:3]
            settings.coat_thickness = inputs['Coat thickness'].default_value
            settings.coat_roughness = inputs['Coat roughness'].default_value
            settings.coat_normal = inputs['Coat normal'].default_value
            settings.flipflop_color[:] = inputs['Flipflop color'].default_value[:3]
            settings.flipflop_falloff = inputs['Flipflop falloff'].default_value

        elif idname == 'OSPRayGlass':
            update.type = MaterialUpdate.GLASS
            settings = GlassSettings()
            settings.eta = inputs['Eta'].default_value   
            settings.attenuation_color[:] = list(inputs['Attenuation color'].default_value)[:3]
            settings.attenuation_distance = inputs['Attenuation distance'].default_value

        elif idname == 'OSPRayLuminous':
            update.type = MaterialUpdate.LUMINOUS
            settings = LuminousSettings()
            settings.color[:] = inputs['Color'].default_value[:3]  
            settings.intensity = inputs['Intensity'].default_value
            settings.transparency = inputs['Transparency'].default_value

        elif idname == 'OSPRayMetal':
            update.type = MaterialUpdate.METAL
            settings = MetalSettings()

            metal = inputs['Metal'].metal
            metal = ['ALUMINIUM', 'CHROMIUM', 'COPPER', 'GOLD', 'SILVER'].index(metal)

            settings.metal = metal
            settings.roughness = inputs['Roughness'].default_value

        elif idname == 'OSPRayMetallicPaint':
            update.type = MaterialUpdate.METALLIC_PAINT
            settings = MetallicPaintSettings()
            settings.base_color[:] = inputs['Base color'].default_value[:3]  
            settings.flake_color[:] = inputs['Flake color'].default_value[:3]  
            settings.flake_amount = inputs['Flake amount'].default_value
            settings.flake_spread = inputs['Flake spread'].default_value
            settings.eta = inputs['Eta'].default_value

        elif idname == 'OSPRayOBJMaterial':
            # XXX Check Kd + Ks + Tf = 1.36853, should be <= 1
            update.type = MaterialUpdate.OBJMATERIAL
            settings = OBJMaterialSettings()
            settings.kd[:] = list(inputs['Diffuse'].default_value)[:3]
            settings.ks[:] = list(inputs['Specular'].default_value)[:3]
            settings.ns = inputs['Shininess'].default_value
            settings.d = inputs['Opacity'].default_value

        elif idname == 'OSPRayPrincipled':
            update.type = MaterialUpdate.PRINCIPLED
            settings = PrincipledSettings()
            settings.base_color[:] = inputs['Base color'].default_value[:3]  
            settings.edge_color[:] = inputs['Edge color'].default_value[:3]  
            settings.metallic = inputs['Metallic'].default_value
            settings.diffuse = inputs['Diffuse'].default_value
            settings.specular = inputs['Specular'].default_value
            settings.ior = inputs['IOR'].default_value
            settings.transmission = inputs['Transmission'].default_value
            settings.transmission_color[:] = inputs['Transmission color'].default_value[:3]
            settings.transmission_depth = inputs['Transmission depth'].default_value
            settings.roughness = inputs['Roughness'].default_value
            settings.anisotropy = inputs['Anisotropy'].default_value
            settings.rotation = inputs['Rotation'].default_value
            settings.normal = inputs['Normal'].default_value
            settings.base_normal = inputs['Base normal'].default_value
            settings.thin = inputs['Thin'].default_value
            settings.thickness = inputs['Thickness'].default_value
            settings.backlight = inputs['Backlight'].default_value
            settings.coat = inputs['Coat'].default_value
            settings.coat_ior = inputs['Coat IOR'].default_value
            settings.coat_color[:] = inputs['Coat color'].default_value[:3]
            settings.coat_thickness = inputs['Coat thickness'].default_value
            settings.coat_roughness = inputs['Coat roughness'].default_value
            settings.coat_normal = inputs['Coat normal'].default_value
            settings.sheen = inputs['Sheen'].default_value
            settings.sheen_color[:] = inputs['Sheen color'].default_value[:3]
            settings.sheen_tint = inputs['Sheen tint'].default_value
            settings.sheen_roughness = inputs['Sheen roughness'].default_value
            settings.opacity = inputs['Opacity'].default_value

        elif idname == 'OSPRayThinGlass':
            update.type = MaterialUpdate.THIN_GLASS
            settings = ThinGlassSettings()
            settings.eta = inputs['Eta'].default_value   
            settings.attenuation_color[:] = list(inputs['Attenuation color'].default_value)[:3]
            settings.attenuation_distance = inputs['Attenuation distance'].default_value
            settings.thickness = inputs['Thickness'].default_value            

        else:
            print('... WARNING: shader of type "%s" not handled!' % shadernode.bl_idname)
            return

        # XXX three messages
        send_protobuf(self.sock, client_message)
        send_protobuf(self.sock, update)
        send_protobuf(self.sock, settings)

        self.materials_exported.add(name)


    def send_updated_mesh_object(self, blend_data, depsgraph, obj, mesh, matrix_world, is_instance, random_id):

        # We do a bit of the logic here in determining what a certain
        # mesh object -> mesh data combination (including their properties)
        # means, so the server isn't bothered with this.

        # XXX for now we assume materials are only linked to object data in the blender
        # scene (and not to objects, even though that is possible).
        # The exported OSPRay scene *does* have materials linked to objects, though,
        # as that is the only way to do it. That's also the reason we handle materials
        # in the object export and not the mesh export. Here we have the detail needed.

        mesh = obj.data

        # Send mesh data first, so the object can refer to it
        self.send_updated_mesh_data(blend_data, depsgraph, mesh)

        # Process mesh object itself
                    
        name = obj.name
        
        s = 'Updating MESH OBJECT "%s"' % name
        if is_instance:
            s += ' (instance %d)' % random_id
            name = '%s [%d]' % (name, random_id)
        print(s)

        client_message = ClientMessage()
        client_message.type = ClientMessage.UPDATE_OBJECT    
        
        update = UpdateObject()
        update.name = name
        update.object2world[:] = matrix2list(matrix_world)
        update.data_link = mesh.name

        expression_locals = {
            'frame': depsgraph.scene.frame_current
        }
    
        custom_properties, plugin_parameters = self._process_properties(obj, expression_locals, False)
        assert len(plugin_parameters.keys()) == 0
        
        update.custom_properties = json.dumps(custom_properties)   

        # Check if a material is set

        material = None
        if len(obj.material_slots) > 0:
            if len(obj.material_slots) > 1:
                print('WARNING: only exporting a single material slot!')

            mslot = obj.material_slots[0]

            if mslot.link == 'DATA':
                material = obj.data.materials[0]
            else:
                # Material linked to object
                material = mslot.material

        # Plugin enabled or not?

        if not obj.ospray.ospray_override or not mesh.ospray.plugin_enabled:
            # Linked mesh data is not enabled for OSPRay, or its plugin is disabled.
            # Treat as regular blender Mesh object       
            update.type = UpdateObject.MESH
                
            # Check that this object isn't a child used for slicing, in which case it 
            # should not be sent as normal geometry
            parent = obj.parent 
            if (parent is not None) and parent.ospray.ospray_override:
                assert parent.type == 'MESH'
                parent_mesh = parent.data
                if (parent_mesh.ospray.plugin_type == 'volume') and (parent.ospray.volume_usage == 'slices'):
                    print('Object "%s" is child of slice-enabled parent "%s", not sending' % (obj.name, parent.name))
                    return

            # Update material first (if set)
            if material is not None:
                self.send_updated_material(blend_data, depsgraph, material)
                update.material_link = material.name

            # Send object itself            
            send_protobuf(self.sock, client_message)
            send_protobuf(self.sock, update)                    

        else:        

            extra = []
        
            plugin_type = mesh.ospray.plugin_type
            print("Plugin type = %s" % plugin_type)

            if plugin_type == 'geometry':
                update.type = UpdateObject.GEOMETRY

                if material is not None:
                    self.send_updated_material(blend_data, depsgraph, material)
                    update.material_link = material.name
                
            elif plugin_type == 'scene':
                update.type = UpdateObject.SCENE   
                
            elif plugin_type == 'volume':
                
                # XXX properties: single shade, gradient shading, ...
                
                volume_usage = obj.ospray.volume_usage
                print('Volume usage is %s' % volume_usage)

                if volume_usage == 'volume':
                    update.type = UpdateObject.VOLUME
                    volume = Volume()

                    # Check if a TF is set (ColorRamp node)
                    if material is not None:

                        tree = material.node_tree
                        assert tree.type == 'SHADER'

                        nodes = tree.nodes
                        print('... %d shader nodes in tree' % len(nodes))

                        # Get output node
                        outputs = list(filter(lambda n: n.bl_idname == 'OSPRayOutputNode', tree.nodes))

                        if len(outputs) > 0:
                            # XXX takes first output
                            output = outputs[0]

                            # Find node attached to the output
                            shadernode = None
                            for link in tree.links:
                                if link.to_node == output:
                                    shadernode = link.from_node
                                    break
                            
                            if shadernode is not None:
                                idname = shadernode.bl_idname 

                                tf_positions = []
                                tf_colors = []

                                if idname == 'ShaderNodeValToRGB':  # ColorRamp
                                    color_ramp = shadernode.color_ramp
                                    for e in color_ramp.elements:
                                        tf_positions.append(e.position)
                                        col = e.color
                                        print(type(col))
                                        color = Color()
                                        color.r = col[0]
                                        color.g = col[1]
                                        color.b = col[2]
                                        color.a = col[3]
                                        tf_colors.append(color)

                                    volume.tf_positions[:] = tf_positions
                                    volume.tf_colors.extend(tf_colors)
                                        
                                else:
                                    print('... No ColorRamp node connected to Output, using default transfer function')

                            else:
                                print('... WARNING: no shader node attached to Output!')                                
                            
                        else:
                            print('... WARNING: no Output shader node found!')
                            
                    extra.append(volume)

                elif volume_usage == 'slices':
                    update.type = UpdateObject.SLICES

                    #  Process child objects to use as slices
                    ss = []

                    # Apparently the depsgraph leaves out the parenting? So get
                    # that information from the original object
                    # XXX need to ignore slice object itself in export, but not its mesh data
                    children = obj.original.children
                    assert children == depsgraph.scene.objects[obj.name].children
                    
                    print('Object "%s" has %d CHILDREN' % (obj.name, len(children)))

                    # XXX volumetric texture and geometric model share the same coordinate space.
                    # child.matrix_local should provide the transform of child in the parent's
                    # coordinate system. unfortunately, this means we need to use this transform
                    # to actually update the geometry on the server to use for slicing. we can
                    # then transform it with the parent's transform to get it in the right position.                        

                    for childobj in children:

                        if childobj.type != 'MESH':
                            print('Ignoring slicing child object "%s", it\'s not a mesh' % childobj.name)
                            continue

                        if childobj.hide_render:
                            print('Ignoring slicing child object "%s", it\'s hidden for rendering' % childobj.name)
                            continue

                        print('Sending slicing child mesh "%s"' % childobj.data.name)

                        self.update_blender_mesh(blend_data, depsgraph, childobj.data, childobj.matrix_local)

                        slice = Slice()
                        slice.name = childobj.name
                        slice.mesh_link = childobj.data.name
                        # Note: this is the parent's object-to-world transform
                        slice.object2world[:] = matrix2list(obj.matrix_world)
                        ss.append(slice)                            

                    slices = Slices()
                    slices.slices.extend(ss)
                    extra.append(slices)

                elif volume_usage == 'isosurfaces':
                    # Isosurface values are read from the custom property 'isovalue'
                    update.type = UpdateObject.ISOSURFACES
                                    
            send_protobuf(self.sock, client_message)
            send_protobuf(self.sock, update)
            for msg in extra:
                send_protobuf(self.sock, msg)
    

    def send_updated_mesh_data(self, blend_data, depsgraph, mesh):
        
        """
        Send an update on a Mesh Data block
        """
                
        if mesh.name in self.mesh_data_exported:
            print('Not sending MESH DATA "%s" again' % mesh.name)
            return
                
        if mesh.ospray.plugin_enabled:
            # Plugin instance
            
            print('Updating plugin-enabled mesh "%s"' % mesh.name)
            
            ospray = mesh.ospray            
            plugin_enabled = ospray.plugin_enabled
            plugin_name = ospray.plugin_name
            plugin_type = ospray.plugin_type

            expression_locals = {
                'frame': depsgraph.scene.frame_current
            }

            custom_properties, plugin_parameters = self._process_properties(mesh, expression_locals, True)
            
            self.update_plugin_instance(mesh.name, plugin_type, plugin_name, plugin_parameters, custom_properties)

        else:
            # Treat as regular blender mesh
            # XXX any need to send custom properties?
            self.update_blender_mesh(blend_data, depsgraph, mesh) 

        # Remember that we exported this mesh
        self.mesh_data_exported.add(mesh.name)        


    def update_plugin_instance(self, name, plugin_type, plugin_name, plugin_parameters, custom_properties):
        
        self.engine().update_stats('', 'Updating plugin instance %s (type: %s)' % (name, plugin_type))
        
        type2enum = dict(
            geometry = UpdatePluginInstance.GEOMETRY,
            volume = UpdatePluginInstance.VOLUME,
            scene = UpdatePluginInstance.SCENE
        )
        
        client_message = ClientMessage()
        client_message.type = ClientMessage.UPDATE_PLUGIN_INSTANCE            
        
        update = UpdatePluginInstance()
        update.type = type2enum[plugin_type]
        update.name = name
        
        update.plugin_name = plugin_name
        update.plugin_parameters = json.dumps(plugin_parameters)
        update.custom_properties = json.dumps(custom_properties)
        
        send_protobuf(self.sock, client_message)
        send_protobuf(self.sock, update)
        
        generate_function_result = GenerateFunctionResult()
        
        receive_protobuf(self.sock, generate_function_result)
        
        if not generate_function_result.success:
            print('ERROR: plugin generation failed:')
            print(generate_function_result.message)
            return
        

    def update_blender_mesh(self, blend_data, depsgraph, mesh, xform=None):

        if mesh.name in self.mesh_data_exported:
            print('Not updating MESH DATA "%s", already sent' % mesh.name)
            return
    
        # XXX we should export meshes separately, keeping a local
        # list which ones we already exported (by name).
        # Then for MESH objects use the name of the mesh to instantiate
        # it using the given xform. This gives us real instancing.
        # But a user can change a mesh's name. However, we can
        # sort of handle this by using the local name list and deleting
        # (also on the server) whichever name's we don't see when exporting.
        # Could also set a custom property on meshes with a unique ID
        # we choose ourselves. But props get copied when duplicating
        # See https://devtalk.blender.org/t/universal-unique-id-per-object/363/3

        self.engine().update_stats('', 'Updating Blender MESH DATA "%s"' % mesh.name)

        # Get triangulated geometry
        mesh.calc_loop_triangles()

        nv = len(mesh.vertices)
        nt = len(mesh.loop_triangles)

        print('... MESH DATA "%s": %d vertices, %d triangles' % (mesh.name, nv, nt))

        if nt == 0 or nv == 0:
            print('... No vertices/triangles, NOT sending mesh')
            self.mesh_data_exported.add(mesh.name)
            return        

        # Send client message
        
        client_message = ClientMessage()
        client_message.type = ClientMessage.UPDATE_BLENDER_MESH       
        client_message.string_value = mesh.name
        
        send_protobuf(self.sock, client_message)
        
        # Send the actual mesh geometry
        
        mesh_data = MeshData()
        mesh_data.num_vertices = nv
        mesh_data.num_triangles = nt

        flags = 0    

        # Check if any faces use smooth shading
        # XXX we currently don't handle meshes with both smooth
        # and non-smooth faces, but those are probably not very common anyway

        use_smooth = False
        for tri in mesh.loop_triangles:
            if tri.use_smooth:
                print('... mesh uses smooth shading')
                use_smooth = True
                flags |= MeshData.NORMALS
                break

        # Vertex colors
        #https://blender.stackexchange.com/a/8561
        if mesh.vertex_colors:
            flags |= MeshData.VERTEX_COLORS

        # Send mesh data

        mesh_data.flags = flags

        send_protobuf(self.sock, mesh_data)

        # Send vertices

        vertices = numpy.empty(nv*3, dtype=numpy.float32)

        if xform is None:
            xform = Matrix()    # Identity

        for idx, v in enumerate(mesh.vertices):
                p = xform @ v.co
                vertices[3*idx+0] = p.x
                vertices[3*idx+1] = p.y
                vertices[3*idx+2] = p.z
            
        #print(vertices)

        self.sock.send(vertices.tobytes())

        # Send vertex normals (if set)

        if use_smooth:
            normals = numpy.empty(nv*3, dtype=numpy.float32)

            for idx, v in enumerate(mesh.vertices):
                # XXX use .index?
                n = v.normal
                normals[3*idx+0] = n.x
                normals[3*idx+1] = n.y
                normals[3*idx+2] = n.z

            self.sock.send(normals.tobytes())

        # Send vertex colors (if set)

        if mesh.vertex_colors:
            vcol_layer = mesh.vertex_colors.active
            vcol_data = vcol_layer.data

            vertex_colors = numpy.empty(nv*4, dtype=numpy.float32)

            for poly in mesh.polygons:
                for loop_index in poly.loop_indices:
                    loop_vert_index = mesh.loops[loop_index].vertex_index
                    color = vcol_data[loop_index].color
                    # RGBA vertex colors in Blender 2.8x
                    vertex_colors[4*loop_vert_index+0] = color[0]
                    vertex_colors[4*loop_vert_index+1] = color[1]
                    vertex_colors[4*loop_vert_index+2] = color[2]
                    vertex_colors[4*loop_vert_index+3] = 1.0

            self.sock.send(vertex_colors.tobytes())

        # Send triangles

        triangles = numpy.empty(nt*3, dtype=numpy.uint32)   # XXX opt possible with <64k vertices ;-)

        for idx, tri in enumerate(mesh.loop_triangles):
            triangles[3*idx+0] = tri.vertices[0]
            triangles[3*idx+1] = tri.vertices[1]
            triangles[3*idx+2] = tri.vertices[2]
            
        #print(triangles)

        self.sock.send(triangles.tobytes())

        self.mesh_data_exported.add(mesh.name)

                
                
    #
    # Rendering
    #
    
    def render(self, depsgraph):

        """
        if self.is_preview:
            self.render_preview(depsgraph)
        else:
            self.render_scene(depsgraph)
        """

        # Signal server to start rendering

        scene = depsgraph.scene
        ospray = scene.ospray
    
        client_message = ClientMessage()
        client_message.type = ClientMessage.START_RENDERING
        client_message.string_value = "final"
        self.render_samples = client_message.uint_value = ospray.render_samples
        client_message.uint_value2 = ospray.framebuffer_update_rate
        send_protobuf(self.sock, client_message)

        # Read back successive framebuffer samples

        num_pixels = self.framebuffer_width * self.framebuffer_height
        # RGBA floats
        framebuffer = numpy.zeros(num_pixels*4*4, dtype=numpy.uint8)

        t0 = time.time()

        result = self.engine().begin_result(0, 0, self.framebuffer_width, self.framebuffer_height)
        # Only Combined and Depth seem to be available
        # https://docs.blender.org/manual/en/latest/render/layers/passes.html
        # Combined = lighting only        
        #layer = result.layers[0].passes["Combined"]

        FBFILE = '/dev/shm/blosprayfb.exr'

        sample = 1
        cancel_sent = False

        self.engine().update_stats('', 'Rendering sample %d/%d' % (sample, self.render_samples))

        # XXX this loop blocks too often, might need to move it to a separate thread,
        # but OTOH we're already using select() to detect when to read
        while True:

            # Check for incoming render results

            r, w, e = select.select([self.sock], [], [], 0)

            if len(r) == 1:

                render_result = RenderResult()
                # XXX handle receive error
                receive_protobuf(self.sock, render_result)

                if render_result.type == RenderResult.FRAME:

                    # New framebuffer (for a single pixel sample) is available
                    
                    if render_result.file_size > 0:

                        """
                        # XXX Slow: get as raw block of floats
                        print('[%6.3f] _read_framebuffer start' % (time.time()-t0))
                        self._read_framebuffer(framebuffer, self.framebuffer_width, self.framebuffer_height)
                        print('[%6.3f] _read_framebuffer end' % (time.time()-t0))

                        pixels = framebuffer.view(numpy.float32).reshape((num_pixels, 4))
                        print(pixels.shape)
                        print('[%6.3f] view() end' % (time.time()-t0))

                        # Here we write the pixel values to the RenderResult
                        # XXX This is the slow part
                        print(type(layer.rect))
                        layer.rect = pixels
                        self.update_result(result)
                        """

                        # We read the framebuffer file content from the server
                        # and locally write it to FBFILE, which then gets loaded by Blender

                        # XXX both receiving into a file and loading from file 
                        # block the blender UI for a short time

                        #print('[%6.3f] _read_framebuffer_to_file start' % (time.time()-t0))
                        self._read_framebuffer_to_file(FBFILE, render_result.file_size)
                        #print('[%6.3f] _read_framebuffer_to_file end' % (time.time()-t0))

                        # This needs an image file format. I.e. reading in a raw framebuffer
                        # of floats isn't possible, hence the OpenEXR file. This isn't as
                        # bad as it looks as we can include several layers in the OpenEXR file
                        # and they get picked up automatically.
                        # XXX result.load_from_file(...), instead of result.layers[0].load_from_file(...), would work as well?
                        result.layers[0].load_from_file(FBFILE)
                        #result.load_from_file(FBFILE)

                        # Remove file
                        os.unlink(FBFILE)

                        self.engine().update_result(result)
                        
                    self.engine().update_progress(sample/self.render_samples)
                    self.engine().update_memory_stats(memory_used=render_result.memory_usage, memory_peak=render_result.peak_memory_usage)

                    #print('[%6.3f] update_result() done' % (time.time()-t0))                
                    
                    sample += 1
                    
                    self.engine().update_stats(
                        'Server %.1fM (peak %.1fM)' % (render_result.memory_usage, render_result.peak_memory_usage),
                        'Variance %.3f | Rendering sample %d/%d' % (render_result.variance, sample, self.render_samples))                

                elif render_result.type == RenderResult.CANCELED:
                    print('Rendering CANCELED!')
                    self.engine().update_stats('', 'Rendering canceled')
                    cancel_sent = True
                    break

                elif render_result.type == RenderResult.DONE:
                    # XXX this message is never really shown, the final timing stats get shown instead
                    self.engine().update_stats('', 'Variance %.3f | Rendering done' % render_result.variance)
                    print('Rendering done!')
                    break

            # Check if render was canceled

            if self.engine().test_break() and not cancel_sent:
                client_message = ClientMessage()
                client_message.type = ClientMessage.CANCEL_RENDERING
                send_protobuf(self.sock, client_message)
                cancel_sent = True

            time.sleep(0.001)

        self.engine().end_result(result)

        print('Done with render loop')

    # Utility

    """
    def _read_framebuffer(self, framebuffer, width, height):

        # XXX use select() in a loop, to allow UI updates more frequently

        num_pixels = width * height
        bytes_left = num_pixels * 4*4

        #self.update_stats('%d bytes left' % bytes_left, 'Reading back framebuffer')

        view = memoryview(framebuffer)

        while bytes_left > 0:
            n = self.sock.recv_into(view, bytes_left)
            view = view[n:]
            bytes_left -= n
            sys.stdout.write('.')
            sys.stdout.flush()
            #self.update_stats('%d bytes left' % bytes_left, 'Reading back framebuffer')
    """
    
    def _read_framebuffer_to_file(self, fname, size):

        #print('_read_framebuffer_to_file(%s, %d)' % (fname, size))

        # XXX use select() in a loop, to allow UI updates more frequently

        with open(fname, 'wb') as f:
            bytes_left = size
            while bytes_left > 0:
                d = self.sock.recv(bytes_left)
                # XXX check d
                f.write(d)
                bytes_left -= len(d)
